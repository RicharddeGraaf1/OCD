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

**Twee bronnen, twee soorten.** Ontwerpen komen uit de Presenteren-listing
(`besluitMetadata.citeerTitel`). Besluitversies dragen dat veld niet — daar
komt de naam uit de Ontsluiten-API, de API achter het Omgevingsloket
(`_ontsluiten_citeertitel`). Andersom voegt Ontsluiten voor ontwerpen niets
toe: van de 65 ontwerpen zonder `besluitMetadata` heeft er daar 0 een naam
(gemeten 2026-08-10).

    python scripts/backfill_besluit_citeertitel.py                    # droogloop, beide
    python scripts/backfill_besluit_citeertitel.py --uitvoeren
    python scripts/backfill_besluit_citeertitel.py --soort ontwerp    # één soort
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.loaders.ontwerp_loader import (
    _besluit_citeertitel,
    _get,
    _ontsluiten_citeertitel,
)

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


def _ontwerp_wijzigingen(conn) -> list[tuple[str, str | None, str]]:
    """(ontwerpbesluit_id, oud, nieuw) voor ontwerpen, uit de Presenteren-listing."""
    titels = citeertitels_uit_api()
    if not titels:
        raise RuntimeError("geen citeertitels uit de Presenteren-listing — API-probleem?")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT ontwerpbesluit_id, opschrift, citeertitel
            FROM   p2pwijziging.besluit
            WHERE  soort = 'ontwerp'
        """)
        rijen = cur.fetchall()

    zonder_api = sum(1 for r in rijen if r["ontwerpbesluit_id"] not in titels)
    console.print(f"{len(rijen)} ontwerpen in de DB · "
                  f"{zonder_api} zonder besluitMetadata (blijven ongemoeid)")
    return [
        (r["ontwerpbesluit_id"], r["citeertitel"], titels[r["ontwerpbesluit_id"]])
        for r in rijen
        if r["ontwerpbesluit_id"] in titels
        and (r["citeertitel"] or "") != titels[r["ontwerpbesluit_id"]]
    ]


def _besluitversie_wijzigingen(conn) -> list[tuple[str, str | None, str]]:
    """Idem voor besluitversies, één call per besluit bij de Ontsluiten-API.

    Geen listing beschikbaar: `/documenten/_zoek` geeft op een work- of
    uriIdentificatie alleen de geconsolideerde versie terug, niet de
    besluitversies. Vandaar per stuk. Met een korte pauze ertussen — deze host
    rate-limit steviger dan de rest (65 calls zonder pauze gaf 52 fouten).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ontwerpbesluit_id, technisch_id, opschrift, citeertitel
            FROM   p2pwijziging.besluit
            WHERE  soort = 'besluitversie'
            ORDER  BY bekend_op DESC NULLS LAST
        """)
        rijen = cur.fetchall()

    uit, zonder = [], 0
    for i, r in enumerate(rijen, 1):
        titel = _ontsluiten_citeertitel(r["technisch_id"])
        time.sleep(0.4)
        if not titel:
            zonder += 1
            continue
        if (r["citeertitel"] or "") != titel:
            uit.append((r["ontwerpbesluit_id"], r["citeertitel"], titel))
        if i % 25 == 0:
            console.print(f"[dim]  {i}/{len(rijen)} besluitversies bevraagd[/dim]")

    console.print(f"{len(rijen)} besluitversies in de DB · "
                  f"{zonder} zonder besluitCiteertitel (blijven ongemoeid)")
    return uit


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uitvoeren", action="store_true",
                   help="schrijf naar de database (zonder deze vlag: droogloop)")
    p.add_argument("--soort", choices=["ontwerp", "besluitversie", "beide"],
                   default="beide", help="welke soort bijwerken (default: beide)")
    args = p.parse_args()

    conn = get_conn()
    try:
        te_wijzigen: list[tuple[str, str | None, str]] = []
        if args.soort in ("ontwerp", "beide"):
            te_wijzigen += _ontwerp_wijzigingen(conn)
        if args.soort in ("besluitversie", "beide"):
            te_wijzigen += _besluitversie_wijzigingen(conn)

        console.print(f"\n[bold]{len(te_wijzigen)} rijen krijgen een andere "
                      f"citeertitel[/bold]")
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
