"""Migratie: p2p.naammatch_signaal intra-regeling scopen.

WAT & WAAROM
------------
De naam-match-controle (drieslag type 3: "de tekst noemt een objectnaam, maar
het object is hier niet geannoteerd") vergelijkt in de database **elke tekst in
Nederland met elke objectnaam in Nederland**: 6.324.956 treffers. De volgende
laag (`naammatch_signaal_intra`) gooit daar 99,3% van weg en houdt alleen de
treffers binnen dezelfde regeling over: 43.045.

Die kruisvergelijking tussen gemeenten is inhoudelijk zinloos — dat Amsterdam
een woord gebruikt dat in Groningen een object is, zegt niets over Amsterdams
annotatiekwaliteit. Ze kost wel ruim een uur per sync, en op productie haalde
de refresh de drie-uurs-timeout niet (2026-08-01).

De intra-gescopete definitie stáát al in `2026-05-add-naammatch-signaal.sql`
(gebruiker-keuze 2026-05-08) maar is nooit toegepast; de database draait nog v1.
Gemeten 2026-08-01: de gescopete definitie levert **exact dezelfde 43.045 rijen**
(0 verschil in beide richtingen) in 3,2 minuten i.p.v. ~68.

De landelijke kruisvergelijking gaat niet verloren maar wordt on-demand:
`scripts/analyse-naammatch-cross-regeling.sql`.

HOE
---
`naammatch_signaal` vervangen vereist DROP CASCADE, en dat sloopt de hele keten:

    naammatch_signaal → naammatch_signaal_intra → tekst_object_consistentie
                     → tekst_object_consistentie_mv

De drie afhankelijken worden daarom **uit hun huidige definitie in de database**
herbouwd (pg_get_viewdef + pg_indexes + comments), niet uit scriptbestanden —
die bleken al eerder van de werkelijkheid af te wijken, en dat is precies hoe
deze situatie is ontstaan. Zo is de basis het enige dat verandert.

    python scripts/2026-08-naammatch-intra-scoping.py              # dry-run
    python scripts/2026-08-naammatch-intra-scoping.py --apply
    python scripts/2026-08-naammatch-intra-scoping.py --apply --target prod

ACCEPTATIETEST: `tekst_object_consistentie_mv` en de verdeling over
consistentie-klassen moeten ONVERANDERD zijn. Verandert daar iets, dan is de
migratie geen optimalisatie maar een gedragswijziging — dan terugdraaien.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

KETEN = ["naammatch_signaal_intra", "tekst_object_consistentie",
         "tekst_object_consistentie_mv"]


def _quote(tekst: str) -> str:
    """SQL-stringliteral. COMMENT ON accepteert geen bind-parameters."""
    return "'" + tekst.replace("'", "''") + "'"


def scoped_definitie() -> str:
    """De intra-gescopete SELECT uit het canonieke SQL-bestand.

    Bewust uit het bestand gelezen en niet overgetypt: bij het overtypen ging
    de escaping van haakjes mis, waardoor namen als '48 dB(A) geluidscontour'
    stil wegvielen (70 rijen verschil). Zie de sessie-notitie van 2026-08-01.
    """
    pad = ROOT / "scripts" / "2026-05-add-naammatch-signaal.sql"
    sql = pad.read_text(encoding="utf-8")
    start = sql.index("CREATE MATERIALIZED VIEW p2p.naammatch_signaal AS")
    start += len("CREATE MATERIALIZED VIEW p2p.naammatch_signaal AS")
    einde = sql.index("-- UNIQUE index is verplicht")
    body = sql[start:einde].strip().rstrip(";")
    if "regeling_expression = nk.regeling_expression" not in body:
        raise SystemExit("definitie in het bestand is niet intra-gescopet — gestopt")
    return body


def leg_vast(cur) -> dict:
    """Huidige definities, indexen en comments van de afhankelijke keten."""
    vast = {}
    for naam in KETEN:
        cur.connection.rollback()  # schone transactie, ook na een eerdere misser
        cur.execute("SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='p2p' AND c.relname=%s", (naam,))
        rij = cur.fetchone()
        if not rij:
            continue
        soort = rij["relkind"]
        cur.execute("SELECT pg_get_viewdef(%s::regclass, true) d", (f"p2p.{naam}",))
        definitie = cur.fetchone()["d"].strip().rstrip(";")
        cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='p2p' AND tablename=%s", (naam,))
        indexen = [r["indexdef"] for r in cur.fetchall()]
        cur.execute("SELECT obj_description(%s::regclass) c", (f"p2p.{naam}",))
        commentaar = cur.fetchone()["c"]
        vast[naam] = {"soort": soort, "definitie": definitie,
                      "indexen": indexen, "commentaar": commentaar}
    return vast


def tellingen(cur) -> dict:
    """Tellingen; ontbrekende objecten geven None.

    Let op de rollback: een mislukte query laat de transactie in aborted-state
    achter, waarna élk volgend statement faalt met "current transaction is
    aborted". Zonder die rollback strandde de migratie hier (2026-08-01).
    """
    uit = {}
    for t in ["naammatch_signaal"] + KETEN:
        try:
            cur.execute(f"SELECT count(*) n FROM p2p.{t}")
            uit[t] = cur.fetchone()["n"]
        except Exception:
            cur.connection.rollback()
            uit[t] = None
    try:
        cur.execute("SELECT consistentie_klasse, count(*) n "
                    "FROM p2p.tekst_object_consistentie_mv GROUP BY 1 ORDER BY 1")
        uit["klassen"] = {r["consistentie_klasse"]: r["n"] for r in cur.fetchall()}
    except Exception:
        cur.connection.rollback()
        uit["klassen"] = None
    return uit


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="daadwerkelijk uitvoeren (default = dry-run)")
    ap.add_argument("--target", choices=["local", "prod"], default="local")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--yes", action="store_true", help="sla de prod-bevestiging over")
    ap.add_argument("--definities-uit-prod", action="store_true",
                    help="haal de definities van de afhankelijke keten uit de PROD-database "
                         "i.p.v. uit het doelwit. Nodig als het doelwit die objecten niet "
                         "(meer) heeft — bijvoorbeeld na een half gestrande migratie.")
    args = ap.parse_args()

    dsn = args.dsn
    if not dsn and args.target == "prod":
        from dotenv import dotenv_values
        dsn = (dotenv_values(ROOT / ".env") or {}).get("PROD_DB_URL")
        if not dsn:
            raise SystemExit("PROD_DB_URL ontbreekt in .env")
    if dsn:
        os.environ["OCD_DB_URL"] = dsn.strip().strip('"').strip("'")

    from src.db import get_conn

    doel = "PRODUCTIE" if args.target == "prod" else "lokaal"
    print(f"Migratie naammatch-intra-scoping — doelwit: {doel} · "
          f"{'UITVOEREN' if args.apply else 'DRY-RUN'}\n")

    if args.apply and args.target == "prod" and not args.yes:
        if input("Typ exact 'PROD' om door te gaan: ").strip() != "PROD":
            raise SystemExit("Afgebroken.")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SET max_parallel_workers_per_gather = 0")
    cur.execute("SET max_parallel_maintenance_workers = 0")
    conn.commit()

    # Nulmeting op schijf, per doelwit. Zonder dit is de acceptatietest waardeloos
    # zodra je opnieuw draait na een half gestrande poging: de keten is dan al
    # weg, "voor" is overal None, en élke vergelijking meldt een afwijking
    # (2026-08-01 precies zo misgegaan).
    baseline_pad = ROOT / "scripts" / f"naammatch-baseline-{args.target}.json"
    voor = tellingen(cur)
    if voor.get("naammatch_signaal") is not None:
        baseline_pad.write_text(json.dumps(voor, indent=1), encoding="utf-8")
        print(f"Nulmeting weggeschreven → {baseline_pad.name}")
    elif baseline_pad.exists():
        voor = json.loads(baseline_pad.read_text(encoding="utf-8"))
        print(f"Keten ontbreekt in de database; nulmeting geladen uit {baseline_pad.name}")
    print("VOOR:")
    for k, v in voor.items():
        print(f"  {k}: {v}")

    if args.definities_uit_prod:
        import psycopg
        from dotenv import dotenv_values
        from psycopg.rows import dict_row
        ref = (dotenv_values(ROOT / ".env") or {}).get("PROD_DB_URL")
        if not ref:
            raise SystemExit("PROD_DB_URL ontbreekt in .env")
        print("\nDefinities ophalen uit de PROD-database (alleen lezen)...")
        with psycopg.connect(ref.strip().strip('"').strip("'"),
                             row_factory=dict_row, connect_timeout=90) as refconn:
            vastgelegd = leg_vast(refconn.cursor())
    else:
        vastgelegd = leg_vast(cur)
    if not vastgelegd:
        raise SystemExit("Geen definities van de afhankelijke keten gevonden — "
                         "gebruik --definities-uit-prod.")
    print(f"\nAfhankelijke objecten vastgelegd: {', '.join(vastgelegd)}")
    for naam, d in vastgelegd.items():
        print(f"  p2p.{naam}: {len(d['definitie'])} tekens definitie, {len(d['indexen'])} indexen")

    body = scoped_definitie()
    print(f"\nNieuwe basisdefinitie: {len(body)} tekens (intra-gescopet, uit het canonieke bestand)")

    if not args.apply:
        print("\nDRY-RUN — niets uitgevoerd. Draai met --apply.")
        conn.close()
        return

    t0 = time.time()
    # ALLES IN ÉÉN TRANSACTIE. DDL is transactioneel in Postgres, dus als de
    # opbouw halverwege faalt rolt ook de DROP terug. Op 2026-08-01 ging dat mis:
    # de DROP was apart gecommit, de opbouw faalde op een COMMENT-statement, en
    # de lokale database bleef achter zonder de hele keten.
    print("\n[1/4] DROP CASCADE op p2p.naammatch_signaal (in transactie)...")
    cur.execute("DROP MATERIALIZED VIEW IF EXISTS p2p.naammatch_signaal CASCADE")

    print("[2/4] Nieuwe basis opbouwen (verwacht ~3 min lokaal)...")
    t = time.time()
    cur.execute("CREATE MATERIALIZED VIEW p2p.naammatch_signaal AS " + body)
    cur.execute("CREATE UNIQUE INDEX naammatch_signaal_pk ON p2p.naammatch_signaal "
                "(tekst_element_id, object_type, object_id)")
    cur.execute("CREATE INDEX naammatch_signaal_object ON p2p.naammatch_signaal (object_type, object_id)")
    cur.execute("CREATE INDEX naammatch_signaal_te ON p2p.naammatch_signaal (tekst_element_id)")
    cur.execute("CREATE INDEX naammatch_signaal_regeling ON p2p.naammatch_signaal (regeling_expression)")
    # COMMENT ON accepteert geen parameters — de tekst moet inline, veilig gequote.
    cur.execute("COMMENT ON MATERIALIZED VIEW p2p.naammatch_signaal IS " + _quote(
        "Naam-overeenkomsten tussen object-namen en tekst_element.inhoud_plain "
        "BINNEN DEZELFDE REGELING (intra-gescopet sinds 2026-08-01; daarvoor "
        "landelijk gekruist = 6,3M rijen waarvan 99,3% direct werd weggefilterd). "
        "Landelijke kruisvergelijking on-demand: "
        "scripts/analyse-naammatch-cross-regeling.sql"))
    print(f"      klaar in {(time.time()-t)/60:.1f} min")

    # 3. Afhankelijken exact terugzetten zoals ze waren.
    print("[3/4] Afhankelijke keten herbouwen uit de vastgelegde definities...")
    for naam in KETEN:
        d = vastgelegd.get(naam)
        if not d:
            print(f"      p2p.{naam}: GEEN definitie vastgelegd — overgeslagen")
            continue
        t = time.time()
        soort = "MATERIALIZED VIEW" if d["soort"] == "m" else "VIEW"
        cur.execute(f"CREATE {soort} p2p.{naam} AS {d['definitie']}")
        for idx in d["indexen"]:
            cur.execute(idx)
        if d["commentaar"]:
            cur.execute(f"COMMENT ON {soort} p2p.{naam} IS " + _quote(d["commentaar"]))
        print(f"      p2p.{naam} ({soort.lower()}) in {(time.time()-t)/60:.1f} min")

    conn.commit()  # één commit: alles staat, of niets.

    # 4. Controle.
    print("[4/4] Controle...")
    na = tellingen(cur)
    print("\nNA:")
    for k, v in na.items():
        print(f"  {k}: {v}")

    print(f"\nTotaal: {(time.time()-t0)/60:.1f} min")
    ok = True
    if voor.get("naammatch_signaal") and na.get("naammatch_signaal"):
        krimp = 100 - 100 * na["naammatch_signaal"] / voor["naammatch_signaal"]
        print(f"  basis: {voor['naammatch_signaal']:,} -> {na['naammatch_signaal']:,} "
              f"({krimp:.1f}% minder rijen — dit hoort zo)")
    if na.get("naammatch_signaal_intra") != voor.get("naammatch_signaal_intra"):
        print("!! intra-telling gewijzigd — ONVERWACHT"); ok = False
    if na.get("tekst_object_consistentie_mv") != voor.get("tekst_object_consistentie_mv"):
        print("!! consistentie-telling gewijzigd — ONVERWACHT"); ok = False
    if na.get("klassen") != voor.get("klassen"):
        print("!! klasse-verdeling gewijzigd — ONVERWACHT"); ok = False
    print("\nACCEPTATIETEST:", "GESLAAGD — downstream ongewijzigd" if ok else
          "AFWIJKING — zie hieronder")
    if not ok:
        print(
            "\n  LET OP: een afwijking is niet automatisch een fout. De nulmeting is\n"
            "  alleen geldig als de keten daarvóór actueel wás. Op prod (2026-08-01)\n"
            "  was hij dat níét — de refresh haalde daar de timeout nooit — dus telde\n"
            "  de nulmeting verouderde data en week alles 'af' terwijl de migratie\n"
            "  juist de achterstand inhaalde.\n"
            "  De harde toets is dan een kruiscontrole: tel dezelfde views op de\n"
            "  andere database (die dezelfde brondata heeft) en vergelijk. Waren die\n"
            "  gelijk, dan is dit geen gedragswijziging maar een inhaalslag.")
    conn.close()


if __name__ == "__main__":
    main()
