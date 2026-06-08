"""Load Wro bestemmingsplannen from PDOK GML downloads.

PDOK provides nationwide GML.GZ files per feature type.
We download, decompress, parse with lxml, filter on CBS code,
and insert into PostGIS.
"""

import gzip
import io
import json
import re
from pathlib import Path

import httpx
from lxml import etree
from rich.console import Console
from rich.progress import track

from src.canonieke_bronhouders import upsert_bronhouder
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


_UNIT_SUFFIX = {
    "m": "_m", "m²": "_m2", "m2": "_m2", "%": "_pct",
    "m³": "_m3", "m3": "_m3", "ha": "_ha",
}
_MV_PAIR_RE = re.compile(r'"([^"]+)"\s*=\s*"([^"]+)"')


def _slug(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"\W+", "_", s.strip().lower())).strip("_")


def _parse_maatvoering_kv(text: str | None) -> dict | None:
    """Parse PDOK app:maatvoering platte k/v-string naar dict.

    Voorbeeld: '"maximum bouwhoogte (m)"="9", "maximum bebouwingspercentage (%)"="60"'
    → {"maximum_bouwhoogte_m": 9.0, "maximum_bebouwingspercentage_pct": 60.0}

    Decimal-comma's worden naar punt geconverteerd. Niet-numerieke waarden
    blijven string. Lege/NULL input → None (kolom blijft NULL).
    """
    if not text:
        return None
    out: dict[str, float | str] = {}
    for raw_key, raw_val in _MV_PAIR_RE.findall(text):
        m = re.match(r"(.*?)\s*\(([^)]+)\)\s*$", raw_key)
        if m:
            base, unit = m.group(1), m.group(2)
            suffix = _UNIT_SUFFIX.get(unit.strip(), "_" + _slug(unit))
            key = _slug(base) + suffix
        else:
            key = _slug(raw_key)
        v = raw_val.strip().replace(",", ".")
        try:
            out[key] = float(v)
        except ValueError:
            out[key] = raw_val.strip()
    return out or None


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

    from src.db import normalize_bronhouder_code
    with conn.cursor() as cur:
        for code, naam in cbs_codes.items():
            gm_code = normalize_bronhouder_code(code)
            upsert_bronhouder(cur, gm_code, naam, bestuurslaag="gemeente")
            cur.execute(
                "INSERT INTO wro.wro_manifest (overheidscode, naam_overheid) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (gm_code, naam),
            )
        for extra in ["onherroepelijk", "vigerend", "goedgekeurd", "geconsolideerde versie",
                      "uitspraak afdeling bestuursrechtspraak", "onbekend"]:
            cur.execute("INSERT INTO core.planstatus (code) VALUES (%s) ON CONFLICT DO NOTHING", (extra,))
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
            # PDOK levert dossierStatus zoals "geheel onherroepelijk in werking";
            # voor wro.wro_dossier.status zetten we deze door.
            dossier_status = _extract_text(elem, "app:dossierStatus")

            gml_str = _extract_gml_geometry(elem)
            if not gml_str or not idn:
                continue

            gm_overheid = normalize_bronhouder_code(overheids_code)
            dossier = idn.rsplit("-", 1)[0] if "-" in idn else idn
            # Self-populate core.dossierstatus voor nieuwe code-waarden (FK).
            if dossier_status:
                cur.execute(
                    "INSERT INTO core.dossierstatus (code) VALUES (%s) ON CONFLICT DO NOTHING",
                    (dossier_status,),
                )
            cur.execute(
                """INSERT INTO wro.wro_dossier (dossiernummer, manifest_code, status)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (dossiernummer) DO UPDATE SET
                       status = COALESCE(EXCLUDED.status, wro.wro_dossier.status)""",
                (dossier, gm_overheid, dossier_status),
            )

            cur.execute(
                """INSERT INTO wro.ruimtelijk_instrument
                   (idn, dossier, type_plan, naam, planstatus, datum, bronhouder,
                    geometrie, gml_source, pons_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s,
                           ST_GeomFromGML(%s, 28992), %s, 'actief')
                   ON CONFLICT (idn) DO NOTHING""",
                (idn, dossier, type_plan or "onbekend", naam or "onbekend",
                 planstatus_val, datum, gm_overheid,
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
    from src.db import normalize_bronhouder_code
    gz_path = _download_gml_gz(feature_type)

    with conn.cursor() as cur:
        if cbs_codes:
            gm_codes = [normalize_bronhouder_code(c) for c in cbs_codes]
            cur.execute("SELECT idn FROM wro.ruimtelijk_instrument WHERE bronhouder = ANY(%s)", (gm_codes,))
        else:
            cur.execute("SELECT idn FROM wro.ruimtelijk_instrument WHERE bronhouder = %s", (normalize_bronhouder_code(cfg.POC_CBS_CODE),))
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
            # PDOK levert artikelnummer voor Gebiedsaanduiding (100% in sample).
            # Voor andere object_types ontbreekt het meestal — _extract_text
            # geeft dan NULL, dus geen extra check nodig.
            artikelnummer = _extract_text(elem, "app:artikelnummer")

            # Object-specific structured info
            maatvoering_info = None
            bouwaanduidingtype = None
            figuurtype = None
            gebiedsaanduidinghoofdgroep = None
            if object_type == "Maatvoering":
                maatvoering_info = _parse_maatvoering_kv(
                    _extract_text(elem, "app:maatvoering")
                )
            elif object_type == "Bouwaanduiding":
                # PDOK heeft geen apart type-veld; naam draagt het type
                # (bv. "aaneengebouwd", "vrijstaand"). Lokale variaties
                # belanden elk in core.bouwaanduidingtype.
                bouwaanduidingtype = naam
            elif object_type == "Figuur":
                # Idem: naam draagt het figuurtype (bv. "hartlijn leiding - water").
                # symboolcode is in praktijk vaak leeg.
                figuurtype = naam or _extract_text(elem, "app:symboolcode")
            elif object_type == "Gebiedsaanduiding":
                # gebiedsaanduidinggroep (zonder 's') is het PDOK-veld; bv.
                # "geluidzone", "veiligheidszone". Gestandaardiseerd.
                gebiedsaanduidinghoofdgroep = _extract_text(
                    elem, "app:gebiedsaanduidinggroep"
                )

            gml_str = _extract_gml_geometry(elem)
            if not gml_str or not identificatie:
                continue

            # Ensure lookup-codes bestaan (self-populate; PDOK kan lokale
            # codes bevatten die niet in de officiële IMRO-set zitten).
            if bestemmingshoofdgroep:
                cur.execute(
                    "INSERT INTO core.bestemmingshoofdgroep (code) VALUES (%s) ON CONFLICT DO NOTHING",
                    (bestemmingshoofdgroep,),
                )
            if bouwaanduidingtype:
                cur.execute(
                    "INSERT INTO core.bouwaanduidingtype (code) VALUES (%s) ON CONFLICT DO NOTHING",
                    (bouwaanduidingtype,),
                )
            if figuurtype:
                cur.execute(
                    "INSERT INTO core.figuurtype (code) VALUES (%s) ON CONFLICT DO NOTHING",
                    (figuurtype,),
                )
            if gebiedsaanduidinghoofdgroep:
                cur.execute(
                    "INSERT INTO core.gebiedsaanduidinghoofdgroep (code) VALUES (%s) ON CONFLICT DO NOTHING",
                    (gebiedsaanduidinghoofdgroep,),
                )

            try:
                cur.execute(
                    """INSERT INTO wro.planobject
                       (identificatie, instrument_idn, object_type, naam,
                        bestemmingshoofdgroep, artikelnummer,
                        bouwaanduidingtype, figuurtype,
                        gebiedsaanduidinghoofdgroep, maatvoering_info,
                        geometrie, gml_source)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                               ST_GeomFromGML(%s, 28992), %s)
                       ON CONFLICT (identificatie) DO UPDATE SET
                           maatvoering_info = COALESCE(EXCLUDED.maatvoering_info, wro.planobject.maatvoering_info),
                           artikelnummer = COALESCE(EXCLUDED.artikelnummer, wro.planobject.artikelnummer),
                           bouwaanduidingtype = COALESCE(EXCLUDED.bouwaanduidingtype, wro.planobject.bouwaanduidingtype),
                           figuurtype = COALESCE(EXCLUDED.figuurtype, wro.planobject.figuurtype),
                           gebiedsaanduidinghoofdgroep = COALESCE(EXCLUDED.gebiedsaanduidinghoofdgroep, wro.planobject.gebiedsaanduidinghoofdgroep)
                       """,
                    (identificatie, plan_idn, object_type, naam,
                     bestemmingshoofdgroep, artikelnummer,
                     bouwaanduidingtype, figuurtype,
                     gebiedsaanduidinghoofdgroep,
                     json.dumps(maatvoering_info) if maatvoering_info else None,
                     gml_str, gml_str),
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


_PROVINCIE_IMRO_TO_PV = {f"99{n:02d}": f"pv{n:02d}" for n in range(20, 32)}
_PROVINCIE_NAMEN = {
    "pv20": "provincie Groningen", "pv21": "provincie Fryslân",
    "pv22": "provincie Drenthe", "pv23": "provincie Overijssel",
    "pv24": "provincie Flevoland", "pv25": "provincie Gelderland",
    "pv26": "provincie Utrecht", "pv27": "provincie Noord-Holland",
    "pv28": "provincie Zuid-Holland", "pv29": "provincie Zeeland",
    "pv30": "provincie Noord-Brabant", "pv31": "provincie Limburg",
}


def _structuurvisie_bronhouder(imro_code: str, niveau: str) -> tuple[str, str] | None:
    """Map IMRO overheidsCode + schaalniveau (G/P/R) naar (bronhouder, naam).

    Niveau G (gemeentelijk): code is gewone 4-digit CBS-code → gm-prefix.
    Niveau P (provinciaal): code is 99XX → pv-prefix via vaste mapping.
    Niveau R (rijk): één regeling-bron, code "rijk".
    """
    from src.db import normalize_bronhouder_code
    if niveau == "P":
        pv = _PROVINCIE_IMRO_TO_PV.get(imro_code)
        return (pv, _PROVINCIE_NAMEN[pv]) if pv else None
    if niveau == "G":
        if imro_code and imro_code.isdigit() and len(imro_code) == 4:
            return (normalize_bronhouder_code(imro_code), f"gemeente {imro_code}")
        return None
    if niveau == "R":
        return ("rijk", "Rijk")
    return None


def _load_structuurvisieplangebied(conn, niveau: str, code_filter: set[str] | None = None) -> int:
    """Laad Structuurvisieplangebied_{G|P|R} uit PDOK.

    code_filter: optioneel set bronhouder-codes (pv25, gm0344, rijk) om op te filteren.
    Geen filter = alle structuurvisies van dat niveau.
    """
    feature = f"Structuurvisieplangebied_{niveau}"
    gz_path = _download_gml_gz(feature)

    console.print(f"  Parsing {feature}{' (filter: ' + ','.join(code_filter) + ')' if code_filter else ''}...")

    count = 0
    skipped = 0
    with conn.cursor() as cur:
        for elem in _iter_features(gz_path, feature):
            imro_code = _extract_text(elem, "app:overheidsCode")
            bron = _structuurvisie_bronhouder(imro_code, niveau)
            if not bron:
                skipped += 1
                continue
            bron_code, bron_naam = bron
            if code_filter and bron_code not in code_filter:
                continue

            idn = _extract_text(elem, "app:identificatie")
            naam = _extract_text(elem, "app:naam")
            type_plan = _extract_text(elem, "app:typePlan") or "structuurvisie"
            planstatus = (_extract_text(elem, "app:planstatus") or "onbekend").split(";")[0].strip()
            datum = _extract_text(elem, "app:datum")
            gml_str = _extract_gml_geometry(elem)
            if not gml_str or not idn:
                continue

            bestuurslaag = {"P": "provincie", "G": "gemeente", "R": "rijk"}.get(niveau, "onbekend")
            upsert_bronhouder(cur, bron_code, bron_naam, bestuurslaag=bestuurslaag)
            cur.execute(
                "INSERT INTO wro.wro_manifest (overheidscode, naam_overheid) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (bron_code, bron_naam),
            )
            cur.execute(
                "INSERT INTO core.planstatus (code) VALUES (%s) ON CONFLICT DO NOTHING",
                (planstatus,),
            )
            dossier = idn.rsplit("-", 1)[0] if "-" in idn else idn
            cur.execute(
                "INSERT INTO wro.wro_dossier (dossiernummer, manifest_code, status) "
                "VALUES (%s, %s, NULL) ON CONFLICT DO NOTHING",
                (dossier, bron_code),
            )
            cur.execute(
                """INSERT INTO wro.ruimtelijk_instrument
                   (idn, dossier, type_plan, naam, planstatus, datum, bronhouder,
                    geometrie, gml_source, pons_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s,
                           ST_GeomFromGML(%s, 28992), %s, 'actief')
                   ON CONFLICT (idn) DO NOTHING""",
                (idn, dossier, type_plan, naam or "onbekend",
                 planstatus, datum, bron_code, gml_str, gml_str),
            )
            count += 1
            if count % 100 == 0:
                conn.commit()
                console.print(f"\r  {count} structuurvisies geladen...", end="")

    conn.commit()
    console.print(f"\n  [green]{count} {feature} geladen[/green]"
                  + (f" ([dim]{skipped} overgeslagen[/dim])" if skipped else ""))
    return count


def load_wro_structuurvisies(niveaus: list[str] | None = None,
                              codes: set[str] | None = None) -> int:
    """Laad provinciale/gemeentelijke/rijks-structuurvisies uit PDOK.

    niveaus: subset van {'G','P','R'}; default = alle drie.
    codes: bronhouder-codes om te filteren (pv25, gm0344, rijk).
    """
    niveaus = niveaus or ["G", "P", "R"]
    conn = get_conn()
    try:
        total = 0
        for niv in niveaus:
            total += _load_structuurvisieplangebied(conn, niv, codes)
        console.print(f"[bold green]{total} structuurvisies totaal geladen[/bold green]")
        return total
    finally:
        conn.close()


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

        # Step 3: Load planteksten via IHR API
        from src.loaders.ihr_loader import load_wro_teksten
        code_list = list(cbs_codes.keys()) if cbs_codes else None
        load_wro_teksten(code_list)

        console.print("[bold green]Wro loading complete![/bold green]")
    finally:
        conn.close()
