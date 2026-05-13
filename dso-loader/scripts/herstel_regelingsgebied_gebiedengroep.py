"""Herlaad regelingsgebieden van regelingen waar de Gebiedengroep zelf
geen `geometrieIdentificatie` heeft — geometry zit dan in `_embedded.omvat`.

Treft typisch Aanwijzingsbesluit N2000, Projectbesluit, Programma,
Toegangsbeperkingsbesluit. De api_loader-patch
(_upsert_locatie_met_kinderen) zorgt nu dat een nieuwe load deze regelingen
correct laadt; dit script roept `load_regeling_expand` opnieuw aan voor
de 88 bestaande regelingen met POINT(0,0) als regelingsgebied.

Gebruik:
    python scripts/herstel_regelingsgebied_gebiedengroep.py --dry-run
    python scripts/herstel_regelingsgebied_gebiedengroep.py
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
from src.loaders.api_loader import load_regeling_expand

console = Console()


def vind_doelen(cur) -> list[dict]:
    cur.execute("""
        SELECT r.frbr_work, r.frbr_expression, r.opschrift, r.documenttype, r.bronhouder
        FROM p2p.regeling r
        JOIN p2p.locatie l ON l.identificatie = r.regelingsgebied_id
        WHERE ST_GeometryType(l.geometrie) = 'ST_Point'
          AND ST_X(l.geometrie) = 0 AND ST_Y(l.geometrie) = 0
        ORDER BY r.documenttype, r.bronhouder, r.opschrift
    """)
    return cur.fetchall()


def refresh_subdiv_voor_herstelde(cur) -> int:
    """Vul/vervang subdiv voor locaties die nu een polygon-geometry hebben."""
    cur.execute("""
        DELETE FROM p2p.locatie_subdiv ls
        USING p2p.locatie l
        WHERE ls.identificatie = l.identificatie
          AND ST_GeometryType(l.geometrie) IN ('ST_Polygon','ST_MultiPolygon')
          AND EXISTS (
              SELECT 1 FROM p2p.locatie_subdiv ls2
              WHERE ls2.identificatie = l.identificatie
                AND ST_GeometryType(ls2.geometrie) NOT IN ('ST_Polygon','ST_MultiPolygon')
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
            doelen = vind_doelen(cur)
        console.print(f"[bold]{len(doelen)}[/bold] regelingen met placeholder-regelingsgebied")
        if args.dry_run:
            from collections import Counter
            c = Counter(d["documenttype"] for d in doelen)
            for t, n in c.most_common():
                console.print(f"  {t}: {n}")
            return

        if args.limit:
            doelen = doelen[:args.limit]

        ok = err = 0
        for i, reg in enumerate(doelen, 1):
            console.print(f"[cyan]({i}/{len(doelen)})[/cyan] "
                          f"[{reg['documenttype']}] {reg['opschrift'][:70]}")
            try:
                load_regeling_expand(conn, reg["frbr_work"], reg["frbr_expression"])
                ok += 1
            except Exception as e:
                console.print(f"  [red]✗ {str(e)[:120]}[/red]")
                err += 1
                conn.rollback()

        with conn.cursor() as cur:
            console.print(f"\n[bold]Subdiv aanvullen…[/bold]")
            n = refresh_subdiv_voor_herstelde(cur)
            conn.commit()
            console.print(f"  +{n} subdiv-rijen")

            cur.execute("""
                SELECT count(*) AS n FROM p2p.locatie
                WHERE ST_GeometryType(geometrie) = 'ST_Point'
                  AND ST_X(geometrie) = 0 AND ST_Y(geometrie) = 0
            """)
            rest = cur.fetchone()["n"]

        console.print(f"\n[bold]Eindstand:[/bold] [green]{ok} OK[/green], "
                      f"[red]{err} fout[/red] · placeholders nog over: {rest}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
