"""Laad één of meerdere specifieke regelingen via DSO API (op frbr_work).

Bedoeld voor gerichte herstel-acties na een coverage-diff (zie
`diff_dso_bronhouder_coverage.py`). In tegenstelling tot `load_via_api`
geen discovery-loop — direct `GET /regelingen/{frbr_work_encoded}` per URI.

Gebruik:
    python scripts/laad_specifieke_regelingen.py \\
        /akn/nl/act/pv24/2026/2_46 \\
        /akn/nl/act/pv25/2025/gelders-programma-sport
"""

import sys
import os

os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.loaders.api_loader import (
    _get,
    _encode_regeling_uri,
    load_documentstructuur,
    load_regeling_expand,
    load_regeltekstannotaties,
    load_divisieannotaties,
    ARTIKELSTRUCTUUR_TYPES,
    VRIJETEKST_TYPES,
    REGELINGMODEL_MAP,
)

console = Console()


def laad(conn, work: str) -> None:
    encoded = _encode_regeling_uri(work)
    meta = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}")
    expr = meta.get("expressionId", work)
    titel = meta.get("officieleTitel", "")
    doc_type = meta.get("type", {}).get("waarde", "")
    bronhouder = meta.get("aangeleverdDoorEen", {}).get("code", "")
    regelingmodel = REGELINGMODEL_MAP.get(doc_type, "RegelingCompact")

    console.print(f"\n[bold cyan]{work}[/bold cyan]")
    console.print(f"  [{doc_type}] {titel[:80]} → bronhouder={bronhouder}")

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO p2p.regeling
               (frbr_expression, frbr_work, regelingmodel, opschrift, citeertitel, bronhouder, documenttype)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (frbr_expression) DO NOTHING""",
            (expr, work, regelingmodel, titel, titel, bronhouder, doc_type),
        )
    conn.commit()

    load_documentstructuur(conn, work, expr)
    load_regeling_expand(conn, work, expr)

    if doc_type in ARTIKELSTRUCTUUR_TYPES:
        load_regeltekstannotaties(conn, work, bronhouder)
    elif doc_type in VRIJETEKST_TYPES:
        load_divisieannotaties(conn, work, bronhouder)
    else:
        console.print(f"  [yellow]Onbekend type {doc_type} — fallback regeltekstannotaties[/yellow]")
        load_regeltekstannotaties(conn, work, bronhouder)


def main() -> None:
    if len(sys.argv) < 2:
        console.print("usage: python scripts/laad_specifieke_regelingen.py <work_uri> [<work_uri> ...]")
        sys.exit(1)

    conn = get_conn()
    ok = err = 0
    try:
        for work in sys.argv[1:]:
            try:
                laad(conn, work)
                console.print(f"  [green]OK[/green] {work}")
                ok += 1
            except Exception as e:
                console.print(f"  [red]FOUT[/red] {work}: {str(e)[:150]}")
                conn.rollback()
                err += 1
    finally:
        conn.close()

    console.print(f"\n[bold]Eindstand:[/bold] [green]{ok} OK[/green], [red]{err} fout[/red]")


if __name__ == "__main__":
    main()
