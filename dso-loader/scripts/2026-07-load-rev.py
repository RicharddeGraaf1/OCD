"""
Ingest Register Externe Veiligheid (REV) risicobronnen → PostGIS `lev`-schema.

Waarom ingest i.p.v. live-bevragen: de PDOK REV-WFS
(`faciliteiten-voor-productie-en-industrie`) blijkt onbruikbaar voor live
puntbevraging — bbox-filtering geeft 0 resultaten (ongeacht CRS) en RD-output
(EPSG:28992) is corrupt; alleen een volledige, ongefilterde fetch in WGS84
levert bruikbare data. Bovendien: ~4,9k features is klein en verandert zelden,
dus één keer laden + periodiek verversen bespaart alle live-calls per request
(zie plan `leefomgevingskwaliteit-bronintegratie.md`, sectie "Belasting & toggles").

Axis-normalisatie: de WFS geeft coördinaten als [lat, lon] (EPSG:4258-asvolgorde,
niet GeoJSON-conform). We normaliseren met een NL-heuristiek (lat ~50–54, lon
~3–7) naar [lon, lat] vóór we ze aan PostGIS voeren.

Draaien:  python dso-loader/scripts/2026-07-load-rev.py
Vereist:  DATABASE_URL (of default localhost:5434/dso), netwerk naar service.pdok.nl.
"""

from __future__ import annotations

import json
import os
import sys

import httpx
import psycopg

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5434/dso")

WFS = (
    "https://service.pdok.nl/rws/faciliteiten-voor-productie-en-industrie/"
    "productie-installaties/wfs/v1_0"
)
LAGEN = {
    "punt": "faciliteiten-voor-productie-en-industrie:production_installation_point",
    "vlak": "faciliteiten-voor-productie-en-industrie:production_instalation_polygon",
}
PAGE = 1000

DDL = """
CREATE SCHEMA IF NOT EXISTS lev;

CREATE TABLE IF NOT EXISTS lev.rev_risicobron (
    id         text NOT NULL,
    soort      text NOT NULL,                     -- 'punt' | 'vlak'
    bron       text,                              -- namespace / registrerende dienst
    status     text,                              -- INSPIRE ConditionOfFacility (bv. functional)
    geom       geometry(Geometry, 28992) NOT NULL,
    ingest_ts  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rev_risicobron_geom_gix ON lev.rev_risicobron USING gist (geom);
CREATE INDEX IF NOT EXISTS rev_risicobron_id_ix   ON lev.rev_risicobron (id);
"""


def _normaliseer(coord):
    """Zet één [a, b]-coördinaat om naar [lon, lat]. De WFS levert [lat, lon]
    (EPSG:4258-asvolgorde); als het eerste getal in de NL-lat-band ligt, wisselen."""
    a, b = coord[0], coord[1]
    if 50.0 <= a <= 54.0 and 3.0 <= b <= 7.5:
        return [b, a]
    return [a, b]


def _normaliseer_geom(geom: dict) -> dict:
    """Diep-normaliseer de coördinaten van een GeoJSON-geometrie naar [lon, lat]."""
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Point":
        return {"type": t, "coordinates": _normaliseer(c)}
    if t in ("LineString", "MultiPoint"):
        return {"type": t, "coordinates": [_normaliseer(p) for p in c]}
    if t in ("Polygon", "MultiLineString"):
        return {"type": t, "coordinates": [[_normaliseer(p) for p in ring] for ring in c]}
    if t == "MultiPolygon":
        return {"type": t, "coordinates": [[[_normaliseer(p) for p in ring] for ring in poly] for poly in c]}
    return geom


def _fetch(client: httpx.Client, typename: str):
    """Haal alle features van één typeName op, gepagineerd (native WGS84)."""
    start = 0
    while True:
        r = client.get(WFS, params={
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": typename, "outputFormat": "application/json",
            "count": PAGE, "startIndex": start,
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
            cur.execute("TRUNCATE lev.rev_risicobron")
        conn.commit()

        totaal = 0
        with httpx.Client() as client:
            for soort, typename in LAGEN.items():
                n = 0
                rows: list[tuple] = []
                for f in _fetch(client, typename):
                    props = f.get("properties") or {}
                    geom = f.get("geometry")
                    if not geom:
                        continue
                    lokaal = (props.get("localId") or {}).get("lokaalID") or props.get("identifier") or ""
                    bron = props.get("namespace") or ""
                    status = (props.get("statusXlinkHref") or "").rsplit("/", 1)[-1]
                    gj = json.dumps(_normaliseer_geom(geom))
                    rows.append((lokaal, soort, bron, status, gj))
                    n += 1
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO lev.rev_risicobron (id, soort, bron, status, geom)
                           VALUES (%s, %s, %s, %s,
                                   ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 28992))""",
                        rows,
                    )
                conn.commit()
                totaal += n
                print(f"  {soort}: {n} geladen")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM lev.rev_risicobron")
            db_n = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM lev.rev_risicobron "
                "WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(90500, 435500), 28992), 5000)"
            )
            rdam = cur.fetchone()[0]
        print(f"KLAAR — {totaal} gefetcht, {db_n} in DB; {rdam} binnen 5 km van Rotterdamse haven (sanity)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
