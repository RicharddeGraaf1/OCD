"""Repliceer het i2a-schema (toepasbare regels) van lokaal naar productie.

Waarom dit script bestaat
-------------------------
Stap 5 van `docs/synchronisatie-runbook.md` was tot 2026-08-13 een afweging
zonder gereedschap: "verschil substantieel → repliceren volgens hetzelfde
principe als stap 3", maar dat script bestond niet. Gevolg: prod draaide nog op
de april-stand terwijl lokaal twee reparaties verder was — de prefix-fix van
G-117 (+384.178 uitvoeringsregels) en de peildatum-fix van G-122. Gemeten
2026-08-13: lokaal 1.232.842 uitvoeringsregels tegen 831.835 op prod.

Waarom een volledige vervanging en geen delta
---------------------------------------------
i2a heeft geen bruikbare sleutel om een delta op te hangen. De tabellen hangen
niet aan `frbr_expression` maar aan `functionele_structuur_ref` / namespace, en
`toepasbaar_regelbestand.laatste_wijziging` bestaat pas sinds 2026-08-08 — op
prod staat hij voor vrijwel alles op NULL, dus vergelijken zou daar niets
opleveren. Het hele schema is bovendien klein genoeg om in één keer over te
zetten: ~1,2 GB, waarvan het meeste in `dmn_element` (598 MB) en
`toepasbaar_regelbestand` (296 MB).

**Alles gebeurt in één transactie op prod.** Lezers blijven de oude stand zien
tot de COMMIT; er is dus geen moment waarop de toepasbare regels half gevuld
zijn. Dat is hier belangrijker dan geheugenzuinigheid: het runbook noteert dat
nog niet vaststaat wélke afnemer deze tabellen op prod gebruikt, en een lezer
die tijdens een replicatie langskomt hoort geen lege set te krijgen.

FK-volgorde en de twee valkuilen
--------------------------------
`dmn_element.parent_id` wijst naar zichzelf, dus de ouder moet vóór het kind
worden ingevoegd — de diepte reist mee als kolom en bepaalt de INSERT-volgorde,
net als in `repliceer_p2p_naar_prod.py`.

`regelbeheerobject.activiteit_id`, `werkzaamheid.activiteit_id` en
`aansluiting.activiteit_id` verwijzen naar `p2p.activiteit`. Staat die
activiteit niet op prod, dan wordt de rij **overgeslagen én geteld** — niet
stilzwijgend weggelaten en niet de hele transactie laten vallen. Dezelfde lijn
als de tekstdeel-koppelingen in G-124.

Draaien:  python scripts/repliceer_i2a_naar_prod.py [--ja]
Zonder --ja toont hij de tellingen aan beide kanten en wat er zou gebeuren.
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

# (tabel, self-FK-kolom of None, filter dat FK's naar p2p bewaakt)
# Volgorde = FK-volgorde: ouders eerst. Verwijderen gebeurt in omgekeerde volgorde.
PLAN = [
    ("i2a.regelbeheerobject", None,
     "q.activiteit_id IS NULL OR EXISTS (SELECT 1 FROM p2p.activiteit a"
     " WHERE a.identificatie = q.activiteit_id)"),
    ("i2a.toepasbaar_regelbestand", None, None),
    ("i2a.dmn_element", "parent_id", None),
    ("i2a.uitvoeringsregel", None, None),
    ("i2a.werkzaamheid", None,
     "q.activiteit_id IS NULL OR EXISTS (SELECT 1 FROM p2p.activiteit a"
     " WHERE a.identificatie = q.activiteit_id)"),
    # Junctietabel sinds 2026-09-02 (gaps#G-136). Bewust GEEN p2p-filter: we
    # bewaren elke koppeling die de RTR geeft en noteren in `gezien_in_p2p` of
    # we de activiteit terugvinden. Filteren zou hier precies de dataverlies
    # herintroduceren die de oude loader had.
    ("i2a.werkzaamheid_activiteit", None, None),
    # i2a.sttr_bestand gaat BEWUST niet mee: ~0,37 GB ruwe XML die prod niet
    # serveert. Hoort in de warme laag, niet in de hot-DB.
    ("i2a.aansluitpunt", None, None),
    ("i2a.aansluiting", None,
     "q.activiteit_id IS NULL OR EXISTS (SELECT 1 FROM p2p.activiteit a"
     " WHERE a.identificatie = q.activiteit_id)"),
]


def log(*a):
    print(*a, flush=True)


def kolommen(cur, tabel: str) -> list[str]:
    schema, naam = tabel.split(".")
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND is_generated <> 'ALWAYS' "
        "ORDER BY ordinal_position", (schema, naam))
    return [r[0] for r in cur.fetchall()]


def identity_altijd(cur, tabel: str) -> bool:
    schema, naam = tabel.split(".")
    cur.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND identity_generation='ALWAYS'",
        (schema, naam))
    return cur.fetchone()[0] > 0


def tel(cur, tabel: str) -> int:
    cur.execute(f"SELECT count(*) FROM {tabel}")
    return cur.fetchone()[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ja", action="store_true", help="echt repliceren (anders droogloop)")
    args = ap.parse_args()

    lconn = psycopg.connect(LOKAAL)
    pconn = psycopg.connect(PROD, connect_timeout=30)
    lc, pc = lconn.cursor(), pconn.cursor()
    pc.execute("SET max_parallel_workers_per_gather = 0")
    pc.execute("SET max_parallel_maintenance_workers = 0")

    # schema-check vóór alles: een COPY op verschoven kolommen schrijft onzin
    fout = []
    for tabel, _, _ in PLAN:
        kl, kp = kolommen(lc, tabel), kolommen(pc, tabel)
        if kl != kp:
            fout.append(f"  {tabel}\n    lokaal: {kl}\n    prod  : {kp}")
    if fout:
        sys.exit("STOP: kolomdrift tussen lokaal en prod:\n" + "\n".join(fout))
    log(f"  schema-check ok — {len(PLAN)} tabellen, identieke kolomvolgorde")

    log(f"\n  {'tabel':32}{'lokaal':>12}{'prod':>12}{'  verschil':>12}")
    standen = {}
    for tabel, _, _ in PLAN:
        a, b = tel(lc, tabel), tel(pc, tabel)
        standen[tabel] = (a, b)
        log(f"  {tabel:32}{a:>12,}{b:>12,}{a - b:>+12,}")

    if not args.ja:
        log("\nDROOGLOOP — draai opnieuw met --ja. Prod wordt dan in één "
            "transactie vervangen door de lokale stand.")
        return

    t0 = time.time()
    overgeslagen: dict[str, int] = {}

    # leegmaken in omgekeerde FK-volgorde, alles binnen dezelfde transactie
    for tabel, _, _ in reversed(PLAN):
        pc.execute(f"DELETE FROM {tabel}")
    log(f"  prod geleegd binnen de transactie ({time.time() - t0:.1f}s)")

    for tabel, self_fk, guard in PLAN:
        t1 = time.time()
        kol_lijst = kolommen(lc, tabel)
        kols = ", ".join(f'"{k}"' for k in kol_lijst)
        gekwalificeerd = ", ".join(f'q."{k}"' for k in kol_lijst)
        waar = f" WHERE {guard}" if guard else ""

        if self_fk:
            # diepte mee laten reizen: een kind mag nooit vóór zijn ouder
            bron = (
                f"WITH RECURSIVE d AS ("
                f"  SELECT id, 0 AS diepte FROM {tabel} WHERE {self_fk} IS NULL"
                f"  UNION ALL"
                f"  SELECT c.id, d.diepte + 1 FROM {tabel} c JOIN d ON c.{self_fk} = d.id"
                f"   WHERE d.diepte < 40)"
                f" SELECT {gekwalificeerd}, d.diepte AS _d FROM {tabel} q JOIN d ON d.id = q.id"
                f"{waar}")
        else:
            bron = f"SELECT {gekwalificeerd} FROM {tabel} q{waar}"

        stg = "stg_" + tabel.split(".")[1]
        pc.execute(f"CREATE TEMP TABLE {stg} ON COMMIT DROP AS "
                   f"SELECT {kols}{', 0::int AS _d' if self_fk else ''} "
                   f"FROM {tabel} WITH NO DATA")
        doel_kols = kols + (", _d" if self_fk else "")
        with lc.copy(f"COPY ({bron}) TO STDOUT (FORMAT TEXT)") as uit:
            with pc.copy(f"COPY {stg} ({doel_kols}) FROM STDIN (FORMAT TEXT)") as inn:
                for blok in uit:
                    inn.write(blok)

        overriding = " OVERRIDING SYSTEM VALUE" if identity_altijd(pc, tabel) else ""
        pc.execute(f"INSERT INTO {tabel} ({kols}){overriding} "
                   f"SELECT {kols} FROM {stg}{' ORDER BY _d' if self_fk else ''}")
        n = pc.rowcount
        gemist = standen[tabel][0] - n
        if gemist:
            overgeslagen[tabel] = gemist
        log(f"  {tabel:32}{n:>12,} ingevoegd"
            f"{f'  ({gemist:,} overgeslagen: ouder ontbreekt op prod)' if gemist else ''}"
            f"  ({time.time() - t1:.1f}s)")

    # sequences meeschuiven vóór de commit
    for tabel, _, _ in PLAN:
        schema, naam = tabel.split(".")
        pc.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s AND (is_identity='YES' "
            "OR column_default LIKE 'nextval%%')", (schema, naam))
        for (kol,) in pc.fetchall():
            pc.execute(f"SELECT setval(pg_get_serial_sequence(%s,%s), "
                       f"  coalesce((SELECT max({kol}) FROM {tabel}), 1))", (tabel, kol))

    pconn.commit()
    log(f"\nKlaar in {(time.time() - t0) / 60:.1f} min.")
    if overgeslagen:
        log("Overgeslagen wegens ontbrekende p2p-activiteit op prod:")
        for t, n in overgeslagen.items():
            log(f"  {t:32}{n:>10,}")
    else:
        log("Niets overgeslagen — alle FK-doelen bestonden op prod.")


if __name__ == "__main__":
    main()
