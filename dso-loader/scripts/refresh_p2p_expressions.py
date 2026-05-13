"""Lichtgewicht refresh van p2p.regeling.frbr_expression.

Doel: voor het ontwerp/besluit-filter is alleen de huidige expression-datum
relevant. Volledige re-load is niet nodig; we updaten alleen de
`frbr_expression` als de DSO een nieuwere versie heeft dan onze p2p.

Aanpak: per regeling-work één API call naar Presenteren v8 om de huidige
expression op te halen, dan UPDATE in p2p.regeling.

Geen annotaties / tekst herladen — alleen metadata.
"""
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from datetime import datetime
from rich.console import Console
from rich.progress import track

from src.config import cfg
from src.db import get_conn
from src.loaders.api_loader import _get

console = Console()


def refresh_all():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT frbr_work, frbr_expression
                FROM p2p.regeling
                ORDER BY bronhouder, frbr_work
            """)
            regelingen = cur.fetchall()

        console.print(f"[bold]{len(regelingen)} regelingen checken[/bold]")

        updated = 0
        unchanged = 0
        not_found = 0
        errors = 0

        for reg in track(regelingen, description="Refreshing"):
            work = reg["frbr_work"]
            current = reg["frbr_expression"]
            try:
                # Encode work voor URL
                encoded = work.replace("/", "_")
                data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}")
                new_expr = data.get("expressionId")

                if not new_expr:
                    not_found += 1
                    continue

                if new_expr == current:
                    unchanged += 1
                    continue

                with conn.cursor() as cur:
                    # Check of nieuwe expression al bestaat (anders FK conflict)
                    cur.execute(
                        "SELECT 1 FROM p2p.regeling WHERE frbr_expression = %s",
                        (new_expr,)
                    )
                    if cur.fetchone():
                        unchanged += 1
                        continue

                    cur.execute(
                        "UPDATE p2p.regeling SET frbr_expression = %s WHERE frbr_expression = %s",
                        (new_expr, current)
                    )
                conn.commit()
                updated += 1
                if updated % 20 == 0:
                    console.print(f"  [{updated} bijgewerkt] {work[:60]}: {current[-25:]} → {new_expr[-25:]}")
            except Exception as e:
                errors += 1
                conn.rollback()

        console.print(f"\n[bold green]Klaar[/bold green]")
        console.print(f"  Bijgewerkt:    {updated}")
        console.print(f"  Onveranderd:   {unchanged}")
        console.print(f"  Niet gevonden: {not_found}")
        console.print(f"  Fouten:        {errors}")
    finally:
        conn.close()


if __name__ == "__main__":
    refresh_all()
