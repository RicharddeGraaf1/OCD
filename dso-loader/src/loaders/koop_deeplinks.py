"""
Deeplink-detectie en -validatie voor omgevingsvergunning-kennisgevingen.

Een 'deeplink' is een URL in de publicatie-XML die naar het concrete
besluit-dossier wijst — niet naar een algemene landingspagina, bezwaar-
formulier, of contact-pagina.

De WHITELIST hieronder bevat hosts die op 2026-05-20 zijn gevalideerd
als structurele deeplink-leveranciers. Uitbreiden door:
  - script `q_koop_deeplink_hosts.py` te draaien op verse data
  - hosts met records >= 30 EN uniqueness >= 0.5 als kandidaten te bekijken
  - HTTP-validatie via `validate_url()` doen op een sample
  - host toevoegen aan WHITELIST

Het patroon van 5 van 8 huidige hosts is Mozard Suite (-> /mozard/!suiteXX
.scherm... ?mObj=...). Nieuwe Mozard-instances zijn waarschijnlijk ook
deeplink-hosts.

Promoted naar src/loaders/ op 2026-05-31 — de wrapper
`scripts/koop-poc/deeplinks.py` re-exporteert deze module zodat bestaande
scripts (`backfill_deeplinks.py`, `validate_deeplinks.py`) ongewijzigd
blijven werken.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

# ---- Whitelist (uitbreidbaar) ----------------------------------------------
#
# host -> (beschrijving, bekend-bevoegd-gezag-prefix)
DEEPLINK_HOSTS: dict[str, dict[str, str]] = {
    "jeleefomgeving.nl": {
        "desc": "Gedeeld inzage-portaal (Tilburg, Bronckhorst, Edam-Volendam, …)",
        "pattern": "/inzien/{bg-id}/{uuid}/",
    },
    "edataloket.odnzkg.nl": {
        "desc": "Omgevingsdienst Noordzeekanaalgebied — incl. Haarlemmermeer, A'dam, NH",
        "pattern": "?q={'search':'...'}",
    },
    "publicaties.eindhoven.nl": {
        "desc": "Eindhoven dossier-portaal",
        "pattern": "/dossier/EHV-ZP{jaar}-{nr}",
    },
    "loket.dcmr.nl": {
        "desc": "DCMR Milieudienst Rijnmond (Mozard)",
        "pattern": "/mozard/!suite92.scherm1007?mObj={id}",
    },
    "eloket.velsen.nl": {
        "desc": "Velsen e-loket",
        "pattern": "/o/search?q={zaaknr}",
    },
    "formulieren.middendrenthe.nl": {
        "desc": "Midden-Drenthe (Mozard)",
        "pattern": "/website/!suite42.scherm1260?mObj={id}",
    },
    "zaakloket.ameland.nl": {
        "desc": "Ameland (Mozard)",
        "pattern": "/mozard/!suite42.scherm1260?mObj={id}",
    },
    "pvgrp.mozardsaas.nl": {
        "desc": "Provincie Groningen (Mozard SaaS)",
        "pattern": "/mozard/!suite92.scherm1007?mObj={id}",
    },
}


# ---- Extractie -------------------------------------------------------------

# Elementen waarvan we de tekst-content en href-/doc-/ref-attributen lezen.
_VERWIJS_TAGS = {"extref", "intref", "a", "extlink", "intlink"}
_VERWIJS_ATTRS = {"doc", "ref", "href"}
_BOILERPLATE_TAGS = {"officiele-publicatie", "meta"}

_URL_RE = re.compile(r"https?://[^\s)>\]\"']+", re.IGNORECASE)


def extract_deeplinks(xml_text: str) -> list[tuple[str, str]]:
    """
    Haal alle deeplink-URLs uit een publicatie-XML.

    Filtert op WHITELIST en eist een non-trivial pad of querystring
    (puur host-only URLs zoals 'https://jeleefomgeving.nl' zijn GEEN
    deeplinks, ook al matcht de host).

    Returns: [(url, bron_element)] — bron_element bijv. 'extref@doc'.
    """
    out: list[tuple[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out

    seen: set[str] = set()
    for elem in root.iter():
        tag = elem.tag.split("}", 1)[-1]
        if tag in _BOILERPLATE_TAGS:
            continue
        if tag not in _VERWIJS_TAGS:
            continue

        candidates: list[tuple[str, str]] = []
        for aname, aval in elem.attrib.items():
            aname_local = aname.split("}", 1)[-1]
            if aname_local in _VERWIJS_ATTRS and aval:
                if aval.lower().startswith(("http://", "https://")):
                    candidates.append((aval, f"{tag}@{aname_local}"))
        if elem.text:
            for u in _URL_RE.findall(elem.text):
                candidates.append((u, f"{tag} text"))

        for url, bron in candidates:
            if not _is_deeplink(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append((url, bron))
    return out


def _is_deeplink(url: str) -> bool:
    """Whitelist-host + non-trivial pad/query."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    host = p.netloc.lower()
    if host not in DEEPLINK_HOSTS:
        return False
    # Verwerp host-only URLs (geen pad, geen query)
    if (p.path in ("", "/")) and not p.query:
        return False
    return True


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


# ---- Validatie -------------------------------------------------------------

_DEFAULT_TIMEOUT = 10.0
_HEADERS = {
    "User-Agent": "OCD-deeplink-validator/0.1 (research)",
    "Accept": "text/html,application/xhtml+xml,*/*",
}


def make_client(timeout: float = _DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(headers=_HEADERS, timeout=timeout)


def validate_url(client: httpx.Client, url: str) -> dict:
    """
    Bezoek een URL, volg redirects, geef terug:
      status, final_url, content_length, error.

    Probeert eerst HEAD, valt terug op GET als HEAD niet ondersteund wordt.
    """
    t0 = time.time()
    try:
        r = client.head(url, follow_redirects=True)
        if r.status_code in (405, 501) or (
            r.status_code >= 400 and r.status_code != 404
        ):
            r = client.get(url, follow_redirects=True)
        cl_hdr = r.headers.get("content-length")
        cl = int(cl_hdr) if cl_hdr and cl_hdr.isdigit() else None
        return {
            "status": r.status_code,
            "final_url": str(r.url),
            "content_length": cl,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "error": None,
        }
    except httpx.TimeoutException:
        return {
            "status": None, "final_url": None, "content_length": None,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "error": "timeout",
        }
    except httpx.HTTPError as e:
        return {
            "status": None, "final_url": None, "content_length": None,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }


# ---- DB-insert (gedeelde helper) -------------------------------------------

_INSERT_VALIDATED_SQL = (
    "INSERT INTO vth.vergunning_deeplink "
    "(koop_id, inzage_url, host, bron_element, "
    " http_status, final_url, content_length, gevalideerd_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (koop_id, inzage_url) DO UPDATE SET "
    "  http_status = EXCLUDED.http_status, "
    "  final_url = EXCLUDED.final_url, "
    "  content_length = EXCLUDED.content_length, "
    "  gevalideerd_at = now(), "
    "  bron_element = EXCLUDED.bron_element"
)

_INSERT_UNVALIDATED_SQL = (
    "INSERT INTO vth.vergunning_deeplink "
    "(koop_id, inzage_url, host, bron_element) "
    "VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (koop_id, inzage_url) DO UPDATE SET "
    "  bron_element = EXCLUDED.bron_element"
)


def upsert_deeplink(
    cur,
    koop_id: str,
    url: str,
    bron_element: str,
    validation: dict | None = None,
) -> None:
    """
    Insert/update één deeplink.

    Als `validation` is meegegeven (met status, final_url, content_length),
    wordt `gevalideerd_at` op now() gezet. Anders blijven validatie-velden
    NULL — voor latere periodieke validate-pass.
    """
    host = host_of(url)
    if validation is not None and validation.get("status") is not None:
        cur.execute(
            _INSERT_VALIDATED_SQL,
            (koop_id, url, host, bron_element,
             validation["status"], validation.get("final_url"),
             validation.get("content_length")),
        )
    else:
        cur.execute(
            _INSERT_UNVALIDATED_SQL,
            (koop_id, url, host, bron_element),
        )
