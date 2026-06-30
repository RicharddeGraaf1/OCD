"""Deploy de RP-planvoorraad-data naar de prod-DB (psycopg, geen psql nodig).

Past de migratie toe (wro-schema + 2 tabellen) en kopieert de lokaal gevalideerde
snapshot 1-op-1 naar prod via COPY. Idempotent: prod-tabellen worden eerst geleegd.

Connectie:
    PROD_DATABASE_URL via env, of scripts/../<scratchpad>/prod_db_url.txt, of als
    eerste CLI-argument. Lokale bron-DB komt uit src.config.cfg.db_url.

Gebruik:
    cd dso-loader && source .venv/Scripts/activate
    PROD_DATABASE_URL="postgresql://...:.../railway" PYTHONPATH=. python scripts/deploy_planvoorraad_to_prod.py
"""

import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from src.config import cfg

MIGRATION = Path(__file__).with_name("2026-06-add-wro-planvoorraad.sql")
TABLES = ["wro.wro_snapshot", "wro.wro_plan_observatie"]  # parent eerst


def _prod_url() -> str:
    if len(sys.argv) > 1 and sys.argv[1].startswith("postgres"):
        return sys.argv[1]
    if os.getenv("PROD_DATABASE_URL"):
        return os.environ["PROD_DATABASE_URL"]
    # fallback: scratchpad-bestand
    sp = Path(os.getenv("SCRATCHPAD_PROD_URL", "")) if os.getenv("SCRATCHPAD_PROD_URL") else None
    for cand in filter(None, [sp]):
        if cand.exists():
            return cand.read_text(encoding="utf-8").strip()
    raise SystemExit("Geen PROD_DATABASE_URL (env, CLI-arg of scratchpad-bestand).")


def _count(cur, table) -> int:
    cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
    return cur.fetchone()["n"]


def main():
    prod_url = _prod_url()
    masked = prod_url.split("@")[-1] if "@" in prod_url else prod_url
    print(f"Deploy planvoorraad -> prod  ({masked})")

    local = psycopg.connect(cfg.db_url, row_factory=dict_row, autocommit=False)
    prod = psycopg.connect(prod_url, row_factory=dict_row, autocommit=False)
    try:
        # 1. migratie
        print("  1. migratie toepassen (schema + tabellen)...")
        with prod.cursor() as pc:
            pc.execute(MIGRATION.read_text(encoding="utf-8"))
        prod.commit()

        # 2. prod legen (idempotent)
        with prod.cursor() as pc:
            pc.execute("TRUNCATE wro.wro_plan_observatie, wro.wro_snapshot RESTART IDENTITY")
        prod.commit()

        # 3. COPY per tabel (text-formaat = versie-portabel)
        for table in TABLES:
            with local.cursor() as lc, prod.cursor() as pc:
                with lc.copy(f"COPY {table} TO STDOUT") as cout, \
                     pc.copy(f"COPY {table} FROM STDIN") as cin:
                    for block in cout:
                        cin.write(block)
            prod.commit()
            with prod.cursor() as pc:
                print(f"  2. {table}: {_count(pc, table)} rijen")

        # 4. identity-sequence bijwerken zodat nieuwe snapshots niet botsen
        with prod.cursor() as pc:
            pc.execute("""SELECT setval(
                pg_get_serial_sequence('wro.wro_snapshot','snapshot_id'),
                GREATEST((SELECT MAX(snapshot_id) FROM wro.wro_snapshot), 1))""")
        prod.commit()

        # 5. verificatie
        with prod.cursor() as pc:
            pc.execute("SELECT snapshot_id, datum, aantal_plannen FROM wro.wro_snapshot ORDER BY datum DESC LIMIT 1")
            snap = pc.fetchone()
            pc.execute("""SELECT
              COUNT(*) FILTER (WHERE NOT is_tam AND lower(planstatus) IN
                ('onherroepelijk','vastgesteld','geconsolideerd') AND verwijderd_op IS NULL) in_voorraad,
              COUNT(*) FILTER (WHERE NOT is_tam AND lower(planstatus) IN
                ('onherroepelijk','vastgesteld','geconsolideerd') AND verwijderd_op IS NOT NULL) weggehaald
              FROM wro.wro_plan_observatie WHERE snapshot_id = %s""", (snap["snapshot_id"],))
            agg = pc.fetchone()
        print(f"KLAAR. prod-snapshot {snap['datum']}: in_voorraad={agg['in_voorraad']} weggehaald={agg['weggehaald']}")
    finally:
        local.close()
        prod.close()


if __name__ == "__main__":
    main()
