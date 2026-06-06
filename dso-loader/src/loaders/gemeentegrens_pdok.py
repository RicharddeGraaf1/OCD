"""Load gemeentegrenzen + provinciegrenzen from PDOK Bestuurlijke Gebieden.

Eenmalige (jaarlijkse) load die `core.gemeentegrens` vult. Levert de noemer
voor "% geponst" en de provincie-toewijzing per gemeente — beide nodig
voor `v2a.ponsenkaart_gemeente_stats`.

Bron: PDOK Bestuurlijke Gebieden WFS v1_0, autoritair via Kadaster.

Refresh-cadans: jaarlijks i.v.m. gemeente-herindelingen. Idempotent —
draai opnieuw en de tabel wordt schoongemaakt en hervuld.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
from rich.console import Console
from rich.progress import track

from src.db import get_conn

console = Console()

PDOK_BG_WFS = "https://service.pdok.nl/kadaster/bestuurlijkegebieden/wfs/v1_0"

# WFS-parameters: GeoJSON-output in RD (EPSG:28992) zodat we geen
# transformatie hoeven te doen.
_WFS_BASE_PARAMS = {
    "service": "WFS",
    "version": "2.0.0",
    "request": "GetFeature",
    "outputFormat": "application/json",
    "srsName": "EPSG:28992",
}


def _fetch_features(type_name: str) -> list[dict]:
    """Haal een WFS-laag als GeoJSON-features op."""
    params = {**_WFS_BASE_PARAMS, "typeName": type_name}
    console.print(f"  Fetching {type_name} from PDOK WFS ...")
    with httpx.Client(timeout=120) as client:
        resp = client.get(PDOK_BG_WFS, params=params)
        resp.raise_for_status()
        data = resp.json()
    features = data.get("features", [])
    console.print(f"    [green]{len(features)} features[/green]")
    return features


def _normalize_overheidscode(raw: str | None, soort: str) -> str | None:
    """PDOK levert codes als 'GM0363' of '0363'. We normaliseren naar
    `gm0363` / `pv26` zoals de rest van OCD ze opslaat."""
    if not raw:
        return None
    s = str(raw).strip().lower()
    prefix = "gm" if soort == "gemeente" else "pv"
    if s.startswith(prefix):
        return s
    # Strip andere prefix, plak prefix erop
    s = s.lstrip("gmpv ")
    return f"{prefix}{s}"


def _geom_to_ewkt(geom: dict) -> str:
    """GeoJSON-geometrie → EWKT in EPSG:28992 voor PostGIS.

    Polygon wordt MultiPolygon zodat de tabel-kolom consistent is.
    """
    if geom["type"] == "Polygon":
        wrapped = {"type": "MultiPolygon", "coordinates": [geom["coordinates"]]}
    else:
        wrapped = geom
    return f"SRID=28992;{json.dumps(wrapped)}"


def load_gemeentegrenzen() -> None:
    """Laad alle Nederlandse gemeentegrenzen + provincies uit PDOK.

    Idempotent: tabel wordt eerst TRUNCATEd.
    """
    gemeenten = _fetch_features("bestuurlijkegebieden:Gemeentegebied")
    provincies = _fetch_features("bestuurlijkegebieden:Provinciegebied")

    if not gemeenten:
        console.print("[red]Geen gemeenten ontvangen van PDOK — abort[/red]")
        return

    peildatum = date.today().isoformat()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 1. Provincies tijdelijk in een staging-tabel zodat we ST_Within
            #    kunnen gebruiken voor de gemeente→provincie-toewijzing.
            cur.execute("""
                CREATE TEMP TABLE _prov_staging (
                    naam       TEXT NOT NULL,
                    geometrie  GEOMETRY(MultiPolygon, 28992) NOT NULL
                ) ON COMMIT DROP
            """)
            for f in provincies:
                naam = (f.get("properties") or {}).get("naam") \
                    or (f.get("properties") or {}).get("statnaam") \
                    or (f.get("properties") or {}).get("provincienaam")
                geom = f.get("geometry")
                if not naam or not geom:
                    continue
                # PDOK levert coördinaten in EPSG:28992 (RD) via srsName=...,
                # maar ST_GeomFromGeoJSON kent default SRID 4326 toe.
                # We overschrijven met SetSRID(28992).
                cur.execute(
                    "INSERT INTO _prov_staging (naam, geometrie) VALUES (%s, "
                    "ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)))",
                    (naam, json.dumps(geom)),
                )

            # 2. Schoonmaken vóór herladen (idempotent)
            cur.execute("TRUNCATE core.gemeentegrens")

            # 3. Per gemeente: bronhouder upserten + gemeentegrens inserten
            ingest_count = 0
            skipped = []
            for f in track(gemeenten, description="  Loading gemeenten"):
                props = f.get("properties") or {}
                naam = props.get("naam") or props.get("statnaam") \
                    or props.get("gemeentenaam")
                code_raw = props.get("code") or props.get("identificatie") \
                    or props.get("gemeentecode")
                geom = f.get("geometry")
                if not (naam and code_raw and geom):
                    skipped.append(str(props)[:80])
                    continue

                overheidscode = _normalize_overheidscode(code_raw, "gemeente")

                # bronhouder upsert (FK-vereiste voor gemeentegrens)
                cur.execute("""
                    INSERT INTO core.bronhouder (overheidscode, naam, bestuurslaag)
                    VALUES (%s, %s, 'gemeente')
                    ON CONFLICT (overheidscode) DO UPDATE
                        SET naam = EXCLUDED.naam,
                            bestuurslaag = COALESCE(core.bronhouder.bestuurslaag,
                                                    EXCLUDED.bestuurslaag)
                """, (overheidscode, naam))

                # gemeentegrens insert; provincie laten we op NULL,
                # ST_Within vult 'm zo. PDOK levert RD-coördinaten via
                # srsName=EPSG:28992; we declareren de SRID expliciet.
                geom_json = json.dumps(geom)
                cur.execute("""
                    INSERT INTO core.gemeentegrens
                        (overheidscode, naam, provincie, geometrie,
                         oppervlak_m2, peildatum)
                    VALUES (
                        %s, %s, NULL,
                        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)),
                        ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)),
                        %s
                    )
                """, (overheidscode, naam, geom_json, geom_json, peildatum))
                ingest_count += 1

            # 4. Provincie afleiden via centroid → ST_Within. ST_Within
            #    op centroïde is robuust tegen randgevallen (gemeenten
            #    op de provinciegrens).
            cur.execute("""
                UPDATE core.gemeentegrens g
                   SET provincie = p.naam
                  FROM _prov_staging p
                 WHERE ST_Within(ST_Centroid(g.geometrie), p.geometrie)
            """)

            conn.commit()

            console.print(
                f"[green]{ingest_count} gemeenten geladen "
                f"(peildatum {peildatum})[/green]"
            )
            if skipped:
                console.print(f"[yellow]  {len(skipped)} overgeslagen "
                              "(geen naam/code/geometrie):[/yellow]")
                for s in skipped[:5]:
                    console.print(f"    {s}")

            # 5. ANALYZE voor de planner
            cur.execute("ANALYZE core.gemeentegrens")
    finally:
        conn.close()


def refresh_ponsenkaart_stats() -> None:
    """Refresh de v2a.ponsenkaart_gemeente_stats matview.

    Gebruikt CONCURRENTLY zodat readers (API) niet geblokkeerd worden;
    valt terug op non-concurrent bij eerste refresh (matview nog leeg).
    """
    conn = get_conn()
    conn.autocommit = True  # REFRESH MATERIALIZED VIEW vereist autocommit
    try:
        with conn.cursor() as cur:
            console.print("Refreshing v2a.ponsenkaart_gemeente_stats ...")
            try:
                cur.execute(
                    "REFRESH MATERIALIZED VIEW CONCURRENTLY "
                    "v2a.ponsenkaart_gemeente_stats"
                )
                console.print("[green]refreshed (concurrent)[/green]")
            except Exception as e:
                # Eerste refresh kan niet concurrent omdat de matview
                # nog niet populated is.
                console.print(f"[yellow]  Concurrent refresh faalde: {e}[/yellow]")
                console.print("  Trying non-concurrent fallback ...")
                cur.execute(
                    "REFRESH MATERIALIZED VIEW v2a.ponsenkaart_gemeente_stats"
                )
                console.print("[green]refreshed (non-concurrent)[/green]")

            # Korte stand tonen
            cur.execute("""
                SELECT COUNT(*) AS n,
                       SUM(geponst_opp_m2) / NULLIF(SUM(gemeente_opp_m2), 0)
                           * 100 AS nl_pct,
                       COUNT(*) FILTER (WHERE pons_count > 0) AS gestart,
                       COUNT(*) FILTER (WHERE pct >= 95) AS klaar
                  FROM v2a.ponsenkaart_gemeente_stats
            """)
            row = cur.fetchone()
            if row:
                n = row["n"] if hasattr(row, "__getitem__") else row[0]
                nl_pct = row["nl_pct"] if hasattr(row, "__getitem__") else row[1]
                gestart = row["gestart"] if hasattr(row, "__getitem__") else row[2]
                klaar = row["klaar"] if hasattr(row, "__getitem__") else row[3]
                console.print(
                    f"  Stand: {n} gemeenten · "
                    f"{nl_pct or 0:.2f}% NL geponst · "
                    f"{gestart} gestart · {klaar} klaar"
                )
    finally:
        conn.close()
