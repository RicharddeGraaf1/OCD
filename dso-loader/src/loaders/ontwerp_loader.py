"""Loader voor ontwerpregelingen en besluitversies (delta-gebaseerd).

Slaat op in het ontwerp-schema. Filter: alleen ontwerpen/besluiten die
de huidige geconsolideerde versie wijzigen OF in de toekomst in werking
treden. Historische wijzigingen worden geskipt.
"""

import json
import re
import time
from datetime import date, datetime
from urllib.parse import urlparse, parse_qs

import httpx
import psycopg
from psycopg.types.json import Jsonb
from rich.console import Console

from src.config import cfg
from src.db import get_conn, normalize_bronhouder_code
from src.loaders.api_loader import _get as _api_get, _parse_kop


def _get(url: str, params: dict | None = None, max_retries: int = 3) -> dict:
    """API-call met retry op 503/timeout (DSO is soms wisselvallig)."""
    for attempt in range(max_retries):
        try:
            return _api_get(url, params=params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (503, 502, 504) and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except httpx.TimeoutException:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return {}

console = Console()

CRS_RD = "http://www.opengis.net/def/crs/EPSG/0/28992"


# ── Filter-logica ────────────────────────────────────────────────────

_EXPRESSION_DATE_RE = re.compile(r"/nld@(\d{4}-\d{2}-\d{2})")


def _expression_date(expression: str | None) -> date | None:
    """Pak de datum uit een FRBR expression: '.../nld@2024-12-01;3' -> 2024-12-01."""
    if not expression:
        return None
    m = _EXPRESSION_DATE_RE.search(expression)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _huidige_versie_datum(conn: psycopg.Connection, regeling_work: str) -> date | None:
    """Datum van onze huidige geconsolideerde versie van deze regeling-work."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT frbr_expression FROM p2p.regeling WHERE frbr_work = %s LIMIT 1",
            (regeling_work,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _expression_date(row["frbr_expression"])


def _is_relevant(conn: psycopg.Connection, regeling_work: str | None,
                 nieuwe_expression: str | None,
                 begin_inwerking: str | date | None = None) -> bool:
    """Bepaal of dit ontwerp/besluit relevant is.

    Strikter filter: de wijziging moet:
    1. Een regeling betreffen die wij in p2p hebben (work-niveau), EN
    2. Een ANDERE expression introduceren dan wat wij al hebben (anders al verwerkt), EN
    3. (voor besluitversies) een begin_inwerking hebben in de toekomst, of geen
       begin_inwerking (ontwerp).
    """
    if not regeling_work:
        return False

    # 1. Kennen we deze regeling?
    with conn.cursor() as cur:
        cur.execute(
            "SELECT frbr_expression FROM p2p.regeling WHERE frbr_work = %s LIMIT 1",
            (regeling_work,)
        )
        row = cur.fetchone()
        if not row:
            return False
        huidige_expression = row["frbr_expression"]

    # 2. Wijziging introduceert een ANDERE versie dan we al hebben
    if nieuwe_expression and nieuwe_expression == huidige_expression:
        return False  # al verwerkt

    # 3. Voor besluitversies: alleen toekomstige inwerkingtreding
    if begin_inwerking:
        if isinstance(begin_inwerking, str):
            try:
                iw_datum = date.fromisoformat(begin_inwerking[:10])
            except ValueError:
                return True  # onbekende datum, niet skippen
        else:
            iw_datum = begin_inwerking

        if iw_datum < date.today():
            return False  # al in werking, geen aankomende wijziging meer

    return True


# ── Geometrie ophalen ────────────────────────────────────────────────

def _get_geometry(geom_id: str) -> str | None:
    """Haal GeoJSON op en converteer naar EWKT voor PostGIS."""
    try:
        url = f"{cfg.GEOMETRIE_BASE}/geometrieen/{geom_id}"
        resp = _get(url, params={"crs": CRS_RD})
        if resp:
            return json.dumps(resp.get("geometrie", resp))
    except Exception:
        pass
    return None


# ── Annotatie-extractie ──────────────────────────────────────────────

ANN_TYPE_MAP = {
    "regelteksten": "regeltekst",
    "regelsVoorIedereen": "juridische_regel",
    "instructieregels": "juridische_regel",
    "omgevingswaarderegels": "juridische_regel",
    "activiteiten": "activiteit",
    "gebiedsaanwijzingen": "gebiedsaanwijzing",
    "omgevingsnormen": "omgevingsnorm",
    "omgevingswaarden": "omgevingswaarde",
    "kaarten": "kaart",
    "tekstdelen": "tekstdeel",
    "hoofdlijnen": "hoofdlijn",
    "divisieteksten": "divisietekst",
    "ponsen": "pons",
    "regelingsgebieden": "regelingsgebied",
}


def _store_annotaties(cur, ontwerpbesluit_id: str, annotaties: dict):
    """Sla alle annotatie-types op in p2pwijziging.annotatie_delta."""
    # Wis bestaande annotaties voor deze besluit (delta is altijd compleet)
    cur.execute("DELETE FROM p2pwijziging.annotatie_delta WHERE ontwerpbesluit_id = %s",
                (ontwerpbesluit_id,))

    for api_key, db_type in ANN_TYPE_MAP.items():
        items = annotaties.get(api_key, [])
        if not items:
            continue
        for item in items:
            ident = item.get("identificatie")
            if not ident:
                continue
            delta = item.get("_delta", {})
            bewerking = delta.get("bewerking", "toevoegen")
            naam = item.get("naam") or item.get("opschrift")
            cur.execute("""
                INSERT INTO p2pwijziging.annotatie_delta
                    (ontwerpbesluit_id, type, identificatie, bewerking, naam, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (ontwerpbesluit_id, db_type, ident, bewerking, naam, Jsonb(item)))


def _store_locaties(cur, ontwerpbesluit_id: str, locaties: list,
                     fetch_geometry: bool = False):
    """Sla locatie-delta's op. Geometrie wordt default NIET opgehaald
    (te traag voor batch-runs). Backfill kan apart via een geometrie-loader."""
    cur.execute("DELETE FROM p2pwijziging.locatie_delta WHERE ontwerpbesluit_id = %s",
                (ontwerpbesluit_id,))
    for loc in locaties:
        ident = loc.get("identificatie")
        if not ident:
            continue
        delta = loc.get("_delta", {})
        bewerking = delta.get("bewerking", "toevoegen")
        geom_id = loc.get("geometrieIdentificatie")
        geom_geojson = None
        if fetch_geometry and geom_id and bewerking != "verwijderen":
            geom_geojson = _get_geometry(geom_id)

        cur.execute("""
            INSERT INTO p2pwijziging.locatie_delta
                (ontwerpbesluit_id, locatie_id, bewerking, locatie_type, noemer, geometrie)
            VALUES (%s, %s, %s, %s, %s,
                    CASE WHEN %s::text IS NOT NULL
                         THEN ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 28992)
                         ELSE NULL END)
        """, (ontwerpbesluit_id, ident, bewerking,
              loc.get("locatieType"), loc.get("noemer"),
              geom_geojson, geom_geojson))


def _store_documentstructuur(cur, ontwerpbesluit_id: str, doc_data: dict):
    """Sla document-structuur als tekst_delta op."""
    cur.execute("DELETE FROM p2pwijziging.tekst_delta WHERE ontwerpbesluit_id = %s",
                (ontwerpbesluit_id,))
    components = doc_data.get("_embedded", {}).get("documentComponenten", [])

    def walk(comps, parent_eid=None, offset=0):
        for i, comp in enumerate(comps):
            eid = comp.get("expressie")
            wid = comp.get("identificatie", eid)
            if not eid:
                continue
            delta = comp.get("_delta", {})
            bewerking = delta.get("bewerking", "toevoegen")
            comp_type = comp.get("type", "ONBEKEND")
            nummer, opschrift = _parse_kop(comp.get("kop"))

            cur.execute("""
                INSERT INTO p2pwijziging.tekst_delta
                    (ontwerpbesluit_id, eid, wid, element_type, bewerking,
                     nummer, opschrift, inhoud_nieuw, parent_eid, volgorde)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ontwerpbesluit_id, eid) DO NOTHING
            """, (ontwerpbesluit_id, eid, wid, comp_type, bewerking,
                  nummer, opschrift,
                  comp.get("inhoud"), parent_eid, offset + i))

            children = comp.get("_embedded", {}).get("documentComponenten", [])
            if children:
                walk(children, parent_eid=eid)

    walk(components)


# ── Ontwerp ophalen + opslaan ────────────────────────────────────────

def load_ontwerp(item: dict, conn: psycopg.Connection) -> str | None:
    """Verwerk één ontwerpregeling. Return ontwerpbesluit_id of None bij skip."""
    ontwerpbesluit_id = item.get("ontwerpbesluitIdentificatie")
    if not ontwerpbesluit_id:
        return None

    expression_id = item.get("expressionId")
    regeling_work = item.get("identificatie")  # work-level FRBR
    bronhouder_code = normalize_bronhouder_code(
        item.get("aangeleverdDoorEen", {}).get("code", "")
    )

    bekend_op = item.get("procedureverloop", {}).get("bekendOp")
    ontvangen_op = item.get("procedureverloop", {}).get("ontvangenOp")

    # Strikt filter: ontwerp moet ná onze huidige versie zijn bekendgemaakt
    # (anders is het gebaseerd op een verouderde versie)
    if not _is_relevant(conn, regeling_work, expression_id):
        return None

    # Extra check: bekend_op moet >= datum van onze huidige p2p-versie
    huidige_datum = _huidige_versie_datum(conn, regeling_work)
    if huidige_datum and bekend_op:
        try:
            bekend_datum = date.fromisoformat(bekend_op[:10])
            if bekend_datum < huidige_datum:
                return None
        except ValueError:
            pass

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO p2pwijziging.besluit
                (ontwerpbesluit_id, technisch_id, regeling_work,
                 wijzigt_expression, nieuwe_expression,
                 soort, status, bekend_op, ontvangen_op,
                 bronhouder, documenttype, opschrift, citeertitel,
                 publicatie_id)
            VALUES (%s, %s, %s, %s, %s, 'ontwerp', 'ontwerp', %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ontwerpbesluit_id) DO UPDATE SET
                wijzigt_expression = EXCLUDED.wijzigt_expression,
                nieuwe_expression = EXCLUDED.nieuwe_expression,
                opschrift = EXCLUDED.opschrift,
                beschikbaar_op = NOW()
        """, (ontwerpbesluit_id, item.get("technischId"),
              item.get("identificatie"), expression_id, expression_id,
              bekend_op, ontvangen_op, bronhouder_code,
              item.get("type", {}).get("waarde"),
              item.get("opschrift"), item.get("citeerTitel"),
              item.get("publicatieID")))

        # Procedurestappen
        for stap in item.get("procedureverloop", {}).get("procedurestappen", []):
            cur.execute("""
                INSERT INTO p2pwijziging.procedurestap
                    (ontwerpbesluit_id, soort, voltooid_op, plaats)
                VALUES (%s, %s, %s, %s)
            """, (ontwerpbesluit_id,
                  stap.get("soortStap", {}).get("waarde", ""),
                  stap.get("voltooidOp"),
                  stap.get("plaatsAanduiding")))

        # Documentstructuur ophalen
        try:
            doc_url = item["_links"]["documentstructuur"]["href"]
            doc_data = _get(doc_url)
            _store_documentstructuur(cur, ontwerpbesluit_id, doc_data)
        except Exception as e:
            console.print(f"      [dim]documentstructuur: {e}[/dim]")

        # Annotaties (renvooi)
        try:
            ann_url = item["_links"]["annotaties"]["href"]
            ann_data = _get(ann_url)
            _store_annotaties(cur, ontwerpbesluit_id, ann_data)
            _store_locaties(cur, ontwerpbesluit_id, ann_data.get("locaties", []))
        except Exception as e:
            import traceback
            console.print(f"      [dim]annotaties: {e}[/dim]")
            console.print(f"      [dim]{traceback.format_exc().splitlines()[-3]}[/dim]")

    return ontwerpbesluit_id


def load_besluitversie(item: dict, conn: psycopg.Connection) -> str | None:
    """Verwerk één besluitversie. Return ontwerpbesluit_id of None bij skip."""
    technisch_id = item.get("technischId")
    expression_id = item.get("expressionId")
    instrumentversie = item.get("instrumentversie", expression_id)
    regeling_work = item.get("identificatie")  # work-level FRBR

    # Filter: alleen relevante besluitversies (toekomstig + nieuwe expression)
    begin_inwerking = item.get("geregistreerdMet", {}).get("beginInwerking")
    if not _is_relevant(conn, regeling_work, instrumentversie, begin_inwerking):
        return None

    bronhouder_code = normalize_bronhouder_code(
        item.get("aangeleverdDoorEen", {}).get("code", "")
    )

    # Voor besluitversie gebruiken we het technisch_id als unieke key
    # (er is geen losse ontwerpbesluitIdentificatie)
    besluit_id = f"besluit:{technisch_id}"
    bekend_op = item.get("bekendOp")
    ontvangen_op = item.get("ontvangenOp")
    begin_geldigheid = item.get("geregistreerdMet", {}).get("beginGeldigheid")

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO p2pwijziging.besluit
                (ontwerpbesluit_id, technisch_id, regeling_work,
                 wijzigt_expression, nieuwe_expression,
                 soort, status, bekend_op, ontvangen_op,
                 begin_geldigheid, begin_inwerking,
                 eindverantwoordelijke, bronhouder, documenttype,
                 opschrift, citeertitel, publicatie_id)
            VALUES (%s, %s, %s, %s, %s, 'besluitversie', 'vastgesteld',
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ontwerpbesluit_id) DO UPDATE SET
                wijzigt_expression = EXCLUDED.wijzigt_expression,
                nieuwe_expression = EXCLUDED.nieuwe_expression,
                begin_inwerking = EXCLUDED.begin_inwerking,
                begin_geldigheid = EXCLUDED.begin_geldigheid,
                beschikbaar_op = NOW()
        """, (besluit_id, technisch_id, item.get("identificatie"),
              instrumentversie, expression_id,
              bekend_op, ontvangen_op, begin_geldigheid, begin_inwerking,
              item.get("eindverantwoordelijke"), bronhouder_code,
              item.get("type", {}).get("waarde"),
              item.get("opschrift"), item.get("citeerTitel"),
              item.get("publicatieID")))

        # Procedurestappen
        for stap in item.get("procedureverloop", {}).get("procedurestappen", []):
            cur.execute("""
                INSERT INTO p2pwijziging.procedurestap
                    (ontwerpbesluit_id, soort, voltooid_op, plaats)
                VALUES (%s, %s, %s, %s)
            """, (besluit_id, stap.get("soortStap", {}).get("waarde", ""),
                  stap.get("voltooidOp"), stap.get("plaatsAanduiding")))

        # Documentstructuur
        try:
            doc_url = item["_links"]["documentstructuur"]["href"]
            doc_data = _get(doc_url)
            _store_documentstructuur(cur, besluit_id, doc_data)
        except Exception as e:
            console.print(f"      [dim]documentstructuur: {e}[/dim]")

        # Annotaties
        try:
            ann_url = item["_links"]["annotaties"]["href"]
            ann_data = _get(ann_url)
            _store_annotaties(cur, besluit_id, ann_data)
            _store_locaties(cur, besluit_id, ann_data.get("locaties", []))
        except Exception as e:
            console.print(f"      [dim]annotaties: {e}[/dim]")

    return besluit_id


# ── Orchestrators ────────────────────────────────────────────────────

def load_alle_ontwerpen():
    """Loop alle ontwerpregelingen door en sla relevante op."""
    conn = get_conn()
    try:
        relevant = 0
        skipped = 0
        errors = 0
        page = 1
        total_seen = 0

        while True:
            data = _get(f"{cfg.PRESENTEREN_BASE}/ontwerpregelingen",
                        params={"page": page, "size": 100})
            items = data.get("_embedded", {}).get("ontwerpregelingen", [])
            if not items:
                break
            total_seen += len(items)

            for item in items:
                try:
                    result = load_ontwerp(item, conn)
                    if result:
                        relevant += 1
                        console.print(f"  + ontwerp {item.get('opschrift', '?')[:55]}")
                    else:
                        skipped += 1
                    conn.commit()
                except Exception as e:
                    errors += 1
                    console.print(f"  [red]Fout: {str(e)[:100]}[/red]")
                    conn.rollback()

            console.print(f"[dim]Pagina {page}: {len(items)} bekeken, "
                          f"{relevant} opgeslagen, {skipped} gefilterd[/dim]")
            page += 1
            if not data.get("_links", {}).get("next", {}).get("href"):
                break

        console.print(f"\n[bold green]Klaar: {total_seen} bekeken, "
                      f"{relevant} relevant, {skipped} historisch, "
                      f"{errors} fouten[/bold green]")
    finally:
        conn.close()


def load_alle_besluitversies():
    """Loop alle besluitversies door en sla relevante op."""
    conn = get_conn()
    try:
        relevant = 0
        skipped = 0
        errors = 0
        page = 1
        total_seen = 0

        while True:
            data = _get(f"{cfg.PRESENTEREN_BASE}/besluitversies",
                        params={"page": page, "size": 100})
            items = data.get("_embedded", {}).get("besluitversies", [])
            if not items:
                break
            total_seen += len(items)

            for item in items:
                try:
                    result = load_besluitversie(item, conn)
                    if result:
                        relevant += 1
                        console.print(f"  + besluit {item.get('opschrift', '?')[:55]}")
                    else:
                        skipped += 1
                    conn.commit()
                except Exception as e:
                    errors += 1
                    console.print(f"  [red]Fout: {str(e)[:100]}[/red]")
                    conn.rollback()

            console.print(f"[dim]Pagina {page}: {len(items)} bekeken, "
                          f"{relevant} opgeslagen, {skipped} gefilterd[/dim]")
            page += 1
            if not data.get("_links", {}).get("next", {}).get("href"):
                break

        console.print(f"\n[bold green]Klaar: {total_seen} bekeken, "
                      f"{relevant} relevant, {skipped} historisch, "
                      f"{errors} fouten[/bold green]")
    finally:
        conn.close()
