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

Daarnaast een optioneel **filter op identificatie** (`?ids=a,b,c`). De viewer
tekent alleen de locaties van de gekozen plek, maar een gewone tegel bevat alles
in dat stuk kaart. Uitgezoomd is dat het verschil tussen een handvol vlakken en
honderdduizenden: rond Utrecht op z4 is een ongefilterde tegel 3,6 MB gzip
(278.335 vlakken), het hele schermbeeld gefilterd op de ~50 locaties van één
adres 179 kB. Gemeten 2026-11-12, zie docs/plans/vector-tiles.md in de viewer.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response

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


# Tot en met deze zoom haalt een gefilterde tegel eerst de selectie op en knipt
# daarna; daarboven andersom. Gemeten (EXPLAIN BUFFERS, Utrecht, ~49 ids) in
# gelezen pagina's, tegel-eerst / selectie-eerst:
#   z5   109.116 /   906    -- tegel-eerst leest een provincie aan vlakken
#   z7     2.727 / 1.058
#   z9       612 / 1.272    -- selectie-eerst leest elk stukje van elk
#   z11      910 / 6.261       provinciaal object, door de hele provincie
SELECTIE_EERST_TOT_ZOOM = 8


# ── Randen (`?rand=1`) ────────────────────────────────────────────────
#
# Een tweede MVT-laag `<laag>_rand` met de omtrek als lijnen. Niet de rand om
# elk opgeslagen vlak: de Ow-locaties staan als ST_Subdivide-stukjes in de
# database, dus dat tekent het hele knipraster mee (en de tegel knipt elk vlak
# nog eens af op de tegelgrens). Drie manieren, naar wat de bron toelaat:
#
#   tabel        Ow z0-z10: p2p.locatie_rand, de vooraf vereenvoudigde omtrek
#                van de héle locatie. Live samenvoegen kostte op z5 7,7 s per
#                schermbeeld, dit is een gewone indexlookup.
#   samenvoegen  Ow z11+: de stukjes per object in deze tegel samenvoegen en
#                daarvan de omtrek nemen. Op die zoom zijn het er weinig: +0,2 s
#                per schermbeeld. Stukjes die net buiten de tegel liggen doen mee
#                als hun bbox de tegel raakt, en hun naden vallen buiten de tegel
#                en worden weggeknipt.
#   omtrek       Wro: planobjecten zijn niet opgedeeld, dus ST_Boundary van de
#                rij zelf volstaat.
RAND_TABEL = "p2p.locatie_rand"


def _rand_wijze(laag: str, niveau: int | None) -> str:
    if laag == "planobjecten":
        return "omtrek"
    return "samenvoegen" if niveau is None else "tabel"


def _rand_cte(wijze: str, sleutel: str, van: str, waar: str, niveau: int | None,
              selectie_eerst: bool) -> str:
    """De CTE `rand`. `van`/`waar` zijn die van de vlakkenlaag, zodat omtrek en
    samenvoegen precies dezelfde rijen gebruiken als de vulling."""
    geom = f"ST_AsMVTGeom({{g}}, {{env}}, {MVT_EXTENT}, {MVT_BUFFER}, true)"
    if wijze == "omtrek":
        return f""",
        rand AS (
          SELECT b.{sleutel} AS id, {geom.format(g="ST_Boundary(b.geometrie)", env="env.b")} AS geom
            FROM {van}
           WHERE {waar} AND ST_Dimension(b.geometrie) = 2
        )"""
    if wijze == "samenvoegen":
        # gridSize schakelt OverlayNG met snap-rounding in: robuust tegen de
        # TopologyException die een gewone union op deze stukjes kan geven.
        unie = "ST_Boundary(ST_UnaryUnion(ST_Collect(b.geometrie), %(grid)s))"
        return f""",
        rand AS (
          SELECT b.{sleutel} AS id, {geom.format(g=unie, env="(SELECT b FROM env)")} AS geom
            FROM {van}
           WHERE {waar}
           GROUP BY b.{sleutel}
        )"""
    # tabel -- dezelfde afweging selectie-eerst/envelope-eerst als de vlakken
    if selectie_eerst:
        return f""",
        randsel AS MATERIALIZED (
          SELECT r.identificatie, r.geometrie
            FROM {RAND_TABEL} r
           WHERE r.identificatie = ANY(%(ids)s) AND r.niveau = {niveau}
        ),
        rand AS (
          SELECT r.identificatie AS id, {geom.format(g="r.geometrie", env="env.b")} AS geom
            FROM randsel r, env
           WHERE r.geometrie && env.b
        )"""
    return f""",
        rand AS (
          SELECT r.identificatie AS id, {geom.format(g="r.geometrie", env="env.b")} AS geom
            FROM {RAND_TABEL} r, env
           WHERE r.geometrie && env.b AND r.identificatie = ANY(%(ids)s)
             AND r.niveau = {niveau}
        )"""


def _sql(laag: dict, niveau: int | None, gefilterd: bool = False,
         selectie_eerst: bool = True, rand: str | None = None) -> str:
    """`rand`: None = alleen vlakken; anders de wijze uit `_rand_wijze`.
    Randen bestaan alleen bij een gefilterde tegel."""
    tabel = laag["bron"] if niveau is None else laag["generalisatie"]
    niveau_filter = "" if niveau is None else f"AND b.niveau = {niveau}"
    sleutel = laag["sleutel"]
    if gefilterd and not selectie_eerst:
        # Ingezoomd: de envelope is klein, dus eerst de GIST-index en dan op id
        # zeven. Dezelfde pagina's als een ongefilterde tegel.
        bron = ""
        van = f"{tabel} b, env"
        waar = f"b.geometrie && env.b AND b.{sleutel} = ANY(%(ids)s) {niveau_filter}"
    elif gefilterd:
        # Eerst de selectie, dan pas de envelope. MATERIALIZED is hier de kern:
        # zonder die hint mag de planner de CTE inlinen en dan kiezen voor de
        # GIST-index op de envelope. Op lage zoom beslaat die envelope een
        # provincie of heel Nederland, en dan scant hij honderdduizenden rijen om
        # er vijftig over te houden -- gemeten: een schermbeeld liep zo tegen een
        # timeout van 10 minuten, en met de selectie eerst in 0,1-0,6 s.
        bron = f"""
        sel AS MATERIALIZED (
          SELECT b.{sleutel}, b.geometrie
            FROM {tabel} b
           WHERE b.{sleutel} = ANY(%(ids)s) {niveau_filter}
        ),"""
        van, waar = "sel b, env", "b.geometrie && env.b"
    else:
        bron = ""
        van, waar = f"{tabel} b, env", f"b.geometrie && env.b {niveau_filter}"
    kop = f"""
        WITH env AS (
          SELECT ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 28992) AS b
        ),{bron}
        mvt AS (
          SELECT b.{sleutel} AS id,
                 ST_AsMVTGeom(b.geometrie, env.b, {MVT_EXTENT}, {MVT_BUFFER}, true) AS geom
            FROM {van}
           WHERE {waar}
        )"""
    if rand is None or not gefilterd:
        return kop + f"""
        SELECT ST_AsMVT(mvt, %(laagnaam)s, {MVT_EXTENT}, 'geom') AS tegel
          FROM mvt WHERE geom IS NOT NULL
    """  # noqa: S608 — tabel/niveau komen uit LAGEN resp. _niveau_voor, niet uit de request
    # Twee lagen in één tegel: MVT-lagen zijn los te encoderen en achter elkaar
    # te plakken. Lege laag = lege bytea, zodat een tegel zonder randen (of
    # zonder vlakken) gewoon de andere laag draagt.
    return kop + _rand_cte(rand, sleutel, van, waar, niveau, selectie_eerst) + f"""
        SELECT COALESCE((SELECT ST_AsMVT(mvt, %(laagnaam)s, {MVT_EXTENT}, 'geom')
                           FROM mvt WHERE geom IS NOT NULL), ''::bytea)
            || COALESCE((SELECT ST_AsMVT(rand, %(randnaam)s, {MVT_EXTENT}, 'geom')
                           FROM rand WHERE geom IS NOT NULL), ''::bytea) AS tegel
    """  # noqa: S608


# Grenzen aan het filter. Een adres heeft er in de praktijk 40-60 (gemeten op
# vijf punten, 2026-11-12); de bovengrens houdt de URL onder de 16 kB die
# Cloudflare en uvicorn doorlaten, met ruimte voor identificaties van ~50 tekens.
MAX_IDS = 250
MAX_ID_LENGTE = 200


def _ids_uit(ids: str | None) -> list[str] | None:
    """Ontleed `?ids=`. None = geen filter; een lege lijst = filter op niets.

    Gesorteerd en ontdubbeld, zodat dezelfde selectie in een andere volgorde
    dezelfde ETag krijgt."""
    if ids is None:
        return None
    lijst = sorted({i.strip() for i in ids.split(",") if i.strip()})
    if len(lijst) > MAX_IDS:
        raise HTTPException(
            400, f"Te veel identificaties in ids: {len(lijst)} (max {MAX_IDS}).")
    if any(len(i) > MAX_ID_LENGTE for i in lijst):
        raise HTTPException(400, f"Identificatie langer dan {MAX_ID_LENGTE} tekens in ids.")
    return lijst


# Tabellen waarvan we al weten dat ze bestaan. Alleen positieve uitkomsten
# onthouden: staat een tabel er niet, dan blijven we kijken, zodat hij na het
# bouwen meteen meedoet zonder herstart.
_BESTAAT: set[str] = set()


def _bestaat(conn, tabel: str) -> bool:
    """Bestaat deze tabel? Alleen een ja wordt onthouden (zie `_BESTAAT`)."""
    if tabel in _BESTAAT:
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS bestaat", (tabel,))
        rij = cur.fetchone()
    if rij and rij["bestaat"]:
        _BESTAAT.add(tabel)
        return True
    return False


def _eis_generalisatie(conn, tabel: str) -> None:
    """503 als de generalisatietabel ontbreekt.

    Het endpoint kan gedeployed zijn zonder dat de tabellen op die database
    gebouwd zijn — dat was op 2026-08-09 precies de situatie op productie. Dan
    is z11+ gewoon goed (die leest de brontabel) maar loopt elke lagere zoom op
    een 'relation does not exist'. Een 500 met een psycopg-stacktrace vertelt de
    aanroeper niet wat er moet gebeuren; dit wel.
    """
    if _bestaat(conn, tabel):
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
    ids: str | None = Query(
        None,
        description="Komma-gescheiden identificaties. Alleen deze komen in de tegel. "
                    f"Zonder: alles in het tegelgebied. Max {MAX_IDS}.",
    ),
    rand: bool = Query(
        False,
        description="Met ids: ook een laag `<laag>_rand` met de omtrek als lijnen.",
    ),
) -> Response:
    """Eén vector tile. Leeg gebied levert een lege tegel (200, 0 bytes),
    geen 404 — dat is voor een tegelbron een geldig antwoord."""
    if laag not in LAGEN:
        raise HTTPException(404, f"Onbekende laag: {laag}")

    grens = _tegels_op_zoom(z)
    if x >= grens or y >= grens:
        raise HTTPException(404, f"Tegel buiten de RD-piramide op z{z} (max {grens - 1})")

    selectie = _ids_uit(ids)
    niveau = _niveau_voor(z)
    minx, miny, maxx, maxy = envelope(z, x, y)

    with get_conn() as conn:
        # De selectie hoort in de cache-sleutel: zelfde tegel, andere ids is
        # een andere inhoud. Gehasht, want de lijst zelf kan kilobytes zijn.
        filter_sleutel = "" if selectie is None else hashlib.sha1(  # noqa: S324
            ",".join(selectie).encode()).hexdigest()
        # Randen alleen bij een gefilterde tegel: ongefilterd zou het de hele
        # omtrek van alles in beeld zijn, en dat tekent niemand.
        rand_wijze = _rand_wijze(laag, niveau) if rand and selectie is not None else None
        # Ontbreekt de randtabel (een database waar hij nog niet gebouwd is),
        # dan de tegel zónder randen in plaats van een fout: een mislukte tegel
        # laat de viewer de hele tegel-weg opgeven en terugvallen op GeoJSON.
        rand_status = None
        if rand_wijze == "tabel" and not _bestaat(conn, RAND_TABEL):
            rand_wijze, rand_status = None, "ontbreekt"
        etag = 'W/"{}"'.format(
            hashlib.sha1(  # noqa: S324 — cache-sleutel, geen beveiliging
                f"{_versie(conn)}|{laag}|{z}/{x}/{y}|{filter_sleutel}|{rand_wijze}".encode()
            ).hexdigest()
        )
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})

        if niveau is not None:
            _eis_generalisatie(conn, LAGEN[laag]["generalisatie"])

        if selectie == []:
            rij = None  # filter op niets: geen query nodig
        else:
            with conn.cursor() as cur:
                cur.execute(
                    _sql(LAGEN[laag], niveau, gefilterd=selectie is not None,
                         selectie_eerst=z <= SELECTIE_EERST_TOT_ZOOM, rand=rand_wijze),
                    {
                        "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy,
                        "laagnaam": laag, "randnaam": f"{laag}_rand", "ids": selectie,
                        # snap-raster voor het samenvoegen: een kwart pixel
                        "grid": RES_Z0 / 2**z / 4,
                    },
                )
                rij = cur.fetchone()

    inhoud = bytes(rij["tegel"]) if rij and rij["tegel"] is not None else b""
    kop = {
        "ETag": etag,
        # Tegels zijn stabiel tussen twee sync-runs; de ETag vangt de
        # revalidatie af, dus een korte max-age volstaat.
        "Cache-Control": "public, max-age=3600",
        # Voor diagnose in de netwerk-tab: uit welke tabel kwam dit?
        "X-Tegel-Bron": "subdiv" if niveau is None else f"generalisatie-n{niveau}",
    }
    if selectie is not None:
        kop["X-Tegel-Filter"] = str(len(selectie))
    if rand:
        kop["X-Tegel-Rand"] = rand_status or rand_wijze or "geen"
    return Response(content=inhoud, media_type=MEDIATYPE, headers=kop)
