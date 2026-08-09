# -*- coding: utf-8 -*-
"""Zet v2a.pad_categorie + v2a.artikel_indeling op productie.

Waarom kopiëren en niet herberekenen: `bouw_indeling.py` trekt de hele
opschriftketen van 148.282 artikelen naar Python. Dat is lokaal een minuut over
een unix-socket en over het internet naar Railway een veelvoud, op een instance
met een harde geheugenkap van 2 GB. De uitkomst is bovendien deterministisch —
zelfde tekst_element-ids in, zelfde indeling uit — dus er valt niets te winnen
met opnieuw rekenen.

**De ids zijn NIET uitwisselbaar.** Gemeten 2026-08-09: lokaal en prod hebben
allebei 154.725 artikelen, maar een verschillende md5 over de id-lijst — de
serials zijn in een andere volgorde uitgedeeld. Het oudere script
`2026-08-06-categorie-naar-productie.py` mag wél op id kopiëren omdat
`v2a.tekst_embedding` daar aantoonbaar identiek is; hier zou dat de indeling
aan de verkeerde artikelen hangen.

Daarom gaat het over de **natuurlijke sleutel** `(regeling_expression, wid)`.
Die is aan beide kanten uniek voor alle 154.725 artikelen en nul ervan mist een
wid, dus de FK naar `tekst_element_id` wordt op prod opnieuw opgezocht.

Schaduwtabel + rename in plaats van TRUNCATE, om dezelfde reden als het oudere
script: TRUNCATE pakt een ACCESS EXCLUSIVE lock die pas bij COMMIT valt, dus de
hele laadtijd zou élke lezer blokkeren — en het onderwerpen-endpoint hangt
daaraan. De rename blokkeert milliseconden.

De oude tabellen blijven staan als `*_oud`. Terugdraaien is twee renames.

Draaien:  python scripts/indeling_naar_productie.py [--ja]
Zonder --ja alleen meten en vergelijken (droogloop).
"""
import os
import sys
import time

import psycopg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LOKAAL = os.environ.get("DSO_DB", "postgresql://postgres:postgres@localhost:5434/dso")
PROD = os.environ["PROD_DB_URL"]
DDL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "2026-08-add-pad-categorie.sql")

# Vingerafdruk over de NATUURLIJKE sleutel, niet over de serials. Zo meet je of
# beide kanten dezelfde artikelen kennen — de vraag die er hier toe doet.
VINGERAFDRUK = """
    SELECT count(*), count(*) FILTER (WHERE wid IS NULL),
           md5(string_agg(regeling_expression || '|' || coalesce(wid,''),
                          ',' ORDER BY regeling_expression, wid))
    FROM p2p.tekst_element WHERE element_type = 'Artikel'
"""


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def controleer_pariteit(lc, pc) -> None:
    lc.execute(VINGERAFDRUK)
    l = lc.fetchone()
    pc.execute(VINGERAFDRUK)
    p = pc.fetchone()
    log(f"lokaal artikelen n={l[0]} zonder wid={l[1]} hash={l[2][:12]}")
    log(f"prod   artikelen n={p[0]} zonder wid={p[1]} hash={p[2][:12]}")
    if l != p:
        sys.exit(
            "STOP: prod kent andere artikelen dan lokaal.\n"
            "De sleutel (regeling_expression, wid) matcht niet, dus de indeling\n"
            "zou deels aan niets hangen. Eerst p2p synchroniseren\n"
            "(repliceer_p2p_naar_prod.py), dan dit script."
        )
    log("pariteit bevestigd op (regeling_expression, wid)")


def laad(lconn, pconn, pc, tabel: str, kolommen: list[str],
         nullable: list[str] = ()) -> int:
    pc.execute(f"DROP TABLE IF EXISTS v2a.{tabel}_nieuw")
    pc.execute(f"CREATE TABLE v2a.{tabel}_nieuw (LIKE v2a.{tabel})")
    # `LIKE` neemt NOT NULL mee. Voor kolommen die pas ná het laden gevuld
    # worden — tekst_element_id wordt op prod opgezocht — moet die eis er
    # tijdelijk af, anders faalt de COPY op de eerste rij.
    for kol_naam in nullable:
        pc.execute(f"ALTER TABLE v2a.{tabel}_nieuw ALTER COLUMN {kol_naam} DROP NOT NULL")
    pconn.commit()
    kol = ", ".join(kolommen)
    n, t0 = 0, time.time()
    with lconn.cursor(name=f"lees_{tabel}") as rc:
        rc.itersize = 20000
        rc.execute(f"SELECT {kol} FROM v2a.{tabel}")
        with pc.copy(f"COPY v2a.{tabel}_nieuw ({kol}) FROM STDIN") as cp:
            for rij in rc:
                cp.write_row(rij)
                n += 1
                if n % 50000 == 0:
                    log(f"  {tabel}: {n} rijen ({n / (time.time() - t0):.0f}/s)")
    pconn.commit()
    log(f"{tabel}: {n} rijen geladen in {time.time() - t0:.0f}s")
    return n


def wissel_om(pconn, pc, tabel: str, verwacht: int, indices: list[str]) -> None:
    pc.execute(f"SELECT count(*) FROM v2a.{tabel}_nieuw")
    (n,) = pc.fetchone()
    if n != verwacht:
        sys.exit(f"STOP: {tabel} kreeg {n} rijen, {verwacht} verwacht")
    for i, ddl in enumerate(indices):
        pc.execute(ddl.format(tabel=f"{tabel}_nieuw", i=i))
    pconn.commit()
    with pconn.transaction():
        pc.execute(f"DROP TABLE IF EXISTS v2a.{tabel}_oud")
        pc.execute(f"ALTER TABLE IF EXISTS v2a.{tabel} RENAME TO {tabel}_oud")
        pc.execute(f"ALTER TABLE v2a.{tabel}_nieuw RENAME TO {tabel}")
    log(f"{tabel}: omgewisseld (oude versie staat als {tabel}_oud)")


def toon_resultaat(pc) -> None:
    pc.execute("""
        SELECT coalesce(categorie, '(niet ingedeeld)'), count(*)
        FROM v2a.artikel_indeling GROUP BY 1 ORDER BY 2 DESC LIMIT 8
    """)
    log("categorieen op prod:")
    for naam, n in pc.fetchall():
        log(f"    {n:7}  {naam}")
    pc.execute("""SELECT count(*) FILTER (WHERE categorie IS NOT NULL),
                         count(*) FILTER (WHERE type_bepaling IS NOT NULL), count(*)
                  FROM v2a.artikel_indeling""")
    cat, tb, tot = pc.fetchone()
    log(f"  ingedeeld {cat}/{tot} ({100*cat/tot:.1f}%) · "
        f"typeBepaling {tb}/{tot} ({100*tb/tot:.1f}%)")


def main() -> None:
    echt = "--ja" in sys.argv
    with psycopg.connect(LOKAAL) as lconn, psycopg.connect(PROD, connect_timeout=30) as pconn:
        lc, pc = lconn.cursor(), pconn.cursor()

        controleer_pariteit(lc, pc)
        for tabel in ("pad_categorie", "artikel_indeling"):
            lc.execute(f"SELECT count(*) FROM v2a.{tabel}")
            log(f"lokaal v2a.{tabel}: {lc.fetchone()[0]} rijen")

        if not echt:
            log("droogloop — niets geschreven. Draai met --ja.")
            return

        # De tabellen kunnen op prod nog niet bestaan; de DDL is idempotent
        # (DROP + CREATE) en draait daarom vóór het laden.
        pc.execute(open(DDL, encoding="utf-8").read())
        pconn.commit()
        log("tabellen aangemaakt op prod")

        n1 = laad(lconn, pconn, pc, "pad_categorie",
                  ["pad_sleutel", "pad_voorbeeld", "categorie", "subcategorie",
                   "n_artikelen", "n_bronhouders", "bron", "curatie_versie"])
        wissel_om(pconn, pc, "pad_categorie", n1, [])

        # tekst_element_id gaat NIET mee: die is prod-specifiek en wordt na het
        # laden opgezocht op (regeling_expression, wid).
        n2 = laad(lconn, pconn, pc, "artikel_indeling",
                  ["regeling_expression", "wid", "pad_sleutel",
                   "categorie", "subcategorie", "type_bepaling", "herkomst",
                   "curatie_versie"],
                  nullable=["tekst_element_id"])
        pc.execute("""
            UPDATE v2a.artikel_indeling_nieuw ai
            SET tekst_element_id = te.id
            FROM p2p.tekst_element te
            WHERE te.regeling_expression = ai.regeling_expression
              AND te.wid = ai.wid AND te.element_type = 'Artikel'
        """)
        pconn.commit()
        pc.execute("SELECT count(*) FROM v2a.artikel_indeling_nieuw WHERE tekst_element_id IS NULL")
        (wees,) = pc.fetchone()
        if wees:
            sys.exit(f"STOP: {wees} rijen vonden geen artikel op prod — niet omgewisseld")
        log("tekst_element_id opgezocht op prod, 0 wezen")
        # Eis en sleutel weer terug: `LIKE` neemt de PRIMARY KEY niet mee, dus
        # zonder dit zou de tabel op prod zwakker zijn dan lokaal.
        pc.execute("ALTER TABLE v2a.artikel_indeling_nieuw "
                   "ALTER COLUMN tekst_element_id SET NOT NULL")
        pc.execute("ALTER TABLE v2a.artikel_indeling_nieuw "
                   "ADD PRIMARY KEY (tekst_element_id)")
        pconn.commit()
        wissel_om(pconn, pc, "artikel_indeling", n2, [
            "CREATE INDEX ai_regeling_idx{i} ON v2a.{tabel} (regeling_expression)",
            "CREATE INDEX ai_wid_idx{i} ON v2a.{tabel} (wid)",
            "CREATE INDEX ai_cat_idx{i} ON v2a.{tabel} (categorie)",
            "CREATE INDEX ai_type_idx{i} ON v2a.{tabel} (type_bepaling)",
        ])
        toon_resultaat(pc)


if __name__ == "__main__":
    main()
