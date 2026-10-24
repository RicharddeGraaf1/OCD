"""Wro-planteksten v2 als één keten: de enige weg waarlangs de loader Wro-teksten laadt.

Vervangt sinds 2026-10-24 de oude `ihr_loader.load_wro_teksten`. Die stopte na 100
teksten per plan en verloor teksten op een niet-unieke sleutel; zie
docs/wro-teksten-herstel-plan.md. De keten draait de drie v2-scripts in vaste volgorde:

  1. scripts/laad_wro_teksten_v2.py   IHR-teksten + koppeling tekst -> planobject
                                      (slaat plannen over die al zonder fout geladen zijn)
  2. scripts/laad_wro_regels_html.py  plannen zonder objectgerichte teksten: het
                                      HTML-regelsdocument (alleen wat nog niet geprobeerd is)
  3. scripts/bouw_wro_indeling.py     onderwerp-indeling, alleen voor wat in 1 en 2 geladen is

Als subproces, niet geïmporteerd: de scripts hebben elk hun eigen bewaking (alleen
lokaal, hervatbaar, parallel met een eigen rate limit), en zo blijft er één
implementatie. Een script dat faalt stopt de keten met een RuntimeError.

Productie gaat NIET via deze keten maar via scripts/wro_v2_naar_productie.py
(runbook, stap "Wro-teksten naar productie").
"""

import datetime
import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from src.db import normalize_bronhouder_code

console = Console()

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
# Windows kapt een opdrachtregel af rond 32.000 tekens; een plan-idn is ~40 tekens.
IDN_PER_AANROEP = 200


def _draai(script: str, *args: str) -> None:
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    console.print(f"[dim]$ {script} {' '.join(a if len(a) < 80 else a[:77] + '...' for a in args)}[/dim]")
    r = subprocess.run(cmd, cwd=str(SCRIPTS.parent), env=os.environ.copy())
    if r.returncode:
        raise RuntimeError(f"{script} faalde (exit {r.returncode})")


def laad(bronhouders: list[str] | None = None, idns: list[str] | None = None,
         opnieuw: bool = False) -> None:
    """Laad Wro-teksten v2 voor de gegeven bronhouders (CBS- of gm-codes) of plannen.

    Zonder beide: alle plannen. Met `opnieuw` ook plannen die al geladen zijn.
    """
    if os.environ.get("OCD_DB_URL"):
        raise RuntimeError("Wro-teksten v2 laden alleen lokaal; productie via wro_v2_naar_productie.py")
    # Marge van een minuut: geladen_op komt van de databaseklok.
    t0 = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat()
    extra = ["--opnieuw"] if opnieuw else []

    if idns is not None:
        if not idns:
            console.print("[dim]wro-teksten: geen plannen[/dim]")
            return
        for i in range(0, len(idns), IDN_PER_AANROEP):
            _draai("laad_wro_teksten_v2.py", "--idn", ",".join(idns[i:i + IDN_PER_AANROEP]), *extra)
    elif bronhouders:
        codes = sorted({normalize_bronhouder_code(c) for c in bronhouders})
        _draai("laad_wro_teksten_v2.py", "--bronhouder", ",".join(codes), *extra)
    else:
        _draai("laad_wro_teksten_v2.py", "--alle", *extra)

    _draai("laad_wro_regels_html.py", "--alle")
    _draai("bouw_wro_indeling.py", "--sinds", t0)
