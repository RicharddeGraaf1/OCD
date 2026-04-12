"""Load IMTR for all 50 bevoegde gezagen (gemeenten only - provinces/waterschappen have no IMTR)."""
import os
os.environ["PYTHONIOENCODING"] = "utf-8"

from rich.console import Console
from src.loaders.imtr_loader import load_imtr_for, _load_werkzaamheden
from src.db import get_conn

console = Console()

GEMEENTEN = [
    ("0344", "Utrecht"),
    ("0363", "Amsterdam"),
    ("0518", "Den Haag"),
    ("0599", "Rotterdam"),
    ("0014", "Groningen"),
    ("0034", "Almere"),
    ("0153", "Enschede"),
    ("0193", "Eindhoven"),
    ("0202", "Arnhem"),
    ("0268", "Nijmegen"),
    ("0307", "Amersfoort"),
    ("0362", "Amstelveen"),
    ("0392", "Haarlem"),
    ("0394", "Haarlemmermeer"),
    ("0402", "Hilversum"),
    ("0439", "Purmerend"),
    ("0457", "Zaanstad"),
    ("0473", "Leidschendam-Voorburg"),
    ("0484", "Alphen aan den Rijn"),
    ("0503", "Delft"),
    ("0546", "Leiden"),
    ("0569", "Zoetermeer"),
    ("0579", "Dordrecht"),
    ("0637", "Breda"),
    ("0654", "Tilburg"),
    ("0668", "'s-Hertogenbosch"),
    ("0687", "Venlo"),
    ("0757", "Maastricht"),
    ("0758", "Sittard-Geleen"),
    ("0772", "Heerlen"),
    ("0796", "Apeldoorn"),
    ("0855", "Zwolle"),
    ("0995", "Lelystad"),
    ("1680", "Leeuwarden"),
    ("0080", "Leiderdorp"),
    ("0632", "Gooise Meren"),
    ("0060", "Ameland"),
    ("0074", "Schiermonnikoog"),
    ("0093", "Terschelling"),
    ("0400", "Texel"),
]

def main():
    successes = []
    failures = []

    for i, (code, naam) in enumerate(GEMEENTEN, 1):
        console.print(f"\n[bold cyan]=== [{i}/{len(GEMEENTEN)}] {naam} ({code}) ===[/bold cyan]")
        try:
            load_imtr_for(code, naam)
            successes.append((code, naam))
        except Exception as e:
            console.print(f"[red]FAILED: {e}[/red]")
            failures.append((code, naam, str(e)[:80]))

    # Werkzaamheden (landelijk, eenmalig)
    console.print("\n[bold cyan]=== Werkzaamheden (landelijk) ===[/bold cyan]")
    conn = get_conn()
    try:
        _load_werkzaamheden(conn)
    finally:
        conn.close()

    console.print(f"\n[bold green]Done: {len(successes)} succeeded, {len(failures)} failed[/bold green]")
    if failures:
        console.print("[yellow]Failures:[/yellow]")
        for code, naam, err in failures:
            console.print(f"  {naam} ({code}): {err}")

if __name__ == "__main__":
    main()
