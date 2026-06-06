"""One-shot refresh: vul maatvoering_info JSONB voor alle bestaande
Maatvoering-rijen in wro.planobject.

Achtergrond: tot 2026-05-31 liet de wro_pdok loader maatvoering_info NULL,
ook al staat de waarde in de PDOK GML. Patch is doorgevoerd. Dit script
draait _load_planobjecten opnieuw voor 4 feature-types met UPSERT, zodat
bestaande rijen verrijkt worden met de nu wel-geparseerde structurele info.

Veilig om meerdere keren te draaien (ON CONFLICT DO UPDATE met COALESCE).

Gebruik:
    cd dso-loader && PYTHONPATH=. .venv/Scripts/python scripts/refresh_maatvoering_info.py
"""
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from src.db import get_conn
from src.loaders.wro_pdok import _load_planobjecten
from rich.console import Console

console = Console()


def main():
    conn = get_conn()
    try:
        # Trek alle al-geladen gemeente-codes uit de DB. _load_planobjecten
        # filtert dan op deze set en doet UPSERT op planobject.identificatie.
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT bronhouder
                     FROM wro.ruimtelijk_instrument
                    WHERE bronhouder LIKE 'gm%'"""
            )
            # bronhouder is 'gm0307' — strip prefix om naar CBS-code te gaan
            cbs_codes = {row["bronhouder"][2:]: row["bronhouder"][2:] for row in cur.fetchall()}

        console.print(f"[bold]Refresh start voor {len(cbs_codes)} gemeenten[/bold]")

        # Volgorde: Maatvoering eerst (primaire fix), dan de andere
        # object-types die ook lege structurele kolommen hadden.
        for feature, obj_type in [
            ("Maatvoering",        "Maatvoering"),
            ("Bouwaanduiding",     "Bouwaanduiding"),
            ("Figuur",             "Figuur"),
            ("Gebiedsaanduiding",  "Gebiedsaanduiding"),
        ]:
            console.print(f"\n[bold cyan]Refresh {feature}...[/bold cyan]")
            _load_planobjecten(conn, feature, obj_type, set(cbs_codes.keys()))

        # Quick verify
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) AS n_mv,
                       count(*) FILTER (WHERE maatvoering_info IS NOT NULL) AS n_mv_filled
                  FROM wro.planobject WHERE object_type='Maatvoering'
            """)
            r = cur.fetchone()
            console.print(
                f"\n[bold green]Resultaat[/bold green]: "
                f"{r['n_mv_filled']:,} / {r['n_mv']:,} Maatvoering-rijen "
                f"hebben nu maatvoering_info "
                f"({100 * r['n_mv_filled'] / max(r['n_mv'], 1):.1f}%)"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
