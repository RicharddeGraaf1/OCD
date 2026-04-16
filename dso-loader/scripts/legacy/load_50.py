"""Load 50 bevoegde gezagen via API pipeline."""
import sys
import os
import time

os.environ["PYTHONIOENCODING"] = "utf-8"

from rich.console import Console
from rich.table import Table

from src.loaders.api_loader import load_via_api
from src.db import get_conn

console = Console()

# 48 new (Utrecht gm0344 + Amsterdam gm0363 already loaded)
OVERHEDEN = [
    # G4
    ("gm0518", "Den Haag", "0518"),
    ("gm0599", "Rotterdam", "0599"),
    # Grote gemeenten
    ("gm0014", "Groningen", "0014"),
    ("gm0034", "Almere", "0034"),
    ("gm0153", "Enschede", "0153"),
    ("gm0193", "Eindhoven", "0193"),
    ("gm0202", "Arnhem", "0202"),
    ("gm0268", "Nijmegen", "0268"),
    ("gm0307", "Amersfoort", "0307"),
    ("gm0362", "Amstelveen", "0362"),
    ("gm0392", "Haarlem", "0392"),
    ("gm0394", "Haarlemmermeer", "0394"),
    ("gm0402", "Hilversum", "0402"),
    ("gm0439", "Purmerend", "0439"),
    ("gm0457", "Zaanstad", "0457"),
    ("gm0473", "Leidschendam-Voorburg", "0473"),
    ("gm0484", "Alphen aan den Rijn", "0484"),
    ("gm0503", "Delft", "0503"),
    ("gm0546", "Leiden", "0546"),
    ("gm0569", "Zoetermeer", "0569"),
    ("gm0579", "Dordrecht", "0579"),
    ("gm0637", "Breda", "0637"),
    ("gm0654", "Tilburg", "0654"),
    ("gm0668", "'s-Hertogenbosch", "0668"),
    ("gm0687", "Venlo", "0687"),
    ("gm0757", "Maastricht", "0757"),
    ("gm0758", "Sittard-Geleen", "0758"),
    ("gm0772", "Heerlen", "0772"),
    ("gm0796", "Apeldoorn", "0796"),
    ("gm0855", "Zwolle", "0855"),
    ("gm0995", "Lelystad", "0995"),
    ("gm1680", "Leeuwarden", "1680"),
    ("gm0080", "Leiderdorp", "0080"),
    ("gm0632", "Gooise Meren", "0632"),
    # Kleinere gemeenten
    ("gm0060", "Ameland", "0060"),
    ("gm0074", "Schiermonnikoog", "0074"),
    ("gm0093", "Terschelling", "0093"),
    ("gm0400", "Texel", "0400"),
    # Provincies
    ("pv26", "Provincie Utrecht", "pv26"),
    ("pv27", "Provincie Noord-Holland", "pv27"),
    ("pv28", "Provincie Zuid-Holland", "pv28"),
    ("pv30", "Provincie Gelderland", "pv30"),
    ("pv25", "Provincie Flevoland", "pv25"),
    ("pv21", "Provincie Groningen", "pv21"),
    # Waterschappen
    ("ws0636", "Waterschap Amstel Gooi en Vecht", "ws0636"),
    ("ws0155", "Hoogheemraadschap De Stichtse Rijnlanden", "ws0155"),
    ("ws0654", "Hoogheemraadschap van Rijnland", "ws0654"),
    ("ws0621", "Waterschap Rivierenland", "ws0621"),
]

def main():
    successes = []
    failures = []

    for i, (code, naam, bronhouder) in enumerate(OVERHEDEN, 1):
        console.print(f"\n[bold cyan]=== [{i}/{len(OVERHEDEN)}] {naam} ({code}) ===[/bold cyan]")
        try:
            load_via_api(code, naam, bronhouder_code=bronhouder)
            successes.append((code, naam))
        except Exception as e:
            console.print(f"[red]FAILED: {e}[/red]")
            failures.append((code, naam, str(e)[:80]))

    console.print(f"\n[bold green]Done: {len(successes)} succeeded, {len(failures)} failed[/bold green]")
    if failures:
        console.print("[yellow]Failures:[/yellow]")
        for code, naam, err in failures:
            console.print(f"  {naam} ({code}): {err}")

if __name__ == "__main__":
    main()
