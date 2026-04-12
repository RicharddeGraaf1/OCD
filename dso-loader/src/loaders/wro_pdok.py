"""Load Wro bestemmingsplannen from PDOK GML downloads.

PDOK provides nationwide GML.GZ files per feature type.
We download, decompress, parse with lxml, filter on CBS code,
and insert into PostGIS.
"""

import gzip
import io
from pathlib import Path

import httpx
from lxml import etree
from rich.console import Console
from rich.progress import track

from src.config import cfg
from src.db import get_conn

console = Console()

# PDOK download URLs per feature type
PDOK_FEATURES = {
    "Bestemmingsplangebied": "Bestemmingsplangebied",
    "Enkelbestemming": "Enkelbestemming",
    "Dubbelbestemming": "Dubbelbestemming",
    "Bouwvlak": "Bouwvlak",
    "Functieaanduiding": "Functieaanduiding",
    "Bouwaanduiding": "Bouwaanduiding",
    "Maatvoering": "Maatvoering",
    "Figuur": "Figuur",
    "Gebiedsaanduiding": "Gebiedsaanduiding",
}

IMRO_NS = {
    "app": "http://www.opengis.net/gml",
    "gml": "http://www.opengis.net/gml",
}


def _download_gml_gz(feature_type: str) -> Path:
    """Download a PDOK GML.GZ file if not already cached."""
    url = f"{cfg.PDOK_ATOM_BASE}/{feature_type}.gml.gz"
    dest = cfg.DOWNLOAD_DIR / f"{feature_type}.gml.gz"

    if dest.exists():
        console.print(f"  [dim]Using cached {dest.name} ({dest.stat().st_size / 1e6:.0f} MB)[/dim]")
        return dest

    console.print(f"  Downloading {url} ...")
    dest.parent.mkdir(parents=True, exist_ok=True)

    with httpx.stream("GET", url, timeout=600) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        with open(dest, "wb") as f:
            downloaded = 0
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    console.print(f"\r  {pct:.0f}% ({downloaded / 1e6:.0f}/{total / 1e6:.0f} MB)", end="")
        console.print()

    console.print(f"  [green]Downloaded {dest.name} ({dest.stat().st_size / 1e6:.0f} MB)[/green]")
    return dest


def _iter_features(gz_path: Path, tag_local: str):
    """Iterate over GML features in a gzipped file using iterparse.

    Yields lxml Elements one at a time to keep memory low.
    """
    with gzip.open(gz_path, "rb") as f:
        context = etree.iterparse(f, events=("end",), tag=f"{{{IMRO_NS['app']}}}{tag_local}")
        for _, elem in context:
            yield elem
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]


def _extract_text(elem, xpath: str) -> str | None:
    """Extract text from an element via XPath."""
    found = elem.xpath(xpath, namespaces=IMRO_NS)
    if found:
        return found[0].text if hasattr(found[0], "text") else str(found[0])
    return None


def _extract_gml_geometry(elem) -> str | None:
    """Extract the GML geometry from the app:geometrie wrapper."""
    ns = IMRO_NS['gml']
    # First find the app:geometrie wrapper
    geom_wrapper = elem.find(f"{{{ns}}}geometrie")
    if geom_wrapper is None:
        return None
    # Then find the first actual GML geometry element inside
    for geom_tag in ["MultiSurface", "Surface", "Polygon", "MultiCurve",
                     "LineString", "Point", "MultiGeometry"]:
        geom_elem = geom_wrapper.find(f"{{{ns}}}{geom_tag}")
        if geom_elem is not None:
            return etree.tostring(geom_elem, encoding="unicode")
    # Fallback: take the first child element
    for child in geom_wrapper:
        return etree.tostring(child, encoding="unicode")
    return None


def _load_bestemmingsplangebied(conn, cbs_codes: dict[str, str] | None = None):
    """Load Bestemmingsplangebied features for given CBS codes.

    Args:
        cbs_codes: dict of {cbs_code: naam}. If None, uses PoC gemeente.
    """
    if cbs_codes is None:
        cbs_codes = {cfg.POC_CBS_CODE: cfg.POC_GEMEENTE_NAAM}

    gz_path = _download_gml_gz("Bestemmingsplangebied")
    code_set = set(cbs_codes.keys())

    console.print(f"  Parsing Bestemmingsplangebied for {len(code_set)} gemeenten...")

    with conn.cursor() as cur:
        for code, naam in cbs_codes.items():
            cur.execute(
                "INSERT INTO dso.bronhouder (overheidscode, naam) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (code, naam),
            )
            cur.execute(
                "INSERT INTO dso.wro_manifest (overheidscode, naam_overheid) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (code, naam),
            )
        for extra in ["onherroepelijk", "vigerend", "goedgekeurd", "geconsolideerde versie",
                      "uitspraak afdeling bestuursrechtspraak", "onbekend"]:
            cur.execute("INSERT INTO dso.planstatus (code) VALUES (%s) ON CONFLICT DO NOTHING", (extra,))
    conn.commit()

    count = 0
    with conn.cursor() as cur:
        for elem in _iter_features(gz_path, "Bestemmingsplangebied"):
            overheids_code = _extract_text(elem, "app:overheidsCode")
            if overheids_code not in code_set:
                continue

            idn = _extract_text(elem, "app:identificatie")
            naam = _extract_text(elem, "app:naam")
            type_plan = _extract_text(elem, "app:typePlan")
            planstatus_raw = _extract_text(elem, "app:planstatus") or "onbekend"
            planstatus_val = planstatus_raw.split(";")[0].strip()
            datum = _extract_text(elem, "app:datum")

            gml_str = _extract_gml_geometry(elem)
            if not gml_str or not idn:
                continue

            dossier = idn.rsplit("-", 1)[0] if "-" in idn else idn
            cur.execute(
                """INSERT INTO dso.wro_dossier (dossiernummer, manifest_code, status)
                   VALUES (%s, %s, NULL) ON CONFLICT DO NOTHING""",
                (dossier, overheids_code),
            )

            cur.execute(
                """INSERT INTO dso.ruimtelijk_instrument
                   (idn, dossier, type_plan, naam, planstatus, datum, bronhouder,
                    geometrie, gml_source, pons_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s,
                           ST_GeomFromGML(%s, 28992), %s, 'actief')
                   ON CONFLICT (idn) DO NOTHING""",
                (idn, dossier, type_plan or "onbekend", naam or "onbekend",
                 planstatus_val, datum, overheids_code,
                 gml_str, gml_str),
            )
            count += 1

            if count % 100 == 0:
                conn.commit()
                console.print(f"\r  {count} plangebieden geladen...", end="")

    conn.commit()
    console.print(f"\n  [green]{count} bestemmingsplangebieden geladen voor {len(code_set)} gemeenten[/green]")
    return count


def _load_planobjecten(conn, feature_type: str, object_type: str, cbs_codes: set[str] | None = None):
    """Load planobject features for already-loaded instruments."""
    gz_path = _download_gml_gz(feature_type)

    with conn.cursor() as cur:
        if cbs_codes:
            cur.execute("SELECT idn FROM dso.ruimtelijk_instrument WHERE bronhouder = ANY(%s)", (list(cbs_codes),))
        else:
            cur.execute("SELECT idn FROM dso.ruimtelijk_instrument WHERE bronhouder = %s", (cfg.POC_CBS_CODE,))
        loaded_idns = {row["idn"] for row in cur.fetchall()}

    if not loaded_idns:
        console.print(f"  [yellow]No instruments loaded yet — skipping {feature_type}[/yellow]")
        return 0

    console.print(f"  Parsing {feature_type} for {len(loaded_idns)} instruments...")

    count = 0
    with conn.cursor() as cur:
        for elem in _iter_features(gz_path, feature_type):
            identificatie = _extract_text(elem, "app:identificatie")
            # Link to plangebied via plangebied reference
            plangebied_ref = _extract_text(elem, "app:plangebied")
            if not plangebied_ref:
                continue

            # Check if this planobject belongs to a loaded instrument
            # The plangebied href is typically like #NL.IMRO.0344....
            plan_idn = plangebied_ref.lstrip("#") if plangebied_ref.startswith("#") else plangebied_ref
            if plan_idn not in loaded_idns:
                continue

            naam = _extract_text(elem, "app:naam")
            bestemmingshoofdgroep = _extract_text(elem, "app:bestemmingshoofdgroep")

            gml_str = _extract_gml_geometry(elem)
            if not gml_str or not identificatie:
                continue

            # Ensure hoofdgroep exists in lookup (covers dubbelbestemming + unknown values)
            if bestemmingshoofdgroep:
                cur.execute(
                    "INSERT INTO dso.bestemmingshoofdgroep (code) VALUES (%s) ON CONFLICT DO NOTHING",
                    (bestemmingshoofdgroep,),
                )

            try:
                cur.execute(
                    """INSERT INTO dso.planobject
                       (identificatie, instrument_idn, object_type, naam,
                        bestemmingshoofdgroep, geometrie, gml_source)
                       VALUES (%s, %s, %s, %s, %s,
                               ST_GeomFromGML(%s, 28992), %s)
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (identificatie, plan_idn, object_type, naam,
                     bestemmingshoofdgroep, gml_str, gml_str),
                )
            except Exception:
                conn.rollback()  # Skip bad geometries
            count += 1

            if count % 200 == 0:
                conn.commit()
                console.print(f"\r  {count} {feature_type} geladen...", end="")

    conn.commit()
    console.print(f"\n  [green]{count} {feature_type} geladen[/green]")
    return count


def load_wro_plans(cbs_codes: dict[str, str] | None = None):
    """Load Wro plans. If cbs_codes given, load for all; else PoC only."""
    conn = get_conn()
    try:
        n_plans = _load_bestemmingsplangebied(conn, cbs_codes)

        if n_plans == 0:
            console.print("[yellow]No plans found[/yellow]")
            return

        code_set = set(cbs_codes.keys()) if cbs_codes else None
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
            _load_planobjecten(conn, feature, obj_type, code_set)

        console.print("[bold green]Wro loading complete![/bold green]")
    finally:
        conn.close()
