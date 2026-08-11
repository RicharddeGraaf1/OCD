"""
Leefomgevingskwaliteit — readout per locatie.

Eén endpoint dat per milieuthema een genormaliseerde waarde teruggeeft voor een
RD-punt, zodat de OCD-viewer het feitelijke leefomgevingsbeeld naast het
juridische kan tonen. Zie plan `docs/plans/leefomgevingskwaliteit-bronintegratie.md`.

Ontwerpprincipes (belasting):
  * **Toggles** — `OCD_LEEFOMGEVING_ENABLED` (fail-closed 503) en
    `OCD_LEEFOMGEVING_BRONNEN` (per-thema aanzetten). Een uitgezet/onbekend thema
    levert `null` (de viewer toont "onbekend"), geen call.
  * **Cache + single-flight** — resultaat wordt gecachet op een afgerond
    (x,y)-grid (100 m) met TTL; gelijktijdige identieke requests doen samen één
    berekening. Faalt een adapter, dan korte negatieve TTL zodat een transiënte
    storing vanzelf herstelt.
  * **Ingest boven live** — de externe-veiligheid-adapter query't lokaal PostGIS
    (`lev.rev_risicobron`, geladen via `dso-loader/scripts/2026-07-load-rev.py`),
    dus nul live-calls naar externe systemen per request.

Fase B stap 1 (`extern`) + stap 2 (`lucht`) + stap 3 (`geluid`) zijn geïmplementeerd.
`extern` query't lokaal PostGIS (REV-ingest); `lucht` bevraagt de RIVM GCN
grootschalige-concentratiekaarten (jaargemiddelde µg/m³, EU-grenswaarden/WHO);
`geluid` bevraagt de RIVM Atlas Leefomgeving Lden-kaarten (cumulatieve
geluidbelasting, indicatieve hinderklasse). Beide grids via WMS GetFeatureInfo,
live + gecached. De overige thema's staan default uit en leveren `null`.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from db import get_conn

router = APIRouter(prefix="/v1/leefomgeving", tags=["leefomgeving"])

# ── Config ────────────────────────────────────────────────────────────

ALLE_THEMAS = ["geluid", "lucht", "extern", "klimaat", "bodem", "natuur", "cultuur"]

_ENABLED = os.getenv("OCD_LEEFOMGEVING_ENABLED", "true").lower() in ("1", "true", "yes")
# Default live: 'extern'/'natuur'/'cultuur' (PostGIS-ingest) + 'lucht'/'geluid'/'klimaat'
# (RIVM-grids, live+gecached).
_BRONNEN = {
    t.strip()
    for t in os.getenv("OCD_LEEFOMGEVING_BRONNEN", "extern,lucht,geluid,klimaat,natuur,cultuur").split(",")
    if t.strip()
}
_CACHE_TTL = int(os.getenv("OCD_LEEFOMGEVING_CACHE_TTL", "86400"))   # 24 u (default)
_FAIL_TTL = int(os.getenv("OCD_LEEFOMGEVING_FAIL_TTL", "120"))       # 2 min bij fout
_GRID = 100  # cache-grid in meters (naburige klikken delen een cel)

# Per-thema TTL-override. Lucht (GCN-jaargemiddelde) + geluid (RIVM EU-END-kaart, ~5-jaarlijks)
# veranderen zelden → lange TTL.
_THEMA_TTL = {
    "lucht": int(os.getenv("OCD_LEEFOMGEVING_LUCHT_TTL", str(7 * 86400))),      # 7 dagen
    "geluid": int(os.getenv("OCD_LEEFOMGEVING_GELUID_TTL", str(7 * 86400))),    # 7 dagen
    "klimaat": int(os.getenv("OCD_LEEFOMGEVING_KLIMAAT_TTL", str(30 * 86400))),  # 30 dagen (statische kaart)
}

# Live HTTP (WMS) — timeout + globale outbound-limiet (nette buur bij PDOK/RIVM).
_HTTP_TIMEOUT = float(os.getenv("OCD_LEEFOMGEVING_HTTP_TIMEOUT", "5"))
# Lucht = RIVM GCN grootschalige-concentratiekaarten (jaargemiddelde µg/m³), toetsbaar
# aan EU-grenswaarden. GetFeatureInfo op het punt geeft de exacte celwaarde.
_GCN_WMS = os.getenv("OCD_GCN_WMS", "https://data.rivm.nl/geo/gcn/wms")
_GCN_JAAR = os.getenv("OCD_GCN_JAAR", "2025")  # recentste GCN-kaartjaar
# Geluid = RIVM Atlas Leefomgeving Lden-kaarten (cumulatief 'alle bronnen' + per bron);
# GetFeatureInfo geeft de Lden-waarde (dB) in de property GRAY_INDEX.
_ALO_WMS = os.getenv("OCD_ALO_WMS", "https://data.rivm.nl/geo/alo/wms")
_GELUID_TOTAAL_LAAG = os.getenv(
    "OCD_GELUID_TOTAAL_LAAG", "rivm_20220601_Geluid_lden_allebronnen_2020")
_GELUID_WEG_LAAG = os.getenv(
    "OCD_GELUID_WEG_LAAG", "rivm_20220601_Geluid_lden_wegverkeer_2020")
_GELUID_JAAR = os.getenv("OCD_GELUID_JAAR", "2020")  # datajaar (alleen weergave)
_OUTBOUND = threading.Semaphore(int(os.getenv("OCD_LEEFOMGEVING_OUTBOUND", "4")))

# Drempels (jaargemiddelde µg/m³): EU-grenswaarde = harde 'stop' (wettelijk, Richtlijn
# 2008/50/EG); WHO-advieswaarde 2021 = 'warn' (gezondheidskundig, strenger). De herziene
# EU-richtlijn 2024/2881 (grenswaarden per 2030: NO₂ 20, PM2.5 10) staat nog niet als
# drempel — bewuste keuze: toets nu aan de vigerende norm. Herkomst + bronnen:
# vault-concept 'Luchtkwaliteitsnormen' (OmgevingswetKnowledgeBase). Deze tabel en
# de vault-tabel moeten gelijk blijven.
_LUCHT_NORMEN = {
    "no2":  {"label": "NO₂",   "who": 10.0, "eu": 40.0},
    "pm25": {"label": "PM2.5", "who": 5.0,  "eu": 25.0},
}

# Drempels geluid (cumulatief Lden, dB): indicatieve hinder-/blootstellingsklassen,
# géén juridische per-bron-toets (die hangt van bron + situatie af). warn ≥ EU-END-
# rapportagedrempel (Richtlijn 2002/49/EG, kaarten vanaf 55 dB Lden); stop = hoge
# blootstelling. Herkomst: vault-concept 'Geluidsnormen leefomgeving'.
_GELUID_WARN = float(os.getenv("OCD_GELUID_WARN_DB", "55"))
_GELUID_STOP = float(os.getenv("OCD_GELUID_STOP_DB", "65"))

# Klimaat = kans op overstroming uit de nationale RIVM-overstromingskaart (Atlas
# Leefomgeving). De GFI geeft een klasse-index (GRAY_INDEX 1–6); labels + volgorde
# zijn geverifieerd tegen de WMS-legenda (GetLegendGraphic) en met sanity-punten
# (IJsselmeer=6/water, Waal-uiterwaard=4/1×100 jr). De ok/warn/stop-banding is een
# indicatieve leefomgevings-duiding (geen juridische norm), net als bij geluid.
_KLIMAAT_LAAG = os.getenv("OCD_KLIMAAT_OVERSTROMING_LAAG", "20231201_kans_overstroming")
_OVERSTROMING_KLASSEN: dict[int, tuple[str, str]] = {
    1: ("Overstroomt niet", "ok"),
    2: ("1× per 100.000 jaar", "ok"),
    3: ("1× per 1.000 jaar", "warn"),
    4: ("1× per 100 jaar", "stop"),
    5: ("1× per 10 jaar", "stop"),
    6: ("Oppervlaktewater", "ok"),
}

# Natuur = nabijheid dichtstbijzijnde Natura 2000-gebied (lokaal PostGIS-ingest,
# lev.natura2000, geladen via dso-loader/scripts/2026-08-load-natura2000.py). Zoekstraal
# + warn-drempel zijn indicatief: nabijheid → juridische relevantie/stikstofgevoeligheid.
# Depositiewaarden (AERIUS) volgen later; nu een nabijheidsindicatie.
_NATUUR_RADIUS = int(os.getenv("OCD_NATUUR_RADIUS", "3000"))  # zoekstraal (m)
_NATUUR_WARN = int(os.getenv("OCD_NATUUR_WARN", "1000"))      # afstand ≤ → warn

# Cultuur = nabijheid dichtstbijzijnde rijksmonument (lokaal PostGIS-ingest,
# lev.rijksmonument, geladen via dso-loader/scripts/2026-08-load-rijksmonumenten.py).
# Kortere schaal dan natuur: monument-relevantie (welstand/omgevingsvergunning) speelt
# op straatniveau. Archeologische verwachting = latere verrijking op dit thema.
_CULTUUR_RADIUS = int(os.getenv("OCD_CULTUUR_RADIUS", "500"))  # zoekstraal (m)
_CULTUUR_WARN = int(os.getenv("OCD_CULTUUR_WARN", "50"))       # afstand ≤ → warn


# ── Modellen ──────────────────────────────────────────────────────────

class Readout(BaseModel):
    value: str
    unit: str
    ctx: str
    status: Literal["ok", "warn", "stop"]


class Locatie(BaseModel):
    x: float
    y: float


class ReadoutResponse(BaseModel):
    locatie: Locatie
    # None = thema uitgeschakeld of adapter kon geen waarde bepalen.
    readouts: dict[str, Readout | None]


# ── Adapters ──────────────────────────────────────────────────────────
#
# Signatuur: (x, y) -> Readout | None. `None` betekent "geen waarde bepaald"
# (fout/onbekend); een geldige "geen risicobron"-uitkomst is een echte Readout.

def _adapter_extern(x: float, y: float) -> Readout | None:
    """Externe veiligheid: dichtstbijzijnde geregistreerde risicobron (REV),
    lokaal uit PostGIS. Aandachtsgebied-contouren volgen later; nu een
    afstandsindicatie tot de bron zelf."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT round(ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 28992))::numeric, 0) AS dist
            FROM lev.rev_risicobron
            WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 28992), 1000)
            ORDER BY dist
            LIMIT 1
            """,
            (x, y, x, y),
        )
        row = cur.fetchone()

    if row is None:
        return Readout(
            value="Nee",
            unit="risicobron nabij",
            ctx="Geen geregistreerde risicobron binnen 1 km",
            status="ok",
        )
    d = int(row["dist"])
    if d <= 250:
        return Readout(value="Ja", unit="risicobron nabij",
                       ctx=f"Geregistreerde risicobron op {d} m", status="stop")
    return Readout(value="Nabij", unit="risicobron nabij",
                   ctx=f"Geregistreerde risicobron op {d} m", status="warn")


def _adapter_natuur(x: float, y: float) -> Readout | None:
    """Natuur: nabijheid van het dichtstbijzijnde Natura 2000-gebied, lokaal uit
    PostGIS (`lev.natura2000`). In-gebied = sterkste signaal (juridische beperkingen,
    stikstofgevoeligheid); binnen `_NATUUR_WARN` = warn; verder = ok. Zelfde
    nearest-distance-patroon als `extern`."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT naam,
                   round(ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 28992))::numeric, 0) AS dist
            FROM lev.natura2000
            WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 28992), %s)
            ORDER BY dist
            LIMIT 1
            """,
            (x, y, x, y, _NATUUR_RADIUS),
        )
        row = cur.fetchone()

    if row is None:
        return Readout(value="Nee", unit="Natura 2000 nabij",
                       ctx=f"Geen Natura 2000-gebied binnen {_NATUUR_RADIUS // 1000} km", status="ok")
    naam = row["naam"]
    d = int(row["dist"])
    if d == 0:
        return Readout(value="In gebied", unit="Natura 2000",
                       ctx=f"Locatie ligt in Natura 2000-gebied {naam}", status="stop")
    if d <= _NATUUR_WARN:
        return Readout(value="Nabij", unit="Natura 2000 nabij",
                       ctx=f"Natura 2000-gebied {naam} op {d} m", status="warn")
    return Readout(value="Nee", unit="Natura 2000 nabij",
                   ctx=f"Natura 2000-gebied {naam} op {d} m", status="ok")


def _adapter_cultuur(x: float, y: float) -> Readout | None:
    """Cultuur: nabijheid van het dichtstbijzijnde rijksmonument, lokaal uit PostGIS
    (`lev.rijksmonument`, punt + monumentterrein-vlak). Op een monumentterrein = stop;
    binnen `_CULTUUR_WARN` = warn; verder = ok. Zelfde nearest-distance-patroon als
    `extern`/`natuur`. Archeologische verwachting volgt later als aparte laag."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT registernr, soort,
                   round(ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 28992))::numeric, 0) AS dist
            FROM lev.rijksmonument
            WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 28992), %s)
            ORDER BY dist
            LIMIT 1
            """,
            (x, y, x, y, _CULTUUR_RADIUS),
        )
        row = cur.fetchone()

    if row is None:
        return Readout(value="Nee", unit="rijksmonument nabij",
                       ctx=f"Geen rijksmonument binnen {_CULTUUR_RADIUS} m", status="ok")
    ref = f" {row['registernr']}" if row["registernr"] else ""
    d = int(row["dist"])
    if d == 0 and row["soort"] == "vlak":
        return Readout(value="In gebied", unit="rijksmonument",
                       ctx=f"Locatie ligt op rijksmonumentterrein{ref}", status="stop")
    if d <= _CULTUUR_WARN:
        return Readout(value="Nabij", unit="rijksmonument nabij",
                       ctx=f"Rijksmonument{ref} op {d} m", status="warn")
    return Readout(value="Nee", unit="rijksmonument nabij",
                   ctx=f"Rijksmonument{ref} op {d} m", status="ok")


def _gridwaarde(val) -> float | None:
    """Parse een GFI-celwaarde. RIVM-grids geven een negatieve sentinel (bv. -999)
    voor cellen buiten het modelgebied (zee, buitenland) → dat is geen concentratie."""
    if val is None:
        return None
    f = float(val)
    return f if f >= 0 else None


def _wms_gfi(base: str, layer: str, x: float, y: float, prop: str | None = None) -> float | None:
    """WMS 1.3.0 GetFeatureInfo op een RD-punt → de gridwaarde.

    Een 100 m-bbox met 101×101 pixels en I=J=50 mikt op de cel onder het punt.
    EPSG:28992 heeft in WMS 1.3.0 asvolgorde E,N (empirisch bevestigd). `prop` is
    de property met de waarde: bij vector-achtige lagen de laagnaam zelf (GCN), bij
    raster-lagen `GRAY_INDEX` (RIVM geluid). Default = de laagnaam."""
    with _OUTBOUND:
        r = httpx.get(base, params={
            "service": "WMS", "version": "1.3.0", "request": "GetFeatureInfo",
            "layers": layer, "query_layers": layer, "crs": "EPSG:28992",
            "bbox": f"{x - 50},{y - 50},{x + 50},{y + 50}",
            "width": 101, "height": 101, "i": 50, "j": 50,
            "info_format": "application/json",
        }, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return None
    return _gridwaarde((feats[0].get("properties") or {}).get(prop or layer))


def _lucht_band(stof: str, waarde: float) -> str:
    """ok ≤ WHO-advieswaarde < warn ≤ EU-grenswaarde < stop."""
    n = _LUCHT_NORMEN[stof]
    if waarde > n["eu"]:
        return "stop"
    if waarde > n["who"]:
        return "warn"
    return "ok"


def _adapter_lucht(x: float, y: float) -> Readout | None:
    """Luchtkwaliteit: jaargemiddelde NO₂- en PM2.5-concentratie uit de RIVM
    GCN grootschalige-concentratiekaarten (achtergrondconcentratie), de grootheid
    waarop de EU-grenswaarden zijn gedefinieerd. Status = de strengste band over
    beide stoffen (PM2.5 is de gezondheidskundig meest relevante → primaire waarde)."""
    pm25 = _wms_gfi(_GCN_WMS, f"conc_PM25_{_GCN_JAAR}", x, y)
    no2 = _wms_gfi(_GCN_WMS, f"conc_NO2_{_GCN_JAAR}", x, y)
    if pm25 is None and no2 is None:
        return None

    rang = {"ok": 0, "warn": 1, "stop": 2}
    status = "ok"
    delen: list[str] = []
    for stof, waarde in (("pm25", pm25), ("no2", no2)):
        if waarde is None:
            continue
        band = _lucht_band(stof, waarde)
        if rang[band] > rang[status]:
            status = band
        delen.append(f"{_LUCHT_NORMEN[stof]['label']} {waarde:.0f}")

    if pm25 is not None:
        value, unit = f"{pm25:.0f}", "µg/m³ PM2.5"
    else:
        value, unit = f"{no2:.0f}", "µg/m³ NO₂"
    ctx = f"Jaargemiddelde {_GCN_JAAR} · " + " · ".join(delen)
    return Readout(value=value, unit=unit, ctx=ctx, status=status)


def _adapter_geluid(x: float, y: float) -> Readout | None:
    """Geluid: cumulatieve Lden-geluidbelasting (alle bronnen) uit de RIVM Atlas
    Leefomgeving EU-END-kaarten. Indicatieve hinderklasse (ok/warn/stop), geen
    juridische per-bron-toets. Verrijkt met de dominante bron (wegverkeer, dat in
    NL vrijwel overal maatgevend is). Stil gebied = lage/0 dB → ok; buiten het
    modelgebied (zee) = geen feature → None."""
    totaal = _wms_gfi(_ALO_WMS, _GELUID_TOTAAL_LAAG, x, y, "GRAY_INDEX")
    if totaal is None:
        return None

    status = "stop" if totaal > _GELUID_STOP else ("warn" if totaal > _GELUID_WARN else "ok")
    if status == "ok":
        # Stil/laag → geen dominante-bron-duiding (en geen extra call).
        ctx = f"Cumulatief {_GELUID_JAAR} · lage geluidbelasting"
    else:
        weg = _wms_gfi(_ALO_WMS, _GELUID_WEG_LAAG, x, y, "GRAY_INDEX")
        # Energetisch: als wegverkeer ~gelijk is aan het totaal, domineert het geheel.
        bron = "wegverkeer dominant" if (weg is not None and weg >= totaal - 1) else "meerdere bronnen"
        ctx = f"Cumulatief {_GELUID_JAAR} · {bron}"
    return Readout(value=f"{totaal:.0f}", unit="dB Lden", ctx=ctx, status=status)


def _adapter_klimaat(x: float, y: float) -> Readout | None:
    """Klimaat: kans op overstroming uit de nationale RIVM-overstromingskaart.
    De GFI geeft een klasse-index (GRAY_INDEX); label + status uit
    `_OVERSTROMING_KLASSEN`. Buiten het modelgebied → geen feature → None.
    Subthema's hitte/droogte volgen later als aparte lagen."""
    idx = _wms_gfi(_ALO_WMS, _KLIMAAT_LAAG, x, y, "GRAY_INDEX")
    if idx is None:
        return None
    klasse = _OVERSTROMING_KLASSEN.get(int(round(idx)))
    if klasse is None:
        return None
    label, status = klasse
    ctx = ("Locatie ligt in oppervlaktewater" if int(round(idx)) == 6
           else "Nationale overstromingskaart (Atlas Leefomgeving)")
    return Readout(value=label, unit="overstromingskans", ctx=ctx, status=status)


ADAPTERS: dict[str, Callable[[float, float], Readout | None]] = {
    "extern": _adapter_extern,
    "lucht": _adapter_lucht,
    "geluid": _adapter_geluid,
    "klimaat": _adapter_klimaat,
    "natuur": _adapter_natuur,
    "cultuur": _adapter_cultuur,
}


# ── Cache + single-flight ─────────────────────────────────────────────

_cache: dict[tuple, tuple[float, Readout | None, int]] = {}   # key -> (ts, val, ttl)
_cache_lock = threading.Lock()
_key_locks: dict[tuple, threading.Lock] = {}


def _cache_key(thema: str, x: float, y: float) -> tuple:
    return (thema, round(x / _GRID), round(y / _GRID))


def _readout_gecached(thema: str, x: float, y: float) -> Readout | None:
    key = _cache_key(thema, x, y)
    now = time.time()

    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < hit[2]:
            return hit[1]
        lock = _key_locks.setdefault(key, threading.Lock())

    # Single-flight: één thread berekent per cel; de rest wacht en leest de cache.
    with lock:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < hit[2]:
                return hit[1]
        try:
            val = ADAPTERS[thema](x, y)
            ttl = _THEMA_TTL.get(thema, _CACHE_TTL) if val is not None else _FAIL_TTL
        except Exception:  # noqa: BLE001 — fail-soft: één bron mag de rest niet breken
            val, ttl = None, _FAIL_TTL
        with _cache_lock:
            _cache[key] = (time.time(), val, ttl)
        return val


# ── Endpoint ──────────────────────────────────────────────────────────

@router.get("/readout", response_model=ReadoutResponse, summary="Leefomgevingskwaliteit per locatie")
def readout(
    x: float = Query(..., description="RD X (EPSG:28992)"),
    y: float = Query(..., description="RD Y (EPSG:28992)"),
    lagen: str | None = Query(None, description="Comma-separated thema-ids; default alle"),
) -> ReadoutResponse:
    if not _ENABLED:
        raise HTTPException(503, "Leefomgeving-readout is uitgeschakeld (OCD_LEEFOMGEVING_ENABLED)")

    gevraagd = [t.strip() for t in lagen.split(",") if t.strip()] if lagen else list(ALLE_THEMAS)
    readouts: dict[str, Readout | None] = {}
    for thema in gevraagd:
        if thema in _BRONNEN and thema in ADAPTERS:
            readouts[thema] = _readout_gecached(thema, x, y)
        else:
            readouts[thema] = None
    return ReadoutResponse(locatie=Locatie(x=x, y=y), readouts=readouts)
