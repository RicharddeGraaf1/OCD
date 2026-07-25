"""Fysieke prune van verdrongen (verouderde-versie) regelingen uit de OCD-DB.

Verwijdert de tekst + annotaties + embeddings van regelingversies die door een
nieuwere expressie zijn verdrongen (`p2p.regeling.inactief` met
`reden_inactief='verouderde-versie'`). De **vigerende** versie blijft de bron
van waarheid; het `NOT inactief`-retrievalfilter blijft als vangnet staan.

Ontwerp (zie vault: analysis/Opschonen verouderde versies en ontwerpen uit de
OCD-database + gaps G-95; docs/opschoning-verouderde-versies-plan.md):

- **dry-run-default**: zonder --apply telt hij alleen wat hij zóu verwijderen.
- **resumable**: batch-gewijs per expressie, elke batch een eigen transactie;
  een herstart matcht simpelweg 0 rijen voor al-verwijderde expressies.
- **nooit tijdens een load**: weigert als er een `core.load_run` op `running`
  staat (tenzij --force-tijdens-load).
- **prod-veilig**: --target prod / --dsn zet OCD_DB_URL; get_conn() schakelt dan
  parallelisme uit (Railway /dev/shm). Prod vraagt typbevestiging tenzij --yes.
- **zichtbaar**: draait binnen load_run('prune-verouderd') → data-actualiteit-
  dashboard.

Delete-volgorde (FK-analyse 2026-07-25; losse kolommen zonder cascade expliciet):
  1. v2a.tekst_embedding / v2a.chunk / v2a.element_hertaling   (regeling_expression, LOS)
  2. p2p.tekst_object_consistentie                             (tekst_element_id, LOS)
  3. p2p.juridische_regel  -> cascade: activiteit_locatieaanduiding,
                                       juridische_regel_norm, _gebiedsaanwijzing
  4. p2p.geo_informatieobject (NO ACTION vanaf regeling -> vóór regeling;
                               cascade: juridische_borging; SET NULL: tekst_inline_referentie)
  5. p2p.regeling          -> cascade: tekst_element (-> tekst_inline_referentie,
                                       zelf-ref parent_id), besluit_regeling, regeling_load

NIET aangeraakt: gedeelde dimensies activiteit / norm / locatie / pons /
kaartlaag / werkzaamheid / regelbeheerobject (geen regeling_expression; gedeeld
tussen versies). Eventuele wees-dimensies zijn een aparte, lage-prioriteit sweep.

Gebruik:
    python scripts/prune_verouderde_versies.py                 # dry-run lokaal
    python scripts/prune_verouderde_versies.py --apply         # echt, lokaal
    python scripts/prune_verouderde_versies.py --target prod --apply
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Losse BASISTABELLEN (geen cascade-FK) — expliciet, per regeling_expression.
# LET OP: v2a.chunk en v2a.element_hertaling zijn VIEWS (chunk = 1:1 over
# tekst_embedding; element_hertaling hangt aan de content-adresseerbare tabel
# v2a.hertaling op bron_hash, gedeeld tussen versies) -> NIET prunen; ze volgen
# vanzelf of blijven terecht staan. Alleen de echte tabel telt.
LOSSE_EXPRESSIE = [
    ("v2a.tekst_embedding", "regeling_expression"),
]


def _masker(dsn: str) -> str:
    return re.sub(r"://[^:/@]+(:[^@]+)?@", "://***@", dsn)


def kies_doelwit(args):
    dsn = args.dsn
    if not dsn and args.target == "prod":
        from dotenv import dotenv_values
        dsn = (dotenv_values(ROOT / ".env") or {}).get("PROD_DB_URL")
        if not dsn:
            raise SystemExit("PROD_DB_URL ontbreekt in .env.")
    if not dsn:
        print("Doelwit-DB: LOKAAL")
        return False
    dsn = dsn.strip().strip('"').strip("'")
    os.environ["OCD_DB_URL"] = dsn
    prod = ("rlwy.net" in dsn) or ("railway" in dsn) or (args.target == "prod")
    print(f"Doelwit-DB: {'PROD' if prod else 'EXPLICIET'} -> {_masker(dsn)}")
    if prod and args.apply and not args.yes:
        try:
            if input("\n⚠  DIRECT tegen PRODUCTIE prunen. Typ 'PROD': ").strip() != "PROD":
                raise SystemExit("Afgebroken.")
        except EOFError:
            raise SystemExit("Non-interactief zonder --yes; afgebroken.")
    return prod


def q1(cur, sql, params=None):
    cur.execute(sql, params or ())
    r = cur.fetchone()
    return (list(r.values())[0] if r else None)


def _is_table(cur, tbl):
    """True alleen voor een bestaande BASISTABEL (relkind 'r'); False voor
    views/matviews/ontbrekend. Voorkomt 'cannot delete from view'."""
    sch, _, naam = tbl.partition(".")
    return q1(cur, """SELECT c.relkind='r'
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relname=%s""", (sch, naam)) is True


def bepaal_set(cur, reden, limit):
    sql = ("SELECT frbr_expression FROM p2p.regeling "
           "WHERE inactief AND reden_inactief = %s ORDER BY frbr_expression")
    params = [reden]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    cur.execute(sql, params)
    return [r["frbr_expression"] for r in cur.fetchall()]


def tel(cur, tbl, kol, exprs):
    if q1(cur, "SELECT to_regclass(%s)", (tbl,)) is None:
        return None
    return q1(cur, f"SELECT count(*) n FROM {tbl} WHERE {kol} = ANY(%s)", (exprs,))  # noqa: S608


def dry_run(cur, exprs):
    print(f"\n=== DRY-RUN — zou verwijderen voor {len(exprs)} verdrongen expressies ===")
    print("  [expliciete DELETEs — losse/NO-ACTION basistabellen]")
    totaal = {}
    for tbl, kol in LOSSE_EXPRESSIE:
        totaal[tbl] = tel(cur, tbl, kol, exprs)
    for tbl, kol in [("p2p.juridische_regel", "regeling_expression"),
                     ("p2p.geo_informatieobject", "regeling_expression"),
                     ("p2p.regeling", "frbr_expression")]:
        totaal[tbl] = tel(cur, tbl, kol, exprs)
    for tbl, n in totaal.items():
        print(f"    {tbl:34} {('n.v.t.' if n is None else format(n, ',')):>12}")
    print("  [via cascade — meegeteld, niet apart verwijderd]")
    print(f"    {'p2p.tekst_element':34} {tel(cur, 'p2p.tekst_element', 'regeling_expression', exprs):>12,}"
          "   (regeling-cascade -> +tekst_inline_referentie, besluit_regeling)")
    print("  (v2a.chunk/element_hertaling = views; tekst_object_consistentie = view/MV;\n"
          "   activiteit/norm/locatie/hertaling = gedeeld -> blijven terecht staan)")
    return totaal


def prune_batch(cur, batch):
    """Verwijder één batch expressies in de juiste volgorde. Retourneert dict n."""
    n = {}
    for tbl, kol in LOSSE_EXPRESSIE:
        if not _is_table(cur, tbl):
            continue
        cur.execute(f"DELETE FROM {tbl} WHERE {kol} = ANY(%s)", (batch,))  # noqa: S608
        n[tbl] = cur.rowcount
    if _is_table(cur, "p2p.tekst_object_consistentie"):
        cur.execute("""DELETE FROM p2p.tekst_object_consistentie
            WHERE tekst_element_id IN
              (SELECT id FROM p2p.tekst_element WHERE regeling_expression = ANY(%s))""", (batch,))
        n["p2p.tekst_object_consistentie"] = cur.rowcount
    cur.execute("DELETE FROM p2p.juridische_regel WHERE regeling_expression = ANY(%s)", (batch,))
    n["p2p.juridische_regel"] = cur.rowcount
    cur.execute("DELETE FROM p2p.geo_informatieobject WHERE regeling_expression = ANY(%s)", (batch,))
    n["p2p.geo_informatieobject"] = cur.rowcount
    cur.execute("DELETE FROM p2p.regeling WHERE frbr_expression = ANY(%s)", (batch,))
    n["p2p.regeling"] = cur.rowcount  # cascade ruimt tekst_element etc.
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="echt verwijderen (default = dry-run)")
    ap.add_argument("--reden", default="verouderde-versie",
                    help="reden_inactief die geprund mag worden (default verouderde-versie; "
                         "'ingetrokken' NIET default — dat is herstelbaar markeren)")
    ap.add_argument("--batch", type=int, default=25, help="expressies per transactie")
    ap.add_argument("--limit", type=int, default=None, help="max aantal expressies (voor test)")
    ap.add_argument("--target", choices=["local", "prod"], default="local")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--force-tijdens-load", action="store_true")
    args = ap.parse_args()

    kies_doelwit(args)
    from src.db import get_conn

    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    lopend = q1(cur, "SELECT count(*) n FROM core.load_run WHERE status='running'")
    if lopend and not args.force_tijdens_load:
        raise SystemExit(f"Er loopt een load ({lopend} running load_run(s)); prune afgebroken. "
                         "Wacht tot de sync klaar is of gebruik --force-tijdens-load.")

    exprs = bepaal_set(cur, args.reden, args.limit)
    if not exprs:
        print(f"Geen expressies met reden_inactief='{args.reden}'. Niets te doen.")
        return
    totaal = dry_run(cur, exprs)

    if not args.apply:
        print("\nDRY-RUN — niets verwijderd. Voeg --apply toe om echt te prunen.")
        return

    from src.run_log import load_run
    print(f"\n=== APPLY — {len(exprs)} expressies in batches van {args.batch} ===")
    som = {}
    with load_run("prune-verouderd", scope=f"reden={args.reden}, {len(exprs)} expressies") as run:
        verwerkt = 0
        for i in range(0, len(exprs), args.batch):
            batch = exprs[i:i + args.batch]
            n = prune_batch(cur, batch)
            conn.commit()
            for k, v in n.items():
                som[k] = som.get(k, 0) + v
            verwerkt += len(batch)
            print(f"  batch {i // args.batch + 1}: {verwerkt}/{len(exprs)} expressies verwerkt", flush=True)
        run.set(n_verwerkt=verwerkt, n_fout=0)

    print("\n=== Verwijderd (totaal) ===")
    for k, v in som.items():
        print(f"  {k:36} {v:>12,}")
    # onderhoud: statistieken bijwerken zodat de planner niet op oude counts draait
    conn.autocommit = True
    for tbl in ("p2p.regeling", "p2p.tekst_element", "p2p.juridische_regel",
                "v2a.tekst_embedding"):
        try:
            cur.execute(f"ANALYZE {tbl}")  # noqa: S608
        except Exception:
            pass
    print("\nKlaar. Retrieval-filter (NOT inactief) blijft als vangnet staan. "
          "Overweeg los een REINDEX van de HNSW-vectorindex bij grote prunes.")
    conn.close()


if __name__ == "__main__":
    main()
