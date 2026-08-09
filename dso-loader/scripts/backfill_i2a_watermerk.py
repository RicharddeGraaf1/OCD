"""Vul `i2a.toepasbaar_regelbestand.laatste_wijziging` voor wat we in april laadden.

**Eén kolom in één tabel.** Dit is geen inhoudelijke backfill: er wordt geen
regel, DMN-element of uitvoeringsregel aangeraakt. Het enige dat wordt
geschreven is het watermerk waarmee de delta van 2026-08-08 bepaalt of een
DMN-bestand opnieuw moet worden opgehaald.

Waarom het nodig is: het watermerk is pas voor 148 van de 59.646 regelbestanden
gevuld (de gm1699-inhaalronde). Voor de rest is hij `NULL`, dus de eerstvolgende
i2a-fase haalt ~59.500 XML's opnieuw op — ~5,6 uur. Mét watermerk is dat ~20 min.

Waarom het waarheidsgetrouw is: we vragen de STTR om de
`laatsteWijzigingDatum` **op peildatum 10-04-2026** — de datum waarop die
bestanden zijn geladen. We leggen dus vast wat er werkelijk in de database
staat, niet wat er vandaag geldt. De eerstvolgende run draait op peildatum
vandaag en haalt daardoor precies de bestanden op die sindsdien zijn gewijzigd
(~1,7%, gemeten 2026-08-09).

Twee guards, want een watermerk is een claim over dekking:

* **Alleen bestanden die aantoonbaar inhoud hebben** in `i2a.dmn_element` of
  `i2a.uitvoeringsregel`. Gemeten: 59.598 van de 59.646 (99,92%); de 48 zonder
  inhoud krijgen géén watermerk en worden dus gewoon opgehaald.
  Let op: meet over **beide** tabellen — alleen `dmn_element` tellen geeft
  22.599 schijnbaar lege bestanden, want *Maatregelen*-bestanden bevatten per
  ontwerp geen `<semantic:decision>`, alleen uitvoeringsregels.
* **Alleen waar het watermerk nu leeg is.** Een bestaande waarde is door een
  geslaagde verwerking gezet en wordt nooit overschreven.

    python scripts/backfill_i2a_watermerk.py             # droogloop
    python scripts/backfill_i2a_watermerk.py --uitvoeren
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.loaders.imtr_loader import _api_get, _api_post, _rtr_organisatiecode

console = Console()

PEILDATUM_LADING = "10-04-2026"   # de peildatum waarop deze inhoud is geladen


def bronhouders(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT bronhouder FROM p2p.regeling
                        WHERE bronhouder IS NOT NULL ORDER BY bronhouder""")
        return [_rtr_organisatiecode(r["bronhouder"]) for r in cur.fetchall()]


def met_inhoud(conn) -> set[str]:
    """Regelbestanden waarvan de inhoud aantoonbaar geladen is."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.namespace
              FROM i2a.toepasbaar_regelbestand t
             WHERE t.laatste_wijziging IS NULL
               AND (EXISTS (SELECT 1 FROM i2a.dmn_element e
                             WHERE e.regelbestand_ns = t.namespace)
                 OR EXISTS (SELECT 1 FROM i2a.uitvoeringsregel u
                             WHERE u.regelbestand_ns = t.namespace))""")
        return {r["namespace"] for r in cur.fetchall()}


def sttr_lijst(oin: str) -> dict[str, str]:
    uit, page = {}, 1
    while True:
        r = _api_get(cfg.STTR_BASE, "/toepasbareRegels",
                     {"datum": PEILDATUM_LADING, "oin": oin,
                      "pageSize": 50, "page": page})
        emb = r.get("_embedded", {})
        items = emb.get("toepasbareRegelsList", []) or emb.get("toepasbareRegels", [])
        for i in items:
            ns, wz = i.get("functioneleStructuurRef"), i.get("laatsteWijzigingDatum")
            if ns and wz:
                uit[ns] = wz
        tot = r.get("page", {}).get("totalElements", 0)
        if not items or len(uit) >= tot:
            return uit
        page += 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uitvoeren", action="store_true")
    args = p.parse_args()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) n FROM i2a.toepasbaar_regelbestand")
            totaal = cur.fetchone()["n"]
            cur.execute("""SELECT count(*) n FROM i2a.toepasbaar_regelbestand
                            WHERE laatste_wijziging IS NULL""")
            leeg = cur.fetchone()["n"]

        kandidaat = met_inhoud(conn)
        console.print(f"{totaal} regelbestanden · {leeg} zonder watermerk · "
                      f"[bold]{len(kandidaat)} daarvan met aantoonbare inhoud[/bold] "
                      f"({leeg - len(kandidaat)} zonder inhoud blijven leeg)\n")

        codes = bronhouders(conn)
        gezet = geen_datum = niet_in_lijst = fout = 0
        for i, code in enumerate(codes, 1):
            try:
                r = _api_post(cfg.RTR_BASE, "/activiteiten/_zoek",
                              {"datum": PEILDATUM_LADING,
                               "bestuursorgaan": {"organisatieCode": code},
                               "pageSize": 200, "page": 1})
                acts = r.get("_embedded", {}).get("activiteiten", [])
                if not acts:
                    continue
                oin = acts[0].get("bestuursorgaan", {}).get("oin", "")
                if not oin:
                    continue
                lijst = sttr_lijst(oin)
            except Exception as e:
                fout += 1
                console.print(f"  [yellow]{code}: {type(e).__name__}[/yellow]")
                continue

            with conn.cursor() as cur:
                for ns in kandidaat & lijst.keys():
                    if args.uitvoeren:
                        cur.execute(
                            """UPDATE i2a.toepasbaar_regelbestand
                                  SET laatste_wijziging = %s
                                WHERE namespace = %s AND laatste_wijziging IS NULL""",
                            (lijst[ns], ns))
                        gezet += cur.rowcount or 0
                    else:
                        gezet += 1
            if args.uitvoeren:
                conn.commit()
            if i % 50 == 0:
                console.print(f"[dim]  … {i}/{len(codes)} bronhouders, "
                              f"{gezet} watermerken[/dim]")

        console.print(f"\n[bold]{gezet} watermerken "
                      f"{'gezet' if args.uitvoeren else 'te zetten'}[/bold]")
        if fout:
            console.print(f"[yellow]{fout} bronhouders gaven een fout[/yellow]")
        if not args.uitvoeren:
            console.print("\n[bold]Droogloop — niets geschreven.[/bold]")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
