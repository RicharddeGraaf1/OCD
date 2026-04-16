"""Load Wro bestemmingsplannen for 40 gemeenten (batch B) — Wro only.

These gemeenten already have Ow + IMTR loaded via load_all_ow_imtr.py.
This script adds the Wro pillar: GML plangebieden + planobjecten + IHR teksten.
"""
import os, time
os.environ["PYTHONIOENCODING"] = "utf-8"

from datetime import datetime
from rich.console import Console
from src.loaders.wro_pdok import load_wro_plans

console = Console()

# 40 gemeenten — mix van provincies, al Ow+IMTR geladen, nog geen Wro
GEMEENTEN = {
    # Gelderland
    "0274": "Renkum",
    "0275": "Rheden",
    "0281": "Tiel",
    "0293": "Westervoort",
    "0294": "Winterswijk",
    "0296": "Wijchen",
    "0297": "Zaltbommel",
    "0299": "Zevenaar",
    "0301": "Zutphen",
    # Flevoland
    "0303": "Dronten",
    # Utrecht
    "0308": "Baarn",
    "0327": "Leusden",
    "0342": "Soest",
    "0352": "Wijk bij Duurstede",
    "0353": "IJsselstein",
    "0355": "Zeist",
    # Noord-Holland
    "0358": "Aalsmeer",
    "0361": "Alkmaar",
    "0375": "Beverwijk",
    "0377": "Bloemendaal",
    "0383": "Castricum",
    "0396": "Heemskerk",
    "0397": "Heemstede",
    "0405": "Hoorn",
    "0406": "Huizen",
    "0451": "Uithoorn",
    "0453": "Velsen",
    # Zuid-Holland
    "0502": "Capelle aan den IJssel",
    "0534": "Hillegom",
    "0553": "Lisse",
    "0556": "Maassluis",
    "0597": "Ridderkerk",
    "0603": "Rijswijk",
    "0626": "Voorschoten",
    "0629": "Wassenaar",
    # Zeeland
    "0664": "Goes",
    # Noord-Brabant
    "0748": "Bergen op Zoom",
    "0794": "Helmond",
    "0828": "Oss",
    # Limburg
    "0957": "Roermond",
}


def main():
    start = time.monotonic()
    start_dt = datetime.now()
    total = len(GEMEENTEN)
    console.print(f"[bold]Start: {start_dt.strftime('%H:%M:%S')} — Wro laden voor {total} gemeenten[/bold]")

    try:
        load_wro_plans(GEMEENTEN)
    except Exception as e:
        console.print(f"[red]Wro failed: {e}[/red]")

    end_dt = datetime.now()
    elapsed = time.monotonic() - start
    console.print(f"\n[bold green]Done: {total} gemeenten Wro geladen[/bold green]")
    console.print(f"[bold]Start: {start_dt.strftime('%H:%M:%S')}, Eind: {end_dt.strftime('%H:%M:%S')}, Duur: {elapsed/60:.1f} min[/bold]")


if __name__ == "__main__":
    main()
