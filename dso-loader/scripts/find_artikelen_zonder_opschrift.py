"""Diagnose: welke artikelen staan zonder opschrift in de database?

Achtergrond: tot voor kort parseerde `_parse_kop` in api_loader.py de
`<Opschrift>`-regex zonder `re.DOTALL`, waardoor multi-line of mixed-content
opschriften geruisloos op NULL eindigden. Dit script vindt vermoedelijke
slachtoffers: regelingen waar artikelen gemengd zijn (sommige getiteld,
sommige niet) zijn de meest waarschijnlijke kandidaten voor de bug.

Gebruik:
    cd dso-loader
    source .venv/Scripts/activate   (of .venv/bin/activate op Linux)
    python scripts/find_artikelen_zonder_opschrift.py
"""

from src.db import get_conn
from rich.console import Console
from rich.table import Table

console = Console()


def main():
    with get_conn() as conn, conn.cursor() as cur:
        # ── Overzicht per regeling ──
        # Alleen regelingen met >= 1 artikel mét opschrift én >= 1 zonder —
        # dat is het bug-patroon (volledig titelloze regelingen kunnen ook
        # "echt" titelloos zijn en zijn dus minder verdacht).
        cur.execute(
            """
            WITH artikel_stats AS (
                SELECT
                    regeling_expression,
                    COUNT(*) FILTER (WHERE opschrift IS NULL) AS zonder,
                    COUNT(*) FILTER (WHERE opschrift IS NOT NULL) AS met,
                    COUNT(*) AS totaal
                FROM p2p.tekst_element
                WHERE element_type = 'Artikel'
                GROUP BY regeling_expression
            )
            SELECT s.regeling_expression,
                   r.opschrift AS regeling_titel,
                   s.zonder,
                   s.met,
                   s.totaal,
                   ROUND(100.0 * s.zonder / s.totaal, 1) AS pct_zonder
            FROM artikel_stats s
            LEFT JOIN p2p.regeling r ON r.frbr_expression = s.regeling_expression
            WHERE s.zonder > 0 AND s.met > 0     -- alleen mixed = verdacht
            ORDER BY s.zonder DESC, pct_zonder DESC
            LIMIT 30
            """,
        )
        rows = cur.fetchall()

        if not rows:
            console.print("[green]Geen regelingen met gemengde artikel-opschriften gevonden.[/green]")
            console.print("Dat betekent: ofwel geen bug, ofwel alle regelingen zijn óf volledig getiteld óf volledig niet — controleer dan ook 'volledig titelloze' regelingen apart.")
            return

        table = Table(title="Verdachte regelingen — gemengd getitelde artikelen (top 30)")
        table.add_column("zonder", justify="right", style="red")
        table.add_column("met", justify="right", style="green")
        table.add_column("totaal", justify="right")
        table.add_column("% zonder", justify="right")
        table.add_column("regeling", overflow="fold")
        for r in rows:
            table.add_row(
                str(r["zonder"]),
                str(r["met"]),
                str(r["totaal"]),
                f"{r['pct_zonder']}%",
                r["regeling_titel"] or r["regeling_expression"],
            )
        console.print(table)

        # ── Top 5 voor close-up: welke nummers? ──
        console.print()
        console.print("[bold]Detail van top 5 — welke artikel-nummers ontbreken een opschrift:[/bold]")
        for r in rows[:5]:
            cur.execute(
                """
                SELECT nummer, eid
                FROM p2p.tekst_element
                WHERE regeling_expression = %s
                  AND element_type = 'Artikel'
                  AND opschrift IS NULL
                ORDER BY volgorde
                LIMIT 50
                """,
                (r["regeling_expression"],),
            )
            kale = cur.fetchall()
            console.print(f"\n[bold]{r['regeling_titel'] or r['regeling_expression']}[/bold]")
            nrs = ", ".join((k["nummer"] or "(geen nr)") for k in kale)
            extra = "" if len(kale) < 50 else " …(eerste 50)"
            console.print(f"  [yellow]{nrs}{extra}[/yellow]")

        # ── Globale tellingen voor context ──
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE element_type = 'Artikel' AND opschrift IS NULL) AS art_zonder,
                COUNT(*) FILTER (WHERE element_type = 'Artikel') AS art_totaal,
                COUNT(*) FILTER (WHERE element_type = 'Hoofdstuk' AND opschrift IS NULL) AS hfd_zonder,
                COUNT(*) FILTER (WHERE element_type = 'Hoofdstuk') AS hfd_totaal,
                COUNT(*) FILTER (WHERE element_type = 'Afdeling' AND opschrift IS NULL) AS afd_zonder,
                COUNT(*) FILTER (WHERE element_type = 'Afdeling') AS afd_totaal,
                COUNT(*) FILTER (WHERE element_type = 'Paragraaf' AND opschrift IS NULL) AS par_zonder,
                COUNT(*) FILTER (WHERE element_type = 'Paragraaf') AS par_totaal
            FROM p2p.tekst_element
            """,
        )
        g = cur.fetchone()
        console.print()
        console.print("[bold]Globaal — opschrift-loosheid per element-type:[/bold]")
        for label, zonder, totaal in [
            ("Artikel",   g["art_zonder"], g["art_totaal"]),
            ("Hoofdstuk", g["hfd_zonder"], g["hfd_totaal"]),
            ("Afdeling",  g["afd_zonder"], g["afd_totaal"]),
            ("Paragraaf", g["par_zonder"], g["par_totaal"]),
        ]:
            pct = (100.0 * zonder / totaal) if totaal else 0
            console.print(f"  {label:10s}: {zonder:>6}/{totaal:<6} ({pct:5.1f}% zonder opschrift)")


if __name__ == "__main__":
    main()
