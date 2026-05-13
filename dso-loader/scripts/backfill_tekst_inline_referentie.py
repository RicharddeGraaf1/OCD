"""Backfill p2p.tekst_inline_referentie voor de bestaande dataset.

Iteratie per regeling: pak alle tekst_elements met inhoud != NULL, parse
de inhoud op IntIoRef / ExtIoRef / IntRef / ExtRef en INSERT de gevonden
referenties. Daarna één UPDATE-pas per regeling om target_soort en
target_gio_expression in te vullen.

Idempotent: bestaande rijen blijven door de UNIQUE-constraint
(tekst_element_id, soort, target_ref, positie) + ON CONFLICT DO NOTHING
in insert_inline_referenties.

Run:
  python scripts/backfill_tekst_inline_referentie.py
  python scripts/backfill_tekst_inline_referentie.py --regeling <frbr_expression>

Vereist: scripts/2026-05-add-tekst-inline-referentie.sql is gedraaid.
"""
import argparse
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from rich.console import Console
from rich.progress import track

from src.db import get_conn
from src.loaders.inline_referentie import (
    extract_inline_referenties,
    insert_inline_referenties,
    resolve_target_soort,
)

console = Console()


def backfill_regeling(conn, regeling_expression: str) -> dict:
    """Backfill één regeling. Returns counts dict."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, inhoud
               FROM p2p.tekst_element
               WHERE regeling_expression = %s
                 AND inhoud IS NOT NULL""",
            (regeling_expression,),
        )
        rows = cur.fetchall()

    inserted = 0
    elements_with_refs = 0
    for r in rows:
        refs = extract_inline_referenties(r["inhoud"])
        if not refs:
            continue
        elements_with_refs += 1
        inserted += insert_inline_referenties(conn, r["id"], refs)

    resolved = resolve_target_soort(conn, regeling_expression=regeling_expression)
    conn.commit()

    return {
        "elements_scanned": len(rows),
        "elements_with_refs": elements_with_refs,
        "rows_inserted": inserted,
        "resolved_gio": resolved["GIO"],
        "resolved_extern": resolved["Extern"],
        "resolved_tekstcomponent": resolved["Tekstcomponent"],
    }


def backfill_all(only_expression: str | None = None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if only_expression:
                cur.execute(
                    "SELECT frbr_expression FROM p2p.regeling WHERE frbr_expression = %s",
                    (only_expression,),
                )
            else:
                cur.execute(
                    "SELECT frbr_expression FROM p2p.regeling ORDER BY frbr_expression"
                )
            expressions = [r["frbr_expression"] for r in cur.fetchall()]

        if not expressions:
            console.print("[yellow]Geen regelingen gevonden.[/yellow]")
            return

        totals = {
            "elements_scanned": 0,
            "elements_with_refs": 0,
            "rows_inserted": 0,
            "resolved_gio": 0,
            "resolved_extern": 0,
            "resolved_tekstcomponent": 0,
        }
        for expr in track(expressions, description="Backfill inline-referenties"):
            result = backfill_regeling(conn, expr)
            for k in totals:
                totals[k] += result[k]

        console.print()
        console.print("[bold green]Klaar.[/bold green]")
        console.print(f"  Regelingen verwerkt:        {len(expressions)}")
        console.print(f"  Tekst-elements gescand:     {totals['elements_scanned']:,}")
        console.print(f"  Met inline-referenties:     {totals['elements_with_refs']:,}")
        console.print(f"  Rijen ingevoegd:            {totals['rows_inserted']:,}")
        console.print(f"  IntIoRef → GIO opgelost:    {totals['resolved_gio']:,}")
        console.print(f"  Ext(Io)Ref → Extern:        {totals['resolved_extern']:,}")
        console.print(f"  IntRef → Tekstcomponent:    {totals['resolved_tekstcomponent']:,}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regeling",
        help="frbr_expression van één specifieke regeling om te backfillen",
        default=None,
    )
    args = parser.parse_args()
    backfill_all(only_expression=args.regeling)
