"""Load Ow + IMTR for ALL remaining overheden (no Wro — that's separate)."""
import os, json, time
os.environ["PYTHONIOENCODING"] = "utf-8"

from datetime import datetime
from rich.console import Console
from src.loaders.api_loader import load_via_api
from src.loaders.imtr_loader import load_imtr_for
from src.db import get_conn

console = Console()

# Missing provincies
NEW_PROVINCIES = {
    "pv20": "Provincie Groningen",  # different from pv21 which we have
    "pv22": "Provincie Drenthe",
    "pv23": "Provincie Overijssel",
    "pv24": "Provincie Flevoland",
    "pv29": "Provincie Zeeland",
    "pv30": "Provincie Noord-Brabant",
    "pv31": "Provincie Limburg",
}

# Missing waterschappen
NEW_WATERSCHAPPEN = {
    "ws0147": "Waterschap Drents Overijsselse Delta",
    "ws0148": "Waterschap Vechtstromen",
    "ws0151": "Waterschap Zuiderzeeland",
    "ws0152": "Waterschap Rijn en IJssel",
    "ws0153": "Waterschap Vallei en Veluwe",
    "ws0154": "Hoogheemraadschap van Delfland",
    "ws0156": "Hoogheemraadschap van Schieland en de Krimpenerwaard",
    "ws0157": "Waterschap Hollandse Delta",
    "ws0655": "Hoogheemraadschap Hollands Noorderkwartier",
    "ws0668": "Waterschap Brabantse Delta",
    "ws0669": "Waterschap De Dommel",
    "ws0670": "Waterschap Aa en Maas",
    "ws0678": "Waterschap Limburg",
    "ws0680": "Wetterskip Fryslan",
    "ws0694": "Waterschap Hunze en Aas",
    "ws0696": "Waterschap Noorderzijlvest",
    "ws0372": "Waterschap Scheldestromen",
}

def main():
    start = time.monotonic()
    start_dt = datetime.now()
    console.print(f"[bold]Start: {start_dt.strftime('%H:%M:%S')}[/bold]")

    # Load nieuwe gemeenten list
    with open("data/nieuwe_gemeenten.json", encoding="utf-8") as f:
        new_gemeenten = json.load(f)

    # Check which we already have
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT overheidscode FROM core.bronhouder")
        existing = {r["overheidscode"] for r in cur.fetchall()}
    conn.close()

    # Filter out already loaded
    gemeenten = {k: v for k, v in new_gemeenten.items() if k not in existing}
    provincies = {k: v for k, v in NEW_PROVINCIES.items() if k not in existing}
    waterschappen = {k: v for k, v in NEW_WATERSCHAPPEN.items() if k not in existing}

    console.print(f"Te laden: {len(gemeenten)} gemeenten, {len(provincies)} provincies, {len(waterschappen)} waterschappen")

    # === Ow ===
    all_ow = list(gemeenten.items()) + list(provincies.items()) + list(waterschappen.items())
    console.print(f"\n[bold]=== Stap 1/2: Ow laden ({len(all_ow)} overheden) ===[/bold]")

    for i, (code, naam) in enumerate(all_ow, 1):
        if i % 20 == 1 or i == len(all_ow):
            console.print(f"  [{i}/{len(all_ow)}] {naam} ({code})")
        try:
            load_via_api(code if not code[0].isdigit() else f"gm{code}", naam, bronhouder_code=code)
        except Exception as e:
            console.print(f"  [red]{naam} Ow failed: {str(e)[:60]}[/red]")

    t1 = time.monotonic()
    console.print(f"\n[bold]Stap 1 klaar in {(t1-start)/60:.1f} min[/bold]")

    # === IMTR (only gemeenten) ===
    console.print(f"\n[bold]=== Stap 2/2: IMTR laden ({len(gemeenten)} gemeenten) ===[/bold]")

    for i, (code, naam) in enumerate(gemeenten.items(), 1):
        if i % 20 == 1 or i == len(gemeenten):
            console.print(f"  [{i}/{len(gemeenten)}] {naam} ({code})")
        try:
            load_imtr_for(code, naam)
        except Exception as e:
            console.print(f"  [red]{naam} IMTR failed: {str(e)[:60]}[/red]")

    end_dt = datetime.now()
    elapsed = time.monotonic() - start
    console.print(f"\n[bold green]Done[/bold green]")
    console.print(f"[bold]Start: {start_dt.strftime('%H:%M:%S')}, Eind: {end_dt.strftime('%H:%M:%S')}, Duur: {elapsed/60:.1f} min[/bold]")

if __name__ == "__main__":
    main()
