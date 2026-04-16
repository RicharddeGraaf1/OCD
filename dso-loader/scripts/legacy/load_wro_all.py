"""Load Wro for ALL remaining gemeenten in one single pass.

Queries the database for all gemeenten (4-digit bronhouder codes) that
don't yet have Wro data, then runs the full Wro pipeline in one go:
  1. Bestemmingsplangebied (258 MB GML) — one scan
  2. 8 planobject feature types (2.6 GB + smaller) — one scan each
  3. IHR planteksten — one API call per plan

This is much faster than batched loading because each GML file is only
parsed once instead of N times.
"""
import os, time
os.environ["PYTHONIOENCODING"] = "utf-8"

from datetime import datetime
from rich.console import Console
from src.db import get_conn
from src.loaders.wro_pdok import load_wro_plans

console = Console()


def get_remaining_gemeenten() -> dict[str, str]:
    """Find all gemeenten that have Ow but no Wro yet."""
    conn = get_conn()
    with conn.cursor() as cur:
        # All gemeenten bronhouders (4-digit codes)
        cur.execute("""
            SELECT overheidscode, naam
            FROM core.bronhouder
            WHERE length(overheidscode) = 4
              AND overheidscode ~ '^[0-9]+$'
        """)
        all_gem = {r["overheidscode"]: r["naam"] for r in cur.fetchall()}

        # Gemeenten that already have Wro instruments
        cur.execute("""
            SELECT DISTINCT bronhouder
            FROM wro.ruimtelijk_instrument
        """)
        have_wro = {r["bronhouder"] for r in cur.fetchall()}

    conn.close()
    return {k: v for k, v in all_gem.items() if k not in have_wro}


def main():
    start = time.monotonic()
    start_dt = datetime.now()

    remaining = get_remaining_gemeenten()
    total = len(remaining)
    console.print(f"[bold]Start: {start_dt.strftime('%H:%M:%S')}[/bold]")
    console.print(f"[bold]{total} gemeenten zonder Wro gevonden — laden in 1 pass[/bold]")

    if total == 0:
        console.print("[green]Alle gemeenten hebben al Wro data![/green]")
        return

    # Show a sample
    sample = list(remaining.items())[:10]
    for code, naam in sample:
        console.print(f"  {naam} ({code})")
    if total > 10:
        console.print(f"  ... en {total - 10} meer")

    try:
        load_wro_plans(remaining)
    except Exception as e:
        console.print(f"[red]Wro failed: {e}[/red]")

    end_dt = datetime.now()
    elapsed = time.monotonic() - start
    console.print(f"\n[bold green]Done: Wro geladen voor {total} gemeenten[/bold green]")
    console.print(f"[bold]Start: {start_dt.strftime('%H:%M:%S')}, Eind: {end_dt.strftime('%H:%M:%S')}, Duur: {elapsed/60:.1f} min[/bold]")


if __name__ == "__main__":
    main()
