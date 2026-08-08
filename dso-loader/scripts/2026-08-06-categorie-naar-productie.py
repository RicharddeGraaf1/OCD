"""Zet de gecureerde taxonomie + de nieuwe toewijzingen op productie.

Waarom kopiëren en niet herberekenen
------------------------------------
`extend_categorie()` trekt elke embedding naar Python om er numpy op los te
laten. Lokaal is dat een unix-socket en kost het 5,3 minuten; over het internet
naar Railway zou het ~1 miljoen vectoren van 768 floats als tekst zijn, en dat
is gigabytes. Serverside met pgvector (`<=>`) kan ook, maar dat leest ~5 GB aan
vectordata op een instance met een harde geheugenkap van 2 GB.

Kopiëren mag omdat de twee kanten dezelfde chunk-id-ruimte delen. Gemeten
2026-08-06, beide zijden identiek:

    n=1651590  min(id)=1823  max(id)=1745971
    md5(alle ids)            = 6d167fd02a98…
    md5(id|wid|bron_soort)   = ad7c1e5309db…

Dat is geen aanname over "prod is een dump van lokaal" maar een vingerafdruk.

Na een synchronisatie lopen de twee uiteen: stap 6 van het sync-runbook embedt
lokaal de nieuwe tekst en die chunks bestaan op prod nog niet. Dit script trekt
dat daarom eerst gelijk — nieuwe chunks erheen, lokaal opgeruimde chunks daar
weg (`prune_verouderde_versies.py` haalt ze lokaal via CASCADE weg) — en eist
de vingerafdruk pas dáárna. Zou hij hem vooraf eisen, dan blokkeerde hij
precies op het moment waarop je hem nodig hebt.

Waarom een schaduwtabel en geen TRUNCATE
----------------------------------------
`TRUNCATE` pakt een ACCESS EXCLUSIVE lock die pas bij COMMIT valt. De laadtijd
(~737k rijen over het net) zou dus net zo lang élke lezer blokkeren, en het
onderwerpen-endpoint hangt daaraan. De swap hieronder blokkeert alleen tijdens
de RENAME: milliseconden.

De oude tabel blijft staan als `v2a.chunk_categorie_oud`. Terugdraaien is dan
twee renames. Opruimen mag zodra de UI op productie goed staat.

Idempotent: opnieuw draaien bouwt de schaduwtabel opnieuw op.

Draaien:  python scripts/2026-08-06-categorie-naar-productie.py [--ja]
Zonder --ja doet hij alleen de metingen en de vergelijking (droogloop).
"""

import os
import sys
import time

import psycopg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LOKAAL = "postgresql://postgres:postgres@localhost:5434/dso"
PROD = os.environ["PROD_DB_URL"]

VINGERAFDRUK = """
    SELECT count(*), min(id), max(id),
           md5(string_agg(id::text, ',' ORDER BY id))
    FROM v2a.tekst_embedding
"""

CATEGORIE_KOLOMMEN = [
    "naam", "naam_auto", "status", "bron", "parent_id",
    "centroide", "n_chunks_seen", "taxonomie_versie", "merged_into", "bron_run",
]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


EMBEDDING_KOLOMMEN = [
    "id", "tekst_element_id", "regeling_expression", "bron_soort", "kop_pad",
    "inhoud_plain", "embedding", "wid", "source_type", "source_id", "source_ref",
]


def verschil_chunks(lc, pc):
    """Welke chunk-id's staan wél lokaal en niet op prod, en omgekeerd.

    Na een sync loopt dit onvermijdelijk uiteen: stap 6 embedt lokaal de nieuwe
    tekst en die rijen bestaan op prod nog niet. Een kale gelijkheidstest zou de
    overzet dan blokkeren precies wanneer je hem nodig hebt."""
    lc.execute("SELECT id FROM v2a.tekst_embedding")
    lokaal = {r[0] for r in lc.fetchall()}
    pc.execute("SELECT id FROM v2a.tekst_embedding")
    prod = {r[0] for r in pc.fetchall()}
    return lokaal - prod, prod - lokaal


def sync_embeddings(lconn, pconn, pc, nieuw: set, verdwenen: set) -> None:
    """Trek v2a.tekst_embedding op prod gelijk aan lokaal, incrementeel.

    Moet vóór de toewijzingen: chunk_categorie heeft een foreign key op deze
    tabel, dus een toewijzing naar een chunk die prod niet kent, faalt."""
    if verdwenen:
        # Lokaal weggevallen chunks horen ook op prod weg. Ze ontstaan als
        # `prune_verouderde_versies.py` een regeling opruimt; de CASCADE haalt
        # de chunks dan lokaal mee. Blijven ze op prod staan, dan levert de
        # semantische zoektocht daar tekst op die nergens meer bij hoort.
        pc.execute("DELETE FROM v2a.tekst_embedding WHERE id = ANY(%s)", (list(verdwenen),))
        pconn.commit()
        log(f"prod opgeschoond: {len(verdwenen)} chunks verwijderd die lokaal weg zijn")

    if not nieuw:
        log("geen nieuwe chunks om over te zetten")
        return

    kol = ", ".join(EMBEDDING_KOLOMMEN)
    n = 0
    t0 = time.time()
    with lconn.cursor(name="lees_chunks") as rc:
        rc.itersize = 2000
        rc.execute(f"SELECT {kol} FROM v2a.tekst_embedding WHERE id = ANY(%s) ORDER BY id",
                   (list(nieuw),))
        with pc.copy(f"COPY v2a.tekst_embedding ({kol}) FROM STDIN") as cp:
            for rij in rc:
                cp.write_row(rij)
                n += 1
                if n % 20000 == 0:
                    log(f"  {n}/{len(nieuw)} chunks ({n / (time.time() - t0):.0f}/s)")
    pconn.commit()
    # De sequence moet mee, anders botst een latere insert op prod op een
    # bestaande id.
    pc.execute("SELECT setval(pg_get_serial_sequence('v2a.tekst_embedding','id'), "
               "(SELECT max(id) FROM v2a.tekst_embedding))")
    pconn.commit()
    log(f"embeddings bijgewerkt: {n} chunks in {time.time() - t0:.0f}s")


def controleer_pariteit(lc, pc) -> None:
    lc.execute(VINGERAFDRUK)
    l = lc.fetchone()
    pc.execute(VINGERAFDRUK)
    p = pc.fetchone()
    log(f"lokaal n={l[0]} hash={l[3][:12]}")
    log(f"prod   n={p[0]} hash={p[3][:12]}")
    if l != p:
        sys.exit(
            "STOP: v2a.tekst_embedding verschilt tussen lokaal en prod.\n"
            "De chunk_ids zijn dan niet uitwisselbaar en kopiëren zou\n"
            "toewijzingen aan de verkeerde tekst hangen. Herbereken in dat\n"
            "geval serverside in plaats van te kopiëren."
        )
    log("pariteit bevestigd — chunk_ids zijn uitwisselbaar")


def sync_categorie(lc, pconn, pc) -> None:
    """Trek v2a.categorie op prod gelijk aan lokaal. Alleen UPDATE: de 99
    categorie_ids zijn aan beide kanten dezelfde, dus geen INSERT/DELETE en
    daarmee geen gedoe met de foreign keys die eraan hangen."""
    lc.execute(f"SELECT categorie_id, {', '.join(CATEGORIE_KOLOMMEN)} FROM v2a.categorie ORDER BY 1")
    rijen = lc.fetchall()

    pc.execute("SELECT categorie_id FROM v2a.categorie")
    op_prod = {r[0] for r in pc.fetchall()}
    ontbreekt = [r[0] for r in rijen if r[0] not in op_prod]
    if ontbreekt:
        sys.exit(f"STOP: {len(ontbreekt)} categorie_ids ontbreken op prod: {ontbreekt[:5]}")

    # parent_id wijst naar dezelfde tabel. Eerst alle parents leegtrekken zou
    # netter zijn bij een herstructurering; hier verandert de boom alleen naar
    # bestaande ouders, dus een rechttoe UPDATE volstaat.
    zetters = ", ".join(f"{k} = %s" for k in CATEGORIE_KOLOMMEN)
    pc.executemany(
        f"UPDATE v2a.categorie SET {zetters} WHERE categorie_id = %s",
        [tuple(r[1:]) + (r[0],) for r in rijen],
    )
    pconn.commit()

    pc.execute("SELECT count(*) FILTER (WHERE status='bevestigd'), count(*) FILTER (WHERE parent_id IS NOT NULL AND status='bevestigd') FROM v2a.categorie")
    bevestigd, sub = pc.fetchone()
    log(f"categorie gesynchroniseerd: {bevestigd} bevestigd, waarvan {sub} subcategorie")


def laad_annotatie(lconn, pconn, pc) -> int:
    """Zet `v2a.chunk_annotatie` over — dezelfde schaduw+swap als de categorie.

    Toegevoegd 2026-08-08. Dit script synchroniseerde `tekst_embedding` en
    `chunk_categorie`, maar niet `chunk_annotatie`, en dat gat viel pas op na de
    eerste incrementele refresh: door de FK-cascade verdwenen op prod de
    annotatie-rijen van opgeruimde chunks (735.964 → 696.776), terwijl de
    herbouwde rijen voor nieuwe chunks er niet bij kwamen. Lokaal 743.073 tegen
    prod 696.776 — 46.297 rijen verschil op een tabel die de per-chunk
    werkingsgebieden draagt.

    Herbouwen op prod zou ook kunnen (4,2 min lokaal gemeten), maar dat is
    `DROP TABLE` + `CREATE` op een tabel waar de viewer op leest. Kopiëren met
    een swap houdt de leestijd op milliseconden.
    """
    pc.execute("DROP TABLE IF EXISTS v2a.chunk_annotatie_nieuw")
    pc.execute("CREATE TABLE v2a.chunk_annotatie_nieuw (LIKE v2a.chunk_annotatie)")
    pconn.commit()

    kols = "chunk_id, object_type, object_id, locatie_id, herkomst, kwalificatie"
    n = 0
    t0 = time.time()
    with lconn.cursor(name="lees_annotatie") as rc:
        rc.itersize = 20000
        rc.execute(f"SELECT {kols} FROM v2a.chunk_annotatie")
        with pc.copy(f"COPY v2a.chunk_annotatie_nieuw ({kols}) FROM STDIN") as cp:
            for rij in rc:
                cp.write_row(rij)
                n += 1
                if n % 200000 == 0:
                    log(f"  annotatie {n} rijen ({n / (time.time() - t0):.0f}/s)")
    pconn.commit()

    with pconn.transaction():
        pc.execute("DROP TABLE IF EXISTS v2a.chunk_annotatie_oud")
        pc.execute("""SELECT indexname FROM pg_indexes
                       WHERE schemaname='v2a' AND tablename='chunk_annotatie'""")
        for i, (idx,) in enumerate(pc.fetchall(), 1):
            pc.execute(f'ALTER INDEX v2a."{idx}" RENAME TO chunk_annotatie_oud_idx_{i}')
        pc.execute("ALTER TABLE v2a.chunk_annotatie RENAME TO chunk_annotatie_oud")
        pc.execute("ALTER TABLE v2a.chunk_annotatie_nieuw RENAME TO chunk_annotatie")
        pc.execute("CREATE INDEX chunk_annotatie_chunk_idx ON v2a.chunk_annotatie (chunk_id)")
        pc.execute("CREATE INDEX chunk_annotatie_locatie_idx ON v2a.chunk_annotatie (locatie_id)")
        pc.execute("CREATE INDEX chunk_annotatie_object_idx ON v2a.chunk_annotatie (object_id)")
        pc.execute("CREATE INDEX chunk_annotatie_herkomst_idx ON v2a.chunk_annotatie (herkomst)")
    log(f"chunk_annotatie: {n} rijen geladen en omgewisseld in {time.time() - t0:.0f}s")
    return n


def laad_toewijzingen(lconn, pconn, pc) -> int:
    pc.execute("DROP TABLE IF EXISTS v2a.chunk_categorie_nieuw")
    pc.execute("CREATE TABLE v2a.chunk_categorie_nieuw (LIKE v2a.chunk_categorie)")
    pconn.commit()

    n = 0
    t0 = time.time()
    with lconn.cursor(name="lees_toewijzingen") as rc:
        rc.itersize = 20000
        rc.execute("SELECT chunk_id, categorie_id, afstand, taxonomie_versie FROM v2a.chunk_categorie")
        with pc.copy(
            "COPY v2a.chunk_categorie_nieuw (chunk_id, categorie_id, afstand, taxonomie_versie) FROM STDIN"
        ) as cp:
            for rij in rc:
                cp.write_row(rij)
                n += 1
                if n % 100000 == 0:
                    log(f"  {n} rijen ({n / (time.time() - t0):.0f}/s)")
    pconn.commit()
    log(f"geladen: {n} rijen in {time.time() - t0:.0f}s")
    return n


def wissel_om(pconn, pc, verwacht: int) -> None:
    # Controles vóór de swap. Een verkeerde categorie_id zou pas bij het
    # herstellen van de foreign key opvallen, en dan sta je met een halve swap.
    pc.execute("SELECT count(*) FROM v2a.chunk_categorie_nieuw")
    (n,) = pc.fetchone()
    if n != verwacht:
        sys.exit(f"STOP: {n} rijen geladen, {verwacht} verwacht")

    pc.execute("""
        SELECT count(*) FROM v2a.chunk_categorie_nieuw cc
        WHERE NOT EXISTS (SELECT 1 FROM v2a.categorie k WHERE k.categorie_id = cc.categorie_id)
    """)
    (wees,) = pc.fetchone()
    if wees:
        sys.exit(f"STOP: {wees} toewijzingen wijzen naar een onbekende categorie")

    pc.execute("CREATE INDEX chunk_categorie_nieuw_chunk_idx ON v2a.chunk_categorie_nieuw (chunk_id)")
    pc.execute("CREATE INDEX chunk_categorie_nieuw_cat_idx ON v2a.chunk_categorie_nieuw (categorie_id)")
    pconn.commit()

    with pconn.transaction():
        # 1. vorige oude tabel weg — dat geeft ook haar indexnamen vrij
        pc.execute("DROP TABLE IF EXISTS v2a.chunk_categorie_oud")

        # 2. de indices van de huidige tabel uit de weg hernoemen. Indexnamen
        #    zijn schema-uniek en reizen mee met een tabel-rename, dus zonder
        #    deze stap botst de nieuwe index op de naam van de oude. Dat is
        #    precies wat op 2026-08-08 misging: de run van 06-08 had de suffix
        #    al naar `_idx2` geschoven, waarna de volgende run nergens meer
        #    heen kon. Nummeren i.p.v. een vaste suffix houdt het idempotent.
        pc.execute("""SELECT indexname FROM pg_indexes
                       WHERE schemaname='v2a' AND tablename='chunk_categorie'
                       ORDER BY indexname""")
        for i, (idx,) in enumerate(pc.fetchall(), 1):
            pc.execute(f'ALTER INDEX v2a."{idx}" RENAME TO chunk_categorie_oud_idx_{i}')

        # 3. omwisselen
        pc.execute("ALTER TABLE v2a.chunk_categorie RENAME TO chunk_categorie_oud")
        pc.execute("ALTER TABLE v2a.chunk_categorie_nieuw RENAME TO chunk_categorie")

        # 4. de nieuwe indices krijgen altijd dezelfde canonieke namen
        pc.execute("ALTER INDEX v2a.chunk_categorie_nieuw_chunk_idx RENAME TO chunk_categorie_chunk_idx")
        pc.execute("ALTER INDEX v2a.chunk_categorie_nieuw_cat_idx RENAME TO chunk_categorie_cat_idx")
    log("omgewisseld — oude tabel staat als v2a.chunk_categorie_oud")

    # Foreign keys erbij, NOT VALID zodat de swap kort blijft; daarna valideren.
    pc.execute("""ALTER TABLE v2a.chunk_categorie
                  ADD CONSTRAINT chunk_categorie_categorie_id_fkey
                  FOREIGN KEY (categorie_id) REFERENCES v2a.categorie(categorie_id) NOT VALID""")
    pc.execute("""ALTER TABLE v2a.chunk_categorie
                  ADD CONSTRAINT chunk_categorie_chunk_id_fkey
                  FOREIGN KEY (chunk_id) REFERENCES v2a.tekst_embedding(id) ON DELETE CASCADE NOT VALID""")
    pconn.commit()
    pc.execute("ALTER TABLE v2a.chunk_categorie VALIDATE CONSTRAINT chunk_categorie_categorie_id_fkey")
    pc.execute("ALTER TABLE v2a.chunk_categorie VALIDATE CONSTRAINT chunk_categorie_chunk_id_fkey")
    pconn.commit()
    log("foreign keys hersteld en gevalideerd")


def toon_resultaat(pc) -> None:
    pc.execute("""
        SELECT k.naam, count(*) AS n, (k.parent_id IS NOT NULL) AS is_sub
        FROM v2a.chunk_categorie cc JOIN v2a.categorie k USING (categorie_id)
        GROUP BY 1, 3 ORDER BY 2 DESC LIMIT 8
    """)
    for naam, n, is_sub in pc.fetchall():
        # ASCII: de Windows-console draait op cp1252 en een kader-teken laat
        # het script struikelen op de laatste regel, ná al het echte werk.
        log(f"  {'  - ' if is_sub else ''}{naam}: {n}")


def main() -> None:
    echt = "--ja" in sys.argv
    with psycopg.connect(LOKAAL) as lconn, psycopg.connect(PROD, connect_timeout=30) as pconn:
        lc, pc = lconn.cursor(), pconn.cursor()

        nieuw, verdwenen = verschil_chunks(lc, pc)
        log(f"chunks: {len(nieuw)} nieuw lokaal, {len(verdwenen)} alleen nog op prod")
        if not echt:
            lc.execute("SELECT count(*) FROM v2a.chunk_categorie")
            log(f"droogloop — zou {len(nieuw)} chunks en {lc.fetchone()[0]} toewijzingen "
                f"overzetten. Draai met --ja.")
            return

        # Volgorde is dwingend: eerst de chunks (foreign key), dan de
        # categorieën (foreign key), dan pas de toewijzingen.
        sync_embeddings(lconn, pconn, pc, nieuw, verdwenen)
        controleer_pariteit(lc, pc)   # nu moet het wél exact gelijk zijn
        sync_categorie(lc, pconn, pc)
        n = laad_toewijzingen(lconn, pconn, pc)
        wissel_om(pconn, pc, n)
        laad_annotatie(lconn, pconn, pc)   # hangt óók aan tekst_embedding(id)
        toon_resultaat(pc)


if __name__ == "__main__":
    main()
