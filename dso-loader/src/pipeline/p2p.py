"""p2p-keten: Ow-regelingen via DSO Presenteren API.

Eén bronhouder per keer; api_loader.load_via_api regelt download +
parse + insert. Werkt voor gemeenten, provincies, waterschappen.
"""

from rich.console import Console

from src.loaders.api_loader import load_delta, load_via_api
from src.pipeline.bronhouders import Bronhouder

console = Console()


def run(bronhouders: list[Bronhouder],
        doc_types: list[str] | None = None,
        uitstel_subdiv: bool = False,
        gewijzigd: set[str] | None = None) -> dict[str, str]:
    """Laad Ow-content voor elke bronhouder.

    `doc_types` filter (bv. ['Omgevingsplan','Omgevingsvisie']) wordt aan
    de DSO-API doorgegeven; None = alles.

    `uitstel_subdiv`/`gewijzigd` — zie `api_loader.load_via_api`: de
    subdiv-herbouw uitstellen naar de post-fase i.p.v. tijdens het harvesten.

    Returns dict {code: 'ok'|'error: ...'}.
    """
    results: dict[str, str] = {}
    total = len(bronhouders)
    for i, bh in enumerate(bronhouders, 1):
        console.rule(f"[bold]p2p {i}/{total}[/bold] {bh.naam} ({bh.overheid_code})")
        try:
            load_via_api(bh.overheid_code, bh.naam,
                         bronhouder_code=bh.code, doc_types=doc_types,
                         uitstel_subdiv=uitstel_subdiv, gewijzigd=gewijzigd)
            results[bh.code] = "ok"
        except Exception as e:
            console.print(f"[red]p2p fout {bh.code}: {e}[/red]")
            results[bh.code] = f"error: {e}"
    return results


def run_delta(bronhouders: list[Bronhouder],
              sinds: str | None,
              doc_types: list[str] | None = None,
              uitstel_subdiv: bool = False,
              gewijzigd: set[str] | None = None) -> dict[str, str]:
    """Incrementele p2p via één globale registratietijdstip-delta-sweep.

    I.p.v. alle bronhouders volledig te pollen, haalt dit alleen de regelingen
    op die sinds `sinds` (ISO-8601 UTC) zijn bijgeregistreerd. De scope blijft
    gelijk aan `bronhouders`: alleen regelingen van die overheid-codes tellen
    mee. Zie [[Incrementele p2p-sync via registratietijdstip-delta]].

    Returns dict {code: 'ok'|'error: ...'} — alleen de bronhouders die iets
    nieuws hadden. Bronhouders zonder nieuwe registratie komen niet in de dict
    en gelden impliciet als ongewijzigd.
    """
    bronhouder_map = {bh.overheid_code: (bh.code, bh.naam) for bh in bronhouders}
    console.rule(f"[bold]p2p delta-sweep[/bold] over {len(bronhouders)} bronhouders, sinds {sinds or 'begin'}")
    return load_delta(sinds, bronhouder_map=bronhouder_map, doc_types=doc_types,
                      uitstel_subdiv=uitstel_subdiv, gewijzigd=gewijzigd)
