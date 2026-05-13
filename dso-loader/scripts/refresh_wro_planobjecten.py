"""Stap 2 van de Wro-refresh: planobjecten herladen voor recent
geladen plannen.

Pakt alle gemeenten waarvan de meest recente `laatst_geladen` < 1 dag is
en herlaadt voor die gemeenten alle 8 feature-types planobjecten.

Gebruik:
    cd dso-loader && source .venv/Scripts/activate
    PYTHONPATH=. python scripts/refresh_wro_planobjecten.py
"""

import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from datetime import datetime, timedelta

from rich.console import Console

from src.db import get_conn
from src.loaders.wro_pdok import _load_planobjecten

console = Console()


def main():
    started = datetime.now()
    cutoff = datetime.now() - timedelta(days=1)

    conn = get_conn()
    try:
        # Vind gemeenten met recent bijgewerkte plannen
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT bronhouder
                FROM wro.ruimtelijk_instrument
                WHERE laatst_geladen >= %s
                ORDER BY bronhouder
            """, (cutoff,))
            gm_codes = [r["bronhouder"] for r in cur.fetchall()]

        # Strip 'gm'-prefix want PDOK gebruikt bare codes
        cbs_codes = {c.removeprefix("gm") for c in gm_codes}
        console.print(f"[bold]{len(cbs_codes)} gemeenten met recent bijgewerkte plannen[/bold]")

        if not cbs_codes:
            console.print("[yellow]Niets te doen[/yellow]")
            return

        for feature, obj_type in [
            ("Enkelbestemming", "Enkelbestemming"),
            ("Dubbelbestemming", "Dubbelbestemming"),
            ("Bouwvlak", "Bouwvlak"),
            ("Functieaanduiding", "Functieaanduiding"),
            ("Bouwaanduiding", "Bouwaanduiding"),
            ("Maatvoering", "Maatvoering"),
            ("Figuur", "Figuur"),
            ("Gebiedsaanduiding", "Gebiedsaanduiding"),
        ]:
            console.rule(f"[bold]{feature}[/bold]")
            try:
                _load_planobjecten(conn, feature, obj_type, cbs_codes)
            except Exception as e:
                console.print(f"  [red]Fout: {e}[/red]")
                conn.rollback()

        elapsed = datetime.now() - started
        console.print(f"\n[bold green]Klaar in {elapsed}[/bold green]")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
