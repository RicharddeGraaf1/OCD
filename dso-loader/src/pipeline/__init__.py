"""Keten-gedreven load-pipeline.

Per keten een aparte module die de bestaande loaders orchestreert:
  core — DDL + lookup-tabellen (eenmalig)
  p2p  — Ow-regelingen via DSO Presenteren API (api_loader)
  wro  — Bestemmingsplannen via PDOK (wro_pdok) + teksten via IHR (ihr_loader)
  i2a  — Toepasbare regels via RTR/STTR (imtr_loader) + werkzaamhedencatalogus

Bedoeling: één entry-point per keten, een `Bronhouder`-lijst als input,
en een orchestrator (`all`) die alle ketens in de juiste volgorde draait.
"""

from src.pipeline.bronhouders import Bronhouder, load_bronhouders
from src.pipeline import core, p2p, wro, i2a

__all__ = ["Bronhouder", "load_bronhouders", "core", "p2p", "wro", "i2a", "all_ketens"]


def all_ketens(bronhouders: list[Bronhouder],
               doc_types: list[str] | None = None,
               include_wro_teksten: bool = True) -> dict:
    """Run all ketens in sequence: p2p → wro → i2a.

    `core.bootstrap()` wordt NIET automatisch gedraaid — dat is een
    eenmalige setup-stap.
    """
    results = {}
    results["p2p"] = p2p.run(bronhouders, doc_types=doc_types)
    results["wro"] = wro.run(bronhouders, include_teksten=include_wro_teksten)
    results["i2a"] = i2a.run(bronhouders)
    return results
