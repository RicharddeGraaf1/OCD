"""Landelijke conversie: stap 1 (mechanisch) + matcher voor alle gemeenten.

Geen LLM — puur structuurconversie + keyword-matching tegen bestaande
activiteiten en werkzaamheden.

Gebruik:
    cd dso-loader
    source .venv/Scripts/activate
    PYTHONPATH=. python scripts/convert_landelijk.py
"""

import os
import sys

# Fix Windows console encoding
os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import time
from datetime import datetime

from rich.console import Console
from rich.table import Table

from src.db import get_conn
from src.converter.stap1 import convert_bestemmingsplan, clear_gemeente
from src.converter.matcher import ActivityMatcher, match_bestemmingsplan, persist_matches

console = Console()


def get_gemeenten_met_plannen() -> list[dict]:
    """Alle gemeenten met vigerende bestemmingsplannen + tekst."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ri.bronhouder AS code, b.naam,
                       count(DISTINCT ri.idn) AS plannen
                FROM wro.ruimtelijk_instrument ri
                JOIN wro.wro_tekst_object wt ON wt.instrument_idn = ri.idn
                JOIN core.bronhouder b ON b.overheidscode = ri.bronhouder
                WHERE ri.planstatus = 'vastgesteld'
                GROUP BY ri.bronhouder, b.naam
                ORDER BY b.naam
            """)
            return cur.fetchall()
    finally:
        conn.close()


def run_stap1_gemeente(bronhouder_code: str) -> dict:
    """Stap 1 voor alle plannen van een gemeente. Returns stats."""
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
            """, (bronhouder_code,))
            plannen = cur.fetchall()
    finally:
        conn.close()

    ok = 0
    err = 0
    for plan in plannen:
        try:
            convert_bestemmingsplan(plan["idn"])
            ok += 1
        except Exception as e:
            console.print(f"    [red]{plan['idn']}: {e}[/red]")
            err += 1

    return {"ok": ok, "err": err}


def run_matcher_gemeente(bronhouder_code: str, matcher: ActivityMatcher) -> int:
    """Matcher voor alle geconverteerde plannen van een gemeente."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.frbr_expression, r.bronhouder
                FROM conv.regeling r
                WHERE r.bronhouder = %s
            """, (bronhouder_code,))
            regelingen = cur.fetchall()

            total_persisted = 0
            for reg in regelingen:
                # Haal inhoudelijke artikelen
                cur.execute("""
                    SELECT te.id, te.eid, te.wid, te.nummer, te.opschrift, te.inhoud
                    FROM conv.tekst_element te
                    LEFT JOIN conv.tekst_element parent ON parent.id = te.parent_id
                    WHERE te.regeling_expression = %s
                      AND te.element_type = 'Artikel'
                      AND te.inhoud IS NOT NULL
                      AND LENGTH(te.inhoud) > 20
                      AND (parent.opschrift IS NULL OR parent.opschrift != 'Begrippen')
                    ORDER BY te.volgorde
                """, (reg["frbr_expression"],))
                artikelen = cur.fetchall()

                for art in artikelen:
                    tekst = art["inhoud"] or ""
                    matches = matcher.match_artikel(tekst, min_score=0.5)
                    strong = [m for m in matches if m.score >= 0.7]
                    if strong:
                        n = persist_matches(conn, reg["frbr_expression"],
                                          art, strong, reg["bronhouder"])
                        total_persisted += n

            conn.commit()
            return total_persisted
    finally:
        conn.close()


def main():
    start = datetime.now()
    gemeenten = get_gemeenten_met_plannen()
    console.print(f"[bold]{len(gemeenten)} gemeenten, stap 1 + matcher[/bold]")
    console.print(f"[dim]Start: {start.strftime('%H:%M:%S')}[/dim]")

    # Laad matcher eenmalig (2.800+ activiteiten + 291 werkzaamheden)
    conn = get_conn()
    matcher = ActivityMatcher(conn)
    conn.close()

    totals = {"gemeenten_ok": 0, "gemeenten_err": 0,
              "plannen_ok": 0, "plannen_err": 0, "matches": 0}

    for i, gem in enumerate(gemeenten, 1):
        console.rule(f"[bold cyan][{i}/{len(gemeenten)}] {gem['naam']} ({gem['code']}) — {gem['plannen']} plannen[/bold cyan]")

        # Skip gemeenten die al geconverteerd zijn
        conn_check = get_conn()
        try:
            with conn_check.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM conv.regeling WHERE bronhouder = %s",
                            (gem["code"],))
                already = cur.fetchone()["n"]
            if already > 0:
                totals["plannen_ok"] += already
                totals["gemeenten_ok"] += 1
                continue
        finally:
            conn_check.close()

        # Stap 1: mechanische conversie
        try:
            stats = run_stap1_gemeente(gem["code"])
            totals["plannen_ok"] += stats["ok"]
            totals["plannen_err"] += stats["err"]
        except Exception as e:
            try:
                console.print(f"  [red]Stap 1 fout: {e}[/red]")
            except UnicodeEncodeError:
                print(f"  Stap 1 fout: {str(e)[:100]}", file=sys.stderr)
            totals["gemeenten_err"] += 1
            continue

        # Matcher
        try:
            n_matches = run_matcher_gemeente(gem["code"], matcher)
            totals["matches"] += n_matches
            if n_matches > 0:
                try:
                    console.print(f"  [green]Matcher: {n_matches} activiteiten gepersisteerd[/green]")
                except UnicodeEncodeError:
                    pass
        except Exception as e:
            try:
                console.print(f"  [red]Matcher fout: {e}[/red]")
            except UnicodeEncodeError:
                pass

        totals["gemeenten_ok"] += 1

    elapsed = datetime.now() - start
    console.print()
    console.rule("[bold]Landelijke conversie voltooid[/bold]")

    tbl = Table(title="Resultaat")
    tbl.add_column("Component")
    tbl.add_column("Aantal", justify="right")
    tbl.add_row("Gemeenten verwerkt", str(totals["gemeenten_ok"]))
    tbl.add_row("Gemeenten fouten", str(totals["gemeenten_err"]))
    tbl.add_row("Plannen geconverteerd", str(totals["plannen_ok"]))
    tbl.add_row("Plannen fouten", str(totals["plannen_err"]))
    tbl.add_row("Activiteiten (matcher)", str(totals["matches"]))
    tbl.add_row("Doorlooptijd", str(elapsed).split(".")[0])
    console.print(tbl)


if __name__ == "__main__":
    main()
