"""Backfill p2p.geo_informatieobject.naam uit de gecachete Download-ZIP's.

Aanleiding (gebruiker, 2026-06-09): GIO's moeten als object in de objectlijsten
verschijnen, met een leesbare naam i.p.v. een FRBR-URI. De naam (`geo:naam`)
zit in de GIO-GML maar werd niet geladen. `gio_zip.extract_gio_naam` haalt 'm op.

Dit script loopt over alle gecachete ZIP's in CACHE, extraheert per ZIP de
{frbr: naam}-map en zet de naam op bestaande rijen waar die nog NULL is
(COALESCE, dus idempotent en niet-destructief). GIO's die niet in de DB staan
worden overgeslagen — die komen via de reguliere loader binnen.

Run:  python -m scripts.backfill_gio_naam            # alle cache-ZIP's
      python -m scripts.backfill_gio_naam --limit 50 # smoke-test
"""
from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console

from src.db import get_conn
from src.loaders.gio_zip import extract_gio_naam

CACHE = Path("c:/GIT/OCD/dso-loader/data/downloads/ow")
console = Console()


def backfill(limit: int | None = None) -> dict[str, int]:
    zips = sorted(z for z in CACHE.iterdir() if z.suffix == ".zip")
    if limit:
        zips = zips[:limit]
    console.print(f"[cyan]{len(zips)} ZIP's te verwerken uit {CACHE}[/cyan]")

    totaal_gevonden = 0
    totaal_gezet = 0
    zips_met_naam = 0

    with get_conn() as conn:
        for i, zip_path in enumerate(zips, 1):
            try:
                naam_map = extract_gio_naam(zip_path)
            except Exception as e:  # noqa: BLE001 — tolerante backfill, log en door
                console.print(f"  [red]fout in {zip_path.name}: {e}[/red]")
                continue
            if not naam_map:
                continue
            zips_met_naam += 1
            totaal_gevonden += len(naam_map)

            with conn.cursor() as cur:
                for frbr, naam in naam_map.items():
                    cur.execute(
                        """UPDATE p2p.geo_informatieobject
                           SET naam = %s
                           WHERE frbr_expression = %s AND naam IS NULL""",
                        (naam, frbr),
                    )
                    totaal_gezet += cur.rowcount
            conn.commit()

            if i % 100 == 0:
                console.print(
                    f"  [{i}/{len(zips)}] gevonden={totaal_gevonden} gezet={totaal_gezet}"
                )

    console.print(
        f"[green]Klaar. ZIP's met GIO-naam: {zips_met_naam}, "
        f"namen gevonden: {totaal_gevonden}, rijen geüpdatet: {totaal_gezet}[/green]"
    )
    return {
        "zips_met_naam": zips_met_naam,
        "namen_gevonden": totaal_gevonden,
        "rijen_geupdatet": totaal_gezet,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None, help="Beperk tot N ZIP's (smoke-test)")
    args = p.parse_args()
    backfill(limit=args.limit)
