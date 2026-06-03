"""Endpoints voor ponsenkaart.nl — voortgangs-tracker van de
omgevingsplan-transitie (Wro → Ow) per gemeente.

Leest uit:
- `v2a.ponsenkaart_gemeente_stats` — matview met per-gemeente aggregaten
  (refresh nachtelijk of na OW-ingest).
- `core.gemeentegrens` — PDOK Bestuurlijke Gebieden.
- `p2p.pons` + `p2p.locatie` — individuele pons-polygonen.

Drie endpoints:
    GET /v1/ponsenkaart/stats         → nationale + per-provincie aggregaten
    GET /v1/ponsenkaart/gemeenten     → GeoJSON met gemeentegrenzen + stats
    GET /v1/ponsenkaart/ponsen        → GeoJSON met individuele pons-polygonen

Alle GeoJSON wordt in WGS84 (EPSG:4326) geleverd; de frontend doet de
projectie naar Web Mercator zelf.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from db import get_conn

router = APIRouter(prefix="/v1/ponsenkaart", tags=["ponsenkaart"])

# Tolerantie voor ST_Simplify (in graden voor 4326). 0.0001 ≈ 11m,
# voor heel-NL-overzicht ruimschoots genoeg en houdt response klein.
GEMEENTE_SIMPLIFY_TOL = 0.0005
PONS_SIMPLIFY_TOL = 0.0001

# Cache-strategie: matview refresht 1× per week. Browser-cache 1 uur
# (snelle navigatie binnen sessie), CDN-cache 24 uur (verre meerderheid
# van de tijd is de data dezelfde). Bij datarefresh kun je via Cloudflare
# een purge triggeren of gewoon wachten tot s-maxage verstreken is.
CACHE_HEADER = "public, max-age=3600, s-maxage=86400"


# ─────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────


class NationaleAggregaten(BaseModel):
    totaal_opp_km2: float
    geponst_opp_km2: float
    pct: float
    gemeenten_totaal: int
    gemeenten_gestart: int
    gemeenten_klaar: int
    ponsen_totaal: int


class ProvincieAggregaat(BaseModel):
    naam: str
    gemeenten_totaal: int
    gemeenten_gestart: int
    gemeenten_klaar: int
    pct: float


class StatsResponse(BaseModel):
    peildatum: date | None
    nl: NationaleAggregaten
    provincies: list[ProvincieAggregaat]


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


@router.get("/stats", response_model=StatsResponse)
def stats(response: Response) -> StatsResponse:
    """Nationale en per-provincie aggregaten voor de KPI-strip en
    het "Per provincie"-paneel."""
    response.headers["Cache-Control"] = CACHE_HEADER
    with get_conn() as conn:
        with conn.cursor() as cur:
            # NL-stand: één rij
            cur.execute("""
                SELECT
                    SUM(gemeente_opp_m2)        AS tot,
                    SUM(geponst_opp_m2)         AS gepons,
                    COUNT(*)                    AS n,
                    COUNT(*) FILTER (WHERE pons_count > 0) AS gestart,
                    COUNT(*) FILTER (WHERE pct >= 95)      AS klaar,
                    SUM(pons_count)             AS ponsen
                  FROM v2a.ponsenkaart_gemeente_stats
            """)
            r = cur.fetchone()
            tot = float(r["tot"] or 0)
            gepons = float(r["gepons"] or 0)
            nl = NationaleAggregaten(
                totaal_opp_km2=round(tot / 1e6, 1),
                geponst_opp_km2=round(gepons / 1e6, 2),
                pct=round(gepons / tot * 100, 4) if tot > 0 else 0,
                gemeenten_totaal=int(r["n"] or 0),
                gemeenten_gestart=int(r["gestart"] or 0),
                gemeenten_klaar=int(r["klaar"] or 0),
                ponsen_totaal=int(r["ponsen"] or 0),
            )

            cur.execute("""
                SELECT provincie,
                       COUNT(*) AS n,
                       COUNT(*) FILTER (WHERE pons_count > 0) AS gestart,
                       COUNT(*) FILTER (WHERE pct >= 95)      AS klaar,
                       SUM(gemeente_opp_m2)                   AS tot,
                       SUM(geponst_opp_m2)                    AS gepons
                  FROM v2a.ponsenkaart_gemeente_stats
                 WHERE provincie IS NOT NULL
                 GROUP BY provincie
                 ORDER BY (SUM(geponst_opp_m2) / NULLIF(SUM(gemeente_opp_m2), 0)) DESC NULLS LAST,
                          provincie
            """)
            provincies = []
            for row in cur.fetchall():
                ptot = float(row["tot"] or 0)
                pgep = float(row["gepons"] or 0)
                provincies.append(ProvincieAggregaat(
                    naam=row["provincie"],
                    gemeenten_totaal=int(row["n"] or 0),
                    gemeenten_gestart=int(row["gestart"] or 0),
                    gemeenten_klaar=int(row["klaar"] or 0),
                    pct=round(pgep / ptot * 100, 2) if ptot > 0 else 0,
                ))

            # Peildatum = laatste peildatum uit gemeentegrens
            cur.execute("SELECT MAX(peildatum) AS pd FROM core.gemeentegrens")
            pr = cur.fetchone()
            peildatum = pr["pd"] if pr else None

    return StatsResponse(peildatum=peildatum, nl=nl, provincies=provincies)


@router.get("/gemeenten")
def gemeenten(response: Response) -> dict[str, Any]:
    """GeoJSON FeatureCollection met gemeentegrens + stats per gemeente.

    De frontend voedt hiermee de choropleth, KPI-strip, leaderboard
    en gemeente-detailpanel. Geometrieën in WGS84, vereenvoudigd voor
    snelle browser-rendering.
    """
    response.headers["Cache-Control"] = CACHE_HEADER
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    s.overheidscode,
                    s.naam,
                    s.provincie,
                    ROUND((s.gemeente_opp_m2 / 1e6)::numeric, 1) AS gemeente_opp_km2,
                    ROUND((s.geponst_opp_m2  / 1e6)::numeric, 3) AS geponst_opp_km2,
                    s.pons_count,
                    s.pct,
                    ST_AsGeoJSON(
                        ST_Simplify(
                            ST_Transform(g.geometrie, 4326),
                            {GEMEENTE_SIMPLIFY_TOL}
                        )
                    )::json AS geom
                  FROM v2a.ponsenkaart_gemeente_stats s
                  JOIN core.gemeentegrens g ON g.overheidscode = s.overheidscode
                 ORDER BY s.naam
            """)
            rows = cur.fetchall()

    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "id": r["overheidscode"],
            "geometry": r["geom"],
            "properties": {
                "overheidscode": r["overheidscode"],
                "naam": r["naam"],
                "provincie": r["provincie"],
                "gemeente_opp_km2": float(r["gemeente_opp_km2"] or 0),
                "geponst_opp_km2": float(r["geponst_opp_km2"] or 0),
                "pons_count": int(r["pons_count"] or 0),
                "pct": float(r["pct"] or 0),
            },
        })

    return {"type": "FeatureCollection", "features": features}


@router.get("/ponsen")
def ponsen(
    response: Response,
    gemeente: str | None = Query(
        None,
        description="Filter op overheidscode (bv. gm0307). Default: alle ponsen.",
    ),
) -> dict[str, Any]:
    """GeoJSON FeatureCollection met individuele pons-polygonen.

    Gebruikt door de pons-modus op de kaart en het detail-panel per
    gemeente. Zonder filter retourneert dit alle ponsen in NL (op dit
    moment <30 stuks, dus geen pagination nodig).
    """
    response.headers["Cache-Control"] = CACHE_HEADER
    where = ""
    params: list[Any] = []
    if gemeente:
        where = "WHERE p.identificatie LIKE %s"
        params.append(f"nl.imow-{gemeente}.%")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    p.identificatie AS pons_id,
                    SUBSTRING(p.identificatie FROM 'nl.imow-(gm[0-9]+)') AS bronhouder,
                    g.naam AS gemeente_naam,
                    ROUND((ST_Area(l.geometrie) / 1e6)::numeric, 3) AS opp_km2,
                    ST_AsGeoJSON(
                        ST_Simplify(
                            ST_Transform(l.geometrie, 4326),
                            {PONS_SIMPLIFY_TOL}
                        )
                    )::json AS geom
                  FROM p2p.pons p
                  JOIN p2p.locatie l ON l.identificatie = p.locatie_id
                  LEFT JOIN core.gemeentegrens g
                    ON g.overheidscode = SUBSTRING(p.identificatie FROM 'nl.imow-(gm[0-9]+)')
                 {where}
                 AND ST_X(ST_Centroid(l.geometrie)) <> 0
                 ORDER BY pons_id
            """ if where else f"""
                SELECT
                    p.identificatie AS pons_id,
                    SUBSTRING(p.identificatie FROM 'nl.imow-(gm[0-9]+)') AS bronhouder,
                    g.naam AS gemeente_naam,
                    ROUND((ST_Area(l.geometrie) / 1e6)::numeric, 3) AS opp_km2,
                    ST_AsGeoJSON(
                        ST_Simplify(
                            ST_Transform(l.geometrie, 4326),
                            {PONS_SIMPLIFY_TOL}
                        )
                    )::json AS geom
                  FROM p2p.pons p
                  JOIN p2p.locatie l ON l.identificatie = p.locatie_id
                  LEFT JOIN core.gemeentegrens g
                    ON g.overheidscode = SUBSTRING(p.identificatie FROM 'nl.imow-(gm[0-9]+)')
                 WHERE ST_X(ST_Centroid(l.geometrie)) <> 0
                 ORDER BY pons_id
            """, params)
            rows = cur.fetchall()

    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "id": r["pons_id"],
            "geometry": r["geom"],
            "properties": {
                "pons_id": r["pons_id"],
                "bronhouder": r["bronhouder"],
                "gemeente_naam": r["gemeente_naam"],
                "opp_km2": float(r["opp_km2"] or 0),
            },
        })

    return {"type": "FeatureCollection", "features": features}
