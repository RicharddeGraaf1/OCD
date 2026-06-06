"""Productie-backfill voor de drieslag tekst↔object over alle regelingen.

Per Ow-regeling in de DB doorlopen we de volledige optie-C-light-pipeline:
  1. Stap-1: parse `tekst_element.inhoud` → `tekst_inline_referentie`
  2. Optie A: vul `geo_informatieobject` vanuit ExtIoRef.target_ref
  3. Download ZIP via Download API (gecached)
  4. Process ZIP: vul `locatie_basisgeo` + `gio_basisgeo` + resolve_target_soort
  5. Locatie-refresh via Presenteren: vul `locatie.geometrie_identificatie`

Idempotent: skip-logic checkt of een regeling al rijen in
`tekst_inline_referentie` heeft. Met --force overslaan.

Na alle regelingen: handmatig draaien:
  - REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.naammatch_signaal_intra
  - REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.tekst_object_consistentie_mv
  - python scripts/2026-05-add-niet-annoteerbaar.sql  (om nieuwe rijen te markeren)

Run:
  python scripts/backfill_alle_regelingen.py [--limit N] [--force] [--start-from FRBR]
"""
import argparse
import os
import sys
import time
import traceback

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ".")

from src.config import cfg
from src.db import get_conn
from src.loaders.api_loader import (
    _get,
    _encode_regeling_uri,
    load_regeltekstannotaties,
    load_divisieannotaties,
)
from src.loaders.gio_zip import process_zip
from src.loaders.inline_referentie import (
    extract_inline_referenties,
    insert_inline_referenties,
    resolve_target_soort,
)
from src.loaders.ow_loader import _download_regeling


def is_already_processed(conn, regeling_expression: str) -> bool:
    """Heuristiek: regeling is al verwerkt als er tekst_inline_referentie-rijen
    voor zijn (stap-1 is gedraaid)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM p2p.tekst_inline_referentie tir
               JOIN p2p.tekst_element te ON te.id = tir.tekst_element_id
               WHERE te.regeling_expression = %s LIMIT 1""",
            (regeling_expression,),
        )
        return cur.fetchone() is not None


def step1_inline_referenties(conn, regeling_expression: str) -> int:
    """Parse alle tekst_element.inhoud en INSERT in tekst_inline_referentie."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, inhoud FROM p2p.tekst_element "
            "WHERE regeling_expression = %s AND inhoud IS NOT NULL",
            (regeling_expression,),
        )
        rows = cur.fetchall()
    inserted = 0
    for r in rows:
        refs = extract_inline_referenties(r["inhoud"])
        if refs:
            inserted += insert_inline_referenties(conn, r["id"], refs)
    return inserted


def step2_optie_a(conn, regeling_expression: str) -> int:
    """Vul p2p.geo_informatieobject vanuit ExtIoRef.target_ref."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO p2p.geo_informatieobject (frbr_expression, frbr_work, regeling_expression)
               SELECT DISTINCT tir.target_ref, split_part(tir.target_ref, '@', 1), te.regeling_expression
               FROM p2p.tekst_inline_referentie tir
               JOIN p2p.tekst_element te ON te.id = tir.tekst_element_id
               WHERE tir.soort = 'ExtIoRef'
                 AND starts_with(tir.target_ref, '/join/id/regdata/')
                 AND te.regeling_expression = %s
               ON CONFLICT (frbr_expression) DO NOTHING""",
            (regeling_expression,),
        )
        return cur.rowcount


def step5_locatie_refresh(conn, regeling_uri: str, is_vrijetekst: bool) -> int:
    """UPDATE p2p.locatie.geometrie_identificatie via Presenteren-payload.
    Doet alleen UPDATEs, geen INSERTs of geometrie-fetches."""
    encoded = _encode_regeling_uri(regeling_uri)
    endpoint = "divisieannotaties" if is_vrijetekst else "regeltekstannotaties"
    try:
        data = _get(
            f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}/{endpoint}",
            params={"locatieSelectie": "primair"},
        )
    except Exception:
        return 0
    updated = 0
    with conn.cursor() as cur:
        for loc in data.get("locaties", []):
            geom_id = loc.get("geometrieIdentificatie")
            if not geom_id:
                continue
            cur.execute(
                "UPDATE p2p.locatie SET geometrie_identificatie = %s "
                "WHERE identificatie = %s AND geometrie_identificatie IS NULL",
                (geom_id, loc["identificatie"]),
            )
            updated += cur.rowcount
    return updated


def _safe_step(conn, name: str, fn, counts: dict, *args, **kwargs):
    """Wrap een stap met try/except + rollback. Bij fout: log in counts en
    return None zodat de caller kan beslissen of doorgaan zinvol is."""
    try:
        result = fn(*args, **kwargs)
        conn.commit()
        return result
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        counts.setdefault("errors", []).append(f"{name}: {type(e).__name__}: {str(e)[:150]}")
        return None


def process_regeling(conn, reg: dict, force: bool = False) -> dict:
    """Volledige pipeline voor één regeling. Returns counts dict."""
    expr = reg["frbr_expression"]
    work = reg["frbr_work"]

    # Skip-check ook in safe-wrapper, want eerste call op aborted conn faalt.
    counts = {"status": "ok"}
    already = _safe_step(conn, "skip_check", is_already_processed, counts, conn, expr)
    if already is True and not force:
        return {"status": "skipped"}
    if already is None:
        # Skip-check zelf is gefaald — log en ga door (override met force-achtig gedrag)
        pass

    t0 = time.time()

    counts["step1_inline_refs"] = _safe_step(
        conn, "step1", step1_inline_referenties, counts, conn, expr
    ) or 0

    counts["step2_gios"] = _safe_step(
        conn, "step2_optie_a", step2_optie_a, counts, conn, expr
    ) or 0

    _safe_step(conn, "resolve_pre_zip", resolve_target_soort, counts,
               conn, regeling_expression=expr)

    # Stap 3-4: ZIP downloaden + process
    zip_path = None
    try:
        zip_path = _download_regeling(work)
    except Exception as e:
        counts.setdefault("errors", []).append(f"zip_download: {type(e).__name__}: {str(e)[:150]}")
    if not zip_path:
        counts["status"] = "zip_unavailable" if not counts.get("errors") else "zip_download_failed"
        return counts

    zip_counts = _safe_step(conn, "zip_process", process_zip, counts, zip_path, conn)
    if zip_counts:
        counts.update({f"zip_{k}": v for k, v in zip_counts.items()})

    # Stap 5: locatie-refresh
    is_vrijetekst = reg.get("type", "") in (
        "Omgevingsvisie", "Programma", "Instructie", "Natura 2000-besluit",
    )
    counts["step5_loc_updates"] = _safe_step(
        conn, "step5_locatie", step5_locatie_refresh, counts, conn, work, is_vrijetekst
    ) or 0

    _safe_step(conn, "resolve_post_zip", resolve_target_soort, counts,
               conn, regeling_expression=expr)

    if counts.get("errors"):
        counts["status"] = "partial"
    counts["elapsed_s"] = round(time.time() - t0, 1)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Max aantal regelingen (test-mode)")
    parser.add_argument("--force", action="store_true",
                        help="Negeer skip-logic, herverwerk alles")
    parser.add_argument("--start-from", default=None,
                        help="Start vanaf deze frbr_expression (resume)")
    args = parser.parse_args()

    conn = get_conn()
    cur = conn.cursor()
    where = ""
    params = ()
    if args.start_from:
        where = "WHERE frbr_expression >= %s"
        params = (args.start_from,)
    cur.execute(
        f"SELECT frbr_expression, frbr_work, "
        f"  (SELECT documenttype FROM p2p.regeling r2 WHERE r2.frbr_expression = r.frbr_expression) AS type "
        f"FROM p2p.regeling r {where} ORDER BY frbr_expression"
        + (f" LIMIT {args.limit}" if args.limit else ""),
        params,
    )
    regelingen = cur.fetchall()
    print(f"Te verwerken: {len(regelingen):,} regelingen", flush=True)
    print(f"Force: {args.force}, Start-from: {args.start_from}", flush=True)
    print()

    stats = {"ok": 0, "skipped": 0, "failed": 0, "total_inline": 0, "total_loc": 0, "total_gio": 0}
    t_total = time.time()
    for i, reg in enumerate(regelingen, 1):
        try:
            result = process_regeling(conn, reg, force=args.force)
        except Exception as e:
            result = {"status": "exception", "error": str(e)[:200]}
            traceback.print_exc()
            try:
                conn.rollback()
            except Exception:
                pass

        status = result.get("status", "?")
        if status in ("ok", "partial"):
            stats["ok" if status == "ok" else "failed"] += 1
            stats["total_inline"] += result.get("step1_inline_refs", 0) or 0
            stats["total_loc"] += result.get("zip_locatie_rows_inserted", 0) or 0
            stats["total_gio"] += result.get("zip_gio_rows_inserted", 0) or 0
            err_tag = " [partial]" if status == "partial" else ""
            err_str = ""
            if result.get("errors"):
                err_str = f"  {' | '.join(result['errors'][:2])[:140]}"
            msg = (f"  inline={result.get('step1_inline_refs',0) or 0:>5} "
                   f"loc={result.get('zip_locatie_rows_inserted',0) or 0:>5} "
                   f"gio={result.get('zip_gio_rows_inserted',0) or 0:>5} "
                   f"({result.get('elapsed_s',0):.1f}s){err_tag}{err_str}")
        elif status == "skipped":
            stats["skipped"] += 1
            msg = "  [skip]"
        else:
            stats["failed"] += 1
            err_str = ""
            if result.get("errors"):
                err_str = f" | {result['errors'][0][:140]}"
            msg = f"  FAILED: {status}{err_str}"

        elapsed = time.time() - t_total
        eta = (elapsed / i) * (len(regelingen) - i) if i > 0 else 0
        print(f"[{i:>5}/{len(regelingen)}] {reg['frbr_expression'][:80]}{msg} "
              f"(elapsed {elapsed/60:.1f}m, eta {eta/60:.1f}m)", flush=True)

    print()
    print(f"Klaar in {(time.time()-t_total)/60:.1f} min.")
    print(f"  OK:      {stats['ok']:,}")
    print(f"  Skipped: {stats['skipped']:,}")
    print(f"  Failed:  {stats['failed']:,}")
    print(f"  Inline-refs ingevoegd: {stats['total_inline']:,}")
    print(f"  Locatie-basisgeo:      {stats['total_loc']:,}")
    print(f"  GIO-basisgeo:          {stats['total_gio']:,}")
    print()
    print("Vergeet niet na de backfill:")
    print("  python scripts/2026-05-add-niet-annoteerbaar.sql  (recursive UPDATE op nieuwe rijen)")
    print("  REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.naammatch_signaal")
    print("  REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.naammatch_signaal_intra")
    print("  REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.tekst_object_consistentie_mv")
    conn.close()


if __name__ == "__main__":
    main()
