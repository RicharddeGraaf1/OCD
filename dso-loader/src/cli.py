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

    conn = get_conn()
    try:
        console.print("  Creating schema + tables...")
        execute_sql_file(conn, DDL)
        console.print("  Populating lookup tables...")
        execute_sql_file(conn, LOOKUPS)
        console.print("[green]Setup complete![/green]")
    finally:
        conn.close()


@cli.command("load-wro")
def load_wro():
    """Load Wro bestemmingsplannen from PDOK for the PoC municipality."""
    from src.loaders.wro_pdok import load_wro_plans
    console.print(f"[bold]Loading Wro plans[/bold] for {cfg.POC_GEMEENTE_NAAM} (CBS {cfg.POC_CBS_CODE})")
    load_wro_plans()


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
