"""Query interface for the DSO database.

Natural-language-style queries on the 1.6M row database.
"""

import json
import sys

import httpx
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.db import get_conn

console = Console()


LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"


def adres_naar_rd(adres: str) -> tuple[float, float, str]:
    """Resolve a freeform address to RD coordinates via PDOK Locatieserver."""
    resp = httpx.get(LOCATIESERVER, params={"q": adres, "rows": 1, "fq": "type:adres"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    docs = data.get("response", {}).get("docs", [])
    if not docs:
        resp2 = httpx.get(LOCATIESERVER, params={"q": adres, "rows": 1}, timeout=10)
        resp2.raise_for_status()
        docs = resp2.json().get("response", {}).get("docs", [])
    if not docs:
        raise ValueError(f"Adres niet gevonden: {adres}")
    doc = docs[0]
    centroid = doc.get("centroide_rd", "")
    # Format: "POINT(x y)"
    coords = centroid.replace("POINT(", "").replace(")", "").split()
    x, y = float(coords[0]), float(coords[1])
    label = doc.get("weergavenaam", adres)
    return x, y, label


def wat_geldt_op_adres(adres: str):
    """What rules apply at a given address?"""
    x, y, label = adres_naar_rd(adres)
    console.print(f"[dim]{label} -> RD {x:.0f}, {y:.0f}[/dim]")
    wat_geldt_hier(x, y)


def wat_geldt_hier(x: float, y: float):
    """What rules apply at coordinate (x, y) in RD/EPSG:28992?"""
    conn = get_conn()
    with conn.cursor() as cur:
        # Ow: locaties + juridische regels
        cur.execute("""
            SELECT DISTINCT r.opschrift as regeling, r.documenttype,
                   te.nummer, te.opschrift as artikel,
                   jr.regel_type, a.naam as activiteit
            FROM p2p.locatie l
            JOIN p2p.activiteit_locatieaanduiding ala ON ala.locatie_id = l.identificatie
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE ST_Contains(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            ORDER BY r.opschrift, te.nummer
            LIMIT 50
        """, (x, y))
        ow_rows = cur.fetchall()

        # Wro: planobjecten
        cur.execute("""
            SELECT ri.naam as plan, ri.type_plan, ri.planstatus,
                   po.object_type, po.naam as bestemming, po.bestemmingshoofdgroep
            FROM wro.planobject po
            JOIN wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
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
            FROM p2p.activiteit a
            LEFT JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
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
            FROM p2p.norm n
            LEFT JOIN p2p.normwaarde nw ON nw.norm_id = n.identificatie
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
            FROM i2a.werkzaamheid w
            JOIN p2p.activiteit a ON a.identificatie = w.activiteit_id
            JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
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
            FROM p2p.pons p
            JOIN p2p.locatie l ON l.identificatie = p.locatie_id
            WHERE p.identificatie LIKE %s
        """, (f"nl.imow-gm{gemeente}.%",))
        pons = cur.fetchone()

        cur.execute("""
            SELECT count(*) as total,
                   count(*) FILTER (WHERE ri.planstatus = 'vastgesteld') as vastgesteld
            FROM wro.ruimtelijk_instrument ri
            WHERE ri.bronhouder = %s
        """, (gemeente,))
        wro = cur.fetchone()
    conn.close()

    if pons:
        console.print(f"[green]Pons gevonden: {pons['noemer'] or pons['identificatie']}[/green]")
    else:
        console.print(f"[yellow]Geen pons voor gemeente {gemeente}[/yellow]")

    console.print(f"Wro-instrumenten: {wro['total']} totaal, {wro['vastgesteld']} vastgesteld")


def _snippet(tekst: str, zoekterm: str, width: int = 80) -> str:
    """Extract a snippet around the search term."""
    if not tekst:
        return ""
    idx = tekst.lower().find(zoekterm.lower())
    if idx >= 0:
        start = max(0, idx - 30)
        end = min(len(tekst), idx + len(zoekterm) + width - 30)
        return ("..." if start > 0 else "") + tekst[start:end] + ("..." if end < len(tekst) else "")
    return tekst[:width]


def zoek_tekst(zoekterm: str):
    """Full-text search across Ow tekst_elementen and Wro planteksten."""
    conn = get_conn()
    with conn.cursor() as cur:
        # Ow teksten
        cur.execute("""
            SELECT te.nummer, te.opschrift, te.element_type,
                   r.opschrift as regeling, 'Ow' as regime,
                   regexp_replace(te.inhoud, '<[^>]+>', '', 'g') as platte_tekst
            FROM p2p.tekst_element te
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE te.inhoud ILIKE %s
            ORDER BY r.opschrift, te.nummer
            LIMIT 20
        """, (f"%{zoekterm}%",))
        ow_rows = cur.fetchall()

        # Wro teksten
        cur.execute("""
            SELECT wt.nummer, wt.naam as opschrift, wt.object_type as element_type,
                   ri.naam as regeling, 'Wro' as regime,
                   wt.inhoud as platte_tekst
            FROM wro.wro_tekst_object wt
            JOIN wro.ruimtelijk_instrument ri ON ri.idn = wt.instrument_idn
            WHERE wt.inhoud ILIKE %s
            ORDER BY ri.naam, wt.volgnummer
            LIMIT 20
        """, (f"%{zoekterm}%",))
        wro_rows = cur.fetchall()
    conn.close()

    all_rows = ow_rows + wro_rows
    if not all_rows:
        console.print(f"[yellow]Geen resultaten voor '{zoekterm}'[/yellow]")
        return

    tbl = Table(title=f"Zoekresultaten: {zoekterm} ({len(ow_rows)} Ow + {len(wro_rows)} Wro)")
    tbl.add_column("Regime", width=4)
    tbl.add_column("Regeling/Plan", max_width=30)
    tbl.add_column("Type")
    tbl.add_column("Artikel", max_width=20)
    tbl.add_column("Fragment", max_width=55)
    for r in all_rows:
        tbl.add_row(
            r["regime"],
            (r["regeling"] or "")[:30],
            r["element_type"],
            f"{r['nummer'] or ''} {r['opschrift'] or ''}".strip()[:20],
            _snippet(r["platte_tekst"] or "", zoekterm, 55),
        )
    console.print(tbl)


def bevoegde_gezagen():
    """Show all bevoegde gezagen with load status."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT overheidscode, naam, oin, bestuurslaag,
                   ow_geladen, imtr_geladen, wro_geladen, wro_teksten_geladen,
                   ow_regelingen, wro_instrumenten
            FROM core.bronhouder
            ORDER BY bestuurslaag, naam
        """)
        rows = cur.fetchall()
    conn.close()

    tbl = Table(title=f"Bevoegde gezagen ({len(rows)})")
    tbl.add_column("Code", width=6)
    tbl.add_column("Naam", max_width=28)
    tbl.add_column("Laag", width=10)
    tbl.add_column("OIN", max_width=12)
    tbl.add_column("Ow", justify="center", width=4)
    tbl.add_column("IMTR", justify="center", width=4)
    tbl.add_column("Wro", justify="center", width=4)
    tbl.add_column("Tekst", justify="center", width=5)
    tbl.add_column("Reg", justify="right", width=4)
    tbl.add_column("Plan", justify="right", width=5)
    for r in rows:
        check = lambda v: "[green]Y[/green]" if v else "[dim]-[/dim]"
        oin_short = (r["oin"] or "")[-6:] if r["oin"] else ""
        tbl.add_row(
            r["overheidscode"],
            (r["naam"] or "")[:28],
            r["bestuurslaag"] or "",
            f"...{oin_short}" if oin_short else "",
            check(r["ow_geladen"]),
            check(r["imtr_geladen"]),
            check(r["wro_geladen"]),
            check(r["wro_teksten_geladen"]),
            str(r["ow_regelingen"]),
            str(r["wro_instrumenten"]),
        )
    console.print(tbl)


def overzicht():
    """Show database overview."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.bronhouder")
        n_bh = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM p2p.regeling")
        n_reg = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM p2p.juridische_regel")
        n_jr = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM p2p.activiteit")
        n_act = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM wro.ruimtelijk_instrument")
        n_wro = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM wro.planobject")
        n_po = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM i2a.werkzaamheid WHERE activiteit_id IS NOT NULL")
        n_wz = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM p2p.pons")
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
