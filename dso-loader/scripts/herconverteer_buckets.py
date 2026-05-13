"""Herconverteer alle bestemmingsplannen na de Lichaam/Bijlage/Toelichting-fix.

Eerder geconverteerde plannen missen de top-level containers die de viewer
nodig heeft om de tabs Regels/Bijlagen/Toelichting te vullen. Dit script:

  1. Loopt per gemeente over alle plannen die al in `conv.regeling` staan.
  2. Wist daar de oude conv-data voor.
  3. Voert stap 1 (mechanisch) opnieuw uit met de nieuwe converter.

GEEN IHR-API-verkeer: alle data komt al uit `wro.*` (lokaal). Dit is puur
database-werk binnen Postgres en heeft geen externe afhankelijkheden.

Gebruik:
    cd dso-loader
    source .venv/Scripts/activate
    PYTHONPATH=. python scripts/herconverteer_buckets.py            # alles
    PYTHONPATH=. python scripts/herconverteer_buckets.py --code 1980  # één gemeente
    PYTHONPATH=. python scripts/herconverteer_buckets.py --dry-run    # toon plan
"""

import argparse
import os
import sys
from datetime import datetime

# Fix Windows console encoding
os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from src.db import get_conn
from src.converter.stap1 import clear_gemeente, convert_bestemmingsplan

console = Console()


def gemeenten_met_conv() -> list[dict]:
    """Alle gemeenten waarvoor er conv-data bestaat (kandidaten voor reconvertie).

    `bronhouder` is in zowel `conv.regeling` als `wro.ruimtelijk_instrument`
    opgeslagen met `gm`-prefix (bv. "gm1980"). De `--code`-flag accepteert
    beide schrijfwijzen en normaliseert naar de prefixed vorm.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.bronhouder AS code,
                       coalesce(b.naam, r.bronhouder) AS naam,
                       count(DISTINCT r.frbr_expression) AS plannen
                FROM conv.regeling r
                LEFT JOIN core.bronhouder b
                       ON b.overheidscode = regexp_replace(r.bronhouder, '^gm', '')
                GROUP BY r.bronhouder, b.naam
                ORDER BY 2
            """)
            return cur.fetchall()
    finally:
        conn.close()


def plannen_van_gemeente(bronhouder_code: str) -> list[dict]:
    """Alle vigerende plannen mét tekst voor één gemeente."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ri.idn, ri.naam
                FROM wro.ruimtelijk_instrument ri
                JOIN wro.wro_tekst_object wt ON wt.instrument_idn = ri.idn
                WHERE ri.bronhouder = %s AND ri.planstatus = 'vastgesteld'
                GROUP BY ri.idn, ri.naam
                HAVING count(wt.identificatie) > 0
                ORDER BY ri.naam
            """, (bronhouder_code,))
            return cur.fetchall()
    finally:
        conn.close()


def _vacuum_orphan_meta() -> int:
    """Wis hangende `conv.conversie_meta`-rijen waarvan de regeling weg is.

    `clear_gemeente` mist deze opruim-stap door een mismatch in het LIKE-
    patroon (instrument_idn heeft géén `gm`-prefix, bronhouder wel), dus we
    schoonvegen hier proactief alle meta zonder bijhorende regeling.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM conv.conversie_meta cm
                WHERE NOT EXISTS (
                    SELECT 1 FROM conv.regeling r
                    WHERE r.frbr_expression = cm.regeling_expression
                )
            """)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def reconverteer_gemeente(gem: dict) -> dict:
    """Wis + reconverteer per plan. Returns stats-dict."""
    code = gem["code"]
    plannen = plannen_van_gemeente(code)
    if not plannen:
        return {"plannen_ok": 0, "plannen_err": 0}

    cleared = clear_gemeente(code)
    orphans = _vacuum_orphan_meta()
    console.print(f"  [dim]{cleared} oude regelingen + {orphans} verweesde meta-rijen gewist[/dim]")

    ok = 0
    err = 0
    for plan in plannen:
        try:
            convert_bestemmingsplan(plan["idn"])
            ok += 1
        except Exception as e:
            console.print(f"    [red]{plan['idn']}: {e}[/red]")
            err += 1

    return {"plannen_ok": ok, "plannen_err": err}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", help="Beperk tot één bronhouder-code (bv. 1980)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Toon alleen wat er zou gebeuren, voer niets uit")
    args = parser.parse_args()

    start = datetime.now()
    gemeenten = gemeenten_met_conv()
    if args.code:
        wanted = args.code if args.code.startswith("gm") else f"gm{args.code}"
        gemeenten = [g for g in gemeenten if g["code"] == wanted]
        if not gemeenten:
            console.print(f"[red]Geen conv-data voor bronhouder {wanted}[/red]")
            sys.exit(1)

    totaal_plannen = sum(g["plannen"] for g in gemeenten)
    console.print(f"[bold]{len(gemeenten)} gemeenten, ~{totaal_plannen} plannen herconverteren[/bold]")
    console.print(f"[dim]Start: {start.strftime('%H:%M:%S')}[/dim]")

    if args.dry_run:
        tbl = Table(title="Dry run — geen wijzigingen")
        tbl.add_column("Code")
        tbl.add_column("Gemeente")
        tbl.add_column("Plannen", justify="right")
        for g in gemeenten[:50]:
            tbl.add_row(g["code"], g["naam"], str(g["plannen"]))
        if len(gemeenten) > 50:
            tbl.add_row("…", f"+{len(gemeenten) - 50} gemeenten", "…")
        console.print(tbl)
        return

    totals = {"gemeenten_ok": 0, "gemeenten_err": 0,
              "plannen_ok": 0, "plannen_err": 0}

    for i, gem in enumerate(gemeenten, 1):
        console.rule(f"[bold cyan][{i}/{len(gemeenten)}] {gem['naam']} ({gem['code']}) — {gem['plannen']} plannen[/bold cyan]")
        try:
            stats = reconverteer_gemeente(gem)
            totals["plannen_ok"] += stats["plannen_ok"]
            totals["plannen_err"] += stats["plannen_err"]
            totals["gemeenten_ok"] += 1
            console.print(f"  [green]{stats['plannen_ok']} plannen[/green]")
        except Exception as e:
            console.print(f"  [red]Gemeente-fout: {e}[/red]")
            totals["gemeenten_err"] += 1

    elapsed = datetime.now() - start
    console.rule("[bold]Herconversie voltooid[/bold]")
    tbl = Table(title="Resultaat")
    tbl.add_column("Component")
    tbl.add_column("Aantal", justify="right")
    tbl.add_row("Gemeenten verwerkt", str(totals["gemeenten_ok"]))
    tbl.add_row("Gemeenten fouten", str(totals["gemeenten_err"]))
    tbl.add_row("Plannen geconverteerd", str(totals["plannen_ok"]))
    tbl.add_row("Plannen fouten", str(totals["plannen_err"]))
    tbl.add_row("Doorlooptijd", str(elapsed).split(".")[0])
    console.print(tbl)


if __name__ == "__main__":
    main()
