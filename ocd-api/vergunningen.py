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
    q       full-text (ILIKE op titel + beschrijving + inhoud_tekst)
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
"""

from __future__ import annotations

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
    took_ms: int


# ─────────────────────────────────────────────────────────────────────
# Filter-builder
# ─────────────────────────────────────────────────────────────────────


PINS_CAP = 10_000
LIST_MAX_LIMIT = 500


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
) -> tuple[list[str], list[Any]]:
    """Return (where-clauses, params) to be combined with AND."""
    clauses: list[str] = []
    params: list[Any] = []

    if q:
        # ILIKE op gecombineerd zoekveld; geen FTS-rank in v1 — pure filter.
        clauses.append(
            "(titel ILIKE %s OR coalesce(beschrijving,'') ILIKE %s "
            " OR coalesce(inhoud_tekst,'') ILIKE %s "
            " OR coalesce(straatnaam,'') ILIKE %s "
            " OR coalesce(woonplaats,'') ILIKE %s)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])

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
    zaaknummer_bg, preferred_url, pdf_url
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
    sort: Literal["datum", "datum_asc", "ontvangst", "bg"] = Query("datum"),
    limit: int = Query(50, ge=1, le=LIST_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    t0 = time.perf_counter()
    clauses, params = _build_filters(q, tb, ac, bg, org, th, vanaf, totd, geom, ontv, zaak, bbox)
    where = _where_sql(clauses)
    order = _SORT_SQL[sort]

    count_sql = f"SELECT count(*) AS n FROM vth.vergunningkennisgeving {where}"
    list_sql = (
        f"SELECT {_LIST_COLS} FROM vth.vergunningkennisgeving "
        f"{where} ORDER BY {order} LIMIT %s OFFSET %s"
    )
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(count_sql, params)
        total = cur.fetchone()["n"]
        cur.execute(list_sql, params + [limit, offset])
        records = [VergunningSummary(**dict(r)) for r in cur.fetchall()]
    took = int((time.perf_counter() - t0) * 1000)
    return ListResponse(
        records=records, total=total, limit=limit, offset=offset, took_ms=took
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
    cap: int = Query(PINS_CAP, ge=100, le=50_000),
):
    t0 = time.perf_counter()
    # Pins-endpoint heeft 'geom' default-true: forceer NOT NULL ongeacht user-input.
    clauses, params = _build_filters(
        q, tb, ac, bg, org, th, vanaf, totd, True, ontv, zaak, bbox
    )
    where = _where_sql(clauses)
    count_sql = f"SELECT count(*) AS n FROM vth.vergunningkennisgeving {where}"
    pins_sql = (
        f"SELECT koop_id AS id, type_besluit AS tb, "
        f"  ST_X(geometrie_wgs_pt) AS lon, ST_Y(geometrie_wgs_pt) AS lat "
        f"FROM vth.vergunningkennisgeving {where} "
        f"ORDER BY datum_publicatie DESC, koop_id DESC LIMIT %s"
    )
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(count_sql, params)
        total = cur.fetchone()["n"]
        cur.execute(pins_sql, params + [cap])
        pins = [Pin(**dict(r)) for r in cur.fetchall()]
    took = int((time.perf_counter() - t0) * 1000)
    return PinsResponse(
        pins=pins,
        total_matching=total,
        returned=len(pins),
        truncated=total > len(pins),
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
):
    """Geeft counts per filter-waarde **met alle filters toegepast**.

    Bewuste keuze v1: een gefilterde waarde verdwijnt uit de facet zodra
    hij niet meer matcht. Een geavanceerder model (counts per dim
    excl. die dim zelf) is mogelijk maar duurder en niet nodig voor de
    eerste viewer-iteratie.
    """
    t0 = time.perf_counter()
    clauses, params = _build_filters(q, tb, ac, bg, org, th, vanaf, totd, geom, ontv, zaak, bbox)
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


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Totalen voor het register (header / lege-staat)",
)
def stats():
    t0 = time.perf_counter()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) AS total,
                   max(datum_publicatie_ts) AS last_publicatie,
                   max(ingest_at) AS last_ingest,
                   count(*) FILTER (WHERE inhoud_tekst IS NOT NULL) AS enriched
            FROM vth.vergunningkennisgeving
        """)
        r = cur.fetchone()
        total = r["total"]
        enriched = r["enriched"]
        cur.execute("""
            SELECT type_besluit AS value, count(*) AS count
            FROM vth.vergunningkennisgeving
            WHERE type_besluit IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC
        """)
        per_tb = [FacetBucket(**dict(b)) for b in cur.fetchall()]
    took = int((time.perf_counter() - t0) * 1000)
    return StatsResponse(
        total=total,
        last_publicatie=r["last_publicatie"],
        last_ingest=r["last_ingest"],
        enriched=enriched,
        enriched_pct=round(enriched / total * 100, 2) if total else 0.0,
        per_type_besluit=per_tb,
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
