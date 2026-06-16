"""Refresh alle materialized views + niet-annoteerbaar-markering voor de
drieslag tekst↔object in de juiste volgorde.

Gebruik: draai na een ingest of backfill die p2p.tekst_element,
p2p.tekst_inline_referentie, p2p.gebiedsaanwijzing, p2p.activiteit,
p2p.norm, p2p.locatie_basisgeo of p2p.gio_basisgeo wijzigt.

Volgorde:
  1. Niet-annoteerbaar markeren (recursive UPDATE op tekst_element)
  2. REFRESH p2p.naammatch_signaal (de duurste — 20-35 min)
  3. REFRESH p2p.naammatch_signaal_intra (~10s; hangt af van #2)
  4. REFRESH p2p.tekst_object_consistentie_mv (~30s; hangt af van #3)

Run: python scripts/refresh_drieslag.py
"""
import os
import sys
import time

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ".")

from src.db import get_conn

STAPPEN = [
    ("Niet-annoteerbaar markeren",
     "scripts/2026-05-add-niet-annoteerbaar.sql",
     "SELECT COUNT(*) FILTER (WHERE is_niet_annoteerbaar) AS n FROM p2p.tekst_element"),
    ("REFRESH p2p.gio_locatie",
     "REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.gio_locatie",
     "SELECT COUNT(*) AS n FROM p2p.gio_locatie"),
    ("REFRESH p2p.naammatch_signaal",
     "REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.naammatch_signaal",
     "SELECT COUNT(*) AS n FROM p2p.naammatch_signaal"),
    ("REFRESH p2p.naammatch_signaal_intra",
     "REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.naammatch_signaal_intra",
     "SELECT COUNT(*) AS n FROM p2p.naammatch_signaal_intra"),
    ("REFRESH p2p.tekst_object_consistentie_mv",
     "REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.tekst_object_consistentie_mv",
     "SELECT COUNT(*) AS n FROM p2p.tekst_object_consistentie_mv"),
]


def main():
    conn = get_conn()
    cur = conn.cursor()
    t_total = time.time()

    for i, (naam, sql_or_path, count_query) in enumerate(STAPPEN, 1):
        print(f"[{i}/{len(STAPPEN)}] {naam}...", flush=True)
        t0 = time.time()

        # Stap 1 is een file; rest zijn inline SQL.
        if sql_or_path.endswith(".sql"):
            with open(sql_or_path, encoding="utf-8") as f:
                cur.execute(f.read())
        else:
            cur.execute(sql_or_path)
        conn.commit()

        elapsed = time.time() - t0
        cur.execute(count_query)
        n = cur.fetchone()["n"]
        print(f"  Klaar in {elapsed/60:.1f} min — {n:,} rijen", flush=True)

    print(f"\nTotaal: {(time.time()-t_total)/60:.1f} min", flush=True)
    print("\n=== Verdeling klassen ===", flush=True)
    cur.execute(
        "SELECT consistentie_klasse, COUNT(*) AS n "
        "FROM p2p.tekst_object_consistentie_mv "
        "GROUP BY consistentie_klasse ORDER BY n DESC"
    )
    for r in cur.fetchall():
        print(f"  {r['consistentie_klasse']:35} {r['n']:>9,}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
