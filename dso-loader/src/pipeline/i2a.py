"""i2a-keten: toepasbare regels (RTR + STTR) en werkzaamhedencatalogus.

Per gemeente een aparte run via imtr_loader.load_imtr_for. De
werkzaamhedencatalogus is landelijk en wordt eenmalig geladen.

Provincies/waterschappen hebben geen IMTR → gefilterd.
"""

from rich.console import Console

from src.db import get_conn
from src.loaders.imtr_loader import load_imtr_for, _load_werkzaamheden
from src.pipeline.bronhouders import Bronhouder, filter_by_type

console = Console()


def run(bronhouders: list[Bronhouder],
        load_werkzaamheden: bool = True) -> dict[str, str]:
    """Laad IMTR per gemeente en optioneel de werkzaamhedencatalogus."""
    gemeenten = filter_by_type(bronhouders, "gemeente")
    results: dict[str, str] = {}

    if load_werkzaamheden:
        console.rule("[bold]i2a werkzaamheden[/bold] — landelijke catalogus")
        conn = get_conn()
        try:
            _load_werkzaamheden(conn)
            conn.commit()
            results["__werkzaamheden__"] = "ok"
        except Exception as e:
            console.print(f"[red]werkzaamheden fout: {e}[/red]")
            results["__werkzaamheden__"] = f"error: {e}"
        finally:
            conn.close()

    total = len(gemeenten)
    for i, bh in enumerate(gemeenten, 1):
        console.rule(f"[bold]i2a {i}/{total}[/bold] {bh.naam} ({bh.overheid_code})")
        try:
            load_imtr_for(bh.overheid_code, bh.naam)
            results[bh.code] = "ok"
        except Exception as e:
            console.print(f"[red]i2a fout {bh.code}: {e}[/red]")
            results[bh.code] = f"error: {e}"

    return results
