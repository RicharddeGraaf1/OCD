"""Vector tiles (MVT) op de PDOK-RD-tegelpiramide.

Zie `OCDviewer/docs/plans/vector-tiles.md`. Twee dingen die dit endpoint
onderscheiden van elk MVT-voorbeeld dat je online vindt:

1. **De piramide is RD, niet Web Mercator.** De viewer draait volledig in
   EPSG:28992 en de BRT-achtergrond komt uit de PDOK-WMTS op matrixset
   `EPSG:28992`. `ST_TileEnvelope` gaat uit van Web Mercator en levert hier
   tegels die niet op de achtergrond aansluiten — de envelope wordt daarom
   uitgerekend op de PDOK-resoluties.
2. **De bron hangt af van de zoom.** Op lage zoom komt de geometrie uit
   `p2p.locatie_generalisatie` (voorberekend vereenvoudigd), vanaf z11
   rechtstreeks uit `p2p.locatie_subdiv`. Voorberekenen levert zowel de
   kleinere tegel als de snellere query; live generaliseren ruilt het een
   tegen het ander in.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Path, Request, Response

from db import get_conn

router = APIRouter(prefix="/v1/tiles", tags=["tiles"])

# ── De PDOK-RD-piramide ───────────────────────────────────────────────
# Exact dezelfde waarden als de WMTS-laag in de viewer
# (frontend/src/app/shared/kaart/kaart.component.ts). Wijkt hier iets af, dan
# schuiven de tegels ten opzichte van de achtergrondkaart.

RD_MINX = -285401.92
RD_MAXY = 903401.92          # origin ligt LINKSBOVEN
RES_Z0 = 3440.640            # m/px op z0, elke stap gehalveerd
TEGEL_PX = 256
MAX_ZOOM = 14                # 15 niveaus: z0 t/m z14

MVT_EXTENT = 4096            # rasterresolutie binnen de tegel

# Geen buffer. De gebruikelijke 64 laat naburige tegels elkaar overlappen zodat
# lijnen en labels niet aan de rand afbreken -- maar deze laag tekent
# half-doorzichtige vlakken zonder contour, en dan wordt elke overlap twee keer
# ingekleurd: een donkere band langs elke tegelrand, precies het raster dat je
# niet wilt zien. Bij buffer 0 sluiten de vlakken exact op elkaar aan.
# Komen hier ooit lijnen of labels bij, dan moet dit terug omhoog.
MVT_BUFFER = 0

MEDIATYPE = "application/vnd.mapbox-vector-tile"

# Welk generalisatieniveau bedient welke zoom. None = rechtstreeks uit de bron.
# Elk niveau dekt twee zoomstappen; meer kan niet, want de sub-pixelgrens
# schaalt mee met de resolutie.
def _niveau_voor(z: int) -> int | None:
    if z <= 6:
        return 6
    if z <= 8:
        return 8
    if z <= 10:
        return 10
    return None


# Lagen zijn een allowlist en geen vrije parameter: de waarde komt in een
# tabelnaam terecht.
LAGEN: dict[str, dict] = {
    "locaties": {
        "bron": "p2p.locatie_subdiv",
        "generalisatie": "p2p.locatie_generalisatie",
        "sleutel": "identificatie",
    },
    # Wro-planobjecten (bestemmingen, bouwvlakken, maatvoering, figuren).
    # Anders dan de Ow-kant: de bron is niet opgedeeld, en ~5% van de objecten
    # is een lijn in plaats van een vlak — de kaartlaag moet die als lijn
    # tekenen, want een lijn heeft geen vulling.
    "planobjecten": {
        "bron": "wro.planobject",
        "generalisatie": "wro.planobject_generalisatie",
        "sleutel": "identificatie",
    },
}


def envelope(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """RD-envelope van tegel (z, x, y). y telt van boven naar beneden."""
    breedte = RES_Z0 / 2**z * TEGEL_PX
    minx = RD_MINX + x * breedte
    maxy = RD_MAXY - y * breedte
    return minx, maxy - breedte, minx + breedte, maxy


def _tegels_op_zoom(z: int) -> int:
    """Aantal tegels per as. De RD-extent is vierkant — 880.803,84 m in x en in
    y — en op z0 precies één tegel breed, dus dit verdubbelt per stap."""
    return 2**z


def _sql(laag: dict, niveau: int | None) -> str:
    tabel = laag["bron"] if niveau is None else laag["generalisatie"]
    niveau_filter = "" if niveau is None else f"AND b.niveau = {niveau}"
    return f"""
        WITH env AS (
          SELECT ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 28992) AS b
        ),
        mvt AS (
          SELECT b.{laag["sleutel"]} AS id,
                 ST_AsMVTGeom(b.geometrie, env.b, {MVT_EXTENT}, {MVT_BUFFER}, true) AS geom
            FROM {tabel} b, env
           WHERE b.geometrie && env.b {niveau_filter}
        )
        SELECT ST_AsMVT(mvt, %(laagnaam)s, {MVT_EXTENT}, 'geom') AS tegel
          FROM mvt WHERE geom IS NOT NULL
    """  # noqa: S608 — tabel/niveau komen uit LAGEN resp. _niveau_voor, niet uit de request


# Tabellen waarvan we al weten dat ze bestaan. Alleen positieve uitkomsten
# onthouden: staat een tabel er niet, dan blijven we kijken, zodat hij na het
# bouwen meteen meedoet zonder herstart.
_BESTAAT: set[str] = set()


def _eis_generalisatie(conn, tabel: str) -> None:
    """503 als de generalisatietabel ontbreekt.

    Het endpoint kan gedeployed zijn zonder dat de tabellen op die database
    gebouwd zijn — dat was op 2026-08-09 precies de situatie op productie. Dan
    is z11+ gewoon goed (die leest de brontabel) maar loopt elke lagere zoom op
    een 'relation does not exist'. Een 500 met een psycopg-stacktrace vertelt de
    aanroeper niet wat er moet gebeuren; dit wel.
    """
    if tabel in _BESTAAT:
        return
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS bestaat", (tabel,))
        rij = cur.fetchone()
    if rij and rij["bestaat"]:
        _BESTAAT.add(tabel)
        return
    raise HTTPException(
        503,
        f"Generalisatietabel {tabel} bestaat niet op deze database. Bouwen met "
        f"dso-loader/scripts/vul_locatie_generalisatie.py (zie het DDL-script "
        f"ernaast). Tot die tijd zijn alleen tegels vanaf z11 beschikbaar.",
    )


def _versie(conn) -> str:
    """Stempel dat verandert zodra er data is bijgeladen. Basis voor de ETag."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(finished_at) AS t FROM core.load_run WHERE status IN ('ok','deels')"
        )
        rij = cur.fetchone()
    return rij["t"].isoformat() if rij and rij["t"] else "leeg"


@router.get(
    "/{laag}/{z}/{x}/{y}.mvt",
    response_class=Response,
    responses={200: {"content": {MEDIATYPE: {}}}},
)
def tegel(
    request: Request,
    laag: str,
    z: int = Path(ge=0, le=MAX_ZOOM),
    x: int = Path(ge=0),
    y: int = Path(ge=0),
) -> Response:
    """Eén vector tile. Leeg gebied levert een lege tegel (200, 0 bytes),
    geen 404 — dat is voor een tegelbron een geldig antwoord."""
    if laag not in LAGEN:
        raise HTTPException(404, f"Onbekende laag: {laag}")

    grens = _tegels_op_zoom(z)
    if x >= grens or y >= grens:
        raise HTTPException(404, f"Tegel buiten de RD-piramide op z{z} (max {grens - 1})")

    niveau = _niveau_voor(z)
    minx, miny, maxx, maxy = envelope(z, x, y)

    with get_conn() as conn:
        etag = 'W/"{}"'.format(
            hashlib.sha1(  # noqa: S324 — cache-sleutel, geen beveiliging
                f"{_versie(conn)}|{laag}|{z}/{x}/{y}".encode()
            ).hexdigest()
        )
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})

        if niveau is not None:
            _eis_generalisatie(conn, LAGEN[laag]["generalisatie"])

        with conn.cursor() as cur:
            cur.execute(
                _sql(LAGEN[laag], niveau),
                {
                    "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy,
                    "laagnaam": laag,
                },
            )
            rij = cur.fetchone()

    inhoud = bytes(rij["tegel"]) if rij and rij["tegel"] is not None else b""
    return Response(
        content=inhoud,
        media_type=MEDIATYPE,
        headers={
            "ETag": etag,
            # Tegels zijn stabiel tussen twee sync-runs; de ETag vangt de
            # revalidatie af, dus een korte max-age volstaat.
            "Cache-Control": "public, max-age=3600",
            # Voor diagnose in de netwerk-tab: uit welke tabel kwam dit?
            "X-Tegel-Bron": "subdiv" if niveau is None else f"generalisatie-n{niveau}",
        },
    )
