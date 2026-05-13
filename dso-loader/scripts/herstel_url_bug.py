"""Herstel regelingen die door de api_loader URL-encoding-bug nooit volledig
zijn geladen.

Achtergrond: api_loader.py vervangt voorheen alleen `/` door `_` in het
DSO-pad. DSO Presenteren v8 vervangt óók `-` door `_`, dus alle child-
endpoints (documentstructuur, regeltekstannotaties, divisieannotaties,
_expand) gaven 404 voor regelingen met dashes na het jaar (UUID-, date-,
tijdelijkdeel-id's). De fix in api_loader._encode_regeling_uri raakt
alleen nieuwe inserts; bestaande regelingen die door de bug nooit hun
documentstructuur kregen blijven leeg.

Dit script vindt alle regelingen met 0 tekst-elementen waar het werk-id
een dash bevat in het laatste pad-segment, en draait per regeling de
4 child-loaders alsnog. Daarna ST_Subdivide voor alle nieuw geladen
locaties (locatie_subdiv heeft geen trigger).

Gebruik:
    cd dso-loader
    .venv/Scripts/activate
    python scripts/herstel_url_bug.py --dry-run   # toon wat er gebeurt
    python scripts/herstel_url_bug.py             # voer uit
    python scripts/herstel_url_bug.py --bronhouder pv25  # gericht
"""

import argparse
import sys
import os

os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from src.db import get_conn
from src.loaders.api_loader import (
    ARTIKELSTRUCTUUR_TYPES,
    VRIJETEKST_TYPES,
    load_documentstructuur,
    load_regeling_expand,
    load_regeltekstannotaties,
    load_divisieannotaties,
)

console = Console()


def vind_gebroken(cur, bronhouder: str | None) -> list[dict]:
    """Regelingen met 0 tekst_element en dash in laatste pad-segment."""
    sql = """
        SELECT r.frbr_expression, r.frbr_work, r.opschrift,
               r.documenttype, r.bronhouder
        FROM p2p.regeling r
        WHERE NOT EXISTS (
            SELECT 1 FROM p2p.tekst_element te
            WHERE te.regeling_expression = r.frbr_expression
        )
          AND substring(r.frbr_work FROM '/[^/]+$') LIKE %s
    """
    params: list = ["%-%"]
    if bronhouder:
        sql += " AND r.bronhouder = %s"
        params.append(bronhouder)
    sql += " ORDER BY r.bronhouder, r.opschrift"
    cur.execute(sql, params)
    return cur.fetchall()


def herstel_één(conn, reg: dict) -> dict:
    """Draai de child-loaders voor één regeling."""
    work = reg["frbr_work"]
    expr = reg["frbr_expression"]
    doc_type = reg["documenttype"]
    bron = reg["bronhouder"]

    out = {"opschrift": reg["opschrift"], "expr": expr,
           "doc_type": doc_type, "bronhouder": bron,
           "te": 0, "annot": None, "error": None}

    try:
        out["te"] = load_documentstructuur(conn, work, expr) or 0
    except Exception as e:
        out["error"] = f"documentstructuur: {e}"
        return out

    try:
        load_regeling_expand(conn, work, expr)
    except Exception as e:
        out["error"] = f"expand: {e}"
        # niet-fataal — door

    try:
        if doc_type in ARTIKELSTRUCTUUR_TYPES:
            out["annot"] = load_regeltekstannotaties(conn, work, bron)
        elif doc_type in VRIJETEKST_TYPES:
            out["annot"] = load_divisieannotaties(conn, work, bron)
        else:
            out["annot"] = load_regeltekstannotaties(conn, work, bron)
    except Exception as e:
        out["error"] = f"annotaties: {e}"

    conn.commit()
    return out


def refresh_subdiv(cur) -> int:
    """Vul locatie_subdiv aan voor alle nieuwe locaties."""
    cur.execute("""
        INSERT INTO p2p.locatie_subdiv (identificatie, geometrie)
        SELECT l.identificatie, ST_Subdivide(l.geometrie, 256)
        FROM p2p.locatie l
        WHERE l.identificatie NOT IN (SELECT DISTINCT identificatie FROM p2p.locatie_subdiv)
          AND ST_GeometryType(l.geometrie) IN ('ST_Polygon','ST_MultiPolygon')
    """)
    return cur.rowcount


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bronhouder", help="Beperk tot één bronhouder-code (bv. pv25, ws0621)")
    ap.add_argument("--dry-run", action="store_true", help="Toon doelen zonder te laden")
    ap.add_argument("--limit", type=int, help="Stop na N regelingen (voor testen)")
    args = ap.parse_args()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            doelen = vind_gebroken(cur, args.bronhouder)

        console.print(f"[bold]{len(doelen)}[/bold] gebroken regeling(en) gevonden"
                      f"{' voor ' + args.bronhouder if args.bronhouder else ''}")

        if not doelen:
            return

        if args.limit:
            doelen = doelen[:args.limit]

        if args.dry_run:
            tbl = Table()
            tbl.add_column("bronhouder")
            tbl.add_column("documenttype")
            tbl.add_column("opschrift", overflow="fold")
            for d in doelen[:50]:
                tbl.add_row(d["bronhouder"], d["documenttype"] or "?", d["opschrift"])
            console.print(tbl)
            if len(doelen) > 50:
                console.print(f"[dim]…en nog {len(doelen) - 50}[/dim]")
            return

        ok = err = 0
        for i, reg in enumerate(doelen, 1):
            console.print(f"\n[cyan]({i}/{len(doelen)})[/cyan] "
                          f"[{reg['bronhouder']}] {reg['opschrift'][:70]}")
            r = herstel_één(conn, reg)
            if r["error"] and r["te"] == 0:
                console.print(f"  [red]✗ {r['error'][:120]}[/red]")
                err += 1
            else:
                a = r["annot"] or {}
                annot_msg = ""
                if a.get("regels"):
                    annot_msg = f", {a['regels']} regels, {a.get('ala',0)} ALA's"
                elif a.get("tekstdelen"):
                    annot_msg = f", {a['tekstdelen']} tekstdelen, {a.get('ga',0)} GA's"
                console.print(f"  [green]✓ {r['te']} tekst{annot_msg}[/green]")
                if r["error"]:
                    console.print(f"  [yellow]  warn: {r['error'][:100]}[/yellow]")
                ok += 1

        console.print(f"\n[bold]Hersteld:[/bold] [green]{ok}[/green] OK, [red]{err}[/red] fout")

        with conn.cursor() as cur:
            console.print("\nLocatie_subdiv aanvullen…")
            n = refresh_subdiv(cur)
            conn.commit()
            console.print(f"  [green]+{n} subdiv-rijen[/green]")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
