"""Retry alleen de GIO-stap voor regelingen die `gio_basisgeo` missen.

Tijdens de full backfill (2026-05-12) faalden 465 van 1.894 regelingen
op een FK-violation in `gio_basisgeo` — de FRBR van de GIO-GML in de
ZIP zat niet in `p2p.geo_informatieobject` (versie-mismatch). Inline-
referenties en locatie_basisgeo zijn al wel gevuld voor die regelingen;
alleen gio_basisgeo werd gerollbackt.

Fix:
1. FK gedropt op `gio_basisgeo.gio_frbr`
2. `gio_zip.process_zip` vult nu ook ontbrekende FRBRs in
   `p2p.geo_informatieobject`
3. Dit script: vind regelingen waar gio_basisgeo ontbreekt EN ZIP
   beschikbaar is (cached), en run alleen process_zip.

Run: python scripts/retry_failed_gios.py
"""
import os
import sys
import time

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ".")

from pathlib import Path
from src.config import cfg
from src.db import get_conn
from src.loaders.gio_zip import process_zip
from src.loaders.ow_loader import _download_regeling


def find_failed_regelingen(conn) -> list[dict]:
    """Regelingen met inline-refs maar geen gio_basisgeo-koppeling."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT r.frbr_expression, r.frbr_work
            FROM p2p.regeling r
            WHERE EXISTS (
                SELECT 1 FROM p2p.tekst_inline_referentie tir
                JOIN p2p.tekst_element te ON te.id = tir.tekst_element_id
                WHERE te.regeling_expression = r.frbr_expression
            )
            AND NOT EXISTS (
                SELECT 1 FROM p2p.gio_basisgeo gb
                JOIN p2p.geo_informatieobject gio
                  ON gio.frbr_expression = gb.gio_frbr
                WHERE gio.regeling_expression = r.frbr_expression
            )
            ORDER BY r.frbr_expression
        """)
        return cur.fetchall()


def main():
    conn = get_conn()
    regelingen = find_failed_regelingen(conn)
    print(f"Te retryen: {len(regelingen):,} regelingen", flush=True)

    stats = {"ok": 0, "no_zip": 0, "failed": 0,
             "new_gios": 0, "loc_inserted": 0, "gio_inserted": 0}
    t_total = time.time()
    for i, reg in enumerate(regelingen, 1):
        work = reg["frbr_work"]
        try:
            zip_path = _download_regeling(work)
        except Exception as e:
            stats["failed"] += 1
            print(f"[{i:>5}/{len(regelingen)}] {work[:70]} download_err: {str(e)[:80]}",
                  flush=True)
            continue
        if not zip_path:
            stats["no_zip"] += 1
            continue
        try:
            counts = process_zip(zip_path, conn, regeling_expression=reg["frbr_expression"])
            stats["ok"] += 1
            stats["new_gios"] += counts.get("new_gios_inserted", 0)
            stats["loc_inserted"] += counts.get("locatie_rows_inserted", 0)
            stats["gio_inserted"] += counts.get("gio_rows_inserted", 0)
            elapsed = time.time() - t_total
            eta = (elapsed / i) * (len(regelingen) - i) if i > 0 else 0
            print(f"[{i:>5}/{len(regelingen)}] {reg['frbr_expression'][:70]} "
                  f"new_gios={counts.get('new_gios_inserted',0):>4} "
                  f"gio_basisgeo={counts.get('gio_rows_inserted',0):>5} "
                  f"(elapsed {elapsed/60:.1f}m, eta {eta/60:.1f}m)",
                  flush=True)
        except Exception as e:
            stats["failed"] += 1
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[{i:>5}/{len(regelingen)}] {reg['frbr_expression'][:70]} "
                  f"FAILED: {type(e).__name__}: {str(e)[:120]}",
                  flush=True)

    print()
    print(f"Klaar in {(time.time()-t_total)/60:.1f} min.")
    print(f"  OK:                {stats['ok']:,}")
    print(f"  No ZIP:            {stats['no_zip']:,}")
    print(f"  Failed:            {stats['failed']:,}")
    print(f"  New GIOs ingev.:   {stats['new_gios']:,}")
    print(f"  Locatie-basisgeo:  {stats['loc_inserted']:,}")
    print(f"  GIO-basisgeo:      {stats['gio_inserted']:,}")
    conn.close()


if __name__ == "__main__":
    main()
