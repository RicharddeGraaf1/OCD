"""
Ingest Rijksmonumenten (RCE, INSPIRE ProtectedSite Cultuurhistorie) → PostGIS `lev`.

Waarom ingest: zelfde afweging als REV/Natura2000 — een klein-tot-middelgrote,
zelden wijzigende set (≈68k) die we per request lokaal willen bevragen zonder
externe live-calls. De `cultuur`-adapter in `ocd-api/leefomgeving.py` doet dan een
nearest-distance-query (`ST_DWithin`/`ST_Distance`), exact zoals `extern`/`natuur`.

Bron: PDOK RCE `ps-ch` (ProtectedSite Cultuurhistorie, INSPIRE-geharmoniseerd),
twee feature-types: `rce_inspire_points` (63.570 rijksmonumenten) en
`rce_inspire_polygons` (4.684 monumentterreinen/complexen). Levert RD (EPSG:28992)
*correct* → meteen in RD ophalen, SRID 28992 zonder transform.

Let op: INSPIRE-data heeft geen leesbare naam (`text` is leeg); we bewaren het
rijksmonument-registernummer (`localid`, bv. '10001.00' → '10001') als referentie
naar het monumentenregister.

Draaien:  python dso-loader/scripts/2026-08-load-rijksmonumenten.py
Vereist:  DATABASE_URL (of default localhost:5434/dso), netwerk naar service.pdok.nl.
"""

from __future__ import annotations

import json
import os
import sys

import httpx
import psycopg

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5434/dso")

WFS = "https://service.pdok.nl/rce/ps-ch/wfs/v1_0"
TYPENAMES = {
    "punt": "ps-ch:rce_inspire_points",
    "vlak": "ps-ch:rce_inspire_polygons",
}
PAGE = 1000

# De WFS capt globale paging op startIndex 50.000 (bevestigd: 50000→200, 51000→400),
# terwijl `rce_inspire_points` er 63.570 heeft. Daarom per bbox-tegel ophalen — dat
# reset het paging-venster per tegel — en dedupliceren op registernummer (een monument
# op een tegelrand komt in twee tegels terug). NL-extent (RD, ruim) + 50 km-tegels
# houden elke tegel ruim onder de cap.
NL_BBOX = (0, 300000, 290000, 625000)
TILE = 50000

DDL = """
CREATE SCHEMA IF NOT EXISTS lev;

CREATE TABLE IF NOT EXISTS lev.rijksmonument (
    registernr   text,                              -- rijksmonumentnummer (uit localid)
    soort        text NOT NULL,                     -- 'punt' | 'vlak'
    aanwijzing   date,                              -- legalfoundationdate
    geom         geometry(Geometry, 28992) NOT NULL,
    ingest_ts    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rijksmonument_geom_gix ON lev.rijksmonument USING gist (geom);
CREATE INDEX IF NOT EXISTS rijksmonument_nr_ix    ON lev.rijksmonument (registernr);
"""


def _tiles():
    """Genereer 50 km-bbox-tegels die NL dekken (RD)."""
    x0, y0, x1, y1 = NL_BBOX
    x = x0
    while x < x1:
        y = y0
        while y < y1:
            yield (x, y, min(x + TILE, x1), min(y + TILE, y1))
            y += TILE
        x += TILE


def _fetch_bbox(client: httpx.Client, typename: str, bbox: tuple):
    """Haal alle features van één typeName binnen één bbox op, gepagineerd (native RD)."""
    start = 0
    while True:
        r = client.get(WFS, params={
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": typename, "outputFormat": "application/json",
            "srsName": "EPSG:28992",
            "bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]},EPSG:28992",
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


def _fetch(client: httpx.Client, typename: str):
    """Alle features van één typeName over alle tegels; dedup op registernummer."""
    seen: set[str] = set()
    for bbox in _tiles():
        for f in _fetch_bbox(client, typename, bbox):
            lid = (f.get("properties") or {}).get("localid")
            if lid is not None:
                if lid in seen:
                    continue
                seen.add(lid)
            yield f


def main() -> int:
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("TRUNCATE lev.rijksmonument")
        conn.commit()

        totaal = 0
        with httpx.Client() as client:
            for soort, typename in TYPENAMES.items():
                rows: list[tuple] = []
                for f in _fetch(client, typename):
                    geom = f.get("geometry")
                    if not geom:
                        continue
                    props = f.get("properties") or {}
                    nr = (props.get("localid") or "").split(".")[0] or None
                    datum = props.get("legalfoundationdate") or None
                    rows.append((nr, soort, datum, json.dumps(geom)))
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO lev.rijksmonument (registernr, soort, aanwijzing, geom)
                           VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992))""",
                        rows,
                    )
                conn.commit()
                totaal += len(rows)
                print(f"  {soort}: {len(rows)} geladen")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM lev.rijksmonument")
            db_n = cur.fetchone()[0]
            # Sanity: de Amsterdamse grachtengordel zit vol rijksmonumenten; een punt
            # op de Herengracht (RD ~121300, 487400) moet er meerdere binnen 100 m hebben.
            cur.execute(
                "SELECT count(*) FROM lev.rijksmonument "
                "WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(121300, 487400), 28992), 100)"
            )
            adam = cur.fetchone()[0]
        print(f"KLAAR — {totaal} gefetcht, {db_n} in DB; {adam} binnen 100 m van Herengracht (sanity)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
