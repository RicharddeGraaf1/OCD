"""Vul `p2pwijziging.besluit.citeertitel` met de citeertitel van het BESLUIT.

Achtergrond: de Presenteren-API kent twee titel-niveaus op een ontwerpregeling.
Top-level `citeerTitel` hoort bij de regeling ("Omgevingsplan gemeente Putten")
en is dus gelijk voor elk besluit op diezelfde regeling.
`besluitMetadata.citeerTitel` hoort bij het besluit zelf ("Wijziging
omgevingsplan gemeente Putten t.b.v. ontwikkeling Stenenkamerseweg 38/38a").
De loader las alleen het eerste veld; daardoor stond de kolom vol met
regelingsnamen en waren de drie Putten-ontwerpen in de viewer niet uit elkaar
te houden.

`ontwerp_loader` is gerepareerd (`_besluit_citeertitel`), inclusief de
`ON CONFLICT DO UPDATE` die `citeertitel` eerder niet ververste. Dit script
haalt de bestaande voorraad in zonder een volledige herload: het paginaert
alleen de goedkope listing en raakt uitsluitend de `citeertitel`-kolom —
geen documentstructuur, geen annotaties.

Alleen ontwerpen. Besluitversies dragen geen `besluitMetadata` (0 van 2812,
gemeten 2026-08-10); het veld staat alleen op het `Ontwerpregeling`-schema.

    python scripts/backfill_besluit_citeertitel.py            # droogloop
    python scripts/backfill_besluit_citeertitel.py --uitvoeren
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.loaders.ontwerp_loader import _besluit_citeertitel, _get

console = Console()


def citeertitels_uit_api() -> dict[str, str]:
    """ontwerpbesluitIdentificatie → citeertitel van het besluit.

    Paginaert de volledige `/ontwerpregelingen`-listing. Dat zijn ~1.000 items
    over ~11 pagina's; we filteren pas in de DB-stap op wat we werkelijk
    hebben, omdat de API geen filter op ontwerpbesluit-id kent.
    """
    uit: dict[str, str] = {}
    page = 1
    gezien = 0
    while True:
        data = _get(f"{cfg.PRESENTEREN_BASE}/ontwerpregelingen",
                    params={"page": page, "size": 100})
        items = data.get("_embedded", {}).get("ontwerpregelingen", [])
        if not items:
            break
        gezien += len(items)
        for item in items:
            ob_id = item.get("ontwerpbesluitIdentificatie")
            titel = _besluit_citeertitel(item)
            if ob_id and titel:
                uit[ob_id] = titel
        if not data.get("_links", {}).get("next", {}).get("href"):
            break
        page += 1

    console.print(f"[dim]{gezien} ontwerpregelingen gelezen, "
                  f"{len(uit)} met citeertitel[/dim]")
    return uit


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uitvoeren", action="store_true",
                   help="schrijf naar de database (zonder deze vlag: droogloop)")
    args = p.parse_args()

    titels = citeertitels_uit_api()
    if not titels:
        console.print("[red]Geen citeertitels opgehaald — API-probleem?[/red]")
        return 1

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ontwerpbesluit_id, opschrift, citeertitel
                FROM   p2pwijziging.besluit
                WHERE  soort = 'ontwerp'
            """)
            rijen = cur.fetchall()

        te_wijzigen = [
            (r["ontwerpbesluit_id"], r["citeertitel"], titels[r["ontwerpbesluit_id"]])
            for r in rijen
            if r["ontwerpbesluit_id"] in titels
            and (r["citeertitel"] or "") != titels[r["ontwerpbesluit_id"]]
        ]
        zonder_api = [r for r in rijen if r["ontwerpbesluit_id"] not in titels]

        console.print(f"{len(rijen)} ontwerpen in de DB · "
                      f"{len(te_wijzigen)} krijgen een andere citeertitel · "
                      f"{len(zonder_api)} zonder besluitMetadata (blijven ongemoeid)")
        for ob_id, oud, nieuw in te_wijzigen[:10]:
            # ASCII-pijl: de Windows-console draait op cp1252 en struikelt
            # over U+2192.
            console.print(f"  [dim]{ob_id[-12:]}[/dim] {oud!r} -> [green]{nieuw!r}[/green]")
        if len(te_wijzigen) > 10:
            console.print(f"  [dim]… en nog {len(te_wijzigen) - 10}[/dim]")

        if not args.uitvoeren:
            console.print("\n[yellow]Droogloop — niets geschreven. "
                          "Draai met --uitvoeren.[/yellow]")
            return 0

        with conn.cursor() as cur:
            for ob_id, _oud, nieuw in te_wijzigen:
                cur.execute(
                    "UPDATE p2pwijziging.besluit SET citeertitel = %s "
                    "WHERE ontwerpbesluit_id = %s",
                    (nieuw, ob_id))
        conn.commit()
        console.print(f"\n[bold green]{len(te_wijzigen)} rijen bijgewerkt[/bold green]")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
