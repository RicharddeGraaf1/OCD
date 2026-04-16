"""Load 40 extra gemeenten — all pipelines, with timing."""
import os
os.environ["PYTHONIOENCODING"] = "utf-8"

import time
from datetime import datetime
from rich.console import Console
from src.loaders.api_loader import load_via_api
from src.loaders.imtr_loader import load_imtr_for
from src.loaders.wro_pdok import load_wro_plans

console = Console()

GEMEENTEN = {
    "0018": "Hoogeveen",
    "0024": "Coevorden",
    "0037": "Stadskanaal",
    "0040": "Tynaarlo",
    "0048": "Vlagtwedde",
    "0051": "Skarsterlân",
    "0055": "Boarnsterhim",
    "0063": "Heerenveen",
    "0072": "Opsterland",
    "0076": "Smallingerland",
    "0079": "Tytsjerksteradiel",
    "0081": "Losser",
    "0085": "Vlaardingen",
    "0090": "Súdwest-Fryslân",
    "0096": "Midden-Drenthe",
    "0098": "De Wolden",
    "0109": "Noordenveld",
    "0118": "Borger-Odoorn",
    "0140": "Dalfsen",
    "0147": "Borne",
    "0148": "Dinkelland",
    "0158": "Haaksbergen",
    "0163": "Twenterand",
    "0166": "Hof van Twente",
    "0173": "Oldenzaal",
    "0177": "Steenwijkerland",
    "0184": "Urk",
    "0187": "Tubbergen",
    "0193": "Eindhoven",
    "0203": "Berkelland",
    "0209": "Bronckhorst",
    "0214": "Beuningen",
    "0222": "Epe",
    "0226": "Heumen",
    "0230": "Oldebroek",
    "0236": "Putten",
    "0241": "Scherpenzeel",
    "0246": "Voorst",
    "0262": "Kaag en Braassem",
    "0267": "Nijkerk",
}

def main():
    start = time.monotonic()
    start_dt = datetime.now()
    console.print(f"[bold]Start: {start_dt.strftime('%H:%M:%S')}[/bold]")

    total = len(GEMEENTEN)

    console.print(f"\n[bold]=== Stap 1/4: Ow laden ({total} gemeenten) ===[/bold]")
    for i, (code, naam) in enumerate(GEMEENTEN.items(), 1):
        console.print(f"\n[cyan][{i}/{total}] {naam} ({code})[/cyan]")
        try:
            load_via_api(f"gm{code}", naam, bronhouder_code=code)
        except Exception as e:
            console.print(f"[red]Ow failed: {e}[/red]")
    t1 = time.monotonic()
    console.print(f"\n[bold]Stap 1 klaar in {(t1-start)/60:.1f} min[/bold]")

    console.print(f"\n[bold]=== Stap 2/4: IMTR laden ({total} gemeenten) ===[/bold]")
    for i, (code, naam) in enumerate(GEMEENTEN.items(), 1):
        console.print(f"\n[cyan][{i}/{total}] {naam} ({code})[/cyan]")
        try:
            load_imtr_for(code, naam)
        except Exception as e:
            console.print(f"[red]IMTR failed: {e}[/red]")
    t2 = time.monotonic()
    console.print(f"\n[bold]Stap 2 klaar in {(t2-t1)/60:.1f} min[/bold]")

    console.print(f"\n[bold]=== Stap 3/4: Wro laden ({total} gemeenten, single pass) ===[/bold]")
    try:
        load_wro_plans(GEMEENTEN)
    except Exception as e:
        console.print(f"[red]Wro failed: {e}[/red]")
    t3 = time.monotonic()
    console.print(f"\n[bold]Stap 3 klaar in {(t3-t2)/60:.1f} min[/bold]")

    end_dt = datetime.now()
    elapsed = time.monotonic() - start
    console.print(f"\n[bold green]Done: {total} gemeenten geladen[/bold green]")
    console.print(f"[bold]Start: {start_dt.strftime('%H:%M:%S')}, Eind: {end_dt.strftime('%H:%M:%S')}, Duur: {elapsed/60:.1f} min[/bold]")

if __name__ == "__main__":
    main()
