"""p2p-keten: Ow-regelingen via DSO Presenteren API.

Eén bronhouder per keer; api_loader.load_via_api regelt download +
parse + insert. Werkt voor gemeenten, provincies, waterschappen.
"""

from rich.console import Console

from src.loaders.api_loader import load_via_api
from src.pipeline.bronhouders import Bronhouder

console = Console()


def run(bronhouders: list[Bronhouder],
        doc_types: list[str] | None = None) -> dict[str, str]:
    """Laad Ow-content voor elke bronhouder.

    `doc_types` filter (bv. ['Omgevingsplan','Omgevingsvisie']) wordt aan
    de DSO-API doorgegeven; None = alles.

    Returns dict {code: 'ok'|'error: ...'}.
    """
    results: dict[str, str] = {}
    total = len(bronhouders)
    for i, bh in enumerate(bronhouders, 1):
        console.rule(f"[bold]p2p {i}/{total}[/bold] {bh.naam} ({bh.overheid_code})")
        try:
            load_via_api(bh.overheid_code, bh.naam,
                         bronhouder_code=bh.code, doc_types=doc_types)
            results[bh.code] = "ok"
        except Exception as e:
            console.print(f"[red]p2p fout {bh.code}: {e}[/red]")
            results[bh.code] = f"error: {e}"
    return results
