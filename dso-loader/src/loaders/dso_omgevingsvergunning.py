"""Loader voor DSO-omgevingsvergunningen — satelliet naast het KOOP-register.

Bron: DSO presenteren/v8 `/omgevingsvergunningen` (~14.6k records, 99,9% gemeente).
Opname in dit endpoint ÍS de planafwijking (buitenplanse omgevingsplanactiviteit,
BOPA) per definitie: een binnenplanse vergunning hoeft geen omgevingsdocument te
zijn. De loader legt daarom niet de afwijk-classificatie vast (die zit op het
KOOP-register uit de bodytekst), maar:

  1. de harde koppeling naar bevoegd gezag  (bevoegd_gezag_code -> core.bronhouder),
  2. de 1:1-koppeling naar het KOOP-register (koop_id, afgeleid uit publicatieID),
  3. de vergunningsgebied-geometrie          (geometrie_identificatie),
  4. de bronfout-inversie                    (binnenplans_anomalie).

Zie de vault-analyse "Afwijkvergunning (BOPA) vastleggen - DSO-bron, join en
classificatie" + gaps#G-84.
"""
import datetime as dt
import uuid as uuidlib

import httpx
from rich.console import Console

from src.canonieke_bronhouders import upsert_bronhouder
from src.config import cfg
from src.db import get_conn
from src.ddl import OVG_DDL
from src.rate_limiter import limiter

console = Console()

_PAGE_SIZE = 200


def _headers() -> dict:
    return {"x-api-key": cfg.DSO_API_KEY}


def _fetch_page(page: int) -> dict:
    url = f"{cfg.PRESENTEREN_BASE}/omgevingsvergunningen"
    with limiter:
        r = httpx.get(url, headers=_headers(),
                      params={"page": page, "size": _PAGE_SIZE, "_expand": "true"},
                      timeout=30)
    r.raise_for_status()
    return r.json()


def _valid_uuid(val):
    try:
        return str(uuidlib.UUID(val)) if val else None
    except (ValueError, TypeError, AttributeError):
        return None


def _parse(item: dict) -> dict:
    aang = item.get("aangeleverdDoorEen") or {}
    geo = (item.get("_embedded") or {}).get("vergunningsgebied") or {}
    reg = item.get("geregistreerdMet") or {}
    imro = [s for s in (item.get("iMROPlanidentificatie") or []) if s and s.strip()]
    return {
        "identificatie": item["identificatie"],
        "bevoegd_gezag_code": aang.get("code"),
        "bevoegd_gezag_naam": aang.get("naam"),
        "bestuurslaag": aang.get("bestuurslaag"),
        "officiele_titel": item.get("officieleTitel"),
        "referentienummer": item.get("referentienummer"),
        "publicatie_id": item.get("publicatieID"),
        "begin_geldigheid": reg.get("beginGeldigheid"),
        "begin_inwerking": reg.get("beginInwerking"),
        "tijdstip_registratie": reg.get("tijdstipRegistratie"),
        "versie": reg.get("versie"),
        "vergunningsgebied_id": geo.get("identificatie"),
        "geometrie_identificatie": _valid_uuid(geo.get("geometrieIdentificatie")),
        "imro_planidentificatie": imro or None,
    }


_UPSERT = """
INSERT INTO vth.omgevingsvergunning_dso
  (identificatie, bevoegd_gezag_code, officiele_titel, referentienummer,
   publicatie_id, begin_geldigheid, begin_inwerking, tijdstip_registratie, versie,
   vergunningsgebied_id, geometrie_identificatie, imro_planidentificatie, ingest_run_id)
VALUES
  (%(identificatie)s, %(bevoegd_gezag_code)s, %(officiele_titel)s, %(referentienummer)s,
   %(publicatie_id)s, %(begin_geldigheid)s, %(begin_inwerking)s, %(tijdstip_registratie)s,
   %(versie)s, %(vergunningsgebied_id)s, %(geometrie_identificatie)s,
   %(imro_planidentificatie)s, %(ingest_run_id)s)
ON CONFLICT (identificatie) DO UPDATE SET
   bevoegd_gezag_code=EXCLUDED.bevoegd_gezag_code, officiele_titel=EXCLUDED.officiele_titel,
   referentienummer=EXCLUDED.referentienummer, publicatie_id=EXCLUDED.publicatie_id,
   begin_geldigheid=EXCLUDED.begin_geldigheid, begin_inwerking=EXCLUDED.begin_inwerking,
   tijdstip_registratie=EXCLUDED.tijdstip_registratie, versie=EXCLUDED.versie,
   vergunningsgebied_id=EXCLUDED.vergunningsgebied_id,
   geometrie_identificatie=EXCLUDED.geometrie_identificatie,
   imro_planidentificatie=EXCLUDED.imro_planidentificatie,
   ingest_run_id=EXCLUDED.ingest_run_id, ingest_at=now()
"""

# koop_id (nullable FK) los invullen: alleen zetten waar het KOOP-record bestaat.
# publicatieID akn/.../gmb/2024/114849/... -> koop_id 'gmb-2024-114849'.
_LINK_KOOP = r"""
UPDATE vth.omgevingsvergunning_dso o
SET koop_id = v.koop_id
FROM vth.vergunningkennisgeving v
WHERE v.koop_id = (
    (regexp_match(o.publicatie_id, '/officialGazette/([a-z]+)/([0-9]+)/([^/]+)/'))[1]
    || '-' ||
    (regexp_match(o.publicatie_id, '/officialGazette/([a-z]+)/([0-9]+)/([^/]+)/'))[2]
    || '-' ||
    (regexp_match(o.publicatie_id, '/officialGazette/([a-z]+)/([0-9]+)/([^/]+)/'))[3]
)
AND o.koop_id IS DISTINCT FROM v.koop_id
"""

# Bronfout-inversie: binnenplans in de KOOP-body, geen enkel buitenplans/BOPA-signaal.
_ANOMALIE = r"""
UPDATE vth.omgevingsvergunning_dso o
SET binnenplans_anomalie = (
        v.inhoud_tekst ILIKE '%%binnenplanse omgevingsplanactiviteit%%'
    AND v.inhoud_tekst NOT ILIKE '%%buitenplanse omgevingsplanactiviteit%%'
    AND v.inhoud_tekst NOT ILIKE '%%(bopa)%%'
    AND o.officiele_titel NOT ILIKE '%%bopa%%'
    AND o.officiele_titel NOT ILIKE '%%buitenplanse omgevingsplanactiviteit%%'
)
FROM vth.vergunningkennisgeving v
WHERE o.koop_id = v.koop_id AND v.inhoud_tekst IS NOT NULL
"""


def setup_ovg() -> None:
    """Pas OVG_DDL toe (idempotent). Vereist core.bronhouder + vth.vergunningkennisgeving."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(OVG_DDL)
        conn.commit()
        console.print("[green]OVG-schema toegepast (vth.omgevingsvergunning_dso).[/green]")
    finally:
        conn.close()


def load_ovg() -> None:
    """Haal alle DSO-omgevingsvergunningen op en upsert ze; koppel + detecteer bronfouten."""
    run_id = "ovg-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    conn = get_conn()
    seen_bg: set[str] = set()
    total = 0
    try:
        first = _fetch_page(1)
        pages = first["page"]["totalPages"]
        elements = first["page"]["totalElements"]
        console.print(f"DSO-omgevingsvergunningen: {elements} records over {pages} pages")

        for page in range(1, pages + 1):
            data = first if page == 1 else _fetch_page(page)
            items = data.get("_embedded", {}).get("omgevingsvergunningen", [])
            recs = [_parse(it) for it in items]

            with conn.cursor() as cur:
                # self-heal bevoegd gezag zodat de harde FK nooit breekt
                for r in recs:
                    code = r["bevoegd_gezag_code"]
                    if code and code not in seen_bg:
                        upsert_bronhouder(cur, code, r["bevoegd_gezag_naam"] or code,
                                          r["bestuurslaag"])
                        seen_bg.add(code)
                cur.executemany(_UPSERT, [{**r, "ingest_run_id": run_id} for r in recs])
            conn.commit()
            total += len(recs)
            if page % 10 == 0 or page == pages:
                console.print(f"  page {page}/{pages} — {total} records")

        console.print("koppelen aan KOOP-register (koop_id)…")
        with conn.cursor() as cur:
            cur.execute(_LINK_KOOP)
            linked = cur.rowcount
        conn.commit()

        console.print("bronfout-detectie (binnenplans_anomalie)…")
        with conn.cursor() as cur:
            cur.execute(_ANOMALIE)
        conn.commit()

        _summary(conn, total, linked)
    finally:
        conn.close()


def _summary(conn, total: int, linked: int) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) n, count(koop_id) k, "
                    "count(*) FILTER (WHERE binnenplans_anomalie) a, "
                    "count(*) FILTER (WHERE geometrie_identificatie IS NOT NULL) g "
                    "FROM vth.omgevingsvergunning_dso")
        s = cur.fetchone()
        console.print(f"\n[bold]Klaar.[/bold] {s['n']} records | "
                      f"koop_id gekoppeld: {s['k']} ({s['k']/max(1,s['n']):.1%}) | "
                      f"geometrie: {s['g']} | [red]bronfout-anomalie: {s['a']}[/red]")
        if s["a"]:
            cur.execute("SELECT identificatie, bevoegd_gezag_code, officiele_titel "
                        "FROM vth.omgevingsvergunning_dso WHERE binnenplans_anomalie LIMIT 20")
            console.print("Anomalieën (binnenplans maar tóch in DSO — vermoedelijke bronfout BG):")
            for r in cur.fetchall():
                console.print(f"  {r['identificatie']} · {r['bevoegd_gezag_code']} · "
                              f"{(r['officiele_titel'] or '')[:80]}")
