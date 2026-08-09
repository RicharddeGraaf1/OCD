"""Ruim het volume op van ontwerpen/besluiten die de intake-toets niet meer halen.

Achtergrond: vault gaps.md G-123. `ontwerp_loader` beslist bij binnenkomst of een
ontwerp of besluitversie relevant is. Niets past die toets later opnieuw toe, dus
rijen komen binnen onder een voorwaarde en vertrekken niet als die vervalt.

Het criterium hier is **de intake-logica van de loader zelf**, niet een eigen
regel. Dat is geen esthetische keuze maar een noodzakelijke: verwijder je iets
wat de loader morgen weer binnenlaat, dan koop je churn — weggooien, opnieuw
downloaden, weggooien. Door precies te spiegelen wat `load_ontwerp` en
`load_besluitversie` vandaag zouden doen, is het resultaat stabiel.

    ontwerp        : _is_relevant(work, nieuwe_expression)
                     EN bekend_op >= datum van onze vigerende versie
    besluitversie  : _is_relevant(work, nieuwe_expression, begin_inwerking)

Wat blijft staan, en waarom:

* **De rij in `p2pwijziging.besluit` zelf.** Die is de enige bron van
  inwerkingtredingsdatum die we hebben (G-121 → G-108: 98 van de 124
  besluitversies matchen op een vigerende regeling). Alleen het volume gaat weg.
  Het is bovendien de enige rem op herladen: zonder die rij is er niets dat
  zegt "hier is al over besloten".
* **Alles wat aan een andere regeling hangt.** De OW-objecttabellen
  (`p2p.activiteit`, `locatie`, `norm`, `gebiedsaanwijzing`) worden hier niet
  aangeraakt — die hebben geen regeling-kolom en zijn gedeeld; gemeten op de
  inactieve regelingen was 0 van de 18 activiteiten en 0 van de 22 locaties
  exclusief.
* **De vectorlaag.** Nagemeten 2026-08-09: van de 469.666 chunks met
  `source_type='ontwerp'` hoort er **0** bij de te ruimen besluiten — die horen
  allemaal bij de 202 ontwerpen die blijven. Er staan wél 70.376 chunks op de
  expressies van deze besluiten, maar dat zijn gewone vigerende p2p-chunks
  (Artikel/Lid/Divisietekst) van de 98 expressies die inmiddels vigeren. Die
  moeten juist blijven. `v2a.wijziging_categorie`: 0 geraakte rijen.

Draai standaard als droogloop. `--uitvoeren` verwijdert pas echt.

    python scripts/ruim_wijzigingsspoor_op.py
    python scripts/ruim_wijzigingsspoor_op.py --uitvoeren
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from src.db import get_conn
from src.loaders.ontwerp_loader import _is_relevant, _huidige_versie_datum

console = Console()

# Volgorde is FK-volgorde: kinderen eerst, de besluit-rij raken we nooit aan.
AFHANKELIJK = [
    "juridische_regel_norm_delta",
    "juridische_regel_gebiedsaanwijzing_delta",
    "juridische_regel_activiteit_delta",
    "juridische_regel_delta",
    "annotatie_delta",
    "locatie_delta",
    "tekst_element",
    "procedurestap",
]


def _als_datum(x) -> date | None:
    return x.date() if isinstance(x, datetime) else x


def zou_vandaag_binnenlaten(conn, rij) -> bool:
    """Spiegelt de intake-logica van `load_ontwerp` / `load_besluitversie`."""
    inwerking = rij["begin_inwerking"] if rij["soort"] == "besluitversie" else None
    if not _is_relevant(conn, rij["regeling_work"], rij["nieuwe_expression"],
                        inwerking):
        return False
    if rij["soort"] == "ontwerp":
        huidige = _huidige_versie_datum(conn, rij["regeling_work"])
        bekend = _als_datum(rij["bekend_op"])
        if huidige and bekend and bekend < huidige:
            return False
    return True


def basis_verouderd(conn) -> set[str]:
    """Ontwerpen waarvan `wijzigt_expression` niet meer de vigerende versie is.

    Apart signaal, géén verwijdercriterium — de twee toetsen zijn het oneens
    over een flink deel van de ontwerpen en dat verschil hoort zichtbaar te zijn
    in plaats van weggemiddeld.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT b.ontwerpbesluit_id
              FROM p2pwijziging.besluit b
              LEFT JOIN p2p.regeling v
                     ON v.frbr_expression = b.wijzigt_expression AND NOT v.inactief
             WHERE b.wijzigt_expression IS NOT NULL AND v.frbr_expression IS NULL""")
        return {r["ontwerpbesluit_id"] for r in cur.fetchall()}


def bepaal_scope(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT ontwerpbesluit_id, soort, regeling_work,
                              wijzigt_expression, nieuwe_expression,
                              begin_inwerking, bekend_op, opschrift
                         FROM p2pwijziging.besluit""")
        rijen = cur.fetchall()

    oud = basis_verouderd(conn)
    weg, blijft = [], []
    for r in rijen:
        r["oude_basis"] = r["ontwerpbesluit_id"] in oud
        (blijft if zou_vandaag_binnenlaten(conn, r) else weg).append(r)
    return weg, blijft


def tel_afhankelijk(conn, ids: list[str]) -> dict[str, int]:
    uit = {}
    with conn.cursor() as cur:
        for tabel in AFHANKELIJK:
            cur.execute(f"""SELECT count(*) n FROM p2pwijziging.{tabel}
                             WHERE ontwerpbesluit_id = ANY(%s)""", (ids,))
            uit[tabel] = cur.fetchone()["n"]
    return uit


def rapporteer(conn, weg, blijft):
    t = Table(title="Zou de loader dit vandaag nog binnenlaten?")
    t.add_column("soort")
    t.add_column("blijft", justify="right")
    t.add_column("gaat weg", justify="right")
    t.add_column("waarvan oude basis", justify="right")
    for soort in ("ontwerp", "besluitversie"):
        w = [r for r in weg if r["soort"] == soort]
        b = [r for r in blijft if r["soort"] == soort]
        t.add_row(soort, str(len(b)), str(len(w)),
                  str(sum(1 for r in w if r["oude_basis"])))
    console.print(t)

    # Het signaal dat het verwijdercriterium NIET dekt.
    blijft_oud = [r for r in blijft if r["oude_basis"]]
    if blijft_oud:
        console.print(
            f"\n[yellow]Let op: {len(blijft_oud)} rijen blijven staan terwijl hun "
            f"basis-expressie niet meer vigeert.[/yellow]\n"
            "  De twee toetsen zijn het hier oneens: de loader kijkt of het ontwerp\n"
            "  jonger is dan onze vigerende versie, niet of het erop voortbouwt. Een\n"
            "  bronhouder die in juli een ontwerp publiceert op de januari-consolidatie\n"
            "  terwijl juni al vigeert, glipt daar doorheen. Ze hier weghalen zou\n"
            "  churn opleveren: de eerstvolgende sync haalt ze terug.")
        for r in blijft_oud[:5]:
            console.print(f"    {_als_datum(r['bekend_op'])}  "
                          f"{str(r['opschrift'])[:60]}")

    ids = [r["ontwerpbesluit_id"] for r in weg]
    if not ids:
        console.print("\n[green]Niets te doen.[/green]")
        return ids

    tellingen = tel_afhankelijk(conn, ids)
    t2 = Table(title="Wat er verdwijnt")
    t2.add_column("tabel")
    t2.add_column("weg", justify="right")
    t2.add_column("totaal", justify="right")
    t2.add_column("aandeel", justify="right")
    with conn.cursor() as cur:
        for tabel, n in tellingen.items():
            cur.execute(f"SELECT count(*) n FROM p2pwijziging.{tabel}")
            tot = cur.fetchone()["n"]
            t2.add_row(f"p2pwijziging.{tabel}", f"{n:,}", f"{tot:,}",
                       f"{100 * n / tot:.0f}%" if tot else "—")
    console.print(t2)
    console.print(f"[dim]De {len(ids)} rijen in p2pwijziging.besluit blijven staan "
                  f"— metadata en herlaad-rem.[/dim]")
    return ids


def verwijder(conn, ids: list[str]):
    with conn.cursor() as cur:
        for tabel in AFHANKELIJK:
            cur.execute(f"""DELETE FROM p2pwijziging.{tabel}
                             WHERE ontwerpbesluit_id = ANY(%s)""", (ids,))
            console.print(f"  {tabel}: {cur.rowcount} verwijderd")
    conn.commit()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uitvoeren", action="store_true",
                   help="verwijder echt (standaard is droogloop)")
    args = p.parse_args()

    conn = get_conn()
    try:
        weg, blijft = bepaal_scope(conn)
        ids = rapporteer(conn, weg, blijft)
        if not ids:
            return
        if not args.uitvoeren:
            console.print("\n[bold]Droogloop — er is niets verwijderd.[/bold] "
                          "Draai met --uitvoeren om door te zetten.")
            return
        console.print("\n[bold red]Verwijderen…[/bold red]")
        verwijder(conn, ids)
        console.print("[green]Klaar.[/green] Draai hierna VACUUM ANALYZE op "
                      "p2pwijziging als je de ruimte wilt terugzien.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
