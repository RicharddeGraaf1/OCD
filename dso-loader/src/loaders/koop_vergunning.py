"""KOOP omgevingsvergunning-kennisgevingen — Postgres loader.

Bron: KOOP SRU 2.0 op https://repository.overheid.nl/sru
Filter: dcterms:type scheme="OVERHEIDop.Rubriek" == "omgevingsvergunning"

Persistentie: schema `vth` (losstaand, geen FK's naar dso.*). Geometrie
als PostGIS POINT/POLYGON. Idempotent op koop_id. Restartable per dag
via vth.etl_run.

Eigenschappen:
- Per-record TYPE_BESLUIT-classifier op basis van titel
- Geometrie:
    * Adres   -> POINT + adresvelden
    * Punt    -> POINT
    * Vlak    -> POLYGON + centroid (RD en WGS84)
    * (geen)  -> label/beschrijving in tekstvelden
- httpx Session met retry/backoff (5/15/45/90/180s, max 5 pogingen)
- Enrichment: haalt volledige publicatie-XML op via xml_url, parset
  body-tekst, extraheert zaaknummer/datum_ontvangst, valt terug op
  titel voor adresvelden. Deeplink-extractie via koop_deeplinks.

Promoted naar src/loaders/ op 2026-05-31. De PoC-CLI in
scripts/koop-poc/ingest.py importeert nu uit deze module en behoudt
zijn SQLite-fallback en argparse-CLI (alleen voor lokaal debuggen).

Achtergrond: zie vault_v1/analysis/Ingest omgevingsvergunningen uit
officielebekendmakingen.md en vault_v1/model.md §14.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
import time
import urllib.parse
import uuid
from typing import Iterator, Optional
from xml.etree import ElementTree as ET

import httpx

from src.db import get_conn
from src.loaders.koop_deeplinks import extract_deeplinks, upsert_deeplink

SRU_BASE = "https://repository.overheid.nl/sru"
PAGE_SIZE = 200
USER_AGENT = "OCD-dso-loader/koop-0.3"
REQUEST_INTERVAL = 0.3  # ~3.3 req/sec — KOOP-vriendelijk, geen 503-throttle gemeten
REQUEST_TIMEOUT = 120
MAX_RETRIES = 5
RETRY_BACKOFF_SEQ = [5, 15, 45, 90, 180]  # seconds; 5 min max total wait

NS = {
    "sru": "http://docs.oasis-open.org/ns/search-ws/sruResponse",
    "gzd": "http://standaarden.overheid.nl/sru",
    "dcterms": "http://purl.org/dc/terms/",
    "ow": "http://standaarden.overheid.nl/wetgeving/",
    "c": "http://standaarden.overheid.nl/collectie/",
    "overheid": "http://standaarden.overheid.nl/owms/terms/",
    "cup": "http://standaarden.overheid.nl/cup/data",
}

log = logging.getLogger("koop")


# ---------- HTTP -------------------------------------------------------------

_client: Optional[httpx.Client] = None


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/xml"},
            timeout=REQUEST_TIMEOUT,
        )
    return _client


# ---------- Type-besluit classifier -----------------------------------------

# Order matters: more specific patterns first. Each entry: (regex, label).
TYPE_BESLUIT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(rectificatie)\b", re.IGNORECASE), "rectificatie"),
    (re.compile(r"\bvan rechtswege\b", re.IGNORECASE), "van_rechtswege"),
    (re.compile(r"\b(verleng(en|ing)(\s+(van\s+de\s+)?beslistermijn)?)\b", re.IGNORECASE),
     "verlenging_beslistermijn"),
    (re.compile(r"\b(ingetrokken|intrekking|intrekken|trek\w* in)\b", re.IGNORECASE), "ingetrokken"),
    (re.compile(r"\b(geweigerd[e]?|weigering)\b", re.IGNORECASE), "geweigerd"),
    (re.compile(r"\b(ontwerp(besluit|beschikking|vergunning)?|voornemen)\b", re.IGNORECASE),
     "ontwerp"),
    (re.compile(r"\b(verleende|verleend|verlening|verlenen|verleen)\b", re.IGNORECASE), "verleend"),
    (re.compile(r"\b(geaccepteerd(e)?)\b", re.IGNORECASE), "melding_geaccepteerd"),
    (re.compile(r"\b(melding|sloopmelding)\b", re.IGNORECASE), "melding"),
    (re.compile(r"\b(aanvraag|aangevraagd[e]?|ingediende?|ingekomen|binnengekomen|"
                r"ontvangen|ontvangst|verzoek)\b", re.IGNORECASE), "aanvraag"),
    (re.compile(r"\b(kennisgeving)\b", re.IGNORECASE), "kennisgeving"),
]


def classify_type_besluit(titel: str) -> Optional[str]:
    if not titel:
        return None
    for pat, label in TYPE_BESLUIT_RULES:
        if pat.search(titel):
            return label
    return "overig"


# ---------- Geometry helpers ------------------------------------------------

_POINT_RE = re.compile(r"^\s*POINT\s*\(\s*([\-0-9.eE]+)\s+([\-0-9.eE]+)\s*\)\s*$")
_POLY_RE = re.compile(r"POLYGON\s*\(\s*\(([^)]+)\)", re.IGNORECASE)


def parse_point(text: str) -> Optional[tuple[float, float]]:
    if not text:
        return None
    m = _POINT_RE.match(text)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def polygon_centroid(text: str) -> Optional[tuple[float, float]]:
    """Approximate centroid as the mean of the unique outer-ring vertices."""
    if not text:
        return None
    m = _POLY_RE.search(text)
    if not m:
        return None
    pts: list[tuple[float, float]] = []
    for pair in m.group(1).split(","):
        parts = pair.strip().split()
        if len(parts) != 2:
            continue
        try:
            pts.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    if not pts:
        return None
    # Drop the closing duplicate vertex if present.
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return None
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return cx, cy


def parse_latlon(text: str) -> Optional[tuple[float, float]]:
    """Parse a 'lat lon' or 'lat,lon' string."""
    if not text:
        return None
    parts = text.replace(",", " ").split()
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


_LABEL_ADDR_RE = re.compile(
    r"^(?P<straat>[^\d,]+?)\s+"
    r"(?P<huisnr>\d+)\s*(?P<toev>[A-Za-z0-9\-/]*?)\s*[,;]?\s*"
    r"(?P<postcode>\d{4}\s?[A-Z]{2})?\s*"
    r"(?P<plaats>[A-Za-zÀ-ÿ\-\s]+)?$"
)

# Titel-extractie. Patronen die vaak voorkomen:
#   "het bouwen van X aan Straatnaam 12 te Plaats"
#   "Asterstraat 13 Wageningen"
#   "Verleende ..., Laan 1954 36, 3454CR De Meern, ZAAKNR"
# We zoeken naar een straat-nummer-(postcode)-plaats-fragment ergens in de titel.
_TITLE_ADDR_RE = re.compile(
    r"\b(?P<straat>[A-ZÉÈÊËÀÁÂÃÄÅÆ][\w' \-]{2,40}?)"
    r"\s+(?P<huisnr>\d{1,5})\s*(?P<toev>[A-Z0-9][\w\-/]{0,5})?"
    r"\s*[,]?\s*"
    r"(?:(?P<postcode>\d{4}\s?[A-Z]{2})\s+)?"
    r"(?P<plaats>(?:[A-ZÉÈÊËÀÁÂÃÄÅÆ][\w' \-]{2,30})(?:\s+[A-ZÉÈÊËÀÁÂÃÄÅÆ][\w' \-]{1,30})?)"
    r"(?=\s*[,.]|\s*$)"
)

# Zaaknummer-patronen die we tegenkomen in NL publicaties.
# Examples: GU-Z2026-0047412, Z-2026-001234, 2026M0841, OV-2026-12345
_ZAAKNR_PATTERNS = [
    re.compile(r"\b([A-Z]{1,4}-?Z[-/]?20\d{2}[-/]?\d{3,8})\b"),  # GU-Z2026-0047412
    re.compile(r"\b(Z[-/]?20\d{2}[-/]?\d{3,8})\b"),               # Z-2026-001234
    re.compile(r"\b(OV[-/]?20\d{2}[-/]?\d{3,8})\b"),              # OV-2026-12345
    re.compile(r"\b(20\d{2}[A-Z][A-Z0-9]{3,8})\b"),               # 2026M0841
    re.compile(r"\bZaaknummer[:\s]+([A-Z0-9][\w\-/]{4,30})", re.IGNORECASE),
    re.compile(r"\b(kenmerk|dossiernummer)[:\s]+([A-Z0-9][\w\-/]{4,30})", re.IGNORECASE),
]


def extract_adres_uit_titel(titel: str) -> dict[str, Optional[str]]:
    """Best-effort: trek straat/huisnr/postcode/woonplaats uit een titel."""
    out: dict[str, Optional[str]] = {
        "straatnaam": None, "huisnummer": None, "huisnummertoevoeging": None,
        "postcode": None, "woonplaats": None,
    }
    if not titel:
        return out
    m = _TITLE_ADDR_RE.search(titel)
    if not m:
        return out
    out["straatnaam"] = (m.group("straat") or "").strip() or None
    out["huisnummer"] = m.group("huisnr")
    toev = (m.group("toev") or "").strip() or None
    out["huisnummertoevoeging"] = toev
    pc = m.group("postcode")
    if pc:
        out["postcode"] = pc.replace(" ", "").upper()
    plaats = (m.group("plaats") or "").strip()
    # Filter veelvoorkomende false positives (woorden die geen plaats zijn)
    if plaats and plaats.lower() not in {
        "te", "het", "een", "de", "voor", "aan", "van", "in"
    }:
        out["woonplaats"] = plaats
    return out


def extract_zaaknummer_bg(text: str) -> Optional[str]:
    """Zoek de eerste plausibele zaaknummer-string in een tekst."""
    if not text:
        return None
    for pat in _ZAAKNR_PATTERNS:
        m = pat.search(text)
        if m:
            # 'kenmerk: XYZ' geeft 2 groepen — pak de laatste
            return m.group(m.lastindex or 1)
    return None


# --- Aanvraag-/ontvangstdatum-extractor ------------------------------------

_DATUM_ONTVANGST_PREFIX = re.compile(
    r"(?i)\b(?:"
    r"datum[ \-]*ontvangst(?:\s+aanvraag)?"
    r"|ontvangstdatum(?:\s+aanvraag|\s+van\s+de\s+zaak)?"
    r"|(?:datum\s+van\s+)?ontvangen\s+op"
    r"|aangevraagd\s+op"
    r")"
)

_NL_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sept": 9, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}

_DATE_NUM = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b")
_DATE_TXT = re.compile(r"^(\d{1,2})\s+([a-z]+)\s+(\d{4})\b", re.IGNORECASE)


def extract_datum_ontvangst(text: str) -> Optional[str]:
    """ISO-datum 'YYYY-MM-DD' voor de eerste 'Datum ontvangst'-mention.

    Tolereert formaten DD-MM-YYYY, DD/MM/YYYY, en NL-tekst 'DD maand YYYY'.
    Negeert valse positieven zoals 'ontvangen op grond van de APV'.
    """
    if not text:
        return None
    for m in _DATUM_ONTVANGST_PREFIX.finditer(text):
        tail = text[m.end():m.end() + 40].lstrip(": \t ")
        nm = _DATE_NUM.match(tail)
        if nm:
            d, mo, y = (int(g) for g in nm.groups())
            if 1 <= d <= 31 and 1 <= mo <= 12 and 1900 < y < 2200:
                return f"{y:04d}-{mo:02d}-{d:02d}"
            continue
        tm = _DATE_TXT.match(tail)
        if tm:
            d_s, mon_s, y_s = tm.groups()
            mon = _NL_MONTHS.get(mon_s.lower())
            if mon:
                d, y = int(d_s), int(y_s)
                if 1 <= d <= 31 and 1900 < y < 2200:
                    return f"{y:04d}-{mon:02d}-{d:02d}"
    return None


# Plain-text extractor for officielepublicaties XML (geen XML-namespaces).
_OP_TEKST_TAGS = {"al", "li.nr"}


def extract_tekst_uit_publicatie_xml(xml_bytes: bytes) -> str:
    """
    Parse de body van een KOOP officielepublicaties-XML en geef alle
    tekst uit <al> / <li> elementen terug, als platte tekst gescheiden
    door newlines. Geen namespace want OP-XML gebruikt default ns.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ""
    parts: list[str] = []
    for elem in root.iter():
        tag = elem.tag.split("}", 1)[-1].lower()
        if tag == "al":
            # `<al>` is een alinea; trek alle nested text eruit
            text = "".join(elem.itertext()).strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def parse_geometrielabel(label: str) -> dict[str, Optional[str]]:
    """
    Best-effort parser for an address-style geometrielabel like
    'Scholten-Hofmansbrink 7, 7152JJ Eibergen'. Returns a dict of address
    parts; values are None where parsing fails.
    """
    out: dict[str, Optional[str]] = {
        "straatnaam": None, "huisnummer": None, "huisnummertoevoeging": None,
        "postcode": None, "woonplaats": None,
    }
    if not label:
        return out
    m = _LABEL_ADDR_RE.match(label.strip())
    if not m:
        return out
    out["straatnaam"] = (m.group("straat") or "").strip() or None
    out["huisnummer"] = m.group("huisnr")
    toev = (m.group("toev") or "").strip() or None
    out["huisnummertoevoeging"] = toev
    pc = m.group("postcode")
    if pc:
        out["postcode"] = pc.replace(" ", "").upper()
    plaats = (m.group("plaats") or "").strip()
    out["woonplaats"] = plaats or None
    return out


# ---------- Record model ----------------------------------------------------

@dataclasses.dataclass
class Record:
    koop_id: str
    publicatieblad: str
    bg_naam: str
    bg_scheme: Optional[str]
    organisatietype: Optional[str]
    titel: str
    datum_publicatie: str
    jaargang: Optional[int]
    publicatienummer: Optional[str]
    rubriek: Optional[str]
    activiteit_code: Optional[str]
    type_besluit: Optional[str]
    geometrie_type: Optional[str]
    geometrie_rd_x: Optional[float]
    geometrie_rd_y: Optional[float]
    geometrie_lat: Optional[float]
    geometrie_lon: Optional[float]
    geometrie_rd: Optional[str]       # WKT (POINT or POLYGON) in EPSG:28992
    geometrie_rd_pt: Optional[str]    # WKT POINT in EPSG:28992 (centroid for Vlak)
    geometrie_wgs_pt: Optional[str]   # WKT POINT in EPSG:4326 (centroid for Vlak)
    geometrielabel: Optional[str]
    postcode: Optional[str]
    huisnummer: Optional[str]
    huisletter: Optional[str]
    huisnummertoevoeging: Optional[str]
    straatnaam: Optional[str]
    woonplaats: Optional[str]
    ligt_in_gemeente: Optional[str]
    beschrijving: Optional[str]
    preferred_url: Optional[str]
    xml_url: Optional[str]
    pdf_url: Optional[str]
    datum_publicatie_ts: Optional[str]   # ISO timestamp w/ tz (cup:datumTijdstipWijzigingWork)
    subject_taxonomie: Optional[str]     # dcterms:subject OVERHEID.TaxonomieBeleidsagendaDecentraal
    datum_ontvangst: Optional[str]       # ISO date, gevuld door enrichment (None bij parse-time)
    raw_xml: str


def text_of(elem: Optional[ET.Element]) -> Optional[str]:
    if elem is None:
        return None
    txt = elem.text
    return txt.strip() if txt and txt.strip() else None


_BLAD_MAP = {
    "Gemeenteblad": "gmb",
    "Provinciaal blad": "prb",
    "Waterschapsblad": "wsb",
    "Staatscourant": "stcrt",
    "Tractatenblad": "trb",
    "Staatsblad": "stb",
    "Blad gemeenschappelijke regeling": "bgr",
}


def parse_record(record_elem: ET.Element) -> Optional[Record]:
    original = record_elem.find(".//gzd:originalData", NS)
    if original is None:
        return None

    koop_id = text_of(original.find(".//dcterms:identifier", NS))
    titel = text_of(original.find(".//dcterms:title", NS))
    if not koop_id or not titel:
        return None

    rubriek = None
    for t in original.findall(".//dcterms:type", NS):
        if t.get("scheme") == "OVERHEIDop.Rubriek":
            rubriek = text_of(t)
            break

    creator_elem = original.find(".//dcterms:creator", NS)
    bg_naam = text_of(creator_elem) or ""
    bg_scheme = creator_elem.get("scheme") if creator_elem is not None else None

    datum_publicatie = (
        text_of(original.find(".//dcterms:available", NS))
        or text_of(original.find(".//dcterms:modified", NS))
        or ""
    )

    organisatietype = text_of(original.find(".//ow:organisatietype", NS))
    publicatienaam = text_of(original.find(".//ow:publicatienaam", NS)) or ""
    publicatieblad = _BLAD_MAP.get(publicatienaam, koop_id.split("-", 1)[0])

    jaargang_str = text_of(original.find(".//ow:jaargang", NS))
    jaargang = int(jaargang_str) if jaargang_str and jaargang_str.isdigit() else None
    publicatienummer = text_of(original.find(".//ow:publicatienummer", NS))

    activiteit_code = None
    for a in original.findall(".//ow:activiteit", NS):
        if a.get("scheme") == "OVERHEIDop.ActiviteitOmgevingsvergunning":
            activiteit_code = text_of(a)
            break

    subject_taxonomie = None
    for s in original.findall(".//dcterms:subject", NS):
        if s.get("scheme") == "OVERHEID.TaxonomieBeleidsagendaDecentraal":
            subject_taxonomie = text_of(s)
            break

    datum_publicatie_ts = text_of(
        original.find(".//cup:datumTijdstipWijzigingWork", NS)
    )

    geom = parse_gebiedsmarkering(original.find(".//ow:gebiedsmarkering", NS))
    beschrijving = text_of(original.find(".//dcterms:abstract", NS))

    enriched = record_elem.find(".//gzd:enrichedData", NS)
    preferred_url = text_of(enriched.find(".//gzd:preferredUrl", NS)) if enriched is not None else None
    xml_url = None
    pdf_url = None
    if enriched is not None:
        for item in enriched.findall(".//gzd:itemUrl", NS):
            mf = item.get("manifestation")
            if mf == "xml" and xml_url is None:
                xml_url = text_of(item)
            elif mf == "pdf" and pdf_url is None:
                pdf_url = text_of(item)

    raw_xml = ET.tostring(record_elem, encoding="unicode")
    type_besluit = classify_type_besluit(titel)

    return Record(
        koop_id=koop_id,
        publicatieblad=publicatieblad,
        bg_naam=bg_naam,
        bg_scheme=bg_scheme,
        organisatietype=organisatietype,
        titel=titel,
        datum_publicatie=datum_publicatie,
        jaargang=jaargang,
        publicatienummer=publicatienummer,
        rubriek=rubriek,
        activiteit_code=activiteit_code,
        type_besluit=type_besluit,
        geometrie_type=geom["geometrie_type"],
        geometrie_rd_x=geom["rd_x"],
        geometrie_rd_y=geom["rd_y"],
        geometrie_lat=geom["lat"],
        geometrie_lon=geom["lon"],
        geometrie_rd=geom["rd_wkt"],
        geometrie_rd_pt=(
            f"POINT({geom['rd_x']} {geom['rd_y']})"
            if geom["rd_x"] is not None and geom["rd_y"] is not None else None
        ),
        geometrie_wgs_pt=(
            f"POINT({geom['lon']} {geom['lat']})"
            if geom["lat"] is not None and geom["lon"] is not None else None
        ),
        geometrielabel=geom["geometrielabel"],
        postcode=geom["postcode"],
        huisnummer=geom["huisnummer"],
        huisletter=geom["huisletter"],
        huisnummertoevoeging=geom["huisnummertoevoeging"],
        straatnaam=geom["straatnaam"],
        woonplaats=geom["woonplaats"],
        ligt_in_gemeente=geom["ligt_in_gemeente"],
        beschrijving=beschrijving,
        preferred_url=preferred_url,
        xml_url=xml_url,
        pdf_url=pdf_url,
        datum_publicatie_ts=datum_publicatie_ts,
        subject_taxonomie=subject_taxonomie,
        datum_ontvangst=None,
        raw_xml=raw_xml,
    )


def parse_gebiedsmarkering(gebied: Optional[ET.Element]) -> dict[str, object]:
    out: dict[str, object] = {
        "geometrie_type": None, "rd_x": None, "rd_y": None, "lat": None, "lon": None,
        "rd_wkt": None, "wgs_wkt": None,
        "geometrielabel": None, "postcode": None, "huisnummer": None,
        "huisletter": None, "huisnummertoevoeging": None,
        "straatnaam": None, "woonplaats": None, "ligt_in_gemeente": None,
    }
    if gebied is None:
        return out

    for child in gebied:
        tag = child.tag.split("}", 1)[-1]
        if tag not in ("Adres", "Punt", "Vlak", "Postcodegebied", "PostcodeHuisnummer"):
            continue
        out["geometrie_type"] = tag

        # Geometry
        rd_text = text_of(child.find("ow:geometrie", NS))
        wgs_text = text_of(child.find("ow:locatiegebied", NS))
        out["rd_wkt"] = rd_text
        out["wgs_wkt"] = wgs_text

        if rd_text and rd_text.upper().startswith("POINT"):
            pt = parse_point(rd_text)
            if pt:
                out["rd_x"], out["rd_y"] = pt
        elif rd_text and rd_text.upper().startswith("POLYGON"):
            c = polygon_centroid(rd_text)
            if c:
                out["rd_x"], out["rd_y"] = c

        # WGS84 — locatiepunt is preferred (clean lat lon); fall back to polygon centroid
        ll = parse_latlon(text_of(child.find("ow:locatiepunt", NS)) or "")
        if ll:
            out["lat"], out["lon"] = ll
        elif wgs_text and wgs_text.upper().startswith("POLYGON"):
            c = polygon_centroid(wgs_text)
            if c:
                out["lon"], out["lat"] = c  # locatiegebied is lon lat

        # Structured address fields (Adres has them; Punt/Vlak usually don't)
        out["geometrielabel"] = text_of(child.find("ow:geometrielabel", NS))
        out["postcode"] = text_of(child.find("ow:postcode", NS))
        out["huisnummer"] = text_of(child.find("ow:huisnummer", NS))
        out["huisletter"] = text_of(child.find("ow:huisletter", NS))
        out["huisnummertoevoeging"] = text_of(child.find("ow:huisnummertoevoeging", NS))
        out["straatnaam"] = text_of(child.find("ow:straatnaam", NS))
        out["woonplaats"] = text_of(child.find("ow:woonplaats", NS))
        out["ligt_in_gemeente"] = text_of(child.find("ow:ligtInGemeente", NS))

        # Fallback: parse geometrielabel for address-shaped Vlak/Punt records
        if out["geometrielabel"] and not out["straatnaam"]:
            parsed = parse_geometrielabel(out["geometrielabel"])
            for k, v in parsed.items():
                if v and not out.get(k):
                    out[k] = v
        break

    return out


# ---------- HTTP fetching ----------------------------------------------------

def fetch_page(query: str, start: int, page_size: int) -> tuple[ET.Element, int]:
    params = {
        "query": query,
        "startRecord": str(start),
        "maximumRecords": str(page_size),
    }
    url = f"{SRU_BASE}?{urllib.parse.urlencode(params)}"
    client = get_client()
    last_exc: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            n_elem = root.find("sru:numberOfRecords", NS)
            total = int(n_elem.text) if n_elem is not None and n_elem.text else 0
            return root, total
        except (httpx.HTTPError, ET.ParseError) as e:
            last_exc = e
            wait = RETRY_BACKOFF_SEQ[min(attempt, len(RETRY_BACKOFF_SEQ) - 1)]
            log.warning(
                "  fetch failed (attempt %d/%d): %s — retry in %ds",
                attempt + 1, MAX_RETRIES, type(e).__name__, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"giving up after {MAX_RETRIES} retries: {last_exc}")


def iter_day_records(day: dt.date) -> Iterator[ET.Element]:
    day_str = day.isoformat()
    next_day_str = (day + dt.timedelta(days=1)).isoformat()
    query = (
        f'dt.type="omgevingsvergunning" '
        f"AND dt.modified>={day_str} "
        f"AND dt.modified<{next_day_str}"
    )
    start = 1
    while True:
        root, total = fetch_page(query, start, PAGE_SIZE)
        records = root.findall(".//sru:record", NS)
        if not records:
            break
        for rec in records:
            yield rec
        next_pos_elem = root.find("sru:nextRecordPosition", NS)
        if next_pos_elem is None or not next_pos_elem.text:
            break
        next_start = int(next_pos_elem.text)
        if next_start <= start:
            break
        start = next_start
        time.sleep(REQUEST_INTERVAL)
        log.info("  paginate: at %d/%d for %s", start, total, day_str)


def _fetch_publication_xml(url: str) -> bytes:
    """GET the full publication XML, with the same retry-backoff as SRU."""
    client = get_client()
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as e:
            last_exc = e
            wait = RETRY_BACKOFF_SEQ[min(attempt, len(RETRY_BACKOFF_SEQ) - 1)]
            log.warning("  fetch %s failed (%d/%d): %s — retry in %ds",
                        url[-60:], attempt + 1, MAX_RETRIES, type(e).__name__, wait)
            time.sleep(wait)
    raise RuntimeError(f"giving up after {MAX_RETRIES} retries: {last_exc}")


# ---------- Postgres backend ------------------------------------------------

# Columns that need ST_GeomFromText() wrapping in Postgres.
_GEOM_WKT_COLS = {
    "geometrie_rd": 28992,
    "geometrie_rd_pt": 28992,
    "geometrie_wgs_pt": 4326,
}

# Record fields that exist for SQLite debugging only; not in the Postgres DDL.
# (The Postgres schema relies on the WKT-geometry columns instead.)
_PG_SKIP_COLS = {"geometrie_rd_x", "geometrie_rd_y", "geometrie_lat", "geometrie_lon"}


def pg_get_conn():
    """Open een nieuwe Postgres-connection via de centrale src.db helper."""
    return get_conn()


def _pg_columns() -> list[str]:
    """Record fields that map to Postgres columns (excluding sqlite-only helpers)."""
    return [f.name for f in dataclasses.fields(Record) if f.name not in _PG_SKIP_COLS]


def _pg_build_upsert_sql() -> str:
    """Build the INSERT … ON CONFLICT DO UPDATE statement for Postgres."""
    cols = _pg_columns() + ["ingest_run_id"]
    placeholders = [
        f"ST_GeomFromText(%s, {_GEOM_WKT_COLS[c]})" if c in _GEOM_WKT_COLS
        else "%s"
        for c in cols
    ]
    update_set = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "koop_id")
    return (
        f"INSERT INTO vth.vergunningkennisgeving ({','.join(cols)}) "
        f"VALUES ({','.join(placeholders)}) "
        f"ON CONFLICT (koop_id) DO UPDATE SET {update_set}, ingest_at=now()"
    )


_PG_UPSERT_SQL_CACHE: Optional[str] = None


def pg_upsert(conn, rec: Record, run_id: str) -> None:
    global _PG_UPSERT_SQL_CACHE
    if _PG_UPSERT_SQL_CACHE is None:
        _PG_UPSERT_SQL_CACHE = _pg_build_upsert_sql()

    cols = _pg_columns() + ["ingest_run_id"]
    values = [getattr(rec, c) if c != "ingest_run_id" else run_id for c in cols]
    with conn.cursor() as cur:
        cur.execute(_PG_UPSERT_SQL_CACHE, values)


def pg_setup() -> None:
    """Apply de canonical KOOP-DDL (src/ddl.py:KOOP_DDL) tegen Postgres."""
    from src.ddl import KOOP_DDL  # late import to avoid cycle
    conn = pg_get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(KOOP_DDL)
        conn.commit()
        log.info("KOOP-schema applied (src.ddl.KOOP_DDL)")
    finally:
        conn.close()


def pg_mark_run(conn, run_id: str, day: dt.date, started: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO vth.etl_run "
            "(run_id, source, processed_date, started_at, status) "
            "VALUES (%s, %s, %s, %s, 'running') "
            "ON CONFLICT (source, processed_date) DO UPDATE SET "
            "run_id=EXCLUDED.run_id, started_at=EXCLUDED.started_at, "
            "status='running', finished_at=NULL, error=NULL, record_count=NULL",
            (run_id, "koop_omgevingsvergunning", day.isoformat(), started),
        )
    conn.commit()


def pg_finish_run(conn, run_id: str, count: int, finished: str,
                  ok: bool, error: Optional[str]) -> None:
    # If the previous UPSERT failed, the transaction is in an aborted state.
    # Roll back first so we can still record the failure in etl_run.
    if not ok:
        conn.rollback()
    with conn.cursor() as cur:
        if ok:
            cur.execute(
                "UPDATE vth.etl_run SET record_count=%s, finished_at=%s, "
                "status='ok' WHERE run_id=%s",
                (count, finished, run_id),
            )
        else:
            cur.execute(
                "UPDATE vth.etl_run SET record_count=%s, finished_at=%s, "
                "status='error', error=%s WHERE run_id=%s",
                (count, finished, (error or "")[:500], run_id),
            )
    conn.commit()


def pg_run_status(conn, day: dt.date) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM vth.etl_run "
            "WHERE source=%s AND processed_date=%s",
            ("koop_omgevingsvergunning", day.isoformat()),
        )
        row = cur.fetchone()
        if not row:
            return None
        # dict_row → dict, otherwise tuple
        return row["status"] if isinstance(row, dict) else row[0]


# ---------- Process day-by-day ----------------------------------------------

def process_day(conn, day: dt.date) -> int:
    """Verwerk één dag tegen een open Postgres-connection.

    Markeert de run in vth.etl_run als 'running' bij start en als 'ok'/
    'error' bij einde. Idempotent op koop_id via pg_upsert.
    """
    run_id = str(uuid.uuid4())
    started = dt.datetime.now().isoformat(timespec="seconds")
    pg_mark_run(conn, run_id, day, started)

    count = 0
    try:
        for rec_elem in iter_day_records(day):
            rec = parse_record(rec_elem)
            if rec is None:
                continue
            pg_upsert(conn, rec, run_id)
            count += 1
        conn.commit()
        finished = dt.datetime.now().isoformat(timespec="seconds")
        pg_finish_run(conn, run_id, count, finished, ok=True, error=None)
        log.info("%s: %d records ingested", day.isoformat(), count)
        return count
    except Exception as e:
        finished = dt.datetime.now().isoformat(timespec="seconds")
        pg_finish_run(conn, run_id, count, finished, ok=False, error=str(e))
        raise


def daterange(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


# ---------- Public wrappers --------------------------------------------------

def load_koop_range(from_date: str, to_date: str, force: bool = False) -> int:
    """Laad een dagbereik [from_date, to_date] (ISO YYYY-MM-DD) inclusief.

    Slaat al-succesvol-verwerkte dagen over tenzij `force=True`.
    Returns: totaal aantal upserts.
    """
    start = dt.date.fromisoformat(from_date)
    end = dt.date.fromisoformat(to_date)
    conn = pg_get_conn()
    try:
        total = 0
        for day in daterange(start, end):
            status = pg_run_status(conn, day)
            if status == "ok" and not force:
                log.info("%s: already ingested (use force=True to redo)", day.isoformat())
                continue
            total += process_day(conn, day)
        log.info("Total upserts across range: %d", total)
        return total
    finally:
        conn.close()


def enrich_records(
    limit: int = 5000,
    loop: bool = False,
    sleep: int = 120,
    stop_after_empty: int = 5,
    type_filter: Optional[tuple[str, ...]] = None,
) -> int:
    """Verrijk records zonder inhoud_geladen_at met publicatie-XML.

    Haalt voor elk record xml_url op, parset body-tekst, extraheert
    zaaknummer/datum_ontvangst, valt terug op titel voor adres, en
    extraheert deeplinks (whitelist) zonder HTTP-validatie.

    Met `loop=True` blijft hij draaien zolang er pending records bijkomen
    (handig parallel naast load_koop_range); stopt na `stop_after_empty`
    lege cycles op rij.

    Returns: totaal aantal verrijkte records over alle batches.
    """
    if type_filter:
        log.info("Filtering on type_besluit IN %s", type_filter)

    empty_cycles = 0
    total_enriched = 0
    while True:
        n = _enrich_one_batch(limit, type_filter)
        total_enriched += n
        if n == 0:
            empty_cycles += 1
            if not loop or empty_cycles >= stop_after_empty:
                log.info("enrich done. total this run: %d", total_enriched)
                return total_enriched
            log.info("no pending records, sleeping %ds (empty cycle %d/%d)",
                     sleep, empty_cycles, stop_after_empty)
            time.sleep(sleep)
        else:
            empty_cycles = 0
            if not loop:
                log.info("enrich done. total this run: %d", total_enriched)
                return total_enriched


def _enrich_one_batch(limit: int,
                      type_filter: Optional[tuple[str, ...]] = None) -> int:
    """Run a single enrich-batch up to `limit` records. Returns count processed.

    If `type_filter` is given, only records whose type_besluit is in that
    tuple are considered. Records outside the filter are left untouched
    (inhoud_geladen_at stays NULL) and can be picked up by a later run
    without filter — true incremental processing.
    """
    conn = pg_get_conn()
    try:
        with conn.cursor() as cur:
            if type_filter:
                cur.execute(
                    "SELECT koop_id, titel, xml_url, straatnaam, postcode, "
                    "       huisnummer, woonplaats "
                    "FROM vth.vergunningkennisgeving "
                    "WHERE inhoud_geladen_at IS NULL "
                    "  AND xml_url IS NOT NULL "
                    "  AND type_besluit = ANY(%s) "
                    "ORDER BY datum_publicatie DESC "
                    "LIMIT %s",
                    (list(type_filter), limit),
                )
            else:
                cur.execute(
                    "SELECT koop_id, titel, xml_url, straatnaam, postcode, "
                    "       huisnummer, woonplaats "
                    "FROM vth.vergunningkennisgeving "
                    "WHERE inhoud_geladen_at IS NULL "
                    "  AND xml_url IS NOT NULL "
                    "ORDER BY datum_publicatie DESC "
                    "LIMIT %s",
                    (limit,),
                )
            todo = cur.fetchall()
        if not todo:
            return 0
        log.info("Enriching %d records ...", len(todo))

        updated = 0
        for i, row in enumerate(todo, 1):
            koop_id = row["koop_id"]
            try:
                xml_bytes = _fetch_publication_xml(row["xml_url"])
            except Exception as e:
                log.warning("  %s: fetch failed: %s — skipping", koop_id, e)
                continue
            xml_text = xml_bytes.decode("utf-8", errors="replace")
            tekst = extract_tekst_uit_publicatie_xml(xml_bytes)
            zaaknr = (
                extract_zaaknummer_bg(tekst)
                or extract_zaaknummer_bg(row["titel"] or "")
            )
            ontvangst = extract_datum_ontvangst(tekst)

            # If address fields are empty, try parsing the title as a fallback.
            addr: dict = {}
            if not row["straatnaam"]:
                addr = extract_adres_uit_titel(row["titel"] or "")

            # Deeplinks (whitelist) — extract zonder validatie; periodieke
            # `python backfill_deeplinks.py --no-validate` of validate-pass
            # zet http_status later.
            deeplinks_found = extract_deeplinks(xml_text)

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE vth.vergunningkennisgeving SET "
                    "  inhoud_xml = %s, "
                    "  inhoud_tekst = %s, "
                    "  zaaknummer_bg = COALESCE(zaaknummer_bg, %s), "
                    "  datum_ontvangst = COALESCE(datum_ontvangst, %s), "
                    "  straatnaam = COALESCE(straatnaam, %s), "
                    "  huisnummer = COALESCE(huisnummer, %s), "
                    "  huisnummertoevoeging = COALESCE(huisnummertoevoeging, %s), "
                    "  postcode = COALESCE(postcode, %s), "
                    "  woonplaats = COALESCE(woonplaats, %s), "
                    "  inhoud_geladen_at = now() "
                    "WHERE koop_id = %s",
                    (
                        xml_text, tekst, zaaknr, ontvangst,
                        addr.get("straatnaam"),
                        addr.get("huisnummer"),
                        addr.get("huisnummertoevoeging"),
                        addr.get("postcode"),
                        addr.get("woonplaats"),
                        koop_id,
                    ),
                )
                for url, bron in deeplinks_found:
                    upsert_deeplink(cur, koop_id, url, bron, validation=None)
            updated += 1
            if i % 50 == 0:
                conn.commit()
                log.info("  progress: %d/%d (last: %s)", i, len(todo), koop_id)
            time.sleep(REQUEST_INTERVAL)
        conn.commit()
        log.info("Enriched %d records.", updated)
        return updated
    finally:
        conn.close()
