"""Load 30 extra gemeenten — all pipelines (Ow + IMTR + Wro + teksten)."""
import os
os.environ["PYTHONIOENCODING"] = "utf-8"

from rich.console import Console
from src.loaders.api_loader import load_via_api
from src.loaders.imtr_loader import load_imtr_for
from src.loaders.wro_pdok import load_wro_plans

console = Console()

# 30 nieuwe gemeenten — mix van middelgroot en klein, verspreid over NL
GEMEENTEN = {
    "0003": "Appingedam",
    "0059": "Achtkarspelen",
    "0088": "Schiedam",
    "0106": "Assen",
    "0114": "Emmen",
    "0150": "Deventer",
    "0164": "Hengelo",
    "0171": "Noordoostpolder",
    "0183": "Olst-Wijhe",
    "0197": "Mook en Middelaar",
    "0200": "Ede",
    "0213": "Barneveld",
    "0228": "Nunspeet",
    "0233": "Ermelo",
    "0243": "Bunnik",
    "0244": "Bunschoten",
    "0252": "Gouda",
    "0258": "Gorinchem",
    "0263": "Katwijk",
    "0289": "Wageningen",
    "0310": "De Bilt",
    "0321": "Houten",
    "0335": "Stichtse Vecht",
    "0340": "Rhenen",
    "0345": "Veenendaal",
    "0356": "Woudenberg",
    "0384": "Diemen",
    "0420": "Medemblik",
    "0431": "Oostzaan",
    "0437": "Ouder-Amstel",
}

def main():
    codes = list(GEMEENTEN.keys())
    total = len(codes)

    # Step 1: Ow via API
    console.print(f"\n[bold]=== Stap 1/4: Ow laden ({total} gemeenten) ===[/bold]")
    for i, (code, naam) in enumerate(GEMEENTEN.items(), 1):
        console.print(f"\n[cyan][{i}/{total}] {naam} ({code})[/cyan]")
        try:
            load_via_api(f"gm{code}", naam, bronhouder_code=code)
        except Exception as e:
            console.print(f"[red]Ow failed: {e}[/red]")

    # Step 2: IMTR
    console.print(f"\n[bold]=== Stap 2/4: IMTR laden ({total} gemeenten) ===[/bold]")
    for i, (code, naam) in enumerate(GEMEENTEN.items(), 1):
        console.print(f"\n[cyan][{i}/{total}] {naam} ({code})[/cyan]")
        try:
            load_imtr_for(code, naam)
        except Exception as e:
            console.print(f"[red]IMTR failed: {e}[/red]")

    # Step 3: Wro (one pass through GML files)
    console.print(f"\n[bold]=== Stap 3/4: Wro laden ({total} gemeenten, single pass) ===[/bold]")
    try:
        load_wro_plans(GEMEENTEN)
    except Exception as e:
        console.print(f"[red]Wro failed: {e}[/red]")

    console.print(f"\n[bold green]Done: {total} gemeenten geladen[/bold green]")

if __name__ == "__main__":
    main()
