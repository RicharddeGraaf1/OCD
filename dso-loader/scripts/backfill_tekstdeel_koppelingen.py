"""Vul `tekstdeel_hoofdlijn` en `tekstdeel_gebiedsaanwijzing` alsnog.

Achtergrond: vault gaps.md G-124. De Presenteren-API levert per tekstdeel
`hoofdlijnRefs` en `gebiedsaanwijzingRefs`, maar `load_divisieannotaties` schreef
ze allebei niet weg. Gevolg: `tekstdeel_hoofdlijn` stond landelijk op 0 rijen en
1.942 van de 4.828 gebiedsaanwijzingen (40%) leken nergens aan te hangen.

De loader is gerepareerd; dit script haalt de bestaande voorraad in. Bron is de
API en niet de lokale ZIP's, want die zijn er maar voor een deel van de
regelingen — de API-route is dezelfde die de sync gebruikt.

Alleen regelingen met vrijetekststructuur hebben divisieannotaties. In plaats
van alle 2.000 te bevragen, gaan we uit van wat er al aan tekstdelen in de DB
staat: heeft een regeling geen tekstdeel, dan valt er ook niets te koppelen.

    python scripts/backfill_tekstdeel_koppelingen.py            # droogloop
    python scripts/backfill_tekstdeel_koppelingen.py --uitvoeren
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.loaders.api_loader import _get, _encode_regeling_uri

console = Console()


def kandidaten(conn) -> list[dict]:
    """Regelingen die tekstdelen hebben — alleen daar valt iets te koppelen.

    De join loopt via `p2p.tekstdeel.identificatie`, dat de bronhoudercode
    bevat (`nl.imow-gm0047.tekstdeel.…`). `tekstdeel` heeft namelijk geen
    regeling-kolom; dat is dezelfde losse koppeling als in G-118.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT r.frbr_work, r.bronhouder, r.citeertitel,
                   count(t.identificatie) AS n_tekstdeel
              FROM p2p.regeling r
              JOIN p2p.tekstdeel t
                ON t.identificatie LIKE 'nl.imow-' || r.bronhouder || '.tekstdeel.%'
             WHERE NOT r.inactief
             GROUP BY 1, 2, 3
             ORDER BY 4 DESC""")
        return cur.fetchall()


def verwerk(conn, work: str, schrijf: bool) -> tuple[int, int, int]:
    """Haal de divisieannotaties op en tel/schrijf de twee koppelingen."""
    data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{_encode_regeling_uri(work)}"
                f"/divisieannotaties", params={"locatieSelectie": "primair"})
    hl = ga = mis = 0
    with conn.cursor() as cur:
        for td in data.get("tekstdelen", []):
            tid = td["identificatie"]
            for ref in td.get("hoofdlijnRefs", []):
                if schrijf:
                    cur.execute(
                        """INSERT INTO p2p.tekstdeel_hoofdlijn (tekstdeel_id, hoofdlijn_id)
                           SELECT %s, %s
                            WHERE EXISTS (SELECT 1 FROM p2p.tekstdeel WHERE identificatie=%s)
                              AND EXISTS (SELECT 1 FROM p2p.hoofdlijn WHERE identificatie=%s)
                           ON CONFLICT DO NOTHING""", (tid, ref, tid, ref))
                    hl += cur.rowcount or 0
                    mis += 0 if cur.rowcount else 1
                else:
                    hl += 1
            for ref in td.get("gebiedsaanwijzingRefs", []):
                if schrijf:
                    cur.execute(
                        """INSERT INTO p2p.tekstdeel_gebiedsaanwijzing
                               (tekstdeel_id, gebiedsaanwijzing_id)
                           SELECT %s, %s
                            WHERE EXISTS (SELECT 1 FROM p2p.tekstdeel WHERE identificatie=%s)
                              AND EXISTS (SELECT 1 FROM p2p.gebiedsaanwijzing WHERE identificatie=%s)
                           ON CONFLICT DO NOTHING""", (tid, ref, tid, ref))
                    ga += cur.rowcount or 0
                    mis += 0 if cur.rowcount else 1
                else:
                    ga += 1
    if schrijf:
        conn.commit()
    return hl, ga, mis


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uitvoeren", action="store_true")
    p.add_argument("--limiet", type=int, default=0, help="stop na N regelingen")
    args = p.parse_args()

    conn = get_conn()
    try:
        regs = kandidaten(conn)
        if args.limiet:
            regs = regs[:args.limiet]
        console.print(f"{len(regs)} regelingen met tekstdelen\n")

        tot_hl = tot_ga = tot_mis = fout = 0
        for i, r in enumerate(regs, 1):
            try:
                hl, ga, mis = verwerk(conn, r["frbr_work"], args.uitvoeren)
            except Exception as e:
                fout += 1
                console.print(f"  [yellow]{r['bronhouder']}: {type(e).__name__} "
                              f"{str(e)[:70]}[/yellow]")
                conn.rollback()
                continue
            tot_hl += hl
            tot_ga += ga
            tot_mis += mis
            if hl or ga:
                console.print(f"  {r['bronhouder']} {str(r['citeertitel'])[:44]:<46} "
                              f"hl={hl:<4} ga={ga}")
            if i % 100 == 0:
                console.print(f"[dim]  … {i}/{len(regs)}[/dim]")

        console.print(f"\n[bold]{tot_hl} hoofdlijn-koppelingen, {tot_ga} "
                      f"gebiedsaanwijzing-koppelingen[/bold]")
        if tot_mis:
            console.print(f"[yellow]{tot_mis} verwijzingen zonder doel in de DB "
                          f"(object niet geladen)[/yellow]")
        if fout:
            console.print(f"[yellow]{fout} regelingen gaven een fout[/yellow]")
        if not args.uitvoeren:
            console.print("\n[bold]Droogloop — niets geschreven.[/bold]")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
