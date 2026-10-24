"""wro-keten: bestemmingsplannen via PDOK + teksten via IHR (v2-keten).

PDOK-loader is batch (alle bronhouders in één pass — efficiënter omdat
het GML-bestand toch landelijk is). De teksten gaan sinds 2026-10-24 via
src/loaders/wro_teksten_keten.py (IHR v2 + HTML-regels + onderwerp-indeling).

Alleen gemeenten — Wro-bestemmingsplannen zijn een gemeentelijk
instrument. Provincies/waterschappen worden gefilterd.
"""

from rich.console import Console

from src.loaders.wro_pdok import load_wro_plans
from src.loaders.wro_teksten_keten import laad as laad_wro_teksten
from src.pipeline.bronhouders import Bronhouder, filter_by_type

console = Console()


def run(bronhouders: list[Bronhouder],
        include_teksten: bool = True) -> dict[str, int | str]:
    """Laad Wro-plannen (PDOK) en optioneel teksten (IHR) voor de gemeenten."""
    gemeenten = filter_by_type(bronhouders, "gemeente")
    if not gemeenten:
        console.print("[yellow]wro.run: geen gemeenten in input — overgeslagen[/yellow]")
        return {}

    cbs_codes = {b.code: b.naam for b in gemeenten}

    console.rule(f"[bold]wro plannen[/bold] — {len(cbs_codes)} gemeenten (PDOK batch)")
    try:
        load_wro_plans(cbs_codes, met_teksten=False)
        plans_status: int | str = "ok"
    except Exception as e:
        console.print(f"[red]wro plannen fout: {e}[/red]")
        plans_status = f"error: {e}"

    teksten_status: int | str = "skipped"
    if include_teksten:
        console.rule(f"[bold]wro teksten[/bold] — IHR voor {len(cbs_codes)} gemeenten")
        try:
            laad_wro_teksten(bronhouders=list(cbs_codes.keys()))
            teksten_status = "ok"
        except Exception as e:
            console.print(f"[red]wro teksten fout: {e}[/red]")
            teksten_status = f"error: {e}"

    return {"plannen": plans_status, "teksten": teksten_status}
