"""Herstel locaties met POINT(0,0) placeholder-geometrie.

Achtergrond: api_loader._get_geometry() faalt soms door tijdelijke DSO-API
fouten (peer closed connection, 5xx). Als fallback wordt een POINT(0,0)
ingevoegd zodat de annotatie-rij niet kapot gaat, maar geo-queries leveren
daardoor 0 hits voor die locatie. Het herstel-script voor de URL-encoding-bug
raakt deze niet omdat de bijbehorende regeling wél tekst-elementen heeft.

Aanpak: vind elke unieke regeling die nog naar een placeholder-locatie
verwijst (als regelingsgebied OF via ala/ga), en roep `load_regeling_expand`
+ de juiste annotatie-loader opnieuw aan. De `ON CONFLICT (identificatie)
DO UPDATE SET geometrie = ...` clause overschrijft de placeholder als
DSO deze keer wél een geometry teruggeeft.

Gebruik:
    cd dso-loader
    .venv/Scripts/activate
    python scripts/herstel_placeholder_geometrie.py --dry-run
    python scripts/herstel_placeholder_geometrie.py
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

from src.db import get_conn
from src.loaders.api_loader import (
    ARTIKELSTRUCTUUR_TYPES,
    VRIJETEKST_TYPES,
    load_regeling_expand,
    load_regeltekstannotaties,
    load_divisieannotaties,
)

console = Console()


def tel_placeholders(cur) -> int:
    cur.execute("""
        SELECT count(*) AS n FROM p2p.locatie
        WHERE ST_GeometryType(geometrie) = 'ST_Point'
          AND ST_X(geometrie) = 0 AND ST_Y(geometrie) = 0
    """)
    return cur.fetchone()["n"]


def vind_regelingen_met_placeholders(cur) -> list[dict]:
    """Unieke regelingen die >=1 placeholder-locatie referenceren."""
    cur.execute("""
        WITH placeholders AS (
            SELECT identificatie FROM p2p.locatie
            WHERE ST_GeometryType(geometrie) = 'ST_Point'
              AND ST_X(geometrie) = 0 AND ST_Y(geometrie) = 0
        ),
        via_regelingsgebied AS (
            SELECT r.frbr_work, r.frbr_expression, r.opschrift, r.documenttype, r.bronhouder
            FROM p2p.regeling r
            JOIN placeholders p ON p.identificatie = r.regelingsgebied_id
        ),
        via_ala AS (
            SELECT DISTINCT r.frbr_work, r.frbr_expression, r.opschrift, r.documenttype, r.bronhouder
            FROM placeholders p
            JOIN p2p.activiteit_locatieaanduiding ala ON ala.locatie_id = p.identificatie
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        ),
        via_ga AS (
            SELECT DISTINCT r.frbr_work, r.frbr_expression, r.opschrift, r.documenttype, r.bronhouder
            FROM placeholders p
            JOIN p2p.gebiedsaanwijzing ga ON ga.locatie_id = p.identificatie
            JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.gebiedsaanwijzing_id = ga.identificatie
            JOIN p2p.juridische_regel jr ON jr.identificatie = jrg.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        )
        SELECT * FROM via_regelingsgebied
        UNION SELECT * FROM via_ala
        UNION SELECT * FROM via_ga
        ORDER BY bronhouder, opschrift
    """)
    return cur.fetchall()


def herstel_regeling(conn, reg: dict) -> dict:
    work = reg["frbr_work"]
    expr = reg["frbr_expression"]
    doc_type = reg["documenttype"]
    bron = reg["bronhouder"]
    out = {"ok": True, "warns": []}

    try:
        load_regeling_expand(conn, work, expr)
    except Exception as e:
        out["warns"].append(f"expand: {str(e)[:80]}")

    try:
        if doc_type in ARTIKELSTRUCTUUR_TYPES:
            load_regeltekstannotaties(conn, work, bron)
        elif doc_type in VRIJETEKST_TYPES:
            load_divisieannotaties(conn, work, bron)
        else:
            load_regeltekstannotaties(conn, work, bron)
    except Exception as e:
        out["ok"] = False
        out["warns"].append(f"annotaties: {str(e)[:80]}")

    conn.commit()
    return out


def refresh_subdiv_voor_gerepareerde(cur) -> int:
    """Vul/vervang subdiv-rijen voor locaties die niet langer POINT(0,0) zijn."""
    cur.execute("""
        DELETE FROM p2p.locatie_subdiv ls
        USING p2p.locatie l
        WHERE ls.identificatie = l.identificatie
          AND ST_GeometryType(l.geometrie) IN ('ST_Polygon','ST_MultiPolygon')
          AND NOT EXISTS (
              SELECT 1 FROM p2p.locatie_subdiv ls2
              WHERE ls2.identificatie = l.identificatie
                AND ST_GeometryType(ls2.geometrie) IN ('ST_Polygon','ST_MultiPolygon')
                AND ST_Area(ls2.geometrie) > 0
          )
    """)
    cur.execute("""
        INSERT INTO p2p.locatie_subdiv (identificatie, geometrie)
        SELECT l.identificatie, ST_Subdivide(l.geometrie, 256)
        FROM p2p.locatie l
        WHERE ST_GeometryType(l.geometrie) IN ('ST_Polygon','ST_MultiPolygon')
          AND NOT EXISTS (SELECT 1 FROM p2p.locatie_subdiv ls
                          WHERE ls.identificatie = l.identificatie)
    """)
    return cur.rowcount


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            n_voor = tel_placeholders(cur)
            regelingen = vind_regelingen_met_placeholders(cur)

        console.print(f"[bold]{n_voor}[/bold] placeholder-locaties (POINT 0,0) gevonden")
        console.print(f"[bold]{len(regelingen)}[/bold] unieke regeling(en) refereren ernaar")

        if args.dry_run:
            for r in regelingen[:30]:
                console.print(f"  [{r['bronhouder']}] {r['documenttype']}: {r['opschrift'][:80]}")
            if len(regelingen) > 30:
                console.print(f"[dim]…en nog {len(regelingen) - 30}[/dim]")
            return

        if args.limit:
            regelingen = regelingen[:args.limit]

        ok = warn = err = 0
        for i, reg in enumerate(regelingen, 1):
            console.print(f"\n[cyan]({i}/{len(regelingen)})[/cyan] "
                          f"[{reg['bronhouder']}] {reg['opschrift'][:70]}")
            r = herstel_regeling(conn, reg)
            if not r["ok"]:
                console.print(f"  [red]✗ {'; '.join(r['warns'])}[/red]")
                err += 1
            else:
                ok += 1
                if r["warns"]:
                    warn += 1
                    console.print(f"  [yellow]⚠ {'; '.join(r['warns'])}[/yellow]")

        with conn.cursor() as cur:
            n_na = tel_placeholders(cur)
            console.print(f"\nSubdiv aanvullen voor herstelde locaties…")
            refresh_subdiv_voor_gerepareerde(cur)
            conn.commit()

        console.print(f"\n[bold]Eindstand:[/bold] "
                      f"[green]{ok} OK[/green], [yellow]{warn} warn[/yellow], "
                      f"[red]{err} fout[/red]")
        console.print(f"Placeholders: {n_voor} → [bold]{n_na}[/bold] "
                      f"(–{n_voor - n_na} hersteld)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
