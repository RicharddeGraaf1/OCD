"""
Endpoints voor het publiek vergunningen-register (achter omgevingsvergunningenregister.nl).

Leest uit `vth.vergunningkennisgeving` (zie vault_v1/analysis/Ingest
omgevingsvergunningen uit officielebekendmakingen.md). Bewust losstaand
schema: geen FK's naar `dso.*` of `p2p.*`.

Endpoints:
    GET  /v1/vergunningen           — paginated, filtered list
    GET  /v1/vergunningen/pins      — lightweight pin-only voor kaartweergave
    GET  /v1/vergunningen/facets    — filter-counters (per filter-waarde)
    GET  /v1/vergunningen/stats     — totaal + laatste-ingest, voor header
    GET  /v1/vergunningen/{koop_id} — volledige record-details

Filter-conventie (gedeeld door list / pins / facets):
    q       full-text over titel + beschrijving + inhoud_tekst + adres
            (tsvector 'dutch' met prefix-match, via idx_vk_tsv)
    tb      type_besluit, repeatable (?tb=aanvraag&tb=verleend)
    ac      activiteit_code, repeatable
    bg      bg_naam, repeatable
    org     organisatietype, repeatable
    th      subject_taxonomie, repeatable
    vanaf   datum_publicatie >= …
    totd    datum_publicatie <= …
    geom    true → alleen records met geometrie
    ontv    true → alleen records met datum_ontvangst
    zaak    ILIKE op zaaknummer_bg
    bbox    "west,south,east,north" in WGS84 — alleen records waarvan
            geometrie_wgs_pt binnen de envelope valt
    afwijk  "bopa"     → alleen afwijkvergunningen (buitenplanse omgevingsplan-
                         activiteit, afwijk_status='buitenplans_expliciet')
            "regulier" → alles behalve bevestigde BOPA
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

from db import get_conn

router = APIRouter(prefix="/v1/vergunningen", tags=["vergunningen"])

# ─────────────────────────────────────────────────────────────────────
# Pydantic-modellen
# ─────────────────────────────────────────────────────────────────────


class VergunningSummary(BaseModel):
    """Lichtgewicht record voor lijstweergave — geen body-tekst."""

    koop_id: str
    publicatieblad: str
    bg_naam: str
    organisatietype: str | None
    titel: str
    datum_publicatie: date
    datum_publicatie_ts: datetime | None
    datum_ontvangst: date | None
    activiteit_code: str | None
    type_besluit: str | None
    subject_taxonomie: str | None
    geometrie_type: str | None
    lon: float | None = Field(None, description="WGS84 longitude (pin)")
    lat: float | None = Field(None, description="WGS84 latitude (pin)")
    straatnaam: str | None
    huisnummer: str | None
    postcode: str | None
    woonplaats: str | None
    zaaknummer_bg: str | None
    preferred_url: str | None
    pdf_url: str | None
    afwijk_status: str | None = Field(
        None,
        description=(
            "Afwijkvergunning-classificatie: 'buitenplans_expliciet' (BOPA), "
            "'binnenplans_expliciet', 'opa_onbepaald', of NULL (geen omgevingsplan-"
            "activiteit). Tekst-afgeleid — signaal, geen juridisch oordeel (gaps#G-84)."
        ),
    )


class VergunningDetail(VergunningSummary):
    """Volledige record voor detailweergave."""

    bg_scheme: str | None
    jaargang: int | None
    publicatienummer: str | None
    huisletter: str | None
    huisnummertoevoeging: str | None
    ligt_in_gemeente: str | None
    geometrielabel: str | None
    beschrijving: str | None
    inhoud_tekst: str | None
    geometrie_geojson: dict[str, Any] | None = Field(
        None, description="Volledige geometrie als GeoJSON (WGS84), bij Vlak-records"
    )
    xml_url: str | None
    bg_deeplink_url: str | None = Field(
        None,
        description=(
            "Werkende directe URL naar het inhoudelijke besluit-dossier bij het BG "
            "(uit vth.vergunning_deeplink, werkt=TRUE). NULL voor ~98,9% van records."
        ),
    )


class ListResponse(BaseModel):
    records: list[VergunningSummary]
    total: int
    total_capped: bool = Field(
        False,
        description=(
            "True als `total` op COUNT_CAP is afgetopt — het echte aantal is hoger. "
            "Een exacte count is alleen goedkoop zolang hij index-only kan; met een "
            "bbox- of tekstfilter moet Postgres de heap in en kost hij tientallen "
            "seconden (zie docstring bij _count_sql)."
        ),
    )
    limit: int
    offset: int
    took_ms: int


class Pin(BaseModel):
    id: str
    tb: str | None  # type_besluit
    lon: float
    lat: float


class PinsResponse(BaseModel):
    pins: list[Pin]
    total_matching: int
    total_capped: bool = Field(
        False, description="True als total_matching op COUNT_CAP is afgetopt"
    )
    returned: int
    truncated: bool
    cap: int
    took_ms: int


class FacetBucket(BaseModel):
    value: str
    count: int


class FacetsResponse(BaseModel):
    type_besluit: list[FacetBucket]
    activiteit_code: list[FacetBucket]
    organisatietype: list[FacetBucket]
    bg_naam: list[FacetBucket] = Field(
        ..., description="Top 100 BG's, gesorteerd op aantal"
    )
    subject_taxonomie: list[FacetBucket]
    publicatieblad: list[FacetBucket]
    took_ms: int


class StatsResponse(BaseModel):
    total: int
    last_publicatie: datetime | None
    last_ingest: datetime | None
    enriched: int
    enriched_pct: float
    per_type_besluit: list[FacetBucket]
    # Wanneer de matview met deze totalen is berekend; None = live geaggregeerd
    # (dev-DB zonder de matview-migratie). Verschil met last_ingest laat zien
    # of de refresh-ronde is overgeslagen.
    stats_refreshed_at: datetime | None = None
    took_ms: int


# ── Doorlooptijd (leest uit matview vth.dossier_doorlooptijd) ──────────


class DoorlooptijdKpi(BaseModel):
    n_dossiers: int
    n_bevoegd_gezag: int
    mediaan_dagen: int | None
    gemiddelde_dagen: float | None
    p25_dagen: int | None
    p75_dagen: int | None
    pct_binnen_8wk: float | None = Field(
        None, description="Aandeel dossiers met doorlooptijd <= 56 dagen (reguliere termijn)"
    )
    pct_boven_half_jaar: float | None = Field(
        None, description="Aandeel dossiers met doorlooptijd > 182 dagen"
    )


class DoorlooptijdBin(BaseModel):
    ondergrens_dagen: int
    bovengrens_dagen: int | None = Field(
        None, description="None = open bovengrens (>= ondergrens)"
    )
    count: int


class DoorlooptijdGroep(BaseModel):
    """Aggregaat per activiteit of per bevoegd gezag."""

    waarde: str
    n: int
    mediaan_dagen: int | None
    gemiddelde_dagen: float | None
    pct_binnen_8wk: float | None
    organisatietype: str | None = None


class DoorlooptijdKwartaal(BaseModel):
    kwartaal: date
    n: int
    mediaan_dagen: int | None


class DoorlooptijdMethode(BaseModel):
    """Aantal + mediaan per koppelmethode (altijd over de volledige matview)."""

    methode: str = Field(..., description="'zaaknummer' (exact) of 'adres' (benadering)")
    n: int
    mediaan_dagen: int | None


class DoorlooptijdUitkomst(BaseModel):
    """Aantal + mediaan per uitkomst (altijd over de volledige matview)."""

    uitkomst: str = Field(
        ..., description="verleend | geweigerd | van_rechtswege | ingetrokken"
    )
    n: int
    mediaan_dagen: int | None


class DoorlooptijdType(BaseModel):
    """Aantal + mediaan per planafwijking-type (sluit het afwijk-filter uit)."""

    type: str = Field(..., description="'bopa' (afwijkvergunning) of 'regulier'")
    n: int
    mediaan_dagen: int | None


# Uitkomsten die standaard meetellen (een 'besluit'); ingetrokken is opt-in.
_DLT_UITKOMSTEN = ("verleend", "geweigerd", "van_rechtswege", "ingetrokken")


class DoorlooptijdResponse(BaseModel):
    kpi: DoorlooptijdKpi
    verdeling: list[DoorlooptijdBin]
    per_activiteit: list[DoorlooptijdGroep]
    per_kwartaal: list[DoorlooptijdKwartaal]
    per_bevoegd_gezag: list[DoorlooptijdGroep] = Field(
        ..., description="Alleen BG met >= min_bg dossiers, gesorteerd op aantal"
    )
    per_methode: list[DoorlooptijdMethode] = Field(
        ..., description="Verdeling over koppelmethodes — altijd ongefilterd"
    )
    per_uitkomst: list[DoorlooptijdUitkomst] = Field(
        ..., description="Verdeling over uitkomsten — altijd ongefilterd"
    )
    per_type: list[DoorlooptijdType] = Field(
        ..., description="Verdeling over planafwijking-type (bopa/regulier) — sluit afwijk-filter uit"
    )
    methode: str | None = Field(
        None, description="Actief methode-filter (None = beide trappen samen)"
    )
    uitkomst: list[str] = Field(
        ..., description="Actief uitkomst-filter (leeg = alle uitkomsten)"
    )
    afwijk: str | None = Field(
        None, description="Actief type-filter: 'bopa' | 'regulier' | None (beide)"
    )
    min_bg: int = Field(
        ..., description="Effectieve BG-drempel (kan automatisch verlaagd zijn)"
    )
    min_bg_verlaagd: bool = Field(
        ..., description="True als de drempel automatisch is verlaagd t.o.v. de gevraagde"
    )
    took_ms: int


# ─────────────────────────────────────────────────────────────────────
# Filter-builder
# ─────────────────────────────────────────────────────────────────────


PINS_CAP = 10_000
LIST_MAX_LIMIT = 500

# Bovengrens voor count(*) op filtersets die niet index-only te beantwoorden
# zijn (bbox / tekstzoek). Gelijk aan PINS_CAP zodat de UI één idioom houdt:
# "10.000 van …" op de kaart, "10.000+" in de lijst.
COUNT_CAP = 10_000

# Bovengrens waaronder materialiseren (de OFFSET 0-barrière) nog loont bij een
# niet-tekstueel filter. Veel hoger dan COUNT_CAP omdat de kosten hier anders
# liggen: een bbox-hertoets per rij is spotgoedkoop, dus 40.000 rijen ophalen
# en er een top-N heapsort overheen doen is prima. Bij tekst geldt COUNT_CAP
# als grens, want daar wordt per rij de tsvector over inhoud_tekst herberekend.
BARRIERE_MAX = 150_000

# Lagere pin-cap bij een stadsbrede selectie: die zit boven COUNT_CAP (dus de
# kaart is toch afgekapt en zegt dat ook) én de treffers liggen verspreid over
# de heap. Gemeten prod, Amsterdam-bbox: 10.000 pins = 8,8 s koud / 0,58 s warm.
# Bewust NIET van toepassing op het landelijke kaartbeeld: daar is de selectie
# zó dicht dat 10.000 pins 0,33 s kosten, en juist daar wil je de clusters vol.
PINS_TRUNC_CAP = 4_000

# Lagere pin-cap zodra de zoekterm in meer dan COUNT_CAP records voorkomt.
# Bij zo'n term moet Postgres de datum-index aflopen en per rij de tsvector
# opnieuw uitrekenen over inhoud_tekst; die kosten schalen lineair met de cap.
# Gemeten op prod: 'woning' met cap 10.000 = 16,7 s (koud over de timeout),
# met cap 2.000 = 1,1 s. De kaart is bij zo'n zoekopdracht toch afgetopt en
# meldt dat ook — dan liever 2.000 pins snel dan 10.000 pins niet.
PINS_BREED_CAP = 2_000

# Kolommen waaruit het zoekveld is opgebouwd. Deze expressie moet **letterlijk**
# gelijk zijn aan die van idx_vk_tsv (GIN, 305 MB), anders valt de planner terug
# op een seq scan met to_tsvector() per rij en loopt elke zoekopdracht in de
# statement_timeout. Controleer bij wijziging:
#   SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_vk_tsv';
_TSV_EXPR = (
    "to_tsvector('dutch', coalesce(titel,'') || ' ' || coalesce(beschrijving,'')"
    " || ' ' || coalesce(inhoud_tekst,'') || ' ' || coalesce(straatnaam,'')"
    " || ' ' || coalesce(woonplaats,''))"
)


# Publicatie-id zoals KOOP ze uitgeeft: gmb-2026-173404, prb-2025-1234,
# wsb-2026-20411. Het zoekveld belooft in zijn placeholder dat je hierop kunt
# zoeken, maar koop_id en zaaknummer_bg zitten niet in de tsvector — en met de
# oude ILIKE-implementatie liep zo'n zoekopdracht sowieso in de timeout. Zoekt
# de gebruiker op zo'n code, dan gaan we exact op de PK/btree-index af.
_ID_PATROON = re.compile(r"^[A-Za-z]{2,5}-\d{4}-\d+$")

# Nederlandse postcode, met of zonder spatie, hoofdletter-ongevoelig.
#
# Eigen tak omdat de kolom `postcode` niet in _TSV_EXPR zit en de generieke
# full-text-tak hem juist stukmaakt: _tsquery_arg splitst op niet-alfanumeriek,
# dus "1097 PR" wordt `1097:* & PR:*`, en dat prefix-paar matcht "Professor",
# "procedure", "provincie". Gemeten 2026-08-22 op 899.540 records:
#   "1097PR"  ->   2 treffers (alleen toevallige body-hits)
#   "1097 PR" -> 635 treffers, waarvan er 173 werkelijk in 1097xx liggen
# Eerste treffer was "Kennisgeving WET BODEMBESCHERMING".
_POSTCODE_PATROON = re.compile(r"^(\d{4})\s*([A-Za-z]{2})$")


def _tsquery_arg(q: str) -> str | None:
    """Zet vrije invoer om in een tsquery-string met prefix-match per woord.

    Prefix (`:*`) omdat de viewer voorheen ILIKE '%term%' deed: wie 'Kalver'
    typt verwacht Kalverstraat. websearch_to_tsquery() kan geen prefix, dus
    bouwen we de query zelf — met strikte sanitisatie, want to_tsquery()
    interpreteert &, |, !, ( ) en : als operatoren.

    Geeft None als er na sanitisatie niets bruikbaars overblijft; de caller
    laat het q-filter dan weg in plaats van op alles te matchen.
    """
    woorden = [w for w in re.split(r"[^0-9A-Za-zÀ-ÿ]+", q) if w]
    if not woorden:
        return None
    return " & ".join(f"{w}:*" for w in woorden[:10])


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    try:
        parts = [float(x) for x in bbox.split(",")]
        if len(parts) != 4:
            raise ValueError("expected 4 floats")
        w, s, e, n = parts
        if not (-180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= 90 and -90 <= n <= 90):
            raise ValueError("out of WGS84 range")
        if w >= e or s >= n:
            raise ValueError("west>=east or south>=north")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid bbox '{bbox}': {exc}. Expected 'west,south,east,north' in WGS84.",
        ) from exc
    return w, s, e, n


def _q_filter(q: str | None) -> tuple[str, list[Any], bool] | None:
    """WHERE-clause voor het vrije zoekveld: (sql, params, is_tekstzoek).

    `is_tekstzoek` onderscheidt de twee regimes. False = exacte code-lookup op
    een index (altijd goedkoop, altijd exact te tellen). True = full-text, en
    dan hangt het optimale queryplan af van hoe zeldzaam de term is — zie
    list_vergunningen.
    """
    if not q:
        return None
    s = q.strip()
    if _ID_PATROON.match(s):
        # Exacte code: publicatie-id of zaaknummer. Beide index-backed
        # (vergunningkennisgeving_pkey resp. idx_vk_zaaknummer), dus <10 ms.
        # Full-text zou hier niets vinden: de 'dutch'-parser hakt
        # gmb-2026-173404 in 'gmb', '-2026', '-173404'.
        return ("(koop_id = %s OR zaaknummer_bg = %s)", [s, s], False)
    m = _POSTCODE_PATROON.match(s)
    if m:
        # Twee armen, want de postcode staat op twee plaatsen en zelden op
        # allebei: in de kolom (37,96% gevuld, index idx_vk_postcode) en
        # aaneengeschreven in de body-tekst, waar de dutch-parser er één token
        # van maakt ('Von Guerickestraat 99 1097RA Amsterdam' -> '1097ra').
        # Vandaar de tsquery zónder :* — prefix-matching levert hier alleen
        # ruis op en de glued vorm is al exact wat er in de index staat.
        #
        # is_tekstzoek=True: de tweede arm is een GIN-scan, dus de count moet
        # afgetopt en de barrière-heuristiek moet gelden, net als bij tekst.
        pc = f"{m.group(1)}{m.group(2).upper()}"
        return (
            f"(postcode = %s OR {_TSV_EXPR} @@ to_tsquery('dutch', %s))",
            [pc, pc.lower()],
            True,
        )
    # Full-text via idx_vk_tsv. Was tot 2026-08 vijf maal ILIKE '%term%', wat
    # altijd een seq scan over 5,8 GB opleverde: elke zoekopdracht gaf 500 na
    # de statement_timeout van 20 s. Geen FTS-rank — pure filter, de sortering
    # blijft op datum.
    tsq = _tsquery_arg(q)
    if not tsq:
        return None
    return (f"{_TSV_EXPR} @@ to_tsquery('dutch', %s)", [tsq], True)


def _build_filters(
    q: str | None,
    tb: list[str],
    ac: list[str],
    bg: list[str],
    org: list[str],
    th: list[str],
    vanaf: date | None,
    totd: date | None,
    geom: bool,
    ontv: bool,
    zaak: str | None,
    bbox: str | None,
    afwijk: str | None = None,
) -> tuple[list[str], list[Any]]:
    """Return (where-clauses, params) to be combined with AND."""
    clauses: list[str] = []
    params: list[Any] = []

    qf = _q_filter(q)
    if qf:
        clauses.append(qf[0])
        params.extend(qf[1])

    if tb:
        clauses.append("type_besluit = ANY(%s)")
        params.append(tb)
    if ac:
        clauses.append("activiteit_code = ANY(%s)")
        params.append(ac)
    if bg:
        clauses.append("bg_naam = ANY(%s)")
        params.append(bg)
    if org:
        clauses.append("organisatietype = ANY(%s)")
        params.append(org)
    if th:
        clauses.append("subject_taxonomie = ANY(%s)")
        params.append(th)
    if vanaf:
        clauses.append("datum_publicatie >= %s")
        params.append(vanaf)
    if totd:
        clauses.append("datum_publicatie <= %s")
        params.append(totd)
    if geom:
        clauses.append("geometrie_wgs_pt IS NOT NULL")
    if ontv:
        clauses.append("datum_ontvangst IS NOT NULL")
    if zaak:
        clauses.append("zaaknummer_bg ILIKE %s")
        params.append(f"%{zaak}%")
    if afwijk == "bopa":
        clauses.append("afwijk_status = 'buitenplans_expliciet'")
    elif afwijk == "regulier":
        clauses.append("afwijk_status IS DISTINCT FROM 'buitenplans_expliciet'")

    parsed_bbox = _parse_bbox(bbox)
    if parsed_bbox:
        w, s, e, n = parsed_bbox
        clauses.append(
            "geometrie_wgs_pt && ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
        )
        params.extend([w, s, e, n])

    return clauses, params


def _where_sql(clauses: list[str]) -> str:
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def _needs_capped_count(q: str | None, bbox: str | None) -> bool:
    """Is een exacte count(*) op deze filterset te duur?

    Zonder bbox of tekstfilter kan Postgres de count index-only beantwoorden
    (idx_vk_geom_notnull_facet dekt alle facetkolommen) — 0,06 s over 883k rijen.

    Mét een bbox kan dat niet: `geometrie_wgs_pt && envelope` moet via de
    GiST-index en die vraagt altijd een heap-recheck. Gemeten op productie
    (2026-08-03): landelijke bbox 39,3 s (parallel seq scan, 160.042 blocks),
    stedelijke bbox 8,8-16,0 s (bitmap heap scan, ~1 treffer per block). Beide
    over de statement_timeout van 20 s, dus 500 — en juist dát is de call die
    de viewer bij elke koude start doet, omdat 'binnen kaartbeeld' default aan
    staat. Een tekstfilter heeft hetzelfde probleem: 'dakkapel' (60.596
    treffers) kostte 26,0 s.

    Een ander access-pad forceren helpt niet: in een zuivere A/B op verse
    bboxen was een index scan koud 3,5 s tegen 6,1 s voor de bitmap — dezelfde
    orde van grootte, want de kosten zitten in het aantal heap-blocks, niet in
    het plan. Daarom begrenzen we het wérk in plaats van het te versnellen.

    Let op twee randgevallen die géén cap nodig hebben: invoer van enkel
    spaties/leestekens levert na sanitisatie geen clause op, en een exacte
    publicatie-id/zaaknummer gaat via een index-equality met hooguit een
    handvol rijen.
    """
    if bbox:
        return True
    qf = _q_filter(q)
    return bool(qf and qf[2])


def _count_sql(where: str, capped: bool) -> str:
    """count(*), eventueel afgetopt op COUNT_CAP.

    De LIMIT in de subquery laat Postgres stoppen zodra er COUNT_CAP+1 rijen
    gevonden zijn. Op de landelijke bbox — het default kaartbeeld — brengt dat
    de count van 39,3 s naar 0,07 s terug.
    """
    if not capped:
        return f"SELECT count(*) AS n FROM vth.vergunningkennisgeving {where}"
    return (
        "SELECT count(*) AS n FROM ("
        f"SELECT 1 FROM vth.vergunningkennisgeving {where} LIMIT {COUNT_CAP + 1}"
        ") s"
    )


def _tel(cur, where: str, params: list[Any], capped: bool) -> tuple[int, bool, int]:
    """(totaal, is-afgetopt, planner-schatting) — exact waar dat goedkoop kan.

    Op een filterset die niet index-only te beantwoorden is (bbox of tekst)
    vragen we eerst de planner-schatting. Ligt die boven COUNT_CAP, dan slaan
    we het tellen helemaal over: het antwoord wordt toch "10.000+", en dan is
    élke seconde die we eraan besteden weggegooid. De schatting gaat mee terug
    omdat de caller hem ook voor de plankeuze gebruikt.
    """
    if not capped:
        cur.execute(_count_sql(where, False), params)
        return cur.fetchone()["n"], False, -1
    est = _geschat_aantal(cur, where, params)
    if est > COUNT_CAP:
        return COUNT_CAP, True, est
    cur.execute(_count_sql(where, True), params)
    n = cur.fetchone()["n"]
    return (COUNT_CAP, True, est) if n > COUNT_CAP else (n, False, est)


def _gebruik_barriere(cur, capped: bool, est: int, qf) -> bool:
    """Moet de query de OFFSET 0-barrière gebruiken? Zie list_vergunningen.

    Twee omslagpunten, want de twee filtersoorten hebben een heel andere
    hertoets-prijs per rij:

    - **tekst** — zonder barrière herberekent Postgres per rij `to_tsvector`
      over `inhoud_tekst`. Bij een brede term is dat prima (hij is snel klaar),
      bij een zeldzame term rampzalig. Grens: COUNT_CAP. Gemeten: 'zonnepark'
      (745 treffers) 45 s+ zonder barrière tegen 0,01 s met; 'dakkapel'
      (60k) juist 0,41 s zonder tegen 7,08 s mét.
    - **bbox** — de hertoets is een goedkope geometrie-vergelijking, dus
      materialiseren loont veel langer door. Grens: BARRIERE_MAX. Gemeten:
      wijk-bbox (499) 21 s zonder barrière tegen 0,02 s met; Amsterdam-bbox
      (38.624) 9,5 s zonder tegen 8,4 s mét; landelijke bbox (~660k) 0,33 s
      zonder — mét zou hij honderdduizenden rijen materialiseren.

    Staat er een tekstfilter, dan telt de selectiviteit van de tékst, niet die
    van de hele filterset: een zeldzame term binnen een landelijke bbox heeft
    de barrière nog steeds nodig.
    """
    if not capped:
        return False
    if qf and qf[2]:
        return _geschat_aantal(cur, _where_sql([qf[0]]), qf[1]) <= COUNT_CAP
    return est <= BARRIERE_MAX


def _geschat_aantal(cur, where: str, params: list[Any]) -> int:
    """Geschat aantal treffers uit het queryplan — voert de query niet uit.

    Waarom niet gewoon tellen: bij een tekstfilter helpt de LIMIT in de
    afgetopte count niets. Een Bitmap Index Scan bouwt eerst de vólledige
    bitmap over de GIN-index voordat er ook maar één rij uitkomt, dus een
    brede term als 'woning' kost onverkort tientallen seconden — met of
    zonder cap. Hetzelfde geldt voor een dichte stedelijke bbox (gemeten:
    afgetopte count 6,97 s).

    EXPLAIN raakt de tabel niet en kost ~15-30 ms. De schatting wordt nooit
    aan de gebruiker getoond; hij beslist alleen (a) of een exacte count nog
    de moeite is en (b) welk queryplan de lijst moet gebruiken. Zit de
    schatting er een factor naast, dan is het gevolg hooguit dat we net te
    vroeg of net te laat aftoppen.
    """
    cur.execute(
        "EXPLAIN (FORMAT JSON) SELECT 1 FROM vth.vergunningkennisgeving " + where,
        params,
    )
    plan = list(cur.fetchone().values())[0]
    return int(plan[0]["Plan"]["Plan Rows"])


_SORT_SQL: dict[str, str] = {
    "datum": "datum_publicatie DESC, koop_id DESC",
    "datum_asc": "datum_publicatie ASC, koop_id ASC",
    "ontvangst": "datum_ontvangst DESC NULLS LAST, koop_id DESC",
    "bg": "bg_naam ASC, datum_publicatie DESC",
}


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


_LIST_COLS = """
    koop_id, publicatieblad, bg_naam, organisatietype, titel,
    datum_publicatie, datum_publicatie_ts, datum_ontvangst,
    activiteit_code, type_besluit, subject_taxonomie, geometrie_type,
    ST_X(geometrie_wgs_pt) AS lon, ST_Y(geometrie_wgs_pt) AS lat,
    straatnaam, huisnummer, postcode, woonplaats,
    zaaknummer_bg, preferred_url, pdf_url, afwijk_status
"""


@router.get("", response_model=ListResponse, summary="Gefilterde lijst")
def list_vergunningen(
    q: str | None = Query(None, description="ILIKE op titel/beschrijving/inhoud_tekst/adres"),
    tb: list[str] = Query(default=[], description="type_besluit (repeatable)"),
    ac: list[str] = Query(default=[], description="activiteit_code (repeatable)"),
    bg: list[str] = Query(default=[], description="bg_naam (repeatable)"),
    org: list[str] = Query(default=[], description="organisatietype (repeatable)"),
    th: list[str] = Query(default=[], description="subject_taxonomie (repeatable)"),
    vanaf: date | None = Query(None, description="datum_publicatie >="),
    totd: date | None = Query(None, description="datum_publicatie <="),
    geom: bool = Query(False, description="Alleen records met geometrie"),
    ontv: bool = Query(False, description="Alleen records met datum_ontvangst"),
    zaak: str | None = Query(None, description="ILIKE op zaaknummer_bg"),
    bbox: str | None = Query(None, description="west,south,east,north in WGS84"),
    afwijk: Literal["bopa", "regulier"] | None = Query(
        None, description="bopa = afwijkvergunning (BOPA); regulier = niet-bevestigde BOPA"
    ),
    sort: Literal["datum", "datum_asc", "ontvangst", "bg"] = Query("datum"),
    limit: int = Query(50, ge=1, le=LIST_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    t0 = time.perf_counter()
    clauses, params = _build_filters(q, tb, ac, bg, org, th, vanaf, totd, geom, ontv, zaak, bbox, afwijk)
    where = _where_sql(clauses)
    order = _SORT_SQL[sort]
    capped = _needs_capped_count(q, bbox)

    # Twee plannen voor dezelfde query; welk van de twee wint hangt af van hoe
    # sélectief de filterset is. Dit geldt voor élk filter dat de planner via
    # een aparte index moet oplossen — een tsvector-term net zo goed als een
    # bbox:
    #
    #   zonder barrière — de planner loopt idx_vk_datum aflopend af en toetst
    #     per rij het filter opnieuw tot hij `limit` treffers heeft. Optimaal
    #     bij een ruime selectie ('woning' zit in ~1 op de 5 records, een
    #     landelijke bbox in vrijwel alle: klaar na een paar honderd rijen).
    #     Rampzalig bij een smalle selectie — 'zonnepark' (741 van 883k) kostte
    #     zo 45 s+, en een wijk-bbox (499 van 883k) 21 s.
    #
    #   met barrière (OFFSET 0 blokkeert het samenvouwen van de subquery) —
    #     eerst de GIN-/GiST-scan, dan pas sorteren. Optimaal bij een smalle
    #     selectie (0,02 s), maar bij een ruime materialiseert hij
    #     honderdduizenden rijen vóór het sorteren -> 500 na de timeout.
    #
    # De count vertelt ons gratis in welk regime we zitten: hebben we exact
    # kunnen tellen ónder COUNT_CAP, dan is de selectie klein genoeg om te
    # materialiseren. Is de count afgetopt, dan is hij dat niet.
    def _list_sql(met_barriere: bool) -> str:
        if met_barriere:
            return (
                f"SELECT * FROM (SELECT {_LIST_COLS} FROM vth.vergunningkennisgeving "
                f"{where} OFFSET 0) s ORDER BY {order} LIMIT %s OFFSET %s"
            )
        return (
            f"SELECT {_LIST_COLS} FROM vth.vergunningkennisgeving "
            f"{where} ORDER BY {order} LIMIT %s OFFSET %s"
        )

    with get_conn() as conn, conn.cursor() as cur:
        total, total_capped, est = _tel(cur, where, params, capped)
        cur.execute(
            _list_sql(_gebruik_barriere(cur, capped, est, _q_filter(q))),
            params + [limit, offset],
        )
        records = [VergunningSummary(**dict(r)) for r in cur.fetchall()]
    took = int((time.perf_counter() - t0) * 1000)
    return ListResponse(
        records=records,
        total=total,
        total_capped=total_capped,
        limit=limit,
        offset=offset,
        took_ms=took,
    )


@router.get(
    "/pins",
    response_model=PinsResponse,
    summary="Lichtgewicht pin-only voor kaart (gecapt)",
)
def list_pins(
    q: str | None = Query(None),
    tb: list[str] = Query(default=[]),
    ac: list[str] = Query(default=[]),
    bg: list[str] = Query(default=[]),
    org: list[str] = Query(default=[]),
    th: list[str] = Query(default=[]),
    vanaf: date | None = Query(None),
    totd: date | None = Query(None),
    geom: bool = Query(True, description="Default true — pins zonder geo zinloos"),
    ontv: bool = Query(False),
    zaak: str | None = Query(None),
    bbox: str | None = Query(None),
    afwijk: Literal["bopa", "regulier"] | None = Query(None),
    cap: int = Query(PINS_CAP, ge=100, le=50_000),
):
    t0 = time.perf_counter()
    # Pins-endpoint heeft 'geom' default-true: forceer NOT NULL ongeacht user-input.
    clauses, params = _build_filters(
        q, tb, ac, bg, org, th, vanaf, totd, True, ontv, zaak, bbox, afwijk
    )
    where = _where_sql(clauses)
    capped = _needs_capped_count(q, bbox)
    qf = _q_filter(q)
    tekstzoek = bool(qf and qf[2])
    _pin_cols = (
        "koop_id AS id, type_besluit AS tb, "
        "ST_X(geometrie_wgs_pt) AS lon, ST_Y(geometrie_wgs_pt) AS lat"
    )

    def _pins_sql(met_barriere: bool) -> str:
        # Zelfde afweging als in list_vergunningen; zie de toelichting daar.
        if met_barriere:
            return (
                f"SELECT id, tb, lon, lat FROM ("
                f"SELECT {_pin_cols}, datum_publicatie "
                f"FROM vth.vergunningkennisgeving {where} OFFSET 0) s "
                f"ORDER BY datum_publicatie DESC, id DESC LIMIT %s"
            )
        return (
            f"SELECT {_pin_cols} "
            f"FROM vth.vergunningkennisgeving {where} "
            f"ORDER BY datum_publicatie DESC, koop_id DESC LIMIT %s"
        )

    with get_conn() as conn, conn.cursor() as cur:
        total, total_capped, est = _tel(cur, where, params, capped)
        barriere = _gebruik_barriere(cur, capped, est, qf)
        if total_capped:
            if barriere:
                # Verspreide selectie boven COUNT_CAP = stadszoom. Afgekapt is
                # hij toch; minder pins scheelt hier evenredig veel heap-reads.
                cap = min(cap, PINS_TRUNC_CAP)
            if tekstzoek and not barriere:
                cap = min(cap, PINS_BREED_CAP)
        cur.execute(_pins_sql(barriere), params + [cap])
        pins = [Pin(**dict(r)) for r in cur.fetchall()]
    took = int((time.perf_counter() - t0) * 1000)
    return PinsResponse(
        pins=pins,
        total_matching=total,
        total_capped=total_capped,
        returned=len(pins),
        # `total > len(pins)` alleen is niet genoeg sinds de count afgetopt kan
        # zijn: dan ís total gelijk aan COUNT_CAP en zou een kaart met precies
        # zoveel pins zichzelf als compleet melden. Is de count afgetopt, dan
        # zijn er per definitie méér treffers dan we tonen.
        truncated=total_capped or total > len(pins),
        cap=cap,
        took_ms=took,
    )


@router.get(
    "/facets",
    response_model=FacetsResponse,
    summary="Filter-counters voor de huidige filterset",
)
def list_facets(
    q: str | None = Query(None),
    tb: list[str] = Query(default=[]),
    ac: list[str] = Query(default=[]),
    bg: list[str] = Query(default=[]),
    org: list[str] = Query(default=[]),
    th: list[str] = Query(default=[]),
    vanaf: date | None = Query(None),
    totd: date | None = Query(None),
    geom: bool = Query(False),
    ontv: bool = Query(False),
    zaak: str | None = Query(None),
    bbox: str | None = Query(None),
    afwijk: Literal["bopa", "regulier"] | None = Query(None),
):
    """Geeft counts per filter-waarde **met alle filters toegepast**.

    Bewuste keuze v1: een gefilterde waarde verdwijnt uit de facet zodra
    hij niet meer matcht. Een geavanceerder model (counts per dim
    excl. die dim zelf) is mogelijk maar duurder en niet nodig voor de
    eerste viewer-iteratie.
    """
    t0 = time.perf_counter()
    clauses, params = _build_filters(q, tb, ac, bg, org, th, vanaf, totd, geom, ontv, zaak, bbox, afwijk)
    where = _where_sql(clauses)

    def _bucket_sql(col: str, top: int | None = None) -> str:
        limit_clause = f"LIMIT {top}" if top else ""
        # WHERE-clause includes the user's filter clauses plus "<col> IS NOT NULL".
        # If `where` is empty (no user filters), start with WHERE; otherwise append AND.
        not_null = f"{col} IS NOT NULL"
        where_clause = (
            f"{where} AND {not_null}" if where else f"WHERE {not_null}"
        )
        return (
            f"SELECT {col} AS value, count(*) AS count "
            f"FROM vth.vergunningkennisgeving {where_clause} "
            f"GROUP BY 1 ORDER BY 2 DESC {limit_clause}"
        )

    result: dict[str, list[FacetBucket]] = {}
    with get_conn() as conn, conn.cursor() as cur:
        for field, top in [
            ("type_besluit", None),
            ("activiteit_code", None),
            ("organisatietype", None),
            ("bg_naam", 100),
            ("subject_taxonomie", None),
            ("publicatieblad", None),
        ]:
            cur.execute(_bucket_sql(field, top), params)
            result[field] = [FacetBucket(**dict(r)) for r in cur.fetchall()]
    took = int((time.perf_counter() - t0) * 1000)
    return FacetsResponse(**result, took_ms=took)


_STATS_MATVIEW_PRESENT = False  # positief resultaat cachen; negatief blijven checken


def _has_stats_matview(cur) -> bool:
    """Bestaan de stats-matviews? Eén catalog-lookup, geen tabel-toegang.

    Positief antwoord wordt gecached (matviews verdwijnen niet), negatief niet
    — zo pikt een dev-DB de migratie op zonder herstart van de API.
    """
    global _STATS_MATVIEW_PRESENT
    if _STATS_MATVIEW_PRESENT:
        return True
    cur.execute(
        "SELECT to_regclass('vth.vergunning_stats') IS NOT NULL"
        "   AND to_regclass('vth.vergunning_stats_type_besluit') IS NOT NULL AS ok"
    )
    row = cur.fetchone()
    _STATS_MATVIEW_PRESENT = bool(row and row["ok"])
    return _STATS_MATVIEW_PRESENT


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Totalen voor het register (header / lege-staat)",
)
def stats():
    """Header-totalen.

    Leest uit de matviews vth.vergunning_stats(_type_besluit) — de live
    aggregatie is een seq scan over 1,35 GB heap (geen index op
    datum_publicatie_ts / ingest_at / inhoud_tekst, dus niet index-only te
    maken) en kostte op prod 23-46 s: over de statement_timeout van 20 s,
    dus 500. Zie scripts/2026-07-add-vergunning-stats-matview.sql; verversen
    gebeurt in refresh-koop-to-prod.ps1 -Refresh.

    Ontbreekt de matview (dev-DB zonder migratie), dan valt hij terug op de
    live aggregatie — daar is de tabel klein genoeg.
    """
    t0 = time.perf_counter()
    with get_conn() as conn, conn.cursor() as cur:
        if _has_stats_matview(cur):
            cur.execute("""
                SELECT total, last_publicatie, last_ingest, enriched, refreshed_at
                FROM vth.vergunning_stats
            """)
            r = cur.fetchone() or {}
            cur.execute("""
                SELECT value, count
                FROM vth.vergunning_stats_type_besluit
                ORDER BY count DESC
            """)
        else:
            cur.execute("""
                SELECT count(*) AS total,
                       max(datum_publicatie_ts) AS last_publicatie,
                       max(ingest_at) AS last_ingest,
                       count(*) FILTER (WHERE inhoud_tekst IS NOT NULL) AS enriched,
                       NULL::timestamptz AS refreshed_at
                FROM vth.vergunningkennisgeving
            """)
            r = cur.fetchone()
            cur.execute("""
                SELECT type_besluit AS value, count(*) AS count
                FROM vth.vergunningkennisgeving
                WHERE type_besluit IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC
            """)
        per_tb = [FacetBucket(**dict(b)) for b in cur.fetchall()]
    total = r["total"] or 0
    enriched = r["enriched"] or 0
    took = int((time.perf_counter() - t0) * 1000)
    return StatsResponse(
        total=total,
        last_publicatie=r["last_publicatie"],
        last_ingest=r["last_ingest"],
        enriched=enriched,
        enriched_pct=round(enriched / total * 100, 2) if total else 0.0,
        per_type_besluit=per_tb,
        stats_refreshed_at=r["refreshed_at"],
        took_ms=took,
    )


_DLT_BIN_DAGEN = 14  # binbreedte histogram
_DLT_MAX_BIN = 26    # 26 * 14 = 364; bin 26 = open bovengrens (>= 364 dagen)
_DLT_REGULIER = 56   # 8 weken reguliere termijn
_DLT_HALF_JAAR = 182


def _dlt_stat_select(src: str, group_col: str | None = None) -> str:
    """SELECT-fragment met de standaard doorlooptijd-statistieken.

    src = de FROM-bron (tabel of gefilterde subquery met alias).
    group_col=None → één globale rij; anders één rij per groepswaarde.
    """
    prefix = f"{group_col} AS waarde, " if group_col else ""
    return (
        f"SELECT {prefix}"
        "  count(*) AS n, "
        "  percentile_disc(0.5) WITHIN GROUP (ORDER BY doorlooptijd_dagen) AS mediaan, "
        "  round(avg(doorlooptijd_dagen), 1) AS gemiddelde, "
        f"  round(100.0 * count(*) FILTER (WHERE doorlooptijd_dagen <= {_DLT_REGULIER}) "
        "        / count(*), 1) AS pct_binnen_8wk "
        f"FROM {src} "
    )


@router.get(
    "/doorlooptijd",
    response_model=DoorlooptijdResponse,
    summary="Geaggregeerde doorlooptijd-statistiek (matview vth.dossier_doorlooptijd)",
)
def doorlooptijd(
    min_bg: int = Query(
        30,
        ge=1,
        le=1000,
        description="Minimum aantal dossiers per BG om in per_bevoegd_gezag te verschijnen",
    ),
    methode: Literal["zaaknummer", "adres"] | None = Query(
        None,
        description=(
            "Filter op koppelmethode: 'zaaknummer' (exact) of 'adres' (benadering). "
            "Leeg = beide trappen samen. per_methode blijft altijd ongefilterd."
        ),
    ),
    uitkomst: list[str] = Query(
        default=[],
        description=(
            "Filter op uitkomst (repeatable): verleend | geweigerd | van_rechtswege | "
            "ingetrokken. Leeg = alle uitkomsten. per_uitkomst blijft altijd ongefilterd."
        ),
    ),
    afwijk: Literal["bopa", "regulier"] | None = Query(
        None,
        description=(
            "Filter op planafwijking-type: 'bopa' (afwijkvergunning / buitenplanse "
            "omgevingsplanactiviteit) of 'regulier'. Leeg = beide. per_type blijft "
            "altijd ongefilterd. NB tekst-signaal (G-84): bopa is een ondergrens."
        ),
    ),
):
    """Voedt het doorlooptijd-dashboard.

    Leest uit de matview ``vth.dossier_doorlooptijd``. Een dossier koppelt een
    aanvraag- aan een verlening-kennisgeving via twee trappen (kolom
    ``match_methode``): ``zaaknummer`` (exact) of ``adres`` (benadering op
    gelijk bg+adres+activiteit, verlening ≤365d na aanvraag). Eén call levert
    KPI's, histogram, per-activiteit, kwartaal-trend, BG-benchmark én de
    verdeling over de koppelmethodes. Geschikt voor een nightly JSON-export.

    Let op: kennisgevings-doorlooptijd, geen officiele beslistermijn; en
    selectiebias richting consistent publicerende BG — zie de matview-comment
    en de methodologie-noot in de vault-analyse.
    """
    t0 = time.perf_counter()
    # Valideer uitkomst-waarden (onbekende negeren we niet stil — 400).
    bad = [u for u in uitkomst if u not in _DLT_UITKOMSTEN]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"Onbekende uitkomst(en): {bad}. Toegestaan: {list(_DLT_UITKOMSTEN)}.",
        )

    # Basisbron-helper: bouwt een (FROM-bron, params) uit actieve filters.
    m_filt = ("match_methode = %s", [methode]) if methode else None
    u_filt = ("uitkomst = ANY(%s)", [uitkomst]) if uitkomst else None
    a_filt = ("is_afwijk = %s", [afwijk == "bopa"]) if afwijk else None

    def _src(*filters: tuple[str, list[Any]] | None) -> tuple[str, list[Any]]:
        active = [f for f in filters if f]
        if not active:
            return "vth.dossier_doorlooptijd", []
        where = " AND ".join(c for c, _ in active)
        params = [p for _, plist in active for p in plist]
        return f"(SELECT * FROM vth.dossier_doorlooptijd WHERE {where}) d", params

    # Hoofdbron = alle filters. Facetten sluiten hun eigen dimensie uit zodat
    # per_methode/per_uitkomst/per_type optellen tot het totaal van de huidige selectie.
    src, pre = _src(m_filt, u_filt, a_filt)
    meth_src, meth_pre = _src(u_filt, a_filt)  # per_methode: geen methode-filter
    uitk_src, uitk_pre = _src(m_filt, a_filt)  # per_uitkomst: geen uitkomst-filter
    type_src, type_pre = _src(m_filt, u_filt)  # per_type: geen afwijk-filter

    with get_conn() as conn, conn.cursor() as cur:
        # KPI's
        cur.execute(
            "SELECT count(*) AS n, "
            "  count(DISTINCT (bg_naam, organisatietype)) AS n_bg, "
            "  percentile_disc(0.5)  WITHIN GROUP (ORDER BY doorlooptijd_dagen) AS mediaan, "
            "  round(avg(doorlooptijd_dagen), 1) AS gemiddelde, "
            "  percentile_disc(0.25) WITHIN GROUP (ORDER BY doorlooptijd_dagen) AS p25, "
            "  percentile_disc(0.75) WITHIN GROUP (ORDER BY doorlooptijd_dagen) AS p75, "
            f"  round(100.0 * count(*) FILTER (WHERE doorlooptijd_dagen <= {_DLT_REGULIER}) "
            "        / nullif(count(*), 0), 1) AS pct_binnen_8wk, "
            f"  round(100.0 * count(*) FILTER (WHERE doorlooptijd_dagen > {_DLT_HALF_JAAR}) "
            "        / nullif(count(*), 0), 1) AS pct_half_jaar "
            f"FROM {src}",
            pre,
        )
        k = cur.fetchone()
        kpi = DoorlooptijdKpi(
            n_dossiers=k["n"],
            n_bevoegd_gezag=k["n_bg"],
            mediaan_dagen=k["mediaan"],
            gemiddelde_dagen=float(k["gemiddelde"]) if k["gemiddelde"] is not None else None,
            p25_dagen=k["p25"],
            p75_dagen=k["p75"],
            pct_binnen_8wk=float(k["pct_binnen_8wk"]) if k["pct_binnen_8wk"] is not None else None,
            pct_boven_half_jaar=float(k["pct_half_jaar"]) if k["pct_half_jaar"] is not None else None,
        )

        # Verdeling-histogram (vul ontbrekende bins met 0 → continue x-as)
        cur.execute(
            f"SELECT least(doorlooptijd_dagen / {_DLT_BIN_DAGEN}, {_DLT_MAX_BIN}) AS bin, "
            "       count(*) AS c "
            f"FROM {src} GROUP BY 1 ORDER BY 1",
            pre,
        )
        counts = {r["bin"]: r["c"] for r in cur.fetchall()}
        verdeling = [
            DoorlooptijdBin(
                ondergrens_dagen=b * _DLT_BIN_DAGEN,
                bovengrens_dagen=None if b == _DLT_MAX_BIN else (b + 1) * _DLT_BIN_DAGEN - 1,
                count=counts.get(b, 0),
            )
            for b in range(_DLT_MAX_BIN + 1)
        ]

        # Per activiteit
        cur.execute(
            _dlt_stat_select(src, "activiteit_code")
            + "WHERE activiteit_code IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
            pre,
        )
        per_activiteit = [
            DoorlooptijdGroep(
                waarde=r["waarde"],
                n=r["n"],
                mediaan_dagen=r["mediaan"],
                gemiddelde_dagen=float(r["gemiddelde"]) if r["gemiddelde"] is not None else None,
                pct_binnen_8wk=float(r["pct_binnen_8wk"]) if r["pct_binnen_8wk"] is not None else None,
            )
            for r in cur.fetchall()
        ]

        # Kwartaal-trend
        cur.execute(
            "SELECT kwartaal, count(*) AS n, "
            "  percentile_disc(0.5) WITHIN GROUP (ORDER BY doorlooptijd_dagen) AS mediaan "
            f"FROM {src} GROUP BY 1 ORDER BY 1",
            pre,
        )
        per_kwartaal = [
            DoorlooptijdKwartaal(kwartaal=r["kwartaal"], n=r["n"], mediaan_dagen=r["mediaan"])
            for r in cur.fetchall()
        ]

        # Per bevoegd gezag. Groep op (bg_naam, organisatietype) zodat
        # gelijknamige gemeente/provincie (Utrecht, Groningen) niet
        # samenklonteren. De drempel wordt AUTOMATISCH verlaagd wanneer hij bij
        # een kleine selectie niets oplevert (30 → 10 → 5 → 1), zodat de tabel
        # niet leeg valt en bij een kleine selectie élk BG (tot n=1) zichtbaar
        # is. Alleen omlaag, nooit omhoog t.o.v. de gevraagde min_bg.
        bg_sql = (
            "SELECT bg_naam AS waarde, "
            "  organisatietype, "
            "  count(*) AS n, "
            "  percentile_disc(0.5) WITHIN GROUP (ORDER BY doorlooptijd_dagen) AS mediaan, "
            "  round(avg(doorlooptijd_dagen), 1) AS gemiddelde, "
            f"  round(100.0 * count(*) FILTER (WHERE doorlooptijd_dagen <= {_DLT_REGULIER}) "
            "        / count(*), 1) AS pct_binnen_8wk "
            f"FROM {src} "
            "GROUP BY bg_naam, organisatietype HAVING count(*) >= %s ORDER BY n DESC"
        )
        ladder = sorted({min_bg, 10, 5, 1} & set(range(0, min_bg + 1)), reverse=True)
        bg_rows: list[dict[str, Any]] = []
        min_bg_eff = ladder[-1] if ladder else min_bg
        for drempel in ladder:
            cur.execute(bg_sql, pre + [drempel])
            bg_rows = cur.fetchall()
            min_bg_eff = drempel
            if bg_rows:
                break
        per_bg = [
            DoorlooptijdGroep(
                waarde=r["waarde"],
                n=r["n"],
                mediaan_dagen=r["mediaan"],
                gemiddelde_dagen=float(r["gemiddelde"]) if r["gemiddelde"] is not None else None,
                pct_binnen_8wk=float(r["pct_binnen_8wk"]) if r["pct_binnen_8wk"] is not None else None,
                organisatietype=r["organisatietype"],
            )
            for r in bg_rows
        ]

        # Verdeling over koppelmethodes (sluit het methode-filter uit, respecteert
        # wel het uitkomst-filter → telt op tot het totaal van de selectie).
        cur.execute(
            "SELECT match_methode AS methode, count(*) AS n, "
            "  percentile_disc(0.5) WITHIN GROUP (ORDER BY doorlooptijd_dagen) AS mediaan "
            f"FROM {meth_src} GROUP BY 1 ORDER BY 2 DESC",
            meth_pre,
        )
        per_methode = [
            DoorlooptijdMethode(methode=r["methode"], n=r["n"], mediaan_dagen=r["mediaan"])
            for r in cur.fetchall()
        ]

        # Verdeling over uitkomsten (sluit het uitkomst-filter uit → stabiele
        # chip-totalen; respecteert wel een eventueel methode-filter).
        cur.execute(
            "SELECT uitkomst, count(*) AS n, "
            "  percentile_disc(0.5) WITHIN GROUP (ORDER BY doorlooptijd_dagen) AS mediaan "
            f"FROM {uitk_src} GROUP BY 1 ORDER BY 2 DESC",
            uitk_pre,
        )
        per_uitkomst = [
            DoorlooptijdUitkomst(uitkomst=r["uitkomst"], n=r["n"], mediaan_dagen=r["mediaan"])
            for r in cur.fetchall()
        ]

        # Verdeling over planafwijking-type (sluit het afwijk-filter uit → stabiele
        # bopa/regulier-totalen; respecteert wel methode- en uitkomst-filter).
        cur.execute(
            "SELECT is_afwijk, count(*) AS n, "
            "  percentile_disc(0.5) WITHIN GROUP (ORDER BY doorlooptijd_dagen) AS mediaan "
            f"FROM {type_src} GROUP BY 1 ORDER BY 1 DESC",
            type_pre,
        )
        per_type = [
            DoorlooptijdType(
                type="bopa" if r["is_afwijk"] else "regulier",
                n=r["n"],
                mediaan_dagen=r["mediaan"],
            )
            for r in cur.fetchall()
        ]

    took = int((time.perf_counter() - t0) * 1000)
    return DoorlooptijdResponse(
        kpi=kpi,
        verdeling=verdeling,
        per_activiteit=per_activiteit,
        per_kwartaal=per_kwartaal,
        per_bevoegd_gezag=per_bg,
        per_methode=per_methode,
        per_uitkomst=per_uitkomst,
        per_type=per_type,
        methode=methode,
        uitkomst=uitkomst,
        afwijk=afwijk,
        min_bg=min_bg_eff,
        min_bg_verlaagd=min_bg_eff < min_bg,
        took_ms=took,
    )


@router.get(
    "/{koop_id}",
    response_model=VergunningDetail,
    summary="Volledige record-details + geometrie als GeoJSON",
)
def get_detail(koop_id: str = Path(..., min_length=4, max_length=64)):
    detail_cols = (
        _LIST_COLS
        + ", bg_scheme, jaargang, publicatienummer, huisletter, huisnummertoevoeging, "
        " ligt_in_gemeente, geometrielabel, beschrijving, inhoud_tekst, "
        " xml_url, dl.inzage_url AS bg_deeplink_url, "
        " CASE WHEN geometrie_rd IS NOT NULL "
        "   THEN ST_AsGeoJSON(ST_Transform(geometrie_rd, 4326))::json "
        "   ELSE NULL END AS geometrie_geojson"
    )
    # LEFT JOIN LATERAL ... LIMIT 1: er kunnen meerdere werkende deeplinks per
    # koop_id zijn (gem. 1,3); pak de meest recent gevonden om rij-duplicatie
    # in deze single-row detail-query te voorkomen.
    sql = (
        f"SELECT {detail_cols} FROM vth.vergunningkennisgeving vk "
        f"LEFT JOIN LATERAL ("
        f"  SELECT inzage_url FROM vth.vergunning_deeplink "
        f"  WHERE koop_id = vk.koop_id AND werkt = TRUE "
        f"  ORDER BY gevonden_at DESC LIMIT 1"
        f") dl ON TRUE "
        f"WHERE vk.koop_id = %s"
    )
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, [koop_id])
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Geen vergunning met koop_id={koop_id!r}")
    return VergunningDetail(**dict(row))
