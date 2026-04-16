"""Load Ow regelingen from DSO Download API ZIP packages.

Flow:
1. Find regelingIds for PoC municipality via Presenteren API
2. Request download → poll status → get ZIP URL → download
3. Parse ZIP: Regeling/Tekst.xml → tekst_element
4. Parse ZIP: OW-bestanden/*.xml → CIM-OW tables
5. Parse ZIP: IO-*/GML → locatie geometries
"""

import time
import zipfile
from pathlib import Path

import httpx

from utils import strip_xml
from lxml import etree
from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.parsers.stop_xml import parse_tekst_xml
from src.parsers.ow_xml import (
    parse_activiteiten,
    parse_juridische_regels,
    parse_gebieden,
    parse_gebiedengroepen,
    parse_gebiedsaanwijzingen,
    parse_ponsen,
    parse_omgevingsnormen,
    parse_tekstdelen,
    parse_hoofdlijnen,
)

console = Console()

PRESENTEREN_BASE = "https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v8"
DOWNLOAD_BASE = cfg.DSO_DOWNLOAD_BASE

GML_NS = "http://www.opengis.net/gml/3.2"


def _api_headers() -> dict:
    return {"x-api-key": cfg.DSO_API_KEY}


def _find_regelingen_for_overheid(overheid_code: str, naam: str,
                                   doc_types: list[str] | None = None) -> list[dict]:
    """Find all regelingen for a given overheid via Presenteren API.

    Args:
        overheid_code: e.g. 'gm0344' for gemeente Utrecht, 'pv26' for provincie Utrecht.
        naam: display name.
        doc_types: Optional filter on documenttype (e.g. ['Programma', 'Omgevingsvisie']).
                   None means all types.
    """
    console.print(f"  Searching regelingen for {naam} ({overheid_code})...")

    results = []

    for page in range(1, 21):
        url = f"{PRESENTEREN_BASE}/regelingen?page={page}&size=200"
        resp = httpx.get(url, headers=_api_headers(), timeout=30)
        time.sleep(0.1)
        if resp.status_code != 200:
            break
        data = resp.json()
        for reg in data.get("_embedded", {}).get("regelingen", []):
            if reg.get("aangeleverdDoorEen", {}).get("code") == overheid_code:
                ident = reg["identificatie"]
                reg_type = reg.get("type", {}).get("waarde", "")
                if doc_types and reg_type not in doc_types:
                    continue
                if not any(r["identificatie"] == ident for r in results):
                    results.append({
                        "identificatie": ident,
                        "titel": reg.get("officieleTitel", ""),
                        "type": reg_type,
                        "expressionId": reg.get("expressionId", ""),
                    })
                    console.print(f"    Found: [{reg_type}] {reg.get('officieleTitel', 'onbekend')}")
        if not data.get("_links", {}).get("next"):
            break

    console.print(f"  [green]Found {len(results)} regelingen[/green]")
    return results


def _download_regeling(regeling_id: str) -> Path | None:
    """Download a regeling as ZIP via the Ozon Download API."""
    zip_name = regeling_id.replace("/", "_").strip("_") + ".zip"
    dest = cfg.DOWNLOAD_DIR / "ow" / zip_name

    if dest.exists():
        console.print(f"    [dim]Using cached {dest.name}[/dim]")
        return dest

    console.print(f"    Requesting download for {regeling_id}...")

    # Step 1: Request
    resp = httpx.post(
        f"{DOWNLOAD_BASE}/aanvraag",
        headers={**_api_headers(), "Content-Type": "application/json"},
        json={"regelingId": regeling_id},
        timeout=30,
    )
    if resp.status_code != 202:
        console.print(f"    [red]Request failed: {resp.status_code} {resp.text[:200]}[/red]")
        return None
    uuid = resp.json()["verzoekIdentificatie"]

    # Step 2: Poll status
    for _ in range(60):  # max 60 polls (5 min)
        time.sleep(5)
        status_resp = httpx.get(
            f"{DOWNLOAD_BASE}/status/{uuid}",
            headers=_api_headers(),
            timeout=30,
        )
        status = status_resp.json().get("status", "")
        if status == "BESCHIKBAAR":
            break
        elif status == "MISLUKT":
            console.print(f"    [red]Download failed for {regeling_id}[/red]")
            return None

    # Step 3: Get download URL
    dl_resp = httpx.get(f"{DOWNLOAD_BASE}/download/{uuid}", headers=_api_headers(), timeout=30)
    dl_url = dl_resp.json().get("url", "")

    # Step 4: Download ZIP
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", dl_url, headers=_api_headers(), timeout=300) as stream:
        stream.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in stream.iter_bytes(1024 * 64):
                f.write(chunk)

    console.print(f"    [green]Downloaded {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)[/green]")
    return dest


DOCUMENTTYPE_TO_REGELINGMODEL = {
    "Omgevingsplan": "RegelingCompact",
    "Omgevingsverordening": "RegelingCompact",
    "Waterschapsverordening": "RegelingCompact",
    "AMvB": "RegelingCompact",
    "Ministeriele regeling": "RegelingCompact",
    "Omgevingsvisie": "RegelingVrijetekst",
    "Programma": "RegelingVrijetekst",
    "Instructie": "RegelingVrijetekst",
    "Projectbesluit": "RegelingCompact",
    "Voorbereidingsbesluit": "RegelingCompact",
    "Voorbeschermingsregels Omgevingsplan": "RegelingCompact",
    "Reactieve interventie": "RegelingCompact",
    "Natura 2000-besluit": "RegelingVrijetekst",
}


def _detect_regelingmodel(zip_path: Path, doc_type: str) -> str:
    """Detect regelingmodel from ZIP content or documenttype mapping."""
    z = zipfile.ZipFile(zip_path)
    tekst_files = [n for n in z.namelist() if n == "Regeling/Tekst.xml"]
    if tekst_files:
        header = z.read(tekst_files[0])[:500].decode("utf-8", errors="ignore")
        if "RegelingVrijetekst" in header:
            return "RegelingVrijetekst"
        if "RegelingKlassiek" in header:
            return "RegelingKlassiek"
        if "RegelingTijdelijkdeel" in header:
            return "RegelingTijdelijkdeel"
        if "RegelingCompact" in header:
            return "RegelingCompact"
    return DOCUMENTTYPE_TO_REGELINGMODEL.get(doc_type, "RegelingCompact")


def _load_from_zip(conn, zip_path: Path, regeling_info: dict):
    """Parse a downloaded ZIP and load into database."""
    z = zipfile.ZipFile(zip_path)
    regeling_id = regeling_info["identificatie"]
    expression_id = regeling_info.get("expressionId", regeling_id)
    doc_type = regeling_info.get("type", "")
    regelingmodel = _detect_regelingmodel(zip_path, doc_type)

    console.print(f"    Parsing ZIP ({len(z.infolist())} files), regelingmodel={regelingmodel}...")

    with conn.cursor() as cur:
        # --- Regeling metadata ---
        cur.execute(
            """INSERT INTO p2p.regeling
               (frbr_expression, frbr_work, regelingmodel, opschrift, citeertitel, bronhouder, documenttype)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (frbr_expression) DO NOTHING""",
            (expression_id, regeling_id, regelingmodel,
             regeling_info.get("titel", ""),
             regeling_info.get("titel", ""),
             regeling_info.get("bronhouder", cfg.POC_CBS_CODE),
             doc_type),
        )

        # --- STOP tekst ---
        tekst_files = [n for n in z.namelist() if n == "Regeling/Tekst.xml"]
        if tekst_files:
            console.print("      Parsing Regeling/Tekst.xml...")
            tekst_bytes = z.read(tekst_files[0])
            elements = parse_tekst_xml(tekst_bytes, expression_id)

            # Build eid→db_id mapping for parent references
            eid_to_id = {}
            for elem in elements:
                cur.execute(
                    """INSERT INTO p2p.tekst_element
                       (regeling_expression, eid, wid, element_type, nummer, opschrift, inhoud, inhoud_plain, volgorde)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (regeling_expression, eid) DO NOTHING
                       RETURNING id""",
                    (expression_id, elem["eid"], elem["wid"], elem["element_type"],
                     elem["nummer"], elem["opschrift"], elem["inhoud"],
                     strip_xml(elem["inhoud"]), elem["volgorde"]),
                )
                row = cur.fetchone()
                if row:
                    eid_to_id[elem["eid"]] = row["id"]

            # Set parent_id references
            for elem in elements:
                if elem["parent_eid"] and elem["eid"] in eid_to_id and elem["parent_eid"] in eid_to_id:
                    cur.execute(
                        "UPDATE p2p.tekst_element SET parent_id = %s WHERE id = %s",
                        (eid_to_id[elem["parent_eid"]], eid_to_id[elem["eid"]]),
                    )

            console.print(f"      [green]{len(elements)} tekst-elementen geladen[/green]")

        # --- OW Activiteiten ---
        if "OW-bestanden/activiteiten.xml" in z.namelist():
            acts = parse_activiteiten(z.read("OW-bestanden/activiteiten.xml"))
            # First pass: insert without bovenliggende (to avoid FK issues)
            for act in acts:
                cur.execute(
                    """INSERT INTO p2p.activiteit (identificatie, naam, groep, is_tophaak)
                       VALUES (%s, %s, %s, false)
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (act["identificatie"], act["naam"], act["groep"]),
                )
            # Second pass: set bovenliggende
            for act in acts:
                if act["bovenliggende"]:
                    cur.execute(
                        """UPDATE p2p.activiteit SET bovenliggende = %s
                           WHERE identificatie = %s AND EXISTS
                           (SELECT 1 FROM p2p.activiteit WHERE identificatie = %s)""",
                        (act["bovenliggende"], act["identificatie"], act["bovenliggende"]),
                    )
            console.print(f"      [green]{len(acts)} activiteiten geladen[/green]")

        # --- OW Gebieden (locaties without geometry — geometry comes from GIO via GeometrieRef) ---
        gebieden_parsed = []
        if "OW-bestanden/gebieden.xml" in z.namelist():
            gebieden_parsed = parse_gebieden(z.read("OW-bestanden/gebieden.xml"))
            for g in gebieden_parsed:
                cur.execute(
                    """INSERT INTO p2p.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (g["identificatie"], g["locatie_type"], g["noemer"]),
                )
            console.print(f"      [green]{len(gebieden_parsed)} gebieden (locaties) geladen[/green]")

        # --- OW Gebiedengroepen ---
        if "OW-bestanden/gebiedengroepen.xml" in z.namelist():
            groepen = parse_gebiedengroepen(z.read("OW-bestanden/gebiedengroepen.xml"))
            for g in groepen:
                cur.execute(
                    """INSERT INTO p2p.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (g["identificatie"], g["locatie_type"], g["noemer"]),
                )
                # Insert groep-lid relations
                for lid_id in g.get("leden", []):
                    cur.execute(
                        """INSERT INTO p2p.locatiegroep_lid (groep_identificatie, lid_identificatie)
                           VALUES (%s, %s)
                           ON CONFLICT DO NOTHING""",
                        (g["identificatie"], lid_id),
                    )
            console.print(f"      [green]{len(groepen)} gebiedengroepen geladen[/green]")

        # --- OW Gebiedsaanwijzingen ---
        if "OW-bestanden/gebiedsaanwijzingen.xml" in z.namelist():
            gas = parse_gebiedsaanwijzingen(z.read("OW-bestanden/gebiedsaanwijzingen.xml"))
            for ga in gas:
                loc_id = ga.get("locatie_id")
                if loc_id:
                    # Ensure locatie exists
                    cur.execute(
                        """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                           VALUES (%s, 'Gebied', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                           ON CONFLICT DO NOTHING""",
                        (loc_id,),
                    )
                cur.execute(
                    """INSERT INTO p2p.gebiedsaanwijzing (identificatie, type, naam, groep, locatie_id)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (ga["identificatie"], ga["type"], ga["naam"], ga["groep"], loc_id),
                )
            console.print(f"      [green]{len(gas)} gebiedsaanwijzingen geladen[/green]")

        # --- OW Juridische regels ---
        if "OW-bestanden/regelsvooriedereen.xml" in z.namelist():
            regels = parse_juridische_regels(z.read("OW-bestanden/regelsvooriedereen.xml"))
            for regel in regels:
                cur.execute(
                    """INSERT INTO p2p.juridische_regel
                       (identificatie, regel_type, idealisatie, regeltekst_wid)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (regel["identificatie"], regel["regel_type"],
                     regel["idealisatie"], regel["regeltekst_wid"]),
                )
                # Insert ActiviteitLocatieaanduidingen
                for ala in regel.get("activiteit_locatie_aanduidingen", []):
                    # Ensure locatie and activiteit exist
                    cur.execute(
                        """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                           VALUES (%s, 'Gebied', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                           ON CONFLICT DO NOTHING""",
                        (ala["locatie_id"],),
                    )
                    cur.execute(
                        """INSERT INTO p2p.activiteit (identificatie, naam, is_tophaak)
                           VALUES (%s, '', false) ON CONFLICT DO NOTHING""",
                        (ala["activiteit_id"],),
                    )
                    try:
                        cur.execute(
                            """INSERT INTO p2p.activiteit_locatieaanduiding
                               (juridische_regel_id, activiteit_id, locatie_id, kwalificatie)
                               VALUES (%s, %s, %s, %s)""",
                            (regel["identificatie"], ala["activiteit_id"],
                             ala["locatie_id"], ala["kwalificatie"]),
                        )
                    except Exception:
                        conn.rollback()
            # Insert gebiedsaanwijzing relations
            ga_rel_count = 0
            norm_rel_count = 0
            for regel in regels:
                for ga_ref in regel.get("gebiedsaanwijzing_refs", []):
                    try:
                        cur.execute(
                            """INSERT INTO p2p.juridische_regel_gebiedsaanwijzing
                               (juridische_regel_id, gebiedsaanwijzing_id)
                               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                            (regel["identificatie"], ga_ref),
                        )
                        ga_rel_count += 1
                    except Exception:
                        conn.rollback()
                for norm_ref in regel.get("norm_refs", []):
                    try:
                        cur.execute(
                            """INSERT INTO p2p.juridische_regel_norm
                               (juridische_regel_id, norm_id)
                               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                            (regel["identificatie"], norm_ref),
                        )
                        norm_rel_count += 1
                    except Exception:
                        conn.rollback()

            console.print(f"      [green]{len(regels)} juridische regels + {ga_rel_count} GA-relaties + {norm_rel_count} norm-relaties geladen[/green]")

        # --- OW Ponsen ---
        if "OW-bestanden/ponsen.xml" in z.namelist():
            ponsen = parse_ponsen(z.read("OW-bestanden/ponsen.xml"))
            for p in ponsen:
                loc_id = p.get("locatie_id")
                if loc_id:
                    cur.execute(
                        """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                           VALUES (%s, 'Gebied', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                           ON CONFLICT DO NOTHING""",
                        (loc_id,),
                    )
                cur.execute(
                    """INSERT INTO p2p.pons (identificatie, locatie_id)
                       VALUES (%s, %s)
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (p["identificatie"], loc_id),
                )
            console.print(f"      [green]{len(ponsen)} ponsen geladen[/green]")

        # --- OW Omgevingsnormen ---
        if "OW-bestanden/omgevingsnormen.xml" in z.namelist():
            normen = parse_omgevingsnormen(z.read("OW-bestanden/omgevingsnormen.xml"))
            nw_count = 0
            for n in normen:
                cur.execute(
                    """INSERT INTO p2p.norm
                       (identificatie, norm_type, naam, type_norm, groep)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (n["identificatie"], n["norm_type"], n["naam"],
                     n.get("type_norm"), n.get("groep")),
                )
                for nw in n.get("normwaarden", []):
                    loc_id = nw.get("locatie_id")
                    if loc_id:
                        cur.execute(
                            """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                               VALUES (%s, 'Gebied', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                               ON CONFLICT DO NOTHING""",
                            (loc_id,),
                        )
                    try:
                        cur.execute(
                            """INSERT INTO p2p.normwaarde
                               (norm_id, locatie_id, kwalitatieve_waarde, kwantitatieve_waarde, waarde_in_regeltekst)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (n["identificatie"], loc_id,
                             nw.get("kwalitatieve_waarde"),
                             float(nw["kwantitatieve_waarde"]) if nw.get("kwantitatieve_waarde") else None,
                             nw.get("waarde_in_regeltekst")),
                        )
                        nw_count += 1
                    except Exception:
                        conn.rollback()
            console.print(f"      [green]{len(normen)} omgevingsnormen + {nw_count} normwaarden geladen[/green]")

        # --- OW Tekstdelen (vrijetekst-instrumenten) ---
        if "OW-bestanden/tekstdelen.xml" in z.namelist():
            tekstdelen = parse_tekstdelen(z.read("OW-bestanden/tekstdelen.xml"))
            td_count = 0
            hl_rel_count = 0
            for td in tekstdelen:
                loc_id = td.get("locatie_id")
                if loc_id:
                    cur.execute(
                        """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                           VALUES (%s, 'Gebied', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                           ON CONFLICT DO NOTHING""",
                        (loc_id,),
                    )
                try:
                    cur.execute(
                        """INSERT INTO p2p.tekstdeel
                           (identificatie, divisie_wid, thema, locatie_id)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (identificatie) DO NOTHING""",
                        (td["identificatie"], td["divisie_wid"],
                         [td["thema"]] if td.get("thema") else None,
                         loc_id),
                    )
                    td_count += 1
                except Exception:
                    conn.rollback()
                for hl_ref in td.get("hoofdlijn_refs", []):
                    try:
                        cur.execute(
                            """INSERT INTO p2p.tekstdeel_hoofdlijn
                               (tekstdeel_id, hoofdlijn_id)
                               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                            (td["identificatie"], hl_ref),
                        )
                        hl_rel_count += 1
                    except Exception:
                        conn.rollback()
            msg = f"      [green]{td_count} tekstdelen geladen[/green]"
            if hl_rel_count:
                msg += f" + {hl_rel_count} hoofdlijn-relaties"
            console.print(msg)

        # --- OW Hoofdlijnen ---
        if "OW-bestanden/hoofdlijnen.xml" in z.namelist():
            hoofdlijnen = parse_hoofdlijnen(z.read("OW-bestanden/hoofdlijnen.xml"))
            for hl in hoofdlijnen:
                cur.execute(
                    """INSERT INTO p2p.hoofdlijn (identificatie, soort, naam)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (hl["identificatie"], hl["soort"], hl["naam"]),
                )
            console.print(f"      [green]{len(hoofdlijnen)} hoofdlijnen geladen[/green]")

        # --- GIO's (Geo-InformatieObjecten with GML geometry) ---
        # Step 1: Build basisgeo_id → GML string index from all GIO files
        basisgeo_index = {}  # basisgeo:id (UUID) → GML string
        gml_files = [n for n in z.namelist()
                     if n.startswith("IO-") and n.endswith(".gml")]

        for gml_file in gml_files:
            try:
                root = etree.fromstring(z.read(gml_file))
            except etree.XMLSyntaxError:
                continue
            for loc in root.findall(".//geo:Locatie", GEO_NS):
                bid_el = loc.find(
                    "geo:geometrie/basisgeo:Geometrie/basisgeo:id", GEO_NS
                )
                if bid_el is None or not bid_el.text:
                    continue
                bid = bid_el.text.strip()
                geom_el = _extract_gml_element(loc)
                if geom_el is not None:
                    basisgeo_index[bid] = etree.tostring(geom_el, encoding="unicode")

        # Step 2: Match gebieden via their geometrie_ref → basisgeo_id
        gml_count = 0
        cur.execute(
            """SELECT identificatie FROM p2p.locatie
               WHERE locatie_type IN ('Gebied', 'Punt')
               AND ST_Equals(geometrie, ST_SetSRID(ST_MakePoint(0, 0), 28992))
               AND identificatie LIKE %s""",
            (f"nl.imow-{regeling_info.get('bronhouder', cfg.POC_CBS_CODE)}.%",),
        )
        dummy_ids = {row["identificatie"] for row in cur.fetchall()}

        for g in gebieden_parsed:
            geom_ref = g.get("geometrie_ref")
            if not geom_ref or g["identificatie"] not in dummy_ids:
                continue
            gml_str = basisgeo_index.get(geom_ref)
            if gml_str:
                gml_count += _update_locatie_geom(cur, g["identificatie"], gml_str)

        # Step 3: Compute Gebiedengroep geometries as ST_Union of leden
        if gml_count:
            cur.execute(
                """UPDATE p2p.locatie SET geometrie = sub.geom
                   FROM (
                     SELECT gl.groep_identificatie, ST_Union(l2.geometrie) as geom
                     FROM p2p.locatiegroep_lid gl
                     JOIN p2p.locatie l2 ON l2.identificatie = gl.lid_identificatie
                     WHERE NOT ST_Equals(l2.geometrie, ST_SetSRID(ST_MakePoint(0,0), 28992))
                     GROUP BY gl.groep_identificatie
                   ) sub
                   WHERE locatie.identificatie = sub.groep_identificatie
                   AND ST_Equals(locatie.geometrie, ST_SetSRID(ST_MakePoint(0,0), 28992))"""
            )

        if gml_count:
            console.print(f"      [green]{gml_count} GIO-geometrieën geladen (via GeometrieRef)[/green]")

    conn.commit()


GEO_NS = {
    "geo": "https://standaarden.overheid.nl/stop/imop/geo/",
    "gio": "https://standaarden.overheid.nl/stop/imop/gio/",
    "basisgeo": "http://www.geostandaarden.nl/basisgeometrie/1.0",
    "gml": "http://www.opengis.net/gml/3.2",
}


def _extract_gml_element(loc_element):
    """Extract the GML geometry element from a geo:Locatie."""
    basisgeo_geom = loc_element.find(
        "geo:geometrie/basisgeo:Geometrie/basisgeo:geometrie", GEO_NS
    )
    if basisgeo_geom is None:
        return None
    for child in basisgeo_geom:
        return child
    return None


def _update_locatie_geom(cur, identificatie: str, gml_str: str) -> int:
    """Update a locatie's geometry from GML string. Returns 1 if updated."""
    try:
        cur.execute(
            """UPDATE p2p.locatie
               SET geometrie = ST_GeomFromGML(%s, 28992),
                   gml_source = %s
               WHERE identificatie = %s""",
            (gml_str, gml_str, identificatie),
        )
        return cur.rowcount
    except Exception:
        return 0


def load_ow():
    """Main entry: load all Ow regelingen for the PoC municipality."""
    load_ow_overheid(f"gm{cfg.POC_CBS_CODE}", cfg.POC_GEMEENTE_NAAM, cfg.POC_CBS_CODE)


def load_ow_gemeente(cbs_code: str, naam: str, oin: str | None = None,
                     doc_types: list[str] | None = None):
    """Load Ow regelingen for a specific gemeente."""
    load_ow_overheid(f"gm{cbs_code}", naam, cbs_code, doc_types=doc_types)


def load_ow_overheid(overheid_code: str, naam: str, bronhouder_code: str,
                     doc_types: list[str] | None = None):
    """Load Ow regelingen for any overheid (gemeente, provincie, waterschap, rijk).

    Args:
        overheid_code: e.g. 'gm0344', 'pv26', 'ws0155'.
        naam: display name for logging.
        bronhouder_code: key for the bronhouder table.
        doc_types: Optional filter, e.g. ['Programma', 'Omgevingsvisie'].
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO core.bronhouder (overheidscode, naam) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (bronhouder_code, naam),
            )
        conn.commit()

        old_code = cfg.POC_CBS_CODE
        cfg.POC_CBS_CODE = bronhouder_code

        regelingen = _find_regelingen_for_overheid(overheid_code, naam, doc_types)

        cfg.POC_CBS_CODE = old_code

        if not regelingen:
            console.print("[yellow]No regelingen found[/yellow]")
            return

        for reg in regelingen:
            reg["bronhouder"] = bronhouder_code
            console.print(f"\n  Processing: [{reg['type']}] {reg['titel']}")
            zip_path = _download_regeling(reg["identificatie"])
            if zip_path:
                _load_from_zip(conn, zip_path, reg)

        console.print("\n[bold green]Ow loading complete![/bold green]")
    finally:
        conn.close()


def load_ow_from_zip(zip_path: str, cbs_code: str, naam: str):
    """Load from an already-downloaded ZIP file."""
    from pathlib import Path
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO core.bronhouder (overheidscode, naam) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (cbs_code, naam),
            )
        conn.commit()

        reg_info = {
            "identificatie": f"/akn/nl/act/gm{cbs_code}/2020/omgevingsplan",
            "titel": f"Omgevingsplan gemeente {naam}",
            "type": "Omgevingsplan",
            "expressionId": f"/akn/nl/act/gm{cbs_code}/2020/omgevingsplan",
        }

        _load_from_zip(conn, Path(zip_path), reg_info)
        console.print("\n[bold green]Ow loading from ZIP complete![/bold green]")
    finally:
        conn.close()
