"""
Endpoints voor het publiek MER-register (achter mer-register.nl).

Leest uit schema `mer` (zie mer-register.nl/sql/mer-schema.sql en
vault_v1/analysis/MER-register.nl — harvestbronnen en PoC.md). Bewust losstaand
schema: kanaal A = KOOP-proces-events, kanaal B = Commissie m.e.r.-documenten,
gekoppeld tot 'trajecten'.

Endpoints:
    GET  /v1/mer/trajecten          — paginated, filtered list (traject-samenvatting)
    GET  /v1/mer/trajecten/{slug}   — volledig traject (events + documenten)
    GET  /v1/mer/facets             — filter-counters
    GET  /v1/mer/stats              — totalen + ontsluiting, voor header/cijfers

Een 'traject' = één Commissie m.e.r.-project, verrijkt met zijn gekoppelde
KOOP-events (proces-tijdlijn) en documenten. Filter-conventie (list/facets):
    q     full-text (ILIKE titel + bevoegd gezag + initiatiefnemer)
    instr instrument, repeatable
    prov  provincie, repeatable
    et    event_type dat in het traject voorkomt, repeatable
    mer   true → alleen trajecten met een MER-PDF
    vanaf/totd  op datum van het laatste event
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from psycopg.rows import tuple_row
from db import get_conn

router = APIRouter(prefix="/v1/mer", tags=["mer"])


def _filters(q, instr, prov, et, mer, vanaf, totd):
    """Bouw WHERE-fragment + params, gedeeld door list en facets."""
    where, params = ["1=1"], {}
    if q:
        where.append("(p.titel ILIKE %(q)s OR p.bevoegd_gezag ILIKE %(q)s OR p.initiatiefnemer ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if instr:
        where.append("p.instrument = ANY(%(instr)s)")
        params["instr"] = instr
    if prov:
        where.append("p.provincie = ANY(%(prov)s)")
        params["prov"] = prov
    if et:
        where.append("""EXISTS (SELECT 1 FROM mer.project_event_link l JOIN mer.event e ON e.koop_id=l.koop_id
                                WHERE l.project_slug=p.slug AND e.event_type = ANY(%(et)s))""")
        params["et"] = et
    if mer:
        where.append("EXISTS (SELECT 1 FROM mer.document d WHERE d.project_slug=p.slug AND d.soort='MER')")
    if vanaf:
        where.append("le.laatste >= %(vanaf)s")
        params["vanaf"] = vanaf
    if totd:
        where.append("le.laatste <= %(totd)s")
        params["totd"] = totd
    return " AND ".join(where), params


# laatste-event-datum per project (voor sortering + periodefilter)
_LE = """LEFT JOIN LATERAL (
    SELECT max(e.datum_publicatie) AS laatste
    FROM mer.project_event_link l JOIN mer.event e ON e.koop_id=l.koop_id
    WHERE l.project_slug=p.slug) le ON true"""


@router.get("/trajecten")
def trajecten(
    q: str | None = None,
    instr: list[str] | None = Query(None),
    prov: list[str] | None = Query(None),
    et: list[str] | None = Query(None),
    mer: bool = False,
    vanaf: str | None = None,
    totd: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
) -> dict[str, Any]:
    where, params = _filters(q, instr, prov, et, mer, vanaf, totd)
    params.update(limit=limit, offset=offset)
    with get_conn() as conn, conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(f"SELECT COUNT(*) FROM mer.project p {_LE} WHERE {where}", params)
        total = cur.fetchone()[0]
        cur.execute(f"""
            SELECT p.slug, p.titel, p.bevoegd_gezag, p.initiatiefnemer, p.instrument, p.provincie,
                   p.lat, p.lon, le.laatste,
                   (SELECT count(*) FROM mer.project_event_link l WHERE l.project_slug=p.slug) AS n_events,
                   (SELECT array_agg(DISTINCT d.soort) FROM mer.document d WHERE d.project_slug=p.slug) AS soorten
            FROM mer.project p {_LE}
            WHERE {where}
            ORDER BY le.laatste DESC NULLS LAST, p.titel
            LIMIT %(limit)s OFFSET %(offset)s""", params)
        items = [{
            "id": r[0], "titel": r[1], "bevoegdGezag": r[2], "initiatiefnemer": r[3],
            "instrument": r[4], "provincie": r[5],
            "coord": [r[6], r[7]] if r[6] is not None else None,
            "laatsteEvent": r[8].isoformat() if r[8] else None,
            "aantalEvents": r[9], "documentSoorten": [s for s in (r[10] or []) if s],
        } for r in cur.fetchall()]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/trajecten/{slug}")
def traject(slug: str) -> dict[str, Any]:
    with get_conn() as conn, conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("""SELECT slug,titel,bevoegd_gezag,initiatiefnemer,instrument,provincie,lat,lon,url
                       FROM mer.project WHERE slug=%s""", (slug,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "traject niet gevonden")
        cur.execute("""SELECT e.datum_publicatie,e.event_type,e.publicatieblad,e.url
                       FROM mer.project_event_link l JOIN mer.event e ON e.koop_id=l.koop_id
                       WHERE l.project_slug=%s ORDER BY e.datum_publicatie""", (slug,))
        events = [{"datum": d.isoformat() if d else None, "type": t, "blad": b, "link": u}
                  for d, t, b, u in cur.fetchall()]
        cur.execute("""SELECT soort, bestandsnaam, url FROM mer.document WHERE project_slug=%s AND soort<>'overig'
                       ORDER BY CASE soort WHEN 'MER' THEN 0 WHEN 'startnotitie' THEN 1 WHEN 'richtlijnen' THEN 2
                                           WHEN 'toetsingsadvies' THEN 3 ELSE 4 END""", (slug,))
        docs = [{"soort": s, "titel": f"{s} — {(fn or '').replace('.pdf','')}", "link": u}
                for s, fn, u in cur.fetchall()]
    return {
        "id": row[0], "titel": row[1], "bevoegdGezag": row[2], "initiatiefnemer": row[3],
        "instrument": row[4], "provincie": row[5],
        "coord": [row[6], row[7]] if row[6] is not None else None,
        "bronnen": {"commissie_mer": row[8]}, "events": events, "documenten": docs,
    }


@router.get("/facets")
def facets(
    q: str | None = None, instr: list[str] | None = Query(None), prov: list[str] | None = Query(None),
    et: list[str] | None = Query(None), mer: bool = False, vanaf: str | None = None, totd: str | None = None,
) -> dict[str, Any]:
    where, params = _filters(q, instr, prov, et, mer, vanaf, totd)
    out = {}
    with get_conn() as conn, conn.cursor(row_factory=tuple_row) as cur:
        for key, col in (("instrument", "p.instrument"), ("provincie", "p.provincie")):
            cur.execute(f"SELECT {col}, COUNT(*) FROM mer.project p {_LE} WHERE {where} AND {col} IS NOT NULL "
                        f"GROUP BY 1 ORDER BY 2 DESC", params)
            out[key] = [{"waarde": r[0], "n": r[1]} for r in cur.fetchall()]
    return out


@router.get("/stats")
def stats() -> dict[str, Any]:
    with get_conn() as conn, conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT COUNT(*) FROM mer.project")
        projecten = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM mer.document")
        documenten = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM mer.event")
        events = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT project_slug) FROM mer.project_event_link")
        met_tijdlijn = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT project_slug) FROM mer.document WHERE soort='MER'")
        met_mer_pdf = cur.fetchone()[0]
    return {
        "projecten": projecten, "events": events, "documenten": documenten,
        "trajecten_met_tijdlijn": met_tijdlijn, "trajecten_met_mer_pdf": met_mer_pdf,
    }
