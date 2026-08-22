"""Repliceer het wijzigingsspoor (`p2pwijziging`) van lokaal naar productie.

Waarom dit script bestaat
-------------------------
Sinds 2026-08-08 geldt: prod krijgt gegevens, geen loaders. Voor `p2p.*` doet
`repliceer_p2p_naar_prod.py` dat. Voor `p2pwijziging` bestond die stap niet, en
daarom stond in `docs/synchronisatie-runbook.md` §Stap 1b de enige uitzondering
op die regel: daar draaide de loader wél tegen prod. Dat kostte een tweede
volledige bevraging van de DSO voor gegevens die lokaal al lagen, en het liet
prod en lokaal onafhankelijk van elkaar laden — precies wat de regel wilde
voorkomen. Dit script heft die uitzondering op.

Waarom het niet dezelfde vorm heeft als zijn p2p-broer
------------------------------------------------------
`repliceer_p2p_naar_prod.py` voegt toe en werkt bij, maar verwijdert nooit. Dat
kan hier niet, om één reden: stap 10 van het runbook (`ruim_wijzigingsspoor_op`)
haalt lokaal rijen wég. Gemeten 2026-08-12 — lokaal tegenover prod:

    tekst_element      303.657  tegen    725.428
    annotatie_delta    283.562  tegen    721.253
    locatie_delta      585.793  tegen  1.588.950

Een script dat alleen toevoegt zou dat verschil nooit dichten. Daarom
**spiegelt** dit script: na afloop is prod gelijk aan lokaal.

De eenheid van spiegelen is het besluit. Elke tabel in dit schema hangt via
`ontwerpbesluit_id` aan `p2pwijziging.besluit`, met `ON DELETE CASCADE`. Een
besluit dat verschilt wordt daarom in zijn geheel vervangen: de rij weghalen
(waarmee al zijn gevolg cascadeert), en opnieuw invoegen vanuit lokaal. Dat is
niet alleen simpeler dan per tabel verschillen bepalen, het is ook het enige wat
klopt bij een herladen ontwerp — dan krijgen de kindrijen nieuwe identity-id's
terwijl het besluit hetzelfde blijft.

Elk besluit gaat in zijn eigen transactie. Valt de verbinding halverwege weg,
dan is elk afgehandeld besluit compleet en de rest onaangeroerd; opnieuw draaien
pakt de rest op.

Hoe "verschilt" wordt bepaald
-----------------------------
Per besluit een vingerafdruk aan beide kanten: de besluitrij zelf als md5 over
de hele rij, en per kindtabel `(count(*), sum(hashtext(pk)))`. Die som vangt het
geval dat een herlading evenveel rijen oplevert met andere sleutels — een
telling alleen zou dat missen. Wat identiek is, wordt overgeslagen; bij een
gewone sync is dat de overgrote meerderheid.

Draaien:  python scripts/repliceer_p2pwijziging_naar_prod.py [--ja]
Zonder --ja is het een droogloop: hij toont welke besluiten zouden worden
vervangen, en hoeveel rijen dat aan beide kanten scheelt.
"""

import argparse
import os
import sys
import time

import psycopg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LOKAAL = os.environ.get("OCD_DB_URL", "postgresql://postgres:postgres@localhost:5434/dso")
PROD = os.environ["PROD_DB_URL"]

# Kindtabellen in FK-volgorde: juridische_regel_delta vóór zijn drie deltas,
# want die verwijzen ernaar met (identificatie, ontwerpbesluit_id).
# Per tabel de kolom(men) waarover de vingerafdruk-som loopt.
KINDEREN = [
    ("p2pwijziging.tekst_element", "id::text"),
    ("p2pwijziging.procedurestap", "id::text"),
    ("p2pwijziging.locatie_delta", "id::text"),
    ("p2pwijziging.annotatie_delta", "id::text"),
    ("p2pwijziging.juridische_regel_delta", "identificatie"),
    ("p2pwijziging.juridische_regel_activiteit_delta", "id::text"),
    ("p2pwijziging.juridische_regel_gebiedsaanwijzing_delta", "id::text"),
    ("p2pwijziging.juridische_regel_norm_delta", "id::text"),
]

BESLUIT = "p2pwijziging.besluit"

# tekst_element verwijst met parent_id naar zichzelf. De ouder moet er zijn
# vóór het kind, dus invoegen op diepte-volgorde binnen het besluit.
SELF_FK = {"p2pwijziging.tekst_element"}


# Voortgangsregels bevatten een pijl (U+2192); op een cp1252-console gooit dat een
# UnicodeEncodeError na het zware werk. Zelfde val als in repliceer_p2p_naar_prod.py
# (2026-08-22). 44 scripts in deze repo hebben hem nog - zie [[gaps#G-129]].
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def log(*a):
    print(*a, flush=True)


def kolommen(cur, tabel: str) -> list[str]:
    """Alles behalve GENERATED-kolommen — `tekst_element.inhoud_plain` is stored
    generated en invoegen daarvan is een harde fout."""
    schema, naam = tabel.split(".")
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND is_generated <> 'ALWAYS' "
        "ORDER BY ordinal_position",
        (schema, naam))
    return [r[0] for r in cur.fetchall()]


def identity_kolommen(cur, tabel: str) -> list[tuple[str, bool]]:
    schema, naam = tabel.split(".")
    cur.execute(
        "SELECT column_name, is_identity, identity_generation, column_default "
        "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, naam))
    uit = []
    for kol, is_id, gen, default in cur.fetchall():
        if is_id == "YES" or (default or "").startswith("nextval("):
            uit.append((kol, gen == "ALWAYS"))
    return uit


def controleer_schema(lc, pc) -> None:
    fout = []
    for tabel in [BESLUIT] + [t for t, _ in KINDEREN]:
        kl, kp = kolommen(lc, tabel), kolommen(pc, tabel)
        if kl != kp:
            fout.append(f"  {tabel}\n    lokaal: {kl}\n    prod  : {kp}")
    if fout:
        sys.exit("STOP: kolomdrift tussen lokaal en prod:\n" + "\n".join(fout))
    log(f"  schema-check ok — {len(KINDEREN) + 1} tabellen, identieke kolomvolgorde")


def vingerafdruk(cur) -> dict[str, tuple]:
    """Per ontwerpbesluit_id: (md5 van de besluitrij, per kindtabel n en som).

    De som over `hashtext(pk)` maakt het verschil met een kale telling: een
    herladen ontwerp levert evenveel rijen met nieuwe identity-id's op, en dat
    moet als "gewijzigd" gelden.
    """
    uit: dict[str, list] = {}
    cur.execute(f"SELECT ontwerpbesluit_id, md5(t::text) FROM {BESLUIT} t")
    for bid, h in cur.fetchall():
        uit[bid] = [h] + [(0, 0)] * len(KINDEREN)

    for i, (tabel, pk_expr) in enumerate(KINDEREN):
        cur.execute(
            f"SELECT ontwerpbesluit_id, count(*), "
            f"       coalesce(sum(hashtext({pk_expr})::bigint), 0) "
            f"  FROM {tabel} GROUP BY ontwerpbesluit_id")
        for bid, n, som in cur.fetchall():
            if bid in uit:
                uit[bid][i + 1] = (n, int(som))
    return {k: tuple(v) for k, v in uit.items()}


def kopieer_besluit(lc, pc, bid: str, kolcache: dict, identcache: dict) -> dict[str, int]:
    """Vervang één besluit op prod door de lokale versie. Aanroeper commit."""
    aantallen: dict[str, int] = {}

    # 1. weg met het oude besluit; de FK's cascaderen het hele gevolg.
    pc.execute(f"DELETE FROM {BESLUIT} WHERE ontwerpbesluit_id = %s", (bid,))

    # 2. het besluit zelf terug (geen identity-kolommen, dus COPY volstaat)
    kols = ", ".join(f'"{k}"' for k in kolcache[BESLUIT])
    with lc.copy(f"COPY (SELECT {kols} FROM {BESLUIT} WHERE ontwerpbesluit_id = %s) "
                 f"TO STDOUT (FORMAT TEXT)", (bid,)) as uit:
        with pc.copy(f"COPY {BESLUIT} ({kols}) FROM STDIN (FORMAT TEXT)") as inn:
            for blok in uit:
                inn.write(blok)

    # 3. de kinderen, in FK-volgorde
    for tabel, _ in KINDEREN:
        kol_lijst = kolcache[tabel]
        kols = ", ".join(f'"{k}"' for k in kol_lijst)
        gekwalificeerd = ", ".join(f'q."{k}"' for k in kol_lijst)
        diep = tabel in SELF_FK

        if diep:
            # De diepte reist mee als kolom en bepaalt de INSERT-volgorde:
            # parent_id verwijst binnen dezelfde tabel, dus een kind vóór zijn
            # ouder invoegen faalt. Zelfde patroon als repliceer_p2p_naar_prod.
            bron = (
                f"WITH RECURSIVE d AS ("
                f"  SELECT id, 0 AS diepte FROM {tabel}"
                f"   WHERE ontwerpbesluit_id = %s AND parent_id IS NULL"
                f"  UNION ALL"
                f"  SELECT c.id, d.diepte + 1 FROM {tabel} c JOIN d ON c.parent_id = d.id"
                f"   WHERE c.ontwerpbesluit_id = %s AND d.diepte < 40)"
                f" SELECT {gekwalificeerd}, d.diepte AS _d FROM {tabel} q JOIN d ON d.id = q.id")
            params = (bid, bid)
        else:
            bron = f"SELECT {gekwalificeerd} FROM {tabel} q WHERE q.ontwerpbesluit_id = %s"
            params = (bid,)

        overriding = " OVERRIDING SYSTEM VALUE" if any(a for _, a in identcache[tabel]) else ""

        if overriding or diep:
            # Identity ALWAYS kun je niet via COPY vullen (dat vereist
            # OVERRIDING, en COPY kent die clausule niet), en een diepte-kolom
            # moet ergens landen. Beide gevallen: staging + INSERT.
            stg = "stg_" + tabel.split(".")[1]
            pc.execute(f"CREATE TEMP TABLE {stg} ON COMMIT DROP AS "
                       f"SELECT {kols}{', 0::int AS _d' if diep else ''} "
                       f"FROM {tabel} WITH NO DATA")
            doel_kols = kols + (", _d" if diep else "")
            with lc.copy(f"COPY ({bron}) TO STDOUT (FORMAT TEXT)", params) as uit:
                with pc.copy(f"COPY {stg} ({doel_kols}) FROM STDIN (FORMAT TEXT)") as inn:
                    for blok in uit:
                        inn.write(blok)
            pc.execute(f"INSERT INTO {tabel} ({kols}){overriding} "
                       f"SELECT {kols} FROM {stg}{' ORDER BY _d' if diep else ''}")
        else:
            with lc.copy(f"COPY ({bron}) TO STDOUT (FORMAT TEXT)", params) as uit:
                with pc.copy(f"COPY {tabel} ({kols}) FROM STDIN (FORMAT TEXT)") as inn:
                    for blok in uit:
                        inn.write(blok)
        aantallen[tabel] = pc.rowcount

    return aantallen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ja", action="store_true", help="echt spiegelen (anders droogloop)")
    ap.add_argument("--max", type=int, default=None,
                    help="hooguit N besluiten behandelen (voor een eerste, kleine run)")
    ap.add_argument("--besluit", action="append", metavar="ID",
                    help="dit besluit hoe dan ook vervangen, ook als de vingerafdrukken "
                         "gelijk zijn. Herhaalbaar. Bedoeld om het invoegpad te toetsen: "
                         "een besluit dat aan beide kanten gelijk is moet na afloop nog "
                         "steeds gelijk zijn.")
    args = ap.parse_args()

    lconn = psycopg.connect(LOKAAL)
    pconn = psycopg.connect(PROD, connect_timeout=30)
    lc, pc = lconn.cursor(), pconn.cursor()

    # Kleine /dev/shm op de Railway-container: parallelle workers laten grote
    # operaties falen met "could not resize shared memory segment".
    pc.execute("SET max_parallel_workers_per_gather = 0")
    pc.execute("SET max_parallel_maintenance_workers = 0")

    controleer_schema(lc, pc)

    t0 = time.time()
    log("  vingerafdrukken bepalen...")
    fl, fp = vingerafdruk(lc), vingerafdruk(pc)
    log(f"  lokaal {len(fl)} besluiten, prod {len(fp)} — {time.time() - t0:.1f}s")

    nieuw = sorted(b for b in fl if b not in fp)
    gewijzigd = sorted(b for b in fl if b in fp and fl[b] != fp[b])
    verdwenen = sorted(b for b in fp if b not in fl)
    gelijk = len(fl) - len(nieuw) - len(gewijzigd)

    def rijen(f: dict, ids: list[str]) -> int:
        return sum(n for b in ids for (n, _) in f[b][1:])

    log(f"\n  gelijk      {gelijk:>5}")
    log(f"  nieuw       {len(nieuw):>5}  ({rijen(fl, nieuw):,} rijen naar prod)")
    log(f"  gewijzigd   {len(gewijzigd):>5}  (prod {rijen(fp, gewijzigd):,} rijen → "
        f"lokaal {rijen(fl, gewijzigd):,})")
    log(f"  verdwenen   {len(verdwenen):>5}  ({rijen(fp, verdwenen):,} rijen van prod af)")

    if not args.ja:
        log("\nDROOGLOOP — draai opnieuw met --ja om te spiegelen.")
        return

    kolcache = {t: kolommen(lc, t) for t in [BESLUIT] + [x for x, _ in KINDEREN]}
    identcache = {t: identity_kolommen(pc, t) for t, _ in KINDEREN}

    # verdwenen besluiten: weg (cascade)
    for bid in verdwenen:
        pc.execute(f"DELETE FROM {BESLUIT} WHERE ontwerpbesluit_id = %s", (bid,))
        pconn.commit()
    if verdwenen:
        log(f"  {len(verdwenen)} verdwenen besluiten verwijderd")

    doen = nieuw + gewijzigd
    if args.besluit:
        onbekend = [b for b in args.besluit if b not in fl]
        if onbekend:
            sys.exit(f"STOP: onbekend besluit lokaal: {onbekend}")
        doen = [b for b in args.besluit] + [b for b in doen if b not in args.besluit]
        log(f"  {len(args.besluit)} besluit(en) afgedwongen via --besluit")
    if args.max:
        doen = doen[:args.max]
        log(f"  beperkt tot {len(doen)} besluiten (--max)")

    totaal = 0
    for i, bid in enumerate(doen, 1):
        t1 = time.time()
        try:
            aantallen = kopieer_besluit(lc, pc, bid, kolcache, identcache)
            pconn.commit()
        except Exception as e:
            pconn.rollback()
            log(f"  [{i}/{len(doen)}] {bid} FOUT: {type(e).__name__}: {e}")
            continue
        n = sum(aantallen.values())
        totaal += n
        log(f"  [{i}/{len(doen)}] {bid[:60]:60} {n:>8,} rijen ({time.time() - t1:.1f}s)")

    # sequences meeschuiven, anders botst de eerstvolgende insert op prod
    for tabel, _ in KINDEREN:
        for kol, _altijd in identity_kolommen(pc, tabel):
            pc.execute(f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                       f"  coalesce((SELECT max({kol}) FROM {tabel}), 1))", (tabel, kol))
    pconn.commit()

    log(f"\nKlaar — {totaal:,} rijen geschreven over {len(doen)} besluiten "
        f"({time.time() - t0:.1f}s totaal).")


if __name__ == "__main__":
    main()
