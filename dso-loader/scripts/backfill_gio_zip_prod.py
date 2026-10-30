"""Zet het GIO-ZIP-herstel (herlaad_gio_zip.py) door naar productie.

Het pad naar prod bij `herlaad_gio_zip.py`, conform runbook §3a ("een backfill
levert zijn eigen pad naar prod mee"). Vault gaps.md G-173.

Waarom niet `repliceer_p2p_naar_prod.py --expressies`
----------------------------------------------------
Geprobeerd op 2026-10-30 voor 147 expressies, en afgebroken. Dat script neemt de
hele rijset van een regeling mee (2,59 mln rijen), terwijl het herstel er maar
vier tabellen raakt. Het herschreef 207.558 ongewijzigde locaties in 2,8 uur en
viel daarna om op id-drift die niets met GIO's te maken heeft: een
`activiteit_locatieaanduiding` die op prod onder een ander id staat
(`uq_ala_natural`), en 10.689 tekstelementen idem.

Wat dit script doet, en alleen dat
----------------------------------
1. `p2p.geo_informatieobject` van de expressies: upsert op `frbr_expression`.
2. `p2p.gio_basisgeo` van die GIO's: ontbrekende rijen erbij.
3. `p2p.locatie_basisgeo` van de bronhouders van die expressies: ontbrekende rijen
   erbij, maar alleen waar de locatie op prod bestaat. Wezen gaan niet mee (§3a).
4. `resolve_target_soort` op prod zelf, per expressie. Dat koppelt ExtIoRef en
   IntIoRef aan de GIO's met de data die prod nu heeft, en is daardoor ongevoelig
   voor tekstelementen die op prod een ander id hebben.

Alles is aanvullend en herhaalbaar. Daarna op prod: `refresh_drieslag.py` en de
health-MV's.

Gebruik:
    python scripts/backfill_gio_zip_prod.py --bestand expressies.txt          # droogloop
    python scripts/backfill_gio_zip_prod.py --bestand expressies.txt --ja
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LOKAAL = "postgresql://postgres:postgres@localhost:5434/dso"
PROD = os.environ["PROD_DB_URL"]


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def kopieer_ontbrekend(lc, pc, tabel: str, kols: list[str], select_lokaal: str, params,
                       extra_voorwaarde: str = "", ja: bool = False) -> tuple[int, int]:
    """Zet lokale rijen in een staging-tabel op prod en voeg in wat daar ontbreekt.

    Geeft (aangeboden, ingevoegd); in een droogloop (aangeboden, zou invoegen).
    """
    stg = f"_stg_{tabel.split('.')[1]}"
    kl = ", ".join(kols)
    pc.execute(f"CREATE TEMP TABLE {stg} (LIKE {tabel} INCLUDING DEFAULTS) ON COMMIT DROP")
    lc.execute(select_lokaal, params)
    n = 0
    with pc.copy(f"COPY {stg} ({kl}) FROM STDIN") as cp:
        for r in lc:
            cp.write_row(r)
            n += 1
    gelijk = " AND ".join(f"t.{k} = s.{k}" for k in kols)
    nieuw_sql = (f"SELECT DISTINCT {', '.join('s.' + k for k in kols)} FROM {stg} s "
                 f"WHERE NOT EXISTS (SELECT 1 FROM {tabel} t WHERE {gelijk}) {extra_voorwaarde}")
    if ja:
        pc.execute(f"INSERT INTO {tabel} ({kl}) {nieuw_sql} ON CONFLICT DO NOTHING")
        return n, pc.rowcount
    pc.execute(f"SELECT count(*) FROM ({nieuw_sql}) x")
    return n, pc.fetchone()[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bestand", required=True, help="één frbr_expression per regel")
    ap.add_argument("--ja", action="store_true", help="echt schrijven (anders droogloop)")
    args = ap.parse_args()
    with open(args.bestand, encoding="utf-8") as f:
        expr = [r.strip() for r in f if r.strip()]
    bronhouders = sorted({m.group(1) for e in expr if (m := re.match(r"/akn/nl/act/([a-z]+\d+)/", e))})
    log(f"{len(expr)} expressies, {len(bronhouders)} bronhouders · {'ECHT' if args.ja else 'droogloop'}")

    lconn = psycopg.connect(LOKAAL)
    pconn = psycopg.connect(PROD, connect_timeout=30)
    lc, pc = lconn.cursor(), pconn.cursor()
    pc.execute("SELECT current_database()")
    if pc.fetchone()[0] != "railway":
        sys.exit("STOP: PROD_DB_URL wijst niet naar de prod-database")
    pc.execute("SET max_parallel_workers_per_gather = 0")

    # 1. geo_informatieobject: upsert, met naam en geometrie-id
    kols = ["frbr_expression", "frbr_work", "regeling_expression", "geometrie_identificatie",
            "naam", "naam_informatieobject"]
    lc.execute(f"SELECT {', '.join(kols)} FROM p2p.geo_informatieobject WHERE regeling_expression = ANY(%s)", (expr,))
    gios = lc.fetchall()
    pc.execute("CREATE TEMP TABLE _stg_gio (LIKE p2p.geo_informatieobject INCLUDING DEFAULTS) ON COMMIT DROP")
    with pc.copy(f"COPY _stg_gio ({', '.join(kols)}) FROM STDIN") as cp:
        for r in gios:
            cp.write_row(r)
    pc.execute("SELECT count(*) FROM _stg_gio s WHERE NOT EXISTS "
               "(SELECT 1 FROM p2p.geo_informatieobject g WHERE g.frbr_expression = s.frbr_expression)")
    gio_nieuw = pc.fetchone()[0]
    if args.ja:
        pc.execute(f"""INSERT INTO p2p.geo_informatieobject ({', '.join(kols)})
                       SELECT {', '.join(kols)} FROM _stg_gio
                       ON CONFLICT (frbr_expression) DO UPDATE SET
                         naam = COALESCE(p2p.geo_informatieobject.naam, EXCLUDED.naam),
                         naam_informatieobject = COALESCE(EXCLUDED.naam_informatieobject,
                                                          p2p.geo_informatieobject.naam_informatieobject),
                         geometrie_identificatie = COALESCE(p2p.geo_informatieobject.geometrie_identificatie,
                                                            EXCLUDED.geometrie_identificatie)""")
    log(f"  geo_informatieobject: {len(gios):,} aangeboden, {gio_nieuw:,} nieuw op prod")

    # 2. gio_basisgeo van die GIO's
    n, nieuw = kopieer_ontbrekend(
        lc, pc, "p2p.gio_basisgeo", ["gio_frbr", "basisgeo_id"],
        "SELECT gb.gio_frbr, gb.basisgeo_id FROM p2p.gio_basisgeo gb "
        "JOIN p2p.geo_informatieobject g ON g.frbr_expression = gb.gio_frbr "
        "WHERE g.regeling_expression = ANY(%s)", (expr,), ja=args.ja)
    log(f"  gio_basisgeo: {n:,} aangeboden, {nieuw:,} {'ingevoegd' if args.ja else 'zou invoegen'}")

    # 3. locatie_basisgeo per bronhouder, alleen met bestaande locatie op prod
    prefixen = [f"nl.imow-{b}.%" for b in bronhouders]
    n, nieuw = kopieer_ontbrekend(
        lc, pc, "p2p.locatie_basisgeo", ["locatie_id", "basisgeo_id"],
        "SELECT locatie_id, basisgeo_id FROM p2p.locatie_basisgeo WHERE locatie_id LIKE ANY(%s)",
        (prefixen,),
        extra_voorwaarde="AND EXISTS (SELECT 1 FROM p2p.locatie l WHERE l.identificatie = s.locatie_id)",
        ja=args.ja)
    log(f"  locatie_basisgeo: {n:,} aangeboden, {nieuw:,} {'ingevoegd' if args.ja else 'zou invoegen'} "
        f"(alleen met bestaande locatie op prod)")

    if not args.ja:
        pconn.rollback()
        log("DROOGLOOP — niets geschreven. Draai opnieuw met --ja.")
        return
    pconn.commit()

    # 4. verwijzingen op prod zelf oplossen
    from src.loaders.inline_referentie import resolve_target_soort
    pconn.row_factory = psycopg.rows.dict_row
    totaal: dict[str, int] = {}
    t0 = time.time()
    for i, e in enumerate(expr, 1):
        res = resolve_target_soort(pconn, regeling_expression=e)
        pconn.commit()
        for k, v in res.items():
            totaal[k] = totaal.get(k, 0) + v
        if i % 25 == 0:
            log(f"  resolve {i}/{len(expr)}")
    log(f"  resolve op prod: {totaal} ({time.time() - t0:.0f}s)")
    log("Klaar. Nu op prod: refresh_drieslag.py en refresh_health_mvs.py --target prod.")


if __name__ == "__main__":
    main()
