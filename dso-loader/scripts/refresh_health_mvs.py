#!/usr/bin/env python
"""Ververs de health-MV's, of analyseer de vectorlaag — zonder `psql`.

Waarom dit script bestaat
-------------------------
Het runbook schreef hiervoor een kale `psql "$PROD_DB_URL" -c ...` voor, en
`psql` staat in deze omgeving op **geen enkele PATH** — niet in bash, niet in
PowerShell. Dat het toch al maanden werkte komt doordat de andere routes het zelf
oplossen: de PowerShell-scripts gebruiken een expliciet `$PgBin`-pad, en
`instructieregels.nl` draait zijn SQL via `docker exec`. De losse
runbook-commando's zijn dus nooit uitgevoerd zoals ze er staan. Gemeten
2026-09-05; zie sync-2026-09-04-leerpunten.md punt 7.

Twee dingen die niet vergeten mogen worden en die hier vastliggen in plaats van
in een commentaarregel in het runbook:

1. **Parallellisme uit.** De Railway-container heeft een kleine `/dev/shm`; een
   parallelle REFRESH valt om op `could not resize shared memory segment`.
   `get_conn()` regelt dat voor een prod-DSN zelf, maar wie rechtstreeks
   verbindt moet het zetten — en dat is precies wat hier gebeurt.
2. **`ANALYZE v2a.tekst_embedding` hoort erbij.** Autovacuum heeft die tabel nog
   nooit opgepakt (`last_autoanalyze` is over de hele levensduur leeg), en zonder
   verse statistieken koos de planner een plan voor een lege tabel: `tier1_screen`
   deed >30 s/regel tegen 2,36 s ná ANALYZE. Zie vault G-133.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import psycopg  # noqa: E402

HEALTH_MVS = [
    "core.mv_bronhouder_health",
    "core.mv_geo_health",
    "v2a.ponsenkaart_gemeente_stats",
]


def lokale_dsn() -> str:
    return (f"host={os.getenv('DB_HOST')} port={os.getenv('DB_PORT')} "
            f"dbname={os.getenv('DB_NAME')} user={os.getenv('DB_USER')} "
            f"password={os.getenv('DB_PASSWORD')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["local", "prod"], default="local")
    ap.add_argument("--alleen-analyze", action="store_true",
                    help="alleen ANALYZE v2a.tekst_embedding, geen MV's")
    a = ap.parse_args()

    dsn = os.getenv("PROD_DB_URL") if a.target == "prod" else lokale_dsn()
    if a.target == "prod" and not dsn:
        print("PROD_DB_URL ontbreekt in .env", file=sys.stderr)
        return 2

    werk = ([] if a.alleen_analyze else HEALTH_MVS)
    mislukt = []
    with psycopg.connect(dsn, connect_timeout=20) as conn:
        conn.autocommit = True          # REFRESH/ANALYZE horen niet in een transactie
        with conn.cursor() as cur:
            cur.execute("SET max_parallel_workers_per_gather = 0")
            cur.execute("SET max_parallel_maintenance_workers = 0")
            cur.execute("SET statement_timeout = '90min'")
            for mv in werk:
                t = time.time()
                try:
                    cur.execute(f"REFRESH MATERIALIZED VIEW {mv}")
                    print(f"  {mv}: {time.time() - t:.1f}s", flush=True)
                except Exception as e:
                    print(f"  {mv}: MISLUKT — {str(e)[:120]}", flush=True)
                    mislukt.append(mv)
            if a.alleen_analyze or a.target == "local":
                t = time.time()
                try:
                    cur.execute("ANALYZE v2a.tekst_embedding")
                    print(f"  ANALYZE v2a.tekst_embedding: {time.time() - t:.1f}s")
                except Exception as e:
                    print(f"  ANALYZE: MISLUKT — {str(e)[:120]}")
                    mislukt.append("ANALYZE v2a.tekst_embedding")

    if mislukt:
        print(f"\n{len(mislukt)} stap(pen) mislukt: {', '.join(mislukt)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
