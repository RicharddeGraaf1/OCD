"""Extract GIO geometrie-UUID's + Locatie ↔ basisgeo-mappings uit Download-ZIP.

Werkblokken 3 + 5 van [[Plan implementatie GIO-laden]] §"Optie C-light".

Refactor 2026-05-12 (gebruiker): de eerste optie ging uit van één gedeelde
geometrie-UUID tussen `loc.geometrieIdentificatie` (Presenteren) en
`basisgeo:id` (GML). Dat klopt niet — Presenteren met `locatieSelectie=primair`
levert een geaggregeerde UUID per Gebiedengroep, niet de basisgeo:id van de
leden. Echte koppeling loopt via M:N-junctions `p2p.locatie_basisgeo` en
`p2p.gio_basisgeo`, beide gevuld vanuit de Download-ZIP.

Per ZIP halen we drie soorten data — alleen UUIDs, geen coördinaten:
  - **Per Gebied** (uit `OW-bestanden/gebieden.xml`): NEN3610-id +
    `basisgeo:id` (uit `<l:GeometrieRef xlink:href>`)
  - **Per Gebiedengroep** (uit `OW-bestanden/gebiedengroepen.xml`):
    NEN3610-id + lijst van lid-Gebieden (`<l:GebiedRef xlink:href>`).
    Transitief vullen we `locatie_basisgeo` met (groep_id, basisgeo_id)
    voor elk lid.
  - **Per GIO** (uit `IO-*/*.gml`): FRBR-expression + ALLE `basisgeo:id`s
    (een GIO-GML bevat één of meer geometrieën, één per Locatie binnen
    het GIO).
"""

from pathlib import Path
import re
import zipfile

from lxml import etree

from src.db import get_conn

# Tolerante parser — sommige OW-bestanden hebben een invalide xmlns:schemaLocation
# (een URI met spatie erin) die met de strict parser een XMLSyntaxError oplevert.
# `recover=True` slaat zulke fouten over en parseert verder.
_TOLERANT_PARSER = etree.XMLParser(recover=True)

# Namespaces
NS = {
    "geo": "https://standaarden.overheid.nl/stop/imop/geo/",
    "basisgeo": "http://www.geostandaarden.nl/basisgeometrie/1.0",
    "l": "http://www.geostandaarden.nl/imow/locatie",
    "ow-dc": "http://www.geostandaarden.nl/imow/bestanden/deelbestand",
    "xlink": "http://www.w3.org/1999/xlink",
}


# ── Gebieden + Gebiedengroepen (Locatie-kant) ───────────────────────

def extract_gebied_basisgeo(zip_path: Path) -> dict[str, str]:
    """Parse `OW-bestanden/gebieden.xml` → {gebied_nen3610_id: basisgeo_id}.

    Per `<l:Gebied>` één rij: identificatie + GeometrieRef@xlink:href.
    """
    mapping: dict[str, str] = {}
    z = zipfile.ZipFile(zip_path)
    try:
        with z.open("OW-bestanden/gebieden.xml") as f:
            tree = etree.parse(f, _TOLERANT_PARSER)
    except KeyError:
        return mapping
    for gebied in tree.iterfind(".//l:Gebied", NS):
        ident_el = gebied.find("l:identificatie", NS)
        ref_el = gebied.find("l:geometrie/l:GeometrieRef", NS)
        if ident_el is None or ref_el is None:
            continue
        bgid = ref_el.get("{http://www.w3.org/1999/xlink}href")
        if not bgid:
            continue
        mapping[ident_el.text.strip()] = bgid.strip()
    return mapping


def extract_gebiedengroep_leden(zip_path: Path) -> dict[str, list[str]]:
    """Parse `OW-bestanden/gebiedengroepen.xml` → {groep_nen3610_id: [gebied_ids]}."""
    mapping: dict[str, list[str]] = {}
    z = zipfile.ZipFile(zip_path)
    try:
        with z.open("OW-bestanden/gebiedengroepen.xml") as f:
            tree = etree.parse(f, _TOLERANT_PARSER)
    except KeyError:
        return mapping
    for groep in tree.iterfind(".//l:Gebiedengroep", NS):
        ident_el = groep.find("l:identificatie", NS)
        if ident_el is None:
            continue
        leden = []
        for ref in groep.iterfind("l:groepselement/l:GebiedRef", NS):
            href = ref.get("{http://www.w3.org/1999/xlink}href")
            if href:
                leden.append(href.strip())
        if leden:
            mapping[ident_el.text.strip()] = leden
    return mapping


def build_locatie_basisgeo_rows(zip_path: Path) -> list[tuple[str, str]]:
    """Combineer gebieden + gebiedengroepen → (locatie_id, basisgeo_id)-tupels.

    Voor Gebieden: één tuple per Gebied (de eigen GeometrieRef).
    Voor Gebiedengroepen: één tuple per (groep, lid-Gebied's basisgeo_id).
    """
    gebied_mapping = extract_gebied_basisgeo(zip_path)
    groep_mapping = extract_gebiedengroep_leden(zip_path)

    rows: list[tuple[str, str]] = []
    # Direct Gebied → basisgeo
    for gebied_id, bgid in gebied_mapping.items():
        rows.append((gebied_id, bgid))
    # Gebiedengroep → transitief via leden
    for groep_id, leden in groep_mapping.items():
        for lid in leden:
            bgid = gebied_mapping.get(lid)
            if bgid:
                rows.append((groep_id, bgid))
    return rows


# ── GIO-GMLs ─────────────────────────────────────────────────────────

def extract_gio_basisgeo(zip_path: Path) -> list[tuple[str, str]]:
    """Parse alle GIO-GMLs → list[(gio_frbr, basisgeo_id)].

    Een GIO heeft één of meer `basisgeo:id` (één per Locatie binnen
    het GIO). Returnt één tuple per (gio, basisgeo).
    """
    rows: list[tuple[str, str]] = []
    z = zipfile.ZipFile(zip_path)
    for name in z.namelist():
        if not name.endswith(".gml"):
            continue
        with z.open(name) as f:
            try:
                tree = etree.parse(f, _TOLERANT_PARSER)
            except etree.XMLSyntaxError:
                continue
        root = tree.getroot()
        if not root.tag.endswith("}GeoInformatieObjectVaststelling"):
            continue

        frbr_el = root.find(
            "geo:vastgesteldeVersie/geo:GeoInformatieObjectVersie/geo:FRBRExpression",
            NS,
        )
        if frbr_el is None or not frbr_el.text:
            continue
        frbr = frbr_el.text.strip()

        for bid in root.iterfind(".//basisgeo:id", NS):
            if bid.text:
                rows.append((frbr, bid.text.strip()))
    return rows


# Strip een trailing instantie-suffix als " #10" van een locatie-naam, zodat
# "bed & breakfast #10" en "bed & breakfast #11" tot één label "bed & breakfast"
# collapsen (= het groep-label).
_LOC_SUFFIX = re.compile(r"\s*#\d+\s*$")


def _join_labels(labels: list[str]) -> str:
    """Voeg distinct labels samen met ' / ', gecapt op 5 om mega-strings te
    vermijden."""
    naam = " / ".join(labels[:5])
    if len(labels) > 5:
        naam += f" (+{len(labels) - 5})"
    return naam


def extract_gio_naam(zip_path: Path) -> dict[str, str]:
    """Parse alle GIO-GMLs → {gio_frbr: naam}, met een leesbaar label per GIO.

    Een GIO zélf heeft géén naam op versieniveau (geverifieerd: ook simpele
    TPOD-GIO's niet). De leesbare namen zitten één niveau dieper:

      1. `geo:groepen/geo:Groep/geo:label` (locatiegroep-GIO) — clean labels
         als "bed & breakfast". Distinct, samengevoegd met ' / '.
      2. anders: `geo:locaties/geo:Locatie/geo:naam` met een trailing " #n"
         gestript en gededupliceerd. Dekt zowel single-locatie GIO's
         ("Bedrijf categorie 2") als groeploze GIO's met benoemde locaties.
      3. anders: niet opgenomen → naam blijft NULL. Dit zijn anonieme
         geometrie-dragers (kale-hash FRBR, geen labels/namen); de UI valt
         daar terug op de FRBR.

    De GIO-identiteit blijft de FRBR (PK van geo_informatieobject); `naam` is
    puur een leesbaar label.
    """
    mapping: dict[str, str] = {}
    z = zipfile.ZipFile(zip_path)
    for name in z.namelist():
        if not name.endswith(".gml"):
            continue
        with z.open(name) as f:
            try:
                tree = etree.parse(f, _TOLERANT_PARSER)
            except etree.XMLSyntaxError:
                continue
        root = tree.getroot()
        if not root.tag.endswith("}GeoInformatieObjectVaststelling"):
            continue

        versie = root.find(
            "geo:vastgesteldeVersie/geo:GeoInformatieObjectVersie", NS
        )
        if versie is None:
            continue
        frbr_el = versie.find("geo:FRBRExpression", NS)
        if frbr_el is None or not frbr_el.text:
            continue
        frbr = frbr_el.text.strip()

        # 1. groep-labels (locatiegroep-GIO)
        labels: list[str] = []
        for lbl in versie.iterfind("geo:groepen/geo:Groep/geo:label", NS):
            txt = (lbl.text or "").strip()
            if txt and txt not in labels:
                labels.append(txt)
        if labels:
            mapping[frbr] = _join_labels(labels)
            continue

        # 2. locatie-namen (suffix gestript, gededupliceerd)
        loc_namen: list[str] = []
        for lnaam in versie.iterfind("geo:locaties/geo:Locatie/geo:naam", NS):
            txt = _LOC_SUFFIX.sub("", (lnaam.text or "").strip()).strip()
            if txt and txt not in loc_namen:
                loc_namen.append(txt)
        if loc_namen:
            mapping[frbr] = _join_labels(loc_namen)
        # 3. niets → naam blijft NULL
    return mapping


# ── DB-updates ───────────────────────────────────────────────────────

def update_locatie_basisgeo(conn, rows: list[tuple[str, str]]) -> int:
    """Bulk INSERT in p2p.locatie_basisgeo. ON CONFLICT DO NOTHING.

    Alleen rijen waarvan de locatie_id bestaat in p2p.locatie blijven over
    (FK-constraint). Returnt het aantal werkelijk ingevoegde rijen.
    """
    if not rows:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        for locatie_id, bgid in rows:
            cur.execute(
                """INSERT INTO p2p.locatie_basisgeo (locatie_id, basisgeo_id)
                   VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                (locatie_id, bgid),
            )
            inserted += cur.rowcount
    return inserted


def update_gio_basisgeo(conn, rows: list[tuple[str, str]]) -> int:
    """Bulk INSERT in p2p.gio_basisgeo. ON CONFLICT DO NOTHING."""
    if not rows:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        for gio_frbr, bgid in rows:
            cur.execute(
                """INSERT INTO p2p.gio_basisgeo (gio_frbr, basisgeo_id)
                   VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                (gio_frbr, bgid),
            )
            inserted += cur.rowcount
    return inserted


def insert_missing_gios(conn, gio_rows: list[tuple[str, str]],
                         regeling_expression: str | None = None,
                         naam_map: dict[str, str] | None = None) -> int:
    """Vul p2p.geo_informatieobject aan met GIO-FRBRs uit de ZIP die nog
    niet bekend zijn uit ExtIoRef.target_ref (optie A).

    Dat dekt het gat waar de ZIP nieuwere expressies bevat dan in de
    tekst-content via ExtIoRef worden aangewezen. Voor de drieslag-keten
    is dit nuttig — we kunnen nu ook IntIoRef matchen die toevallig naar
    de huidige expressie verwijst.

    `naam_map` (frbr → geo:naam) vult de leesbare titel. Op bestaande rijen
    wordt de naam alsnog gezet als die nog NULL is (COALESCE-update), zodat
    een reguliere loader-run GIO's die al via ExtIoRef bekend waren retroactief
    van een naam voorziet.
    """
    if not gio_rows:
        return 0
    naam_map = naam_map or {}
    unique_frbrs = {frbr for frbr, _ in gio_rows}
    inserted = 0
    with conn.cursor() as cur:
        for frbr in unique_frbrs:
            # RETURNING (xmax = 0) onderscheidt een echte INSERT (xmax 0) van
            # een DO UPDATE op een bestaande rij, zodat de telling alleen
            # nieuwe GIO's telt en niet de naam-backfill van bekende rijen.
            cur.execute(
                """INSERT INTO p2p.geo_informatieobject
                     (frbr_expression, frbr_work, regeling_expression, naam)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (frbr_expression) DO UPDATE
                     SET naam = COALESCE(p2p.geo_informatieobject.naam, EXCLUDED.naam)
                   RETURNING (xmax = 0) AS was_insert""",
                (frbr, frbr.split("@")[0], regeling_expression, naam_map.get(frbr)),
            )
            row = cur.fetchone()
            if row and row["was_insert"]:
                inserted += 1
    return inserted


def process_zip(zip_path: Path, conn=None,
                regeling_expression: str | None = None) -> dict[str, int]:
    """Convenience wrapper: extract + update voor zowel Locatie als GIO.

    Vult ook p2p.geo_informatieobject aan met FRBRs die de ZIP heeft maar
    optie A nog niet via ExtIoRef heeft ontdekt.
    """
    own_conn = False
    if conn is None:
        conn = get_conn()
        own_conn = True
    try:
        loc_rows = build_locatie_basisgeo_rows(zip_path)
        gio_rows = extract_gio_basisgeo(zip_path)
        naam_map = extract_gio_naam(zip_path)
        new_gios = insert_missing_gios(conn, gio_rows, regeling_expression, naam_map)
        loc_inserted = update_locatie_basisgeo(conn, loc_rows)
        gio_inserted = update_gio_basisgeo(conn, gio_rows)
        conn.commit()
        return {
            "locatie_rows_geextraheerd": len(loc_rows),
            "locatie_rows_inserted": loc_inserted,
            "gio_rows_geextraheerd": len(gio_rows),
            "gio_rows_inserted": gio_inserted,
            "new_gios_inserted": new_gios,
        }
    finally:
        if own_conn:
            conn.close()


# ── Backwards-compat: oude API behouden voor bestaande callers ───────
# Deze functies zijn niet langer onderdeel van de canonieke flow maar
# blijven beschikbaar zolang er geen reden is ze te verwijderen.

def extract_gio_geometrie_ids(zip_path: Path) -> dict[str, str]:
    """DEPRECATED: pakte één basisgeo:id per GIO. Vervangen door
    `extract_gio_basisgeo` die alle basisgeo:ids per GIO retourneert."""
    rows = extract_gio_basisgeo(zip_path)
    out: dict[str, str] = {}
    for frbr, bgid in rows:
        out.setdefault(frbr, bgid)  # eerste wint
    return out


def update_geo_informatieobject_gids(conn, mapping: dict[str, str]) -> dict[str, int]:
    """DEPRECATED: schreef één UUID naar geo_informatieobject.geometrie_identificatie.
    Werkt nog maar is geen onderdeel van de werkende keten. Voor nieuwe code
    gebruik `update_gio_basisgeo`."""
    counts = {"matched": 0, "niet_gevonden": 0, "unchanged": 0}
    if not mapping:
        return counts
    with conn.cursor() as cur:
        for frbr, geom_id in mapping.items():
            cur.execute(
                """UPDATE p2p.geo_informatieobject
                   SET geometrie_identificatie = %s
                   WHERE frbr_expression = %s
                     AND (geometrie_identificatie IS NULL
                          OR geometrie_identificatie <> %s)""",
                (geom_id, frbr, geom_id),
            )
            if cur.rowcount > 0:
                counts["matched"] += cur.rowcount
            else:
                cur.execute(
                    "SELECT 1 FROM p2p.geo_informatieobject WHERE frbr_expression = %s",
                    (frbr,),
                )
                if cur.fetchone():
                    counts["unchanged"] += 1
                else:
                    counts["niet_gevonden"] += 1
    return counts
