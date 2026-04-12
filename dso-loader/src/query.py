"""Query interface for the DSO database.

Natural-language-style queries on the 1.6M row database.
"""

import json
import sys

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.db import get_conn

console = Console()


def wat_geldt_hier(x: float, y: float):
    """What rules apply at coordinate (x, y) in RD/EPSG:28992?"""
    conn = get_conn()
    with conn.cursor() as cur:
        # Ow: locaties + juridische regels
        cur.execute("""
            SELECT DISTINCT r.opschrift as regeling, r.documenttype,
                   te.nummer, te.opschrift as artikel,
                   jr.regel_type, a.naam as activiteit
            FROM dso.locatie l
            JOIN dso.activiteit_locatieaanduiding ala ON ala.locatie_id = l.identificatie
            JOIN dso.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN dso.activiteit a ON a.identificatie = ala.activiteit_id
            JOIN dso.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN dso.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE ST_Contains(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            ORDER BY r.opschrift, te.nummer
            LIMIT 50
        """, (x, y))
        ow_rows = cur.fetchall()

        # Wro: planobjecten
        cur.execute("""
            SELECT ri.naam as plan, ri.type_plan, ri.planstatus,
                   po.object_type, po.naam as bestemming, po.bestemmingshoofdgroep
            FROM dso.planobject po
            JOIN dso.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
            WHERE ST_Contains(po.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            ORDER BY ri.naam, po.object_type
            LIMIT 50
        """, (x, y))
        wro_rows = cur.fetchall()

    conn.close()

    console.print(Panel(f"[bold]Locatie: {x}, {y} (RD)[/bold]"))

    if ow_rows:
        tbl = Table(title="Omgevingswet - regels op deze locatie")
        tbl.add_column("Regeling", max_width=40)
        tbl.add_column("Artikel")
        tbl.add_column("Activiteit", max_width=40)
        tbl.add_column("Type")
        for r in ow_rows:
            tbl.add_row(
                r["regeling"][:40],
                f"{r['nummer'] or ''} {r['artikel'] or ''}".strip()[:30],
                (r["activiteit"] or "")[:40],
                r["regel_type"],
            )
        console.print(tbl)
    else:
        console.print("[yellow]Geen Ow-regels gevonden op deze locatie[/yellow]")

    if wro_rows:
        tbl = Table(title="Wro - bestemmingen op deze locatie")
        tbl.add_column("Plan", max_width=35)
        tbl.add_column("Type")
        tbl.add_column("Bestemming", max_width=30)
        tbl.add_column("Hoofdgroep")
        for r in wro_rows:
            tbl.add_row(
                (r["plan"] or "")[:35],
                r["object_type"],
                (r["bestemming"] or "")[:30],
                r["bestemmingshoofdgroep"] or "",
            )
        console.print(tbl)
    else:
        console.print("[yellow]Geen Wro-bestemmingen gevonden op deze locatie[/yellow]")


def welke_activiteiten(gemeente: str):
    """List all activities for a municipality."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.naam, a.groep, count(ala.id) as ala_count
            FROM dso.activiteit a
            LEFT JOIN dso.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
            WHERE a.identificatie LIKE %s
            GROUP BY a.naam, a.groep
            ORDER BY ala_count DESC
        """, (f"nl.imow-gm{gemeente}.%",))
        rows = cur.fetchall()
    conn.close()

    tbl = Table(title=f"Activiteiten gemeente {gemeente}")
    tbl.add_column("Activiteit", max_width=60)
    tbl.add_column("Groep", max_width=25)
    tbl.add_column("ALA's", justify="right")
    for r in rows:
        tbl.add_row(r["naam"][:60], (r["groep"] or "")[:25], str(r["ala_count"]))
    console.print(tbl)
    console.print(f"[green]Totaal: {len(rows)} activiteiten[/green]")


def normen_gemeente(gemeente: str):
    """List all norms for a municipality."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT n.naam, n.groep, n.type_norm,
                   count(nw.id) as waarden
            FROM dso.norm n
            LEFT JOIN dso.normwaarde nw ON nw.norm_id = n.identificatie
            WHERE n.identificatie LIKE %s
            GROUP BY n.naam, n.groep, n.type_norm
            ORDER BY waarden DESC
        """, (f"nl.imow-gm{gemeente}.%",))
        rows = cur.fetchall()
    conn.close()

    tbl = Table(title=f"Normen gemeente {gemeente}")
    tbl.add_column("Norm", max_width=50)
    tbl.add_column("Groep", max_width=25)
    tbl.add_column("Type", max_width=20)
    tbl.add_column("Waarden", justify="right")
    for r in rows:
        tbl.add_row(r["naam"][:50], (r["groep"] or "")[:25],
                     (r["type_norm"] or "")[:20], str(r["waarden"]))
    console.print(tbl)


def werkzaamheid_keten(werkzaamheid: str):
    """Trace a werkzaamheid to activiteiten and artikelen."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT w.naam as werkzaamheid, a.naam as activiteit,
                   te.nummer, te.opschrift as artikel,
                   r.opschrift as regeling, r.bronhouder
            FROM dso.werkzaamheid w
            JOIN dso.activiteit a ON a.identificatie = w.activiteit_id
            JOIN dso.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
            JOIN dso.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN dso.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN dso.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE w.naam ILIKE %s OR w.urn ILIKE %s
            ORDER BY r.opschrift, te.nummer
            LIMIT 30
        """, (f"%{werkzaamheid}%", f"%{werkzaamheid}%"))
        rows = cur.fetchall()
    conn.close()

    if not rows:
        console.print(f"[yellow]Geen resultaten voor '{werkzaamheid}'[/yellow]")
        return

    tbl = Table(title=f"Keten: {werkzaamheid}")
    tbl.add_column("Werkzaamheid", max_width=30)
    tbl.add_column("Activiteit", max_width=35)
    tbl.add_column("Artikel", max_width=25)
    tbl.add_column("Regeling", max_width=35)
    for r in rows:
        tbl.add_row(
            r["werkzaamheid"][:30],
            r["activiteit"][:35],
            f"{r['nummer'] or ''} {r['artikel'] or ''}".strip()[:25],
            r["regeling"][:35],
        )
    console.print(tbl)


def pons_status(gemeente: str):
    """Show pons status for a municipality."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.identificatie, l.noemer
            FROM dso.pons p
            JOIN dso.locatie l ON l.identificatie = p.locatie_id
            WHERE p.identificatie LIKE %s
        """, (f"nl.imow-gm{gemeente}.%",))
        pons = cur.fetchone()

        cur.execute("""
            SELECT count(*) as total,
                   count(*) FILTER (WHERE ri.planstatus = 'vastgesteld') as vastgesteld
            FROM dso.ruimtelijk_instrument ri
            WHERE ri.bronhouder = %s
        """, (gemeente,))
        wro = cur.fetchone()
    conn.close()

    if pons:
        console.print(f"[green]Pons gevonden: {pons['noemer'] or pons['identificatie']}[/green]")
    else:
        console.print(f"[yellow]Geen pons voor gemeente {gemeente}[/yellow]")

    console.print(f"Wro-instrumenten: {wro['total']} totaal, {wro['vastgesteld']} vastgesteld")


def zoek_tekst(zoekterm: str):
    """Full-text search across all tekst_elementen."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT te.nummer, te.opschrift, te.element_type,
                   r.opschrift as regeling, r.bronhouder, r.documenttype,
                   regexp_replace(te.inhoud, '<[^>]+>', '', 'g') as platte_tekst
            FROM dso.tekst_element te
            JOIN dso.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE te.inhoud ILIKE %s
            ORDER BY r.opschrift, te.nummer
            LIMIT 30
        """, (f"%{zoekterm}%",))
        rows = cur.fetchall()
    conn.close()

    if not rows:
        console.print(f"[yellow]Geen resultaten voor '{zoekterm}'[/yellow]")
        return

    tbl = Table(title=f"Zoekresultaten: {zoekterm} ({len(rows)} hits)")
    tbl.add_column("Regeling", max_width=35)
    tbl.add_column("Type")
    tbl.add_column("Artikel", max_width=25)
    tbl.add_column("Tekstfragment", max_width=60)
    for r in rows:
        tekst = (r["platte_tekst"] or "")[:200]
        idx = tekst.lower().find(zoekterm.lower())
        if idx >= 0:
            start = max(0, idx - 30)
            end = min(len(tekst), idx + len(zoekterm) + 30)
            fragment = ("..." if start > 0 else "") + tekst[start:end] + ("..." if end < len(tekst) else "")
        else:
            fragment = tekst[:60]
        tbl.add_row(
            r["regeling"][:35],
            r["element_type"],
            f"{r['nummer'] or ''} {r['opschrift'] or ''}".strip()[:25],
            fragment,
        )
    console.print(tbl)


def overzicht():
    """Show database overview."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dso.bronhouder")
        n_bh = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM dso.regeling")
        n_reg = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM dso.juridische_regel")
        n_jr = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM dso.activiteit")
        n_act = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM dso.ruimtelijk_instrument")
        n_wro = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM dso.planobject")
        n_po = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM dso.werkzaamheid WHERE activiteit_id IS NOT NULL")
        n_wz = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM dso.pons")
        n_pons = cur.fetchone()["count"]
    conn.close()

    console.print(Panel(
        f"[bold]DSO Database[/bold]\n"
        f"{n_bh} bronhouders, {n_reg} Ow-regelingen, {n_wro} Wro-instrumenten\n"
        f"{n_jr:,} juridische regels, {n_act:,} activiteiten\n"
        f"{n_po:,} Wro-planobjecten, {n_pons} ponsen\n"
        f"{n_wz} werkzaamheden gekoppeld",
        title="Overzicht"
    ))
