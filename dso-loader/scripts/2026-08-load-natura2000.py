"""
Ingest Natura 2000-gebieden (PDOK/RVO) → PostGIS `lev`-schema.

Waarom ingest i.p.v. live-bevragen: net als bij REV (`2026-07-load-rev.py`) is een
per-request live-bevraging onnodig belastend, terwijl de dataset klein (~209
gebieden) is en zelden verandert. Eén keer laden + periodiek verversen bespaart
alle live-calls. De `natuur`-adapter in `ocd-api/leefomgeving.py` doet dan een
lokale nearest-distance-query (`ST_DWithin`/`ST_Distance`), exact zoals `extern`.

Verschil met REV: de PDOK Natura 2000-WFS levert RD (EPSG:28992) *correct* — geen
axis-flip of corrupte output. We halen dus meteen in RD op en zetten de SRID op
28992 zonder transform.

Draaien:  python dso-loader/scripts/2026-08-load-natura2000.py
Vereist:  DATABASE_URL (of default localhost:5434/dso), netwerk naar service.pdok.nl.
"""

from __future__ import annotations

import json
import os
import sys

import httpx
import psycopg

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5434/dso")

WFS = "https://service.pdok.nl/rvo/natura2000/wfs/v1_0"
TYPENAME = "natura2000:natura2000"
PAGE = 1000

DDL = """
CREATE SCHEMA IF NOT EXISTS lev;

CREATE TABLE IF NOT EXISTS lev.natura2000 (
    nr           text,                              -- N2000-gebiedsnummer
    naam         text NOT NULL,                     -- naamN2K
    bescherming  text,                              -- HR | VR | HR+VR (Habitat-/Vogelrichtlijn)
    sitecode     text,                              -- sitecodeH / sitecodeV
    status       text,
    geom         geometry(Geometry, 28992) NOT NULL,
    ingest_ts    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS natura2000_geom_gix ON lev.natura2000 USING gist (geom);
CREATE INDEX IF NOT EXISTS natura2000_naam_ix  ON lev.natura2000 (naam);
"""


def _fetch(client: httpx.Client):
    """Haal alle features op, gepagineerd, native RD (EPSG:28992)."""
    start = 0
    while True:
        r = client.get(WFS, params={
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": TYPENAME, "outputFormat": "application/json",
            "srsName": "EPSG:28992", "count": PAGE, "startIndex": start,
        }, timeout=60)
        r.raise_for_status()
        feats = r.json().get("features", [])
        if not feats:
            break
        yield from feats
        if len(feats) < PAGE:
            break
        start += PAGE


def main() -> int:
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("TRUNCATE lev.natura2000")
        conn.commit()

        rows: list[tuple] = []
        with httpx.Client() as client:
            for f in _fetch(client):
                props = f.get("properties") or {}
                geom = f.get("geometry")
                if not geom:
                    continue
                naam = props.get("naamN2K") or props.get("nr") or "onbekend"
                sitecode = (props.get("sitecodeH") or props.get("sitecodeV") or "").strip() or None
                rows.append((
                    props.get("nr"),
                    naam,
                    (props.get("beschermin") or "").strip() or None,
                    sitecode,
                    (props.get("status") or "").strip() or None,
                    json.dumps(geom),
                ))

        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO lev.natura2000 (nr, naam, bescherming, sitecode, status, geom)
                   VALUES (%s, %s, %s, %s, %s,
                           ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992))""",
                rows,
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM lev.natura2000")
            db_n = cur.fetchone()[0]
            # Sanity: de Veluwe is een groot N2000-gebied; een punt midden op de
            # Veluwe (RD ~185000, 455000) moet ín een gebied liggen (afstand 0).
            cur.execute(
                "SELECT naam, round(ST_Distance(geom, ST_SetSRID(ST_MakePoint(185000, 455000), 28992))::numeric, 0) d "
                "FROM lev.natura2000 ORDER BY d LIMIT 1"
            )
            naam, d = cur.fetchone()
        print(f"KLAAR — {len(rows)} gefetcht, {db_n} in DB; dichtstbij Veluwe-punt: {naam} op {d} m (sanity)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
