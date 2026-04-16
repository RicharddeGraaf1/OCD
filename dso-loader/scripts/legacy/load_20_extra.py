"""Load 20 extra gemeenten — all pipelines."""
import os
os.environ["PYTHONIOENCODING"] = "utf-8"

from rich.console import Console
from src.loaders.api_loader import load_via_api
from src.loaders.imtr_loader import load_imtr_for
from src.loaders.wro_pdok import load_wro_plans

console = Console()

GEMEENTEN = {
    "0009": "Almelo",
    "0017": "Dantumadiel",
    "0047": "Veendam",
    "0050": "Zeewolde",
    "0058": "Dongeradeel",
    "0070": "Littenseradiel",
    "0074": "Schiermonnikoog",
    "0085": "Vlaardingen",
    "0119": "Hoogeveen",
    "0141": "Hellendoorn",
    "0160": "Hardenberg",
    "0168": "Kampen",
    "0175": "Rijssen-Holten",
    "0180": "Staphorst",
    "0189": "Wierden",
    "0196": "Raalte",
    "0216": "Doetinchem",
    "0221": "Elburg",
    "0225": "Hattem",
    "0232": "Oldebroek",
}

def main():
    codes = list(GEMEENTEN.keys())
    total = len(codes)

    console.print(f"\n[bold]=== Stap 1/4: Ow laden ({total} gemeenten) ===[/bold]")
    for i, (code, naam) in enumerate(GEMEENTEN.items(), 1):
        console.print(f"\n[cyan][{i}/{total}] {naam} ({code})[/cyan]")
        try:
            load_via_api(f"gm{code}", naam, bronhouder_code=code)
        except Exception as e:
            console.print(f"[red]Ow failed: {e}[/red]")

    console.print(f"\n[bold]=== Stap 2/4: IMTR laden ({total} gemeenten) ===[/bold]")
    for i, (code, naam) in enumerate(GEMEENTEN.items(), 1):
        console.print(f"\n[cyan][{i}/{total}] {naam} ({code})[/cyan]")
        try:
            load_imtr_for(code, naam)
        except Exception as e:
            console.print(f"[red]IMTR failed: {e}[/red]")

    console.print(f"\n[bold]=== Stap 3/4: Wro laden ({total} gemeenten, single pass) ===[/bold]")
    try:
        load_wro_plans(GEMEENTEN)
    except Exception as e:
        console.print(f"[red]Wro failed: {e}[/red]")

    console.print(f"\n[bold green]Done: {total} gemeenten geladen[/bold green]")

if __name__ == "__main__":
    main()
