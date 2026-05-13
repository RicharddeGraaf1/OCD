"""Herlaad de documentstructuur van één of meerdere regelingen om opschriften
te repareren.

Achtergrond: voorheen parseerde `_parse_kop` zonder `re.DOTALL`, waardoor
multi-line of mixed-content opschriften op NULL eindigden. De fix in
api_loader.py raakt alleen nieuwe inserts (de bestaande upsert is
ON CONFLICT DO NOTHING). Dit script verwijdert de bestaande tekst_element-
rijen voor de geselecteerde regeling(en) en laadt ze opnieuw met de
gecorrigeerde parser.

Veiligheid:
  - juridische_regel joint naar tekst_element via `regeltekst_wid` (TEXT,
    geen FK). De wId's zijn stabiel tussen loads, dus joins blijven werken.
  - parent_id-referenties binnen tekst_element worden via ON DELETE CASCADE
    netjes opgeruimd; load_documentstructuur bouwt ze daarna opnieuw op.
  - inhoud_plain (gebruikt door FTS) is een GENERATED ALWAYS column en wordt
    automatisch gevuld door Postgres bij INSERT — geen backfill nodig.

Gebruik:
    cd dso-loader
    .venv/Scripts/activate    (of .venv/bin/activate)

    # Eén regeling
    python scripts/herlaad_documentstructuur.py --titel "Omgevingsverordening provincie Utrecht"
    python scripts/herlaad_documentstructuur.py --expression "/akn/nl/act/..."

    # Cluster met exact dezelfde artikel-signature (bruidsschat = 13:305)
    python scripts/herlaad_documentstructuur.py --alle-met-exact 13:305 --dry-run
    python scripts/herlaad_documentstructuur.py --alle-met-exact 13:305

    # Alle regelingen met gemengde artikel-stats (zonder > 0 EN met > 0)
    python scripts/herlaad_documentstructuur.py --alle-mixed --dry-run

    # Alle regelingen met "verdachte" artikelen — opschrift NULL maar inhoud
    # gevuld en geen Gereserveerd/Vervallen-marker. Dat is de beste indicator
    # voor de regex-bug en filtert legitieme stubs eruit.
    python scripts/herlaad_documentstructuur.py --alle-verdacht --dry-run
"""

import argparse
import sys

from rich.console import Console
from rich.table import Table

from src.db import get_conn
from src.loaders.api_loader import load_documentstructuur

console = Console()


# ── Lookup ────────────────────────────────────────────────────────────

def _vind_regeling(cur, titel: str | None, expression: str | None) -> dict | None:
    """Zoek één regeling op titel of expression. Bij meerdere matches: error."""
    if expression:
        cur.execute(
            "SELECT frbr_expression, frbr_work, opschrift FROM p2p.regeling "
            "WHERE frbr_expression = %s",
            (expression,),
        )
        return cur.fetchone()

    cur.execute(
        "SELECT frbr_expression, frbr_work, opschrift FROM p2p.regeling "
        "WHERE opschrift ILIKE %s",
        (titel,),
    )
    rows = cur.fetchall()
    if len(rows) == 0:
        cur.execute(
            "SELECT frbr_expression, frbr_work, opschrift FROM p2p.regeling "
            "WHERE opschrift ILIKE %s LIMIT 10",
            (f"%{titel}%",),
        )
        rows = cur.fetchall()
    if len(rows) == 0:
        return None
    if len(rows) > 1:
        console.print(f"[red]Meerdere regelingen matchen '{titel}':[/red]")
        for r in rows:
            console.print(f"  • {r['opschrift']}  ({r['frbr_expression']})")
        console.print("[yellow]Geef --expression mee om er precies één te kiezen.[/yellow]")
        sys.exit(2)
    return rows[0]


def _vind_cluster_exact(cur, zonder: int, totaal: int) -> list[dict]:
    """Alle regelingen met exact (zonder, totaal) artikelen — bruidsschat-cluster."""
    cur.execute(
        """
        WITH stats AS (
            SELECT regeling_expression,
                   COUNT(*) FILTER (WHERE opschrift IS NULL) AS zonder,
                   COUNT(*) AS totaal
            FROM p2p.tekst_element
            WHERE element_type = 'Artikel'
            GROUP BY regeling_expression
        )
        SELECT r.frbr_expression, r.frbr_work, r.opschrift,
               s.zonder, s.totaal
        FROM stats s
        JOIN p2p.regeling r ON r.frbr_expression = s.regeling_expression
        WHERE s.zonder = %s AND s.totaal = %s
        ORDER BY r.opschrift
        """,
        (zonder, totaal),
    )
    return cur.fetchall()


# ── "Verdacht"-heuristiek ─────────────────────────────────────────────
#
# Een artikel zonder opschrift kan twee dingen zijn:
#   1. Legitiem leeg of "Gereserveerd"/"Vervallen" — bron heeft géén Opschrift
#      en zou er ook geen moeten hebben.
#   2. Slachtoffer van de regex-bug — bron heeft wél een Opschrift, maar het
#      werd niet geparseerd door de oude `<Opschrift>(.*?)</Opschrift>` zonder
#      DOTALL.
#
# Onderscheid: een verdacht artikel heeft géén Gereserveerd/Vervallen-marker
# en heeft substantiële inhoud (> 100 chars). Empty stubs en placeholders
# vallen dan netjes buiten de selectie.
_VERDACHT_FILTER = """
    element_type = 'Artikel'
    AND opschrift IS NULL
    AND inhoud IS NOT NULL
    AND inhoud NOT ILIKE '%%Gereserveerd%%'
    AND inhoud NOT ILIKE '%%Vervallen%%'
    AND length(inhoud) > 100
"""


def _vind_alle_verdacht(cur) -> list[dict]:
    """Regelingen met >=1 artikel dat opschrift mist maar gevulde inhoud heeft.

    Sluit "Gereserveerd"/"Vervallen"-stubs en korte placeholders uit, zodat
    alleen waarschijnlijke regex-bug-slachtoffers overblijven.
    """
    cur.execute(
        f"""
        WITH stats AS (
            SELECT regeling_expression,
                   COUNT(*) FILTER (WHERE {_VERDACHT_FILTER}) AS verdacht,
                   COUNT(*) FILTER (WHERE element_type='Artikel') AS totaal
            FROM p2p.tekst_element
            GROUP BY regeling_expression
        )
        SELECT r.frbr_expression, r.frbr_work, r.opschrift,
               s.verdacht, s.totaal
        FROM stats s
        JOIN p2p.regeling r ON r.frbr_expression = s.regeling_expression
        WHERE s.verdacht > 0
        ORDER BY s.verdacht DESC, r.opschrift
        """,
    )
    return cur.fetchall()


def _verdacht_stats(cur, expression: str) -> int:
    """Aantal verdachte artikelen voor één regeling."""
    cur.execute(
        f"SELECT COUNT(*) AS n FROM p2p.tekst_element "
        f"WHERE regeling_expression = %s AND ({_VERDACHT_FILTER})",
        (expression,),
    )
    return cur.fetchone()["n"]


def _vind_alle_mixed(cur) -> list[dict]:
    """Alle regelingen met >=1 artikel zonder én >=1 met opschrift."""
    cur.execute(
        """
        WITH stats AS (
            SELECT regeling_expression,
                   COUNT(*) FILTER (WHERE opschrift IS NULL) AS zonder,
                   COUNT(*) FILTER (WHERE opschrift IS NOT NULL) AS met,
                   COUNT(*) AS totaal
            FROM p2p.tekst_element
            WHERE element_type = 'Artikel'
            GROUP BY regeling_expression
        )
        SELECT r.frbr_expression, r.frbr_work, r.opschrift,
               s.zonder, s.met, s.totaal
        FROM stats s
        JOIN p2p.regeling r ON r.frbr_expression = s.regeling_expression
        WHERE s.zonder > 0 AND s.met > 0
        ORDER BY s.zonder DESC, r.opschrift
        """,
    )
    return cur.fetchall()


# ── Per-regeling herlaad ───────────────────────────────────────────────

def _opschrift_stats(cur, expression: str) -> tuple[int, int, int]:
    """(zonder, met, totaal) — gemeten op artikelen."""
    cur.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE opschrift IS NULL) AS zonder,
            COUNT(*) FILTER (WHERE opschrift IS NOT NULL) AS met,
            COUNT(*) AS totaal
        FROM p2p.tekst_element
        WHERE regeling_expression = %s
          AND element_type = 'Artikel'
        """,
        (expression,),
    )
    row = cur.fetchone()
    return row["zonder"], row["met"], row["totaal"]


def _herlaad_één(conn, cur, reg: dict) -> dict:
    """Voer delete + re-load uit voor één regeling. Retourneert metriek-dict."""
    expression = reg["frbr_expression"]
    work = reg["frbr_work"]

    zonder0, met0, totaal0 = _opschrift_stats(cur, expression)
    verdacht0 = _verdacht_stats(cur, expression)

    # Stap 1: delete
    cur.execute(
        "DELETE FROM p2p.tekst_element WHERE regeling_expression = %s",
        (expression,),
    )
    verwijderd = cur.rowcount

    # Stap 2: re-load
    try:
        load_documentstructuur(conn, work, expression)
    except Exception as e:
        # In autocommit-mode is de delete al definitief; we kunnen niet
        # automatisch terugdraaien. Loggen en doorgeven aan caller.
        return {
            "ok": False,
            "error": str(e),
            "titel": reg["opschrift"],
            "expression": expression,
            "verwijderd": verwijderd,
        }

    zonder1, met1, totaal1 = _opschrift_stats(cur, expression)
    verdacht1 = _verdacht_stats(cur, expression)
    return {
        "ok": True,
        "titel": reg["opschrift"],
        "expression": expression,
        "verwijderd": verwijderd,
        "zonder0": zonder0, "zonder1": zonder1,
        "met0": met0,       "met1": met1,
        "totaal0": totaal0, "totaal1": totaal1,
        "verdacht0": verdacht0, "verdacht1": verdacht1,
    }


# ── Main ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--titel", help="Substring-match op regeling.opschrift (case-insensitive)")
    g.add_argument("--expression", help="Exact frbr_expression")
    g.add_argument("--alle-met-exact", metavar="ZONDER:TOTAAL",
                   help="Alle regelingen met exact dat aantal artikelen zonder/totaal (bv. '13:305' voor bruidsschat)")
    g.add_argument("--alle-mixed", action="store_true",
                   help="Alle regelingen met gemengde artikel-stats (zonder > 0 EN met > 0)")
    g.add_argument("--alle-verdacht", action="store_true",
                   help="Alleen regelingen met 'verdachte' artikelen — opschrift NULL maar inhoud "
                        "gevuld (>100 chars) en niet Gereserveerd/Vervallen. Beste indicator voor regex-bug.")
    ap.add_argument("--dry-run", action="store_true", help="Toon alleen wat er zou gebeuren")
    args = ap.parse_args()

    with get_conn() as conn, conn.cursor() as cur:
        # ── Bepaal doelen ──
        targets: list[dict] = []
        if args.expression or args.titel:
            reg = _vind_regeling(cur, args.titel, args.expression)
            if not reg:
                console.print(f"[red]Geen regeling gevonden voor: {args.titel or args.expression}[/red]")
                sys.exit(1)
            targets = [reg]
        elif args.alle_met_exact:
            try:
                z_str, t_str = args.alle_met_exact.split(":")
                zonder, totaal = int(z_str), int(t_str)
            except ValueError:
                console.print(f"[red]Format moet 'ZONDER:TOTAAL' zijn (kreeg '{args.alle_met_exact}')[/red]")
                sys.exit(2)
            targets = _vind_cluster_exact(cur, zonder, totaal)
            console.print(f"[bold]Cluster {zonder}:{totaal}:[/bold] {len(targets)} regelingen gevonden")
        elif args.alle_mixed:
            targets = _vind_alle_mixed(cur)
            console.print(f"[bold]Mixed-stats:[/bold] {len(targets)} regelingen gevonden")
        elif args.alle_verdacht:
            targets = _vind_alle_verdacht(cur)
            console.print(f"[bold]Verdacht:[/bold] {len(targets)} regelingen met regex-bug-verdachte artikelen")

        if not targets:
            console.print("[yellow]Geen regelingen om te verwerken.[/yellow]")
            return

        # ── Toon doelen ──
        preview = Table(show_header=True, header_style="bold")
        preview.add_column("zonder", justify="right", style="red")
        preview.add_column("verdacht", justify="right", style="yellow")
        preview.add_column("totaal", justify="right")
        preview.add_column("regeling", overflow="fold")
        for t in targets[:30]:
            zonder, _, totaal = _opschrift_stats(cur, t["frbr_expression"])
            verdacht = _verdacht_stats(cur, t["frbr_expression"])
            preview.add_row(str(zonder), str(verdacht), str(totaal), t["opschrift"] or t["frbr_expression"])
        console.print(preview)
        if len(targets) > 30:
            console.print(f"[dim]…en nog {len(targets) - 30} meer.[/dim]")

        if args.dry_run:
            console.print("\n[yellow]--dry-run: geen wijzigingen gemaakt.[/yellow]")
            return

        # ── Verwerk ──
        console.print(f"\n[bold]Verwerken van {len(targets)} regeling(en)...[/bold]")
        results: list[dict] = []
        for i, reg in enumerate(targets, 1):
            console.print(f"\n[bold cyan]({i}/{len(targets)})[/bold cyan] {reg['opschrift']}")
            try:
                r = _herlaad_één(conn, cur, reg)
            except Exception as e:
                console.print(f"  [red]✗ Fout: {e}[/red]")
                results.append({
                    "ok": False, "error": str(e),
                    "titel": reg["opschrift"], "expression": reg["frbr_expression"],
                })
                continue

            if not r["ok"]:
                console.print(f"  [red][FAIL] Re-load gefaald: {r['error']}[/red]")
                console.print(f"  [red]  ({r['verwijderd']} rijen al verwijderd -- handmatige actie nodig)[/red]")
            else:
                verdacht_delta = r["verdacht0"] - r["verdacht1"]
                kleur = "green" if verdacht_delta > 0 else "yellow"
                console.print(
                    f"  [{kleur}][OK] verdacht: {r['verdacht0']} -> {r['verdacht1']}  "
                    f"(zonder: {r['zonder0']} -> {r['zonder1']}, +{r['met1'] - r['met0']} getiteld)[/{kleur}]"
                )
            results.append(r)

        conn.commit()

        # ── Sluitend rapport ──
        console.print("\n[bold]Eindrapport:[/bold]")
        rapport = Table(show_header=True, header_style="bold")
        rapport.add_column("status", justify="center")
        rapport.add_column("verdacht voor", justify="right", style="yellow")
        rapport.add_column("verdacht na", justify="right", style="green")
        rapport.add_column("delta", justify="right")
        rapport.add_column("regeling", overflow="fold")

        verdacht_voor_totaal = verdacht_na_totaal = 0
        ok_count = fail_count = 0
        for r in results:
            if not r.get("ok"):
                fail_count += 1
                rapport.add_row("[red]FAIL[/red]", "-", "-", "-",
                                f"{r['titel']}  [red]({r.get('error', 'onbekend')[:60]})[/red]")
                continue
            ok_count += 1
            verdacht_voor_totaal += r["verdacht0"]
            verdacht_na_totaal += r["verdacht1"]
            delta = r["verdacht1"] - r["verdacht0"]
            rapport.add_row(
                "[green]OK[/green]",
                str(r["verdacht0"]),
                str(r["verdacht1"]),
                f"{delta:+d}",
                r["titel"],
            )
        console.print(rapport)

        gerepareerd_totaal = verdacht_voor_totaal - verdacht_na_totaal
        console.print()
        console.print(
            f"[bold]Samenvatting:[/bold] "
            f"[green]{ok_count}[/green] geslaagd, "
            f"[red]{fail_count}[/red] gefaald  ·  "
            f"[green]{gerepareerd_totaal}[/green] verdachte artikelen kregen alsnog een opschrift"
        )


if __name__ == "__main__":
    main()
