"""Incrementele Wro-refresh: pak alleen nieuwe of gewijzigde plannen op.

Strategie:
1. Download het PDOK Bestemmingsplangebied-GML opnieuw (volledig).
2. Vergelijk per plan (idn) met onze database:
   - Onbekende idn → nieuw plan, laden
   - Bestaand met andere `datum` of `planstatus` → gewijzigd, herladen
   - Bestaand en identiek → skip
3. Voor planobjecten + teksten: alleen herladen voor de plannen die
   we hebben aangeraakt (nieuwe + gewijzigde).
4. Markeer plannen die niet meer in de PDOK-feed staan als `vervallen`
   (zeldzaam, maar gebeurt bij intrekking).

Gebruik:
    cd dso-loader
    source .venv/Scripts/activate
    PYTHONPATH=. python scripts/refresh_wro.py [--dry-run]
"""

import os
import sys
from datetime import date, datetime

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from rich.console import Console
from rich.table import Table

from src.config import cfg
from src.db import get_conn, normalize_bronhouder_code
from src.loaders.wro_pdok import (
    _download_gml_gz,
    _iter_features,
    _extract_text,
    _extract_gml_geometry,
    _load_planobjecten,
)

console = Console()


def _load_existing(conn) -> dict[str, dict]:
    """Pak alle huidige plannen + datum/status uit de DB."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT idn, datum, planstatus, pons_status, bronhouder
            FROM wro.ruimtelijk_instrument
        """)
        return {r["idn"]: r for r in cur.fetchall()}


def _parse_pdok_feed() -> list[dict]:
    """Download PDOK-GML en parse alle plannen (alle gemeenten)."""
    gz_path = _download_gml_gz("Bestemmingsplangebied")
    plannen = []
    for elem in _iter_features(gz_path, "Bestemmingsplangebied"):
        idn = _extract_text(elem, "app:identificatie")
        if not idn:
            continue
        plannen.append({
            "idn": idn,
            "naam": _extract_text(elem, "app:naam"),
            "type_plan": _extract_text(elem, "app:typePlan"),
            "planstatus": (_extract_text(elem, "app:planstatus") or "onbekend").split(";")[0].strip(),
            "datum": _extract_text(elem, "app:datum"),
            "overheids_code": _extract_text(elem, "app:overheidsCode"),
            "gml": _extract_gml_geometry(elem),
        })
    return plannen


def _classify(pdok_plan: dict, existing: dict | None) -> str:
    """Bepaal status van plan: 'new', 'changed', 'unchanged'."""
    if existing is None:
        return "new"
    pdok_datum = pdok_plan.get("datum")
    db_datum = existing.get("datum")
    if pdok_datum and db_datum and str(db_datum) != pdok_datum:
        return "changed"
    if pdok_plan.get("planstatus") != existing.get("planstatus"):
        return "changed"
    return "unchanged"


def _upsert_plan(conn, plan: dict) -> None:
    """INSERT of UPDATE een plan."""
    if not plan.get("gml"):
        return
    gm_overheid = normalize_bronhouder_code(plan["overheids_code"])
    dossier = plan["idn"].rsplit("-", 1)[0] if "-" in plan["idn"] else plan["idn"]

    with conn.cursor() as cur:
        # Zorg dat bronhouder + manifest + dossier bestaan
        cur.execute(
            "INSERT INTO core.bronhouder (overheidscode, naam, bestuurslaag) "
            "VALUES (%s, %s, 'gemeente') ON CONFLICT DO NOTHING",
            (gm_overheid, plan["overheids_code"]),
        )
        cur.execute(
            "INSERT INTO wro.wro_manifest (overheidscode, naam_overheid) "
            "VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (gm_overheid, plan["overheids_code"]),
        )
        cur.execute(
            "INSERT INTO wro.wro_dossier (dossiernummer, manifest_code, status) "
            "VALUES (%s, %s, NULL) ON CONFLICT DO NOTHING",
            (dossier, gm_overheid),
        )

        # Upsert het plan zelf
        cur.execute(
            """INSERT INTO wro.ruimtelijk_instrument
               (idn, dossier, type_plan, naam, planstatus, datum, bronhouder,
                geometrie, gml_source, pons_status, laatst_geladen)
               VALUES (%s, %s, %s, %s, %s, %s, %s,
                       ST_GeomFromGML(%s, 28992), %s, 'actief', NOW())
               ON CONFLICT (idn) DO UPDATE SET
                 type_plan = EXCLUDED.type_plan,
                 naam = EXCLUDED.naam,
                 planstatus = EXCLUDED.planstatus,
                 datum = EXCLUDED.datum,
                 geometrie = EXCLUDED.geometrie,
                 gml_source = EXCLUDED.gml_source,
                 laatst_geladen = NOW()""",
            (plan["idn"], dossier, plan["type_plan"] or "onbekend",
             plan["naam"] or "onbekend", plan["planstatus"], plan["datum"],
             gm_overheid, plan["gml"], plan["gml"]),
        )


def _mark_vervallen(conn, vervallen_idns: list[str]) -> int:
    """Plannen die niet meer in PDOK staan als vervallen markeren."""
    if not vervallen_idns:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE wro.ruimtelijk_instrument SET pons_status = 'vervallen' "
            "WHERE idn = ANY(%s) AND pons_status = 'actief'",
            (vervallen_idns,),
        )
        return cur.rowcount


def main():
    dry_run = "--dry-run" in sys.argv
    started = datetime.now()
    console.print(f"[bold]Wro-refresh start[/bold] {started:%H:%M:%S}"
                  + (" [yellow](dry-run)[/yellow]" if dry_run else ""))

    conn = get_conn()
    try:
        console.print("  [dim]Bestaande plannen ophalen uit DB...[/dim]")
        existing = _load_existing(conn)
        console.print(f"  {len(existing)} plannen in OCD")

        console.print("  [dim]PDOK-feed parsen...[/dim]")
        pdok_plannen = _parse_pdok_feed()
        console.print(f"  {len(pdok_plannen)} plannen in PDOK-feed")

        # Classificeer
        new_plans = []
        changed_plans = []
        unchanged = 0
        for p in pdok_plannen:
            cls = _classify(p, existing.get(p["idn"]))
            if cls == "new":
                new_plans.append(p)
            elif cls == "changed":
                changed_plans.append(p)
            else:
                unchanged += 1

        # Vervallen plannen detecteren
        pdok_idns = {p["idn"] for p in pdok_plannen}
        vervallen = [idn for idn in existing if idn not in pdok_idns]

        # Rapport
        tbl = Table(title="Wro-refresh classificatie")
        tbl.add_column("Categorie")
        tbl.add_column("Aantal", justify="right")
        tbl.add_row("Nieuw (alleen in PDOK)", str(len(new_plans)))
        tbl.add_row("Gewijzigd (datum/status veranderd)", str(len(changed_plans)))
        tbl.add_row("Onveranderd", str(unchanged))
        tbl.add_row("Vervallen (alleen in OCD)", str(len(vervallen)))
        console.print(tbl)

        if dry_run:
            console.print("[yellow]Dry-run: geen wijzigingen doorgevoerd[/yellow]")
            return

        # Doorvoeren
        to_load = new_plans + changed_plans
        if to_load:
            console.print(f"\n  [bold]Plannen bijwerken: {len(to_load)}[/bold]")
            for i, p in enumerate(to_load, 1):
                _upsert_plan(conn, p)
                if i % 50 == 0:
                    conn.commit()
                    console.print(f"\r  {i}/{len(to_load)}...", end="")
            conn.commit()
            console.print(f"\r  {len(to_load)} plannen bijgewerkt   ")

        if vervallen:
            n = _mark_vervallen(conn, vervallen)
            conn.commit()
            console.print(f"  {n} plannen gemarkeerd als 'vervallen'")

        # Planobjecten + teksten alleen voor aangeraakte gemeenten
        affected_codes = {p["overheids_code"] for p in to_load if p.get("overheids_code")}
        if affected_codes:
            console.print(f"\n  [bold]Planobjecten herladen voor {len(affected_codes)} gemeenten[/bold]")
            for feature, obj_type in [
                ("Enkelbestemming", "Enkelbestemming"),
                ("Dubbelbestemming", "Dubbelbestemming"),
                ("Bouwvlak", "Bouwvlak"),
                ("Functieaanduiding", "Functieaanduiding"),
                ("Bouwaanduiding", "Bouwaanduiding"),
                ("Maatvoering", "Maatvoering"),
                ("Figuur", "Figuur"),
                ("Gebiedsaanduiding", "Gebiedsaanduiding"),
            ]:
                _load_planobjecten(conn, feature, obj_type, affected_codes)

        elapsed = datetime.now() - started
        console.print(f"\n[bold green]Refresh klaar in {elapsed}[/bold green]")
        console.print(f"  Nieuw:       {len(new_plans)}")
        console.print(f"  Gewijzigd:   {len(changed_plans)}")
        console.print(f"  Vervallen:   {len(vervallen)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
