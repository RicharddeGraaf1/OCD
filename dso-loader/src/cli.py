"""CLI for loading DSO content into Postgres+PostGIS.

Usage:
    python -m src.cli setup          # Create schema + lookup tables
    python -m src.cli load-wro       # Load Wro bestemmingsplannen from PDOK
    python -m src.cli load-imtr      # Load toepasbare regels (RTR + STTR)
    python -m src.cli load-ow        # Load Ow regelingen (DSO Download API)
    python -m src.cli status         # Show row counts per table
"""

import click
from rich.console import Console
from rich.table import Table

from src.config import cfg
from src.db import get_conn, execute_sql_file, table_count
from src.ddl import DDL, LOOKUPS

console = Console()


@click.group()
def cli():
    """DSO Loader — load all DSO content into Postgres+PostGIS."""
    pass


@cli.command()
def setup():
    """Create the database schema and populate lookup tables."""
    console.print(f"[bold]Setting up database[/bold] at {cfg.DB_HOST}:{cfg.DB_PORT}/{cfg.DB_NAME}")

    from src.ddl import KOOP_DDL

    conn = get_conn()
    try:
        console.print("  Creating schema + tables...")
        execute_sql_file(conn, DDL)
        console.print("  Populating lookup tables...")
        execute_sql_file(conn, LOOKUPS)
        console.print("  Creating vth schema (KOOP_DDL)...")
        execute_sql_file(conn, KOOP_DDL)
        console.print("[green]Setup complete![/green]")
    finally:
        conn.close()


@cli.command("setup-koop")
def setup_koop():
    """Apply the vth schema only (incrementeel; voor PoC-DB's of nieuwe kolommen)."""
    from src.ddl import KOOP_DDL
    console.print("[bold]Applying vth schema[/bold]")
    conn = get_conn()
    try:
        execute_sql_file(conn, KOOP_DDL)
        console.print("[green]vth schema applied.[/green]")
    finally:
        conn.close()


@cli.command("load-koop")
@click.option("--from", "from_date", required=True, help="Startdatum YYYY-MM-DD (inclusief).")
@click.option("--to", "to_date", required=True, help="Einddatum YYYY-MM-DD (inclusief).")
@click.option("--force", is_flag=True,
              help="Re-ingest dagen die al status='ok' hebben in vth.etl_run.")
def load_koop_cmd(from_date, to_date, force):
    """Laad KOOP omgevingsvergunning-kennisgevingen voor een datumbereik."""
    from src.loaders.koop_vergunning import load_koop_range
    console.print(f"[bold]Loading KOOP omgevingsvergunningen[/bold] {from_date} .. {to_date}")
    if force:
        console.print("  [yellow]--force: bestaande status='ok' dagen worden opnieuw verwerkt[/yellow]")
    total = load_koop_range(from_date, to_date, force=force)
    console.print(f"[green]Klaar: {total:,} upserts.[/green]")


@cli.command("enrich-koop")
@click.option("--limit", type=int, default=5000,
              help="Max records per batch (default 5000).")
@click.option("--loop", is_flag=True,
              help="Blijf draaien tot er N empty cycles op rij zijn.")
@click.option("--sleep", type=int, default=120,
              help="Seconden slapen tussen empty cycles in --loop modus.")
@click.option("--stop-after-empty", type=int, default=5,
              help="Stop na N empty cycles in --loop modus.")
@click.option("--type-besluit", default=None,
              help="Filter op type_besluit (comma-separated, bv. "
                   "'verleend,geweigerd,ontwerp,van_rechtswege'). Records "
                   "buiten de filter blijven NULL en kunnen later met een "
                   "run zonder filter worden opgepakt — true incremental.")
def enrich_koop_cmd(limit, loop, sleep, stop_after_empty, type_besluit):
    """Verrijk records met volledige publicatie-XML, zaaknummer, deeplinks."""
    from src.loaders.koop_vergunning import enrich_records
    type_filter: tuple[str, ...] | None = None
    if type_besluit:
        type_filter = tuple(t.strip() for t in type_besluit.split(",") if t.strip())
        console.print(f"  Filtering op type_besluit IN {type_filter}")
    console.print(f"[bold]Enriching KOOP records[/bold] (limit={limit}, loop={loop})")
    total = enrich_records(
        limit=limit, loop=loop, sleep=sleep,
        stop_after_empty=stop_after_empty, type_filter=type_filter,
    )
    console.print(f"[green]Klaar: {total:,} records verrijkt.[/green]")


@cli.command("status-koop")
def status_koop_cmd():
    """Toon KOOP-DB statistieken: totaal, datums, per blad/activiteit/type."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Totaal + datum-bereik
            cur.execute("SELECT count(*) AS n FROM vth.vergunningkennisgeving")
            n_total = cur.fetchone()["n"]
        console.print(f"[bold]KOOP-DB[/bold] — totaal: [green]{n_total:,}[/green] records")
        if n_total == 0:
            return
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(datum_publicatie) AS lo, MAX(datum_publicatie) AS hi "
                "FROM vth.vergunningkennisgeving"
            )
            mm = cur.fetchone()
        console.print(f"Datumbereik: {mm['lo']} .. {mm['hi']}")

        # Per publicatieblad
        tbl = Table(title="Per publicatieblad")
        tbl.add_column("Blad")
        tbl.add_column("Aantal", justify="right")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT publicatieblad, count(*) AS n "
                "FROM vth.vergunningkennisgeving "
                "GROUP BY publicatieblad ORDER BY n DESC"
            )
            for r in cur.fetchall():
                tbl.add_row(r["publicatieblad"], f"{r['n']:,}")
        console.print(tbl)

        # Per activiteit (top 10)
        tbl = Table(title="Per activiteit (top 10)")
        tbl.add_column("Activiteit")
        tbl.add_column("Aantal", justify="right")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(activiteit_code,'(none)') AS a, count(*) AS n "
                "FROM vth.vergunningkennisgeving "
                "GROUP BY 1 ORDER BY n DESC LIMIT 10"
            )
            for r in cur.fetchall():
                tbl.add_row(r["a"], f"{r['n']:,}")
        console.print(tbl)

        # Per type_besluit
        tbl = Table(title="Per type_besluit")
        tbl.add_column("Type")
        tbl.add_column("Aantal", justify="right")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(type_besluit,'(none)') AS t, count(*) AS n "
                "FROM vth.vergunningkennisgeving "
                "GROUP BY 1 ORDER BY n DESC"
            )
            for r in cur.fetchall():
                tbl.add_row(r["t"], f"{r['n']:,}")
        console.print(tbl)

        # Per organisatietype
        tbl = Table(title="Per organisatietype")
        tbl.add_column("Type")
        tbl.add_column("Aantal", justify="right")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(organisatietype,'(none)') AS t, count(*) AS n "
                "FROM vth.vergunningkennisgeving "
                "GROUP BY 1 ORDER BY n DESC"
            )
            for r in cur.fetchall():
                tbl.add_row(r["t"], f"{r['n']:,}")
        console.print(tbl)

        # Per geometrie_type (met RD/WGS-vulling)
        tbl = Table(title="Per geometrie_type (met coords?)")
        tbl.add_column("Geometrie")
        tbl.add_column("Totaal", justify="right")
        tbl.add_column("RD", justify="right")
        tbl.add_column("WGS", justify="right")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(geometrie_type,'(NULL)') AS gt, count(*) AS n, "
                "  SUM(CASE WHEN geometrie_rd IS NOT NULL THEN 1 ELSE 0 END) AS rd, "
                "  SUM(CASE WHEN geometrie_wgs_pt IS NOT NULL THEN 1 ELSE 0 END) AS wgs "
                "FROM vth.vergunningkennisgeving "
                "GROUP BY 1 ORDER BY n DESC"
            )
            for r in cur.fetchall():
                tbl.add_row(r["gt"], f"{r['n']:,}", f"{r['rd']:,}", f"{r['wgs']:,}")
        console.print(tbl)

        # Adres-fields aanwezig
        with conn.cursor() as cur:
            cur.execute(
                "SELECT "
                "  SUM(CASE WHEN postcode IS NOT NULL THEN 1 ELSE 0 END) AS pc, "
                "  SUM(CASE WHEN straatnaam IS NOT NULL THEN 1 ELSE 0 END) AS st, "
                "  SUM(CASE WHEN woonplaats IS NOT NULL THEN 1 ELSE 0 END) AS wp, "
                "  count(*) AS tot "
                "FROM vth.vergunningkennisgeving"
            )
            ar = cur.fetchone()
        total_ar = ar["tot"] or 0
        def _pct(x: int) -> str:
            return f"{x:,} ({x/total_ar*100:.1f}%)" if total_ar else "0"
        tbl = Table(title="Adres-fields aanwezig")
        tbl.add_column("Veld")
        tbl.add_column("Vulling", justify="right")
        tbl.add_row("postcode", _pct(ar["pc"] or 0))
        tbl.add_row("straatnaam", _pct(ar["st"] or 0))
        tbl.add_row("woonplaats", _pct(ar["wp"] or 0))
        tbl.add_row("[bold]totaal[/bold]", f"[bold]{total_ar:,}[/bold]")
        console.print(tbl)

        # Laatste 5 etl_run-entries
        tbl = Table(title="Laatste 5 etl_run entries")
        tbl.add_column("processed_date")
        tbl.add_column("count", justify="right")
        tbl.add_column("status")
        tbl.add_column("error")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT processed_date, record_count, status, error "
                "FROM vth.etl_run ORDER BY processed_date DESC LIMIT 5"
            )
            for r in cur.fetchall():
                tbl.add_row(
                    str(r["processed_date"]),
                    str(r["record_count"] or 0),
                    r["status"],
                    (r["error"] or "")[:60],
                )
        console.print(tbl)

        # Deeplinks: totaal/werkt/404/unvalidated per host
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('vth.vergunning_deeplink') AS t")
            has_dl = cur.fetchone()["t"] is not None
        if has_dl:
            tbl = Table(title="Deeplinks per host")
            tbl.add_column("Host")
            tbl.add_column("Totaal", justify="right")
            tbl.add_column("Werkt", justify="right")
            tbl.add_column("404", justify="right")
            tbl.add_column("Unvalidated", justify="right")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT host, count(*) AS n, "
                    "  SUM(CASE WHEN werkt THEN 1 ELSE 0 END) AS ok, "
                    "  SUM(CASE WHEN http_status = 404 THEN 1 ELSE 0 END) AS nf, "
                    "  SUM(CASE WHEN gevalideerd_at IS NULL THEN 1 ELSE 0 END) AS unv "
                    "FROM vth.vergunning_deeplink "
                    "GROUP BY host ORDER BY n DESC"
                )
                for r in cur.fetchall():
                    tbl.add_row(
                        r["host"], f"{r['n']:,}", f"{r['ok'] or 0:,}",
                        f"{r['nf'] or 0:,}", f"{r['unv'] or 0:,}",
                    )
            console.print(tbl)
    finally:
        conn.close()


@cli.command("load-wro")
def load_wro():
    """Load Wro bestemmingsplannen from PDOK for the PoC municipality."""
    from src.loaders.wro_pdok import load_wro_plans
    console.print(f"[bold]Loading Wro plans[/bold] for {cfg.POC_GEMEENTE_NAAM} (CBS {cfg.POC_CBS_CODE})")
    load_wro_plans()


@cli.command("load-planvoorraad")
@click.option("--datum", default=None,
              help="Snapshotdatum YYYY-MM-DD (default: vandaag).")
@click.option("--page-size", type=int, default=100, help="pageSize voor /plannen.")
@click.option("--max-pages", type=int, default=None,
              help="Beperk aantal pagina's (dev/test; default: volledige trek).")
def load_planvoorraad_cmd(datum, page_size, max_pages):
    """Snapshot de IMRO-plannenvoorraad (bestemmingsplannen) via RP-Opvragen v4.

    Meet de leegloop van de bestemmingsplan-voorraad (wro.wro_snapshot +
    wro.wro_plan_observatie). Maandelijks draaien voor de temporele as.
    """
    from src.loaders.wro_planvoorraad import load_planvoorraad_snapshot
    load_planvoorraad_snapshot(datum=datum, page_size=page_size, max_pages=max_pages)


@cli.command("load-gemeentegrenzen")
def load_gemeentegrenzen_cmd():
    """Laad gemeente- + provinciegrenzen uit PDOK Bestuurlijke Gebieden.

    Vult core.gemeentegrens — voorwaarde voor v2a.ponsenkaart_gemeente_stats.
    Eenmalig/jaarlijks draaien (gemeente-herindelingen).
    """
    from src.loaders.gemeentegrens_pdok import load_gemeentegrenzen
    console.print("[bold]Loading Nederlandse gemeentegrenzen[/bold] (PDOK Bestuurlijke Gebieden)")
    load_gemeentegrenzen()


@cli.command("refresh-ponsenkaart-stats")
def refresh_ponsenkaart_stats_cmd():
    """Refresh v2a.ponsenkaart_gemeente_stats matview (nachtelijk of na OW-ingest)."""
    from src.loaders.gemeentegrens_pdok import refresh_ponsenkaart_stats
    console.print("[bold]Refreshing ponsenkaart-stats[/bold]")
    refresh_ponsenkaart_stats()


@cli.command("repair-pons-placeholders")
def repair_pons_placeholders_cmd():
    """Herstel pons-locaties die als POINT(0,0) zijn opgeslagen.

    Roept load_regeling_expand opnieuw aan voor alle regelingen van
    bronhouders met placeholder-ponsen. Met de fix uit 2026-05-21
    (locatieSelectie=primair) krijgt de Gebiedengroep nu wel een
    samengestelde geometrie. Refresht daarna de matview.
    """
    from src.db import get_conn
    from src.loaders.api_loader import load_regeling_expand
    from src.loaders.gemeentegrens_pdok import refresh_ponsenkaart_stats

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH placeholder_bh AS (
                  SELECT DISTINCT
                         SUBSTRING(p.identificatie FROM 'nl.imow-(gm[0-9]+)') AS bh
                    FROM p2p.pons p
                    JOIN p2p.locatie l ON l.identificatie = p.locatie_id
                   WHERE ST_X(ST_Centroid(l.geometrie)) = 0
                     AND ST_Y(ST_Centroid(l.geometrie)) = 0
                )
                SELECT r.frbr_work, r.frbr_expression, r.bronhouder, r.opschrift
                  FROM placeholder_bh pb
                  JOIN p2p.regeling r ON r.bronhouder = pb.bh
                 ORDER BY r.bronhouder, r.frbr_expression
            """)
            regelingen = cur.fetchall()

        if not regelingen:
            console.print("[yellow]Geen placeholder-ponsen — niets te repareren.[/yellow]")
            return

        console.print(f"[bold]Repair {len(regelingen)} regelingen[/bold]")

        ok, failed = 0, 0
        for r in regelingen:
            vals = list(r.values()) if hasattr(r, "keys") else r
            frbr_work, frbr_expression, bronhouder, opschrift = vals
            short_titel = (opschrift or "")[:50]
            try:
                stats = load_regeling_expand(conn, frbr_work, frbr_expression)
                marker = []
                if stats["regelingsgebied"]:
                    marker.append("rg")
                if stats["pons"]:
                    marker.append("pons")
                tag = f"[green]({','.join(marker)})[/green]" if marker else "[dim](-)[/dim]"
                console.print(f"  {bronhouder} {short_titel:50} {tag}")
                ok += 1
            except Exception as e:
                console.print(f"  [red]{bronhouder} {short_titel}: {e}[/red]")
                failed += 1

        console.print(f"\n[bold]Klaar:[/bold] {ok} ok, {failed} failed")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE ST_X(ST_Centroid(l.geometrie)) = 0
                                    AND ST_Y(ST_Centroid(l.geometrie)) = 0) AS placeholder,
                  COUNT(*) FILTER (WHERE ST_GeometryType(l.geometrie) <> 'ST_Point'
                                     OR ST_X(ST_Centroid(l.geometrie)) <> 0) AS echt,
                  COUNT(*) AS totaal
                  FROM p2p.pons p
                  JOIN p2p.locatie l ON l.identificatie = p.locatie_id
            """)
            row = cur.fetchone()
            v = list(row.values()) if hasattr(row, "keys") else row
            console.print(f"\nPons-locaties na repair: {v[1]} echt, {v[0]} placeholder, {v[2]} totaal")
    finally:
        conn.close()

    console.print()
    refresh_ponsenkaart_stats()


@cli.command("load-wro-structuurvisies")
@click.option("--niveau", "-n", multiple=True, type=click.Choice(["G", "P", "R"]),
              help="G=gemeentelijk, P=provinciaal, R=rijk (herhaalbaar). Default: alle.")
@click.option("--code", "-c", multiple=True,
              help="Filter op bronhouder-code (pv25, gm0344, rijk). Herhaalbaar.")
def load_wro_structuurvisies_cmd(niveau, code):
    """Laad Wro-structuurvisies uit PDOK (provinciaal/gemeentelijk/rijk)."""
    from src.loaders.wro_pdok import load_wro_structuurvisies
    niveaus = list(niveau) if niveau else None
    codes = set(code) if code else None
    load_wro_structuurvisies(niveaus=niveaus, codes=codes)


@cli.command("load-imtr")
def load_imtr():
    """Load toepasbare regels (RTR activiteiten + STTR regelbestanden)."""
    from src.loaders.imtr_loader import load_imtr
    console.print(f"[bold]Loading IMTR[/bold] for {cfg.POC_GEMEENTE_NAAM} (OIN {cfg.POC_OIN})")
    load_imtr()


@cli.command("load-ow")
@click.option("--types", "-t", default=None, help="Comma-separated documenttypes to load (e.g. 'Programma,Omgevingsvisie'). Default: all.")
@click.option("--gemeente", "-g", default=None, help="CBS code + name (e.g. '0307,Amersfoort'). Default: PoC gemeente.")
@click.option("--overheid", "-o", default=None, help="Overheid code + name (e.g. 'pv26,Provincie Utrecht'). For provinces, waterschappen, etc.")
def load_ow(types, gemeente, overheid):
    """Load Ow regelingen via DSO Download API."""
    from src.loaders.ow_loader import load_ow as _load_ow, load_ow_gemeente, load_ow_overheid

    doc_types = [t.strip() for t in types.split(",")] if types else None

    if overheid:
        parts = overheid.split(",", 1)
        code = parts[0].strip()
        naam = parts[1].strip() if len(parts) > 1 else code
        console.print(f"[bold]Loading Ow regelingen[/bold] for {naam} ({code})")
        if doc_types:
            console.print(f"  Filtering: {doc_types}")
        load_ow_overheid(code, naam, code, doc_types=doc_types)
    elif gemeente:
        parts = gemeente.split(",", 1)
        cbs = parts[0].strip()
        naam = parts[1].strip() if len(parts) > 1 else cbs
        console.print(f"[bold]Loading Ow regelingen[/bold] for {naam} (CBS {cbs})")
        if doc_types:
            console.print(f"  Filtering: {doc_types}")
        load_ow_gemeente(cbs, naam, doc_types=doc_types)
    else:
        console.print(f"[bold]Loading Ow regelingen[/bold] for {cfg.POC_GEMEENTE_NAAM}")
        if doc_types:
            console.print(f"  Filtering: {doc_types}")
            load_ow_gemeente(cfg.POC_CBS_CODE, cfg.POC_GEMEENTE_NAAM, doc_types=doc_types)
        else:
            _load_ow()


@cli.command("load-api")
@click.option("--types", "-t", default=None, help="Comma-separated documenttypes (e.g. 'Programma,Omgevingsvisie').")
@click.option("--gemeente", "-g", default=None, help="CBS code + name (e.g. '0307,Amersfoort').")
@click.option("--overheid", "-o", default=None, help="Overheid code + name (e.g. 'pv26,Provincie Utrecht').")
def load_api(types, gemeente, overheid):
    """Load Ow regelingen via Presenteren v8 + Geometrie API (no ZIP)."""
    from src.loaders.api_loader import load_via_api

    doc_types = [t.strip() for t in types.split(",")] if types else None

    if overheid:
        parts = overheid.split(",", 1)
        code = parts[0].strip()
        naam = parts[1].strip() if len(parts) > 1 else code
        console.print(f"[bold]Loading via API[/bold] for {naam} ({code})")
        load_via_api(code, naam, doc_types=doc_types)
    elif gemeente:
        parts = gemeente.split(",", 1)
        cbs = parts[0].strip()
        naam = parts[1].strip() if len(parts) > 1 else cbs
        console.print(f"[bold]Loading via API[/bold] for {naam} (gm{cbs})")
        load_via_api(f"gm{cbs}", naam, bronhouder_code=cbs, doc_types=doc_types)
    else:
        console.print(f"[bold]Loading via API[/bold] for {cfg.POC_GEMEENTE_NAAM} (gm{cfg.POC_CBS_CODE})")
        load_via_api(f"gm{cfg.POC_CBS_CODE}", cfg.POC_GEMEENTE_NAAM,
                     bronhouder_code=cfg.POC_CBS_CODE, doc_types=doc_types)


@cli.command("refresh-subdiv")
@click.option("--bronhouder", "-b", default=None,
              help="Beperk tot één bronhouder-code (bv. gm0344). Default: alle polygon-locaties (volledige rebuild).")
def refresh_subdiv_cmd(bronhouder):
    """(Her)bouw p2p.locatie_subdiv (afgeleide subdivided geometrie).

    Wordt automatisch na elke OW-load gedraaid; gebruik dit command voor een
    handmatige rebuild, bv. na een bulk-geometrie-correctie of een verse DB.
    """
    from src.loaders.subdiv import refresh_main
    refresh_main(bronhouder)


@cli.command()
def status():
    """Show row counts per table."""
    conn = get_conn()
    try:
        tables = [
            "bronhouder", "regeling", "besluit", "tekst_element",
            "geo_informatieobject", "locatie", "juridische_regel",
            "activiteit", "activiteit_locatieaanduiding",
            "gebiedsaanwijzing", "norm", "normwaarde", "kaart", "kaartlaag",
            "tekstdeel", "pons",
            "regelbeheerobject", "toepasbaar_regelbestand", "dmn_element",
            "uitvoeringsregel", "werkzaamheid",
            "ruimtelijk_instrument", "planobject", "wro_tekst_object",
        ]

        tbl = Table(title=f"DSO Database — {cfg.DB_NAME}")
        tbl.add_column("Table", style="cyan")
        tbl.add_column("Rows", justify="right", style="green")

        total = 0
        for t in tables:
            try:
                n = table_count(conn, t)
                tbl.add_row(t, f"{n:,}")
                total += n
            except Exception:
                tbl.add_row(t, "[red]error[/red]")

        tbl.add_row("[bold]TOTAL[/bold]", f"[bold]{total:,}[/bold]")
        console.print(tbl)
    finally:
        conn.close()


@cli.command()
def backup():
    """Create a compressed backup of the database."""
    import subprocess
    from datetime import date

    dest = cfg.DOWNLOAD_DIR.parent / "backup" / f"dso_backup_{date.today().isoformat().replace('-','')}.dump"
    dest.parent.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Creating backup[/bold] → {dest}")
    result = subprocess.run(
        ["docker", "exec", "dso-postgis", "pg_dump", "-U", "postgres", "-Fc", "dso"],
        capture_output=True,
    )
    if result.returncode == 0:
        dest.write_bytes(result.stdout)
        console.print(f"[green]Backup complete: {dest} ({dest.stat().st_size / 1e6:.1f} MB)[/green]")
    else:
        console.print(f"[red]Backup failed: {result.stderr.decode()}[/red]")


@cli.command()
@click.argument("dump_file", type=click.Path(exists=True))
def restore(dump_file):
    """Restore the database from a backup file."""
    import subprocess

    console.print(f"[bold]Restoring from[/bold] {dump_file}")
    console.print("[yellow]This will overwrite all current data![/yellow]")

    # Drop and recreate
    subprocess.run(["docker", "exec", "dso-postgis", "dropdb", "-U", "postgres", "--if-exists", "dso"])
    subprocess.run(["docker", "exec", "dso-postgis", "createdb", "-U", "postgres", "dso"])
    subprocess.run(["docker", "exec", "dso-postgis", "psql", "-U", "postgres", "-d", "dso",
                    "-c", "CREATE EXTENSION IF NOT EXISTS postgis;"])

    # Restore
    with open(dump_file, "rb") as f:
        result = subprocess.run(
            ["docker", "exec", "-i", "dso-postgis", "pg_restore", "-U", "postgres", "-d", "dso", "--no-owner"],
            stdin=f,
        )

    if result.returncode == 0:
        console.print("[green]Restore complete![/green]")
    else:
        console.print("[yellow]Restore finished with warnings (usually harmless for pg_restore)[/yellow]")


@cli.command("load-wro-teksten")
@click.option("--gemeente", "-g", default=None, help="CBS code(s), comma-separated. Default: all loaded.")
def load_wro_teksten(gemeente):
    """Load Wro planteksten via IHR API."""
    from src.loaders.ihr_loader import load_wro_teksten as _load
    codes = [c.strip() for c in gemeente.split(",")] if gemeente else None
    _load(codes)


@cli.command("adres")
@click.argument("adres", nargs=-1)
def adres(adres):
    """What Ow rules and Wro bestemmingen apply at an address? E.g.: adres Keizersgracht 100 Amsterdam"""
    from src.query import wat_geldt_op_adres
    wat_geldt_op_adres(" ".join(adres))


@cli.command("wat-geldt-hier")
@click.argument("x", type=float)
@click.argument("y", type=float)
def wat_geldt_hier(x, y):
    """What Ow rules and Wro bestemmingen apply at coordinate X Y (RD/EPSG:28992)?"""
    from src.query import wat_geldt_hier as _q
    _q(x, y)


@cli.command("activiteiten")
@click.argument("gemeente")
def activiteiten(gemeente):
    """List all activities for a municipality (CBS code, e.g. 0363)."""
    from src.query import welke_activiteiten
    welke_activiteiten(gemeente)


@cli.command("normen")
@click.argument("gemeente")
def normen(gemeente):
    """List all norms for a municipality (CBS code)."""
    from src.query import normen_gemeente
    normen_gemeente(gemeente)


@cli.command("werkzaamheid")
@click.argument("zoekterm")
def werkzaamheid(zoekterm):
    """Trace a werkzaamheid through the full chain to artikelen."""
    from src.query import werkzaamheid_keten
    werkzaamheid_keten(zoekterm)


@cli.command("pons")
@click.argument("gemeente")
def pons_cmd(gemeente):
    """Show pons status for a municipality."""
    from src.query import pons_status
    pons_status(gemeente)


@cli.command("zoek")
@click.argument("zoekterm")
def zoek(zoekterm):
    """Full-text search across all article texts."""
    from src.query import zoek_tekst
    zoek_tekst(zoekterm)


@cli.command("gezagen")
def gezagen():
    """Show all bevoegde gezagen with load status (Ow/IMTR/Wro/Tekst)."""
    from src.query import bevoegde_gezagen
    bevoegde_gezagen()


@cli.command("overzicht")
def overzicht():
    """Show database overview."""
    from src.query import overzicht as _o
    _o()


@cli.group()
def pipeline():
    """Keten-gedreven pipeline (core/p2p/wro/i2a)."""
    pass


def _resolve_bronhouders(file: str | None, code: tuple[str, ...], naam: tuple[str, ...]):
    from src.pipeline.bronhouders import Bronhouder, _infer_type, load_bronhouders
    if file:
        return load_bronhouders(file)
    if code:
        if len(naam) != len(code):
            raise click.UsageError("Aantal --naam moet gelijk zijn aan aantal --code, of laat --naam weg.")
        names = naam if naam else code
        return [Bronhouder(code=c, naam=n, type=_infer_type(c)) for c, n in zip(code, names)]
    raise click.UsageError("Geef --file of minstens één --code op.")


_INPUT_OPTS = [
    click.option("--file", "-f", type=click.Path(exists=True),
                 help="JSON-bestand met {code: naam}-mapping."),
    click.option("--code", "-c", multiple=True, help="Bronhouder-code (herhalen toegestaan)."),
    click.option("--naam", "-n", multiple=True, help="Bronhouder-naam (parallel aan --code)."),
]


def _add_input_opts(fn):
    for opt in reversed(_INPUT_OPTS):
        fn = opt(fn)
    return fn


@pipeline.command("core")
def pipeline_core():
    """Bootstrap: schema's, tabellen en lookup-data."""
    from src.pipeline import core
    core.bootstrap()


@pipeline.command("p2p")
@_add_input_opts
@click.option("--types", "-t", default=None,
              help="Comma-gescheiden documenttypes (bv. 'Omgevingsplan,Omgevingsvisie').")
def pipeline_p2p(file, code, naam, types):
    """Laad Ow-regelingen via DSO Presenteren API."""
    from src.pipeline import p2p
    bhs = _resolve_bronhouders(file, code, naam)
    doc_types = [t.strip() for t in types.split(",")] if types else None
    p2p.run(bhs, doc_types=doc_types)


@pipeline.command("wro")
@_add_input_opts
@click.option("--no-teksten", is_flag=True, help="Sla IHR-teksten over (alleen plannen).")
def pipeline_wro(file, code, naam, no_teksten):
    """Laad Wro-bestemmingsplannen via PDOK + teksten via IHR."""
    from src.pipeline import wro
    bhs = _resolve_bronhouders(file, code, naam)
    wro.run(bhs, include_teksten=not no_teksten)


@pipeline.command("i2a")
@_add_input_opts
@click.option("--no-werkzaamheden", is_flag=True, help="Sla werkzaamhedencatalogus over.")
def pipeline_i2a(file, code, naam, no_werkzaamheden):
    """Laad toepasbare regels (RTR + STTR) en werkzaamhedencatalogus."""
    from src.pipeline import i2a
    bhs = _resolve_bronhouders(file, code, naam)
    i2a.run(bhs, load_werkzaamheden=not no_werkzaamheden)


@pipeline.command("all")
@_add_input_opts
@click.option("--types", "-t", default=None, help="Documenttype-filter voor p2p.")
@click.option("--no-wro-teksten", is_flag=True, help="Sla IHR-teksten over.")
def pipeline_all(file, code, naam, types, no_wro_teksten):
    """Draai p2p, wro en i2a in volgorde voor dezelfde bronhouder-set."""
    from src.pipeline import all_ketens
    bhs = _resolve_bronhouders(file, code, naam)
    doc_types = [t.strip() for t in types.split(",")] if types else None
    results = all_ketens(bhs, doc_types=doc_types,
                         include_wro_teksten=not no_wro_teksten)
    console.print()
    console.print(f"[bold]Pipeline klaar[/bold] — ketens: {list(results.keys())}")


@cli.group()
def wijziging():
    """p2p-wijzigingen: ontwerpen en besluitversies."""
    pass


@wijziging.command("ontwerpen")
def wijziging_load_ontwerpen():
    """Laad alle relevante ontwerpregelingen via Presenteren v8."""
    from src.loaders.ontwerp_loader import load_alle_ontwerpen
    load_alle_ontwerpen()


@wijziging.command("besluiten")
def wijziging_load_besluiten():
    """Laad alle relevante besluitversies via Presenteren v8."""
    from src.loaders.ontwerp_loader import load_alle_besluitversies
    load_alle_besluitversies()


@wijziging.command("status")
def wijziging_status():
    """Toon overzicht van geladen ontwerpen en besluiten."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT soort, status, count(*),
                       count(*) FILTER (WHERE begin_inwerking > now()) AS toekomstig
                FROM p2pwijziging.besluit
                GROUP BY soort, status
                ORDER BY soort, status
            """)
            rows = cur.fetchall()

            tbl = Table(title="p2pwijziging-status")
            tbl.add_column("Soort")
            tbl.add_column("Status")
            tbl.add_column("Aantal", justify="right")
            tbl.add_column("Toekomstig", justify="right")
            for r in rows:
                tbl.add_row(r["soort"], r["status"], str(r["count"]), str(r["toekomstig"]))
            console.print(tbl)

            cur.execute("""SELECT count(*) AS n,
                                  count(*) FILTER (WHERE wijzigactie IS NOT NULL OR vervallen) AS n_wij
                           FROM p2pwijziging.tekst_element""")
            te = cur.fetchone()
            cur.execute("""SELECT count(*) FROM p2pwijziging.annotatie_delta""")
            ann = cur.fetchone()["count"]
            cur.execute("""SELECT count(*) FROM p2pwijziging.locatie_delta""")
            loc = cur.fetchone()["count"]
            console.print(
                f"\n  tekst_element: {te['n']} ({te['n_wij']} met renvooi), "
                f"annotatie_delta: {ann}, locatie_delta: {loc}"
            )
    finally:
        conn.close()


@cli.group()
def convert():
    """Bestemmingsplan -> omgevingsplan conversie (wro -> conv)."""
    pass


@convert.command("plan")
@click.argument("instrument_idn")
def convert_plan(instrument_idn):
    """Converteer een enkel bestemmingsplan (NL.IMRO-identificatie)."""
    from src.converter.stap1 import convert_bestemmingsplan
    convert_bestemmingsplan(instrument_idn)


@convert.command("gemeente")
@click.argument("code")
def convert_gemeente_cmd(code):
    """Converteer alle vigerende bestemmingsplannen van een gemeente (CBS-code)."""
    from src.converter.stap1 import convert_gemeente
    results = convert_gemeente(code)
    ok = sum(1 for r in results if "error" not in r)
    err = sum(1 for r in results if "error" in r)
    console.print(f"\n[bold]Conversie klaar: {ok} gelukt, {err} fouten[/bold]")


@convert.command("annotate")
@click.argument("regeling_expression")
def convert_annotate(regeling_expression):
    """Stap 2: LLM-annotatievoorstel voor een geconverteerd plan."""
    from src.converter.stap2 import annotate_bestemmingsplan
    annotate_bestemmingsplan(regeling_expression)


@convert.command("match")
@click.argument("regeling_expression")
@click.option("--min-score", default=0.5, help="Minimum match-score voor weergave (0-1).")
@click.option("--persist", is_flag=True, help="Sla matches >= 70%% op in conv.activiteit.")
@click.option("--persist-min-score", default=0.7, help="Minimum score voor persistentie.")
def convert_match(regeling_expression, min_score, persist, persist_min_score):
    """Match artikelen tegen bestaande activiteiten/werkzaamheden (geen LLM)."""
    from src.converter.matcher import match_bestemmingsplan
    match_bestemmingsplan(regeling_expression, min_score=min_score,
                          persist=persist, persist_min_score=persist_min_score)


@convert.command("clear")
@click.argument("code")
@click.confirmation_option(prompt="Alle conversie-data voor deze gemeente wissen?")
def convert_clear(code):
    """Wis conversie-data voor een gemeente (voor re-run)."""
    from src.converter.stap1 import clear_gemeente
    n = clear_gemeente(code)
    console.print(f"[green]{n} regelingen gewist uit conv-schema[/green]")


@convert.command("status")
def convert_status():
    """Toon conversie-status per gemeente."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.bronhouder, b.naam,
                       count(DISTINCT r.frbr_expression) AS regelingen,
                       count(DISTINCT te.id) AS tekst_elementen,
                       count(DISTINCT l.identificatie) AS locaties,
                       count(DISTINCT ga.identificatie) AS gebiedsaanwijzingen
                FROM conv.regeling r
                JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
                LEFT JOIN conv.tekst_element te ON te.regeling_expression = r.frbr_expression
                LEFT JOIN conv.locatie l ON l.bron_planobject IS NOT NULL
                LEFT JOIN conv.gebiedsaanwijzing ga ON TRUE
                GROUP BY r.bronhouder, b.naam
                ORDER BY b.naam
            """)
            rows = cur.fetchall()

        if not rows:
            console.print("[yellow]Geen conversie-data gevonden[/yellow]")
            return

        tbl = Table(title="Conversie-status (conv-schema)")
        tbl.add_column("Gemeente")
        tbl.add_column("Regelingen", justify="right")
        tbl.add_column("Teksten", justify="right")
        tbl.add_column("Locaties", justify="right")
        tbl.add_column("GA's", justify="right")
        for r in rows:
            tbl.add_row(r["naam"], str(r["regelingen"]),
                        str(r["tekst_elementen"]), str(r["locaties"]),
                        str(r["gebiedsaanwijzingen"]))
        console.print(tbl)
    finally:
        conn.close()


if __name__ == "__main__":
    cli()
