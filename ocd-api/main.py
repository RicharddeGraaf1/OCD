import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse

from db import get_conn, pool
from keywords import router as keywords_router
from ponsenkaart import router as ponsenkaart_router
from regelteksten_bij_vraag import router as regelteksten_router
from vergunningen import router as vergunningen_router

load_dotenv()

logger = logging.getLogger("ocd_api")
logging.basicConfig(
    level=os.environ.get("OCD_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# Twee-keys-strategie:
# - OCD_API_KEY_PUBLIC: zit in client-side HTML van publieke viewers
#   (ponsenkaart.nl, omgevingsvergunningenregister.nl). Bij scraper-misbruik
#   kun je deze invalideren zonder backend-clients te raken.
# - OCD_API_KEY_PRIVATE: voor backend-clients (Omgevingsbot etc.). Komt
#   nooit in browser-code.
# - OCD_API_KEY: legacy single-key, blijft werken als beide nieuwe leeg zijn.
_LEGACY_KEY  = os.environ.get("OCD_API_KEY", "")
_PUBLIC_KEY  = os.environ.get("OCD_API_KEY_PUBLIC", "")
_PRIVATE_KEY = os.environ.get("OCD_API_KEY_PRIVATE", "")

# Dict-mapping zodat we kunnen loggen welke tier een call gebruikte.
ALLOWED_KEYS: dict[str, str] = {}
if _PUBLIC_KEY:  ALLOWED_KEYS[_PUBLIC_KEY]  = "public"
if _PRIVATE_KEY: ALLOWED_KEYS[_PRIVATE_KEY] = "private"
if _LEGACY_KEY and _LEGACY_KEY not in ALLOWED_KEYS:
    ALLOWED_KEYS[_LEGACY_KEY] = "legacy"

# Fail-closed: in productie weigert de container te starten als auth aan
# moet staan maar er zijn geen keys. Lokaal/test kun je dit uit laten (default
# false) zodat tests met lege keys blijven werken.
REQUIRE_AUTH = os.environ.get("OCD_REQUIRE_AUTH", "false").lower() in ("1", "true", "yes")
if REQUIRE_AUTH and not ALLOWED_KEYS:
    raise RuntimeError(
        "OCD_REQUIRE_AUTH=true maar geen OCD_API_KEY_PUBLIC/PRIVATE/OCD_API_KEY "
        "geconfigureerd. Container weigert te starten."
    )

# Swagger/OpenAPI configurable — in productie kun je ze uit zetten.
ENABLE_DOCS = os.environ.get("OCD_ENABLE_DOCS", "true").lower() in ("1", "true", "yes")
ENABLE_OPENAPI = os.environ.get("OCD_ENABLE_OPENAPI", "true").lower() in ("1", "true", "yes")

# Rate limit per IP. v1 gebruikt één globale limit voor alle tiers; per-tier
# differentiatie (public/private) is nice-to-have voor v2 — zie
# PRODUCTION-CHECKLIST.md §4. Overrideable via env-var zonder redeploy.
RATE_DEFAULT = os.environ.get("OCD_RATE_DEFAULT", "120/minute")

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"


def _client_ip(request: Request) -> str:
    """Resolve client-IP achter Railway's proxy. Eerste IP in X-Forwarded-For
    is de origin, fallback op request.client.host."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=_client_ip, default_limits=[RATE_DEFAULT])


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    logger.info(
        "ocd_api startup require_auth=%s keys_configured=%d docs=%s",
        REQUIRE_AUTH, len(ALLOWED_KEYS), ENABLE_DOCS,
    )
    try:
        yield
    finally:
        pool.close()


app = FastAPI(
    title="OCD API",
    description="Omgevingswet Centraal Datamodel — alle regelgeving van Nederland",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_OPENAPI else None,
)

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    logger.warning(
        "rate_limit_exceeded tier=%s ip=%s path=%s limit=%s",
        getattr(request.state, "tier", "anonymous"),
        _client_ip(request),
        request.url.path,
        exc.detail,
    )
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
        "http://localhost:4201",
        "http://localhost:4202",
        # omgevingsvergunning-register.nl viewer (static dev-server)
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8000",
        "http://localhost:8080",
        # ponsenkaart.nl viewer (static dev-server)
        "http://localhost:8766",
        # Productie-domeinen (Hostnet-registratie 2026-05-23)
        "https://ponsenkaart.nl",
        "https://www.ponsenkaart.nl",
        "https://omgevingsvergunningenregister.nl",       # canoniek
        "https://www.omgevingsvergunningenregister.nl",
        "https://omgevingsvergunning-register.nl",        # legacy/typo-redirect
        "https://www.omgevingsvergunning-register.nl",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Api-Key", "Content-Type"],
)

api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


async def verify_key(
    request: Request,
    key: str | None = Security(api_key_header),
) -> str | None:
    """Valideer X-Api-Key. Retourneert de tier ('public'/'private'/'legacy')
    of None als er geen keys geconfigureerd zijn (open access in dev).

    Zet `request.state.tier` zodat logging/middleware weet welke tier de
    call gebruikte. Logt elke geauthenticeerde call op DEBUG-niveau zodat
    je bij scraper-misbruik kunt achterhalen welke key lekt.
    """
    if not ALLOWED_KEYS:
        request.state.tier = "anonymous"
        return None

    tier = ALLOWED_KEYS.get(key or "")
    if tier is None:
        logger.info(
            "auth_fail ip=%s path=%s",
            _client_ip(request), request.url.path,
        )
        raise HTTPException(status_code=403, detail="Invalid API key")

    request.state.tier = tier
    logger.debug(
        "auth_ok tier=%s ip=%s path=%s",
        tier, _client_ip(request), request.url.path,
    )
    return tier


app.include_router(keywords_router, dependencies=[Depends(verify_key)])
app.include_router(regelteksten_router, dependencies=[Depends(verify_key)])
app.include_router(vergunningen_router, dependencies=[Depends(verify_key)])
app.include_router(ponsenkaart_router, dependencies=[Depends(verify_key)])


@app.get("/health")
def health():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
    return {"status": "ok"}


def _build_keyword_filter(keywords: list[str], text_col: str) -> tuple[str, list]:
    """Build a SQL WHERE clause that matches any keyword in a text column.

    Uses ILIKE for case-insensitive matching. Returns (clause, params).
    """
    if not keywords:
        return "", []
    conditions = [f"{text_col} ILIKE %s" for _ in keywords]
    params = [f"%{kw}%" for kw in keywords]
    return f"AND ({' OR '.join(conditions)})", params


def _build_fts_query(keywords: list[str]) -> str | None:
    """Build a PostgreSQL tsquery string from keywords (OR-joined)."""
    if not keywords:
        return None
    safe = [kw.replace("'", "''") for kw in keywords if kw.strip()]
    return " | ".join(f"'{kw}'" for kw in safe) if safe else None


def _wat_geldt_hier(x: float, y: float, zoektermen: list[str] | None = None):
    """Hybrid query: activiteit-based + per-regeling enrichment.

    1. Activiteit-query: find regels via activiteiten on this location (existing)
    2. Enrichment: for the local omgevingsplan, also search ALL tekst_elementen
       by opschrift (plain text, not XML) — finds articles the activiteit-join misses
    3. Visie + WRO queries as before
    """
    kw = zoektermen or []

    with get_conn() as conn, conn.cursor() as cur:
        # ── Query 1: activiteit-based (existing, proven) ──
        # Provinciale Omgevingsverordeningen en N2000-aanwijzingsbesluiten
        # ontsnappen aan het keyword-filter: hun activiteit-namen + artikel-
        # teksten matchen zelden de leek-zoektermen ("damherten", "wateroverlast"),
        # waardoor relevante regels ten onrechte werden uitgesloten.
        kw_filter, kw_params = _build_keyword_filter(kw, "te.inhoud")
        act_filter, act_params = _build_keyword_filter(kw, "a.naam")
        if kw_filter and act_filter:
            combined_filter = (
                f"AND (({kw_filter[4:]}) OR ({act_filter[4:]}) "
                f"OR r.documenttype IN ('Omgevingsverordening', 'Aanwijzingsbesluit N2000'))"
            )
            combined_params = kw_params + act_params
        else:
            combined_filter = ""
            combined_params = []

        cur.execute(
            f"""
            SELECT r.opschrift AS regeling, r.documenttype,
                   ocd_artikel_label(te.opschrift, te.wid) AS artikel, te.inhoud,
                   string_agg(DISTINCT a.naam, ' | ') AS activiteit,
                   string_agg(DISTINCT ala.kwalificatie, ' | ') AS kwalificatie
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
            WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            {combined_filter}
            GROUP BY r.opschrift, r.documenttype, te.opschrift, te.wid, te.inhoud
            """,
            (x, y, *combined_params),
        )
        ow = cur.fetchall()

        # ── Query 2: enrichment per local regeling (opschrift search) ──
        # Find which regelingen are at this location
        if kw:
            cur.execute(
                """
                SELECT DISTINCT r.frbr_work, r.opschrift, r.documenttype, r.bronhouder
                FROM p2p.activiteit_locatieaanduiding ala
                JOIN p2p.locatie_subdiv ls ON ls.identificatie = ala.locatie_id
                JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
                JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
                JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
                WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                  AND r.documenttype IN ('Omgevingsplan', 'Waterschapsverordening', 'Omgevingsverordening')
                """,
                (x, y),
            )
            local_regs = cur.fetchall()

            # For top 3 local regelingen, search tekst_elementen by opschrift + FTS
            # Join via frbr_work (version-independent) to handle expression mismatches
            opschrift_filter, opschrift_params = _build_keyword_filter(kw, "te.opschrift")
            fts_query = _build_fts_query(kw)
            seen_wids = {r["artikel"] for r in ow if r.get("artikel")}

            for reg in local_regs[:3]:
                work = reg["frbr_work"]

                # A) Opschrift ILIKE (precise article title match)
                if opschrift_filter:
                    cur.execute(
                        f"""
                        SELECT r.opschrift AS regeling, r.documenttype,
                               ocd_artikel_label(te.opschrift, te.wid) AS artikel, te.inhoud
                        FROM p2p.tekst_element te
                        JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
                        WHERE r.frbr_work = %s
                          AND te.inhoud IS NOT NULL AND length(te.inhoud) > 30
                        {opschrift_filter}
                        ORDER BY length(te.inhoud) DESC
                        LIMIT 15
                        """,
                        (work, *opschrift_params),
                    )
                    for row in cur.fetchall():
                        if row["artikel"] not in seen_wids:
                            seen_wids.add(row["artikel"])
                            ow.append(row)

                # B) FTS on inhoud_plain (ranked, finds content matches)
                if fts_query:
                    cur.execute(
                        """
                        SELECT r.opschrift AS regeling, r.documenttype,
                               ocd_artikel_label(te.opschrift, te.wid) AS artikel, te.inhoud,
                               ts_rank(
                                 to_tsvector('dutch', coalesce(te.inhoud_plain, '')),
                                 to_tsquery('dutch', %s)
                               ) AS fts_rank
                        FROM p2p.tekst_element te
                        JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
                        WHERE r.frbr_work = %s
                          AND te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > 30
                          AND to_tsvector('dutch', coalesce(te.inhoud_plain, ''))
                              @@ to_tsquery('dutch', %s)
                        ORDER BY fts_rank DESC
                        LIMIT 10
                        """,
                        (fts_query, work, fts_query),
                    )
                    for row in cur.fetchall():
                        if row["artikel"] not in seen_wids:
                            seen_wids.add(row["artikel"])
                            ow.append(row)

        # ── Query 3: Visie/Programma teksten ──
        visie_kw_filter, visie_kw_params = _build_keyword_filter(kw, "te.inhoud")
        opschrift_visie_filter, opschrift_visie_params = _build_keyword_filter(kw, "te.opschrift")
        if visie_kw_filter and opschrift_visie_filter:
            visie_text_filter = f"AND (({visie_kw_filter[4:]}) OR ({opschrift_visie_filter[4:]}))"
            visie_text_params = visie_kw_params + opschrift_visie_params
        elif visie_kw_filter:
            visie_text_filter = visie_kw_filter
            visie_text_params = visie_kw_params
        else:
            visie_text_filter = ""
            visie_text_params = []

        # Twee paden naar relevantie:
        #   A) bronhouder voert ook een Omgevingsplan op deze coords (gemeenten)
        #   B) regelingsgebied van de visie/programma bevat zelf de coords
        #      (landelijke + provinciale Programma's zoals PIRM, NOVI,
        #      Natuurbeheerplan — die hebben geen Omgevingsplan-bronhouder)
        cur.execute(
            f"""
            SELECT r.opschrift AS regeling, r.documenttype,
                   ocd_artikel_label(te.opschrift, te.wid) AS artikel, te.inhoud
            FROM p2p.tekst_element te
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE r.documenttype IN ('Omgevingsvisie', 'Programma')
              AND (
                r.bronhouder IN (
                    SELECT DISTINCT r2.bronhouder
                    FROM p2p.activiteit_locatieaanduiding ala2
                    JOIN p2p.locatie l2 ON l2.identificatie = ala2.locatie_id
                    JOIN p2p.juridische_regel jr2 ON jr2.identificatie = ala2.juridische_regel_id
                    JOIN p2p.tekst_element te2 ON te2.wid = jr2.regeltekst_wid
                    JOIN p2p.regeling r2 ON r2.frbr_expression = te2.regeling_expression
                    WHERE ST_Intersects(l2.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                      AND r2.documenttype = 'Omgevingsplan'
                )
                OR r.regelingsgebied_id IN (
                    SELECT identificatie FROM p2p.locatie
                    WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                )
              )
              AND te.inhoud IS NOT NULL AND length(te.inhoud) > 50
            {visie_text_filter}
            LIMIT 50
            """,
            (x, y, x, y, *visie_text_params),
        )
        visies = cur.fetchall()

        # ── Query 4: Wro-bestemmingen ──
        # V6.10 fix: GEEN keyword-filter meer op wro_bestemmingen. Wro-data is
        # vaak metadata-only (Maatvoering, Gebiedsaanduiding, Bouwvlak) zonder
        # tekstinhoud waar zoektermen tegen kunnen matchen. Een houtzagerij-
        # vraag op een BP-locatie moet de Maatvoering "max bouwhoogte /
        # bebouwingspercentage" altijd kunnen zien, ook als het woord
        # "houtzagerij" niet voorkomt in de bouwregel-tekst. Geometric-only
        # filter; downstream ranking + LLM doen de inhoudelijke selectie.
        cur.execute(
            """
            SELECT ri.naam AS plan, po.object_type, po.naam AS bestemming,
                   po.bestemmingshoofdgroep,
                   string_agg(DISTINCT wt.inhoud, ' ') FILTER (WHERE wt.inhoud IS NOT NULL) AS inhoud
            FROM wro.planobject po
            JOIN wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
            LEFT JOIN wro.wro_tekst_object wt ON wt.instrument_idn = po.instrument_idn
            WHERE ST_Intersects(po.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            GROUP BY ri.naam, po.object_type, po.naam, po.bestemmingshoofdgroep
            """,
            (x, y),
        )
        wro = cur.fetchall()

    return {"ow_regels": ow, "wro_bestemmingen": wro, "visies": visies}


@app.get("/v1/adres", dependencies=[Depends(verify_key)])
def adres(
    q: str = Query(..., description="Adres (bijv. 'Prinsengracht 263, Amsterdam')"),
    zoektermen: str = Query("", description="Komma-gescheiden zoektermen voor server-side filtering"),
):
    """Wat geldt op een adres? Cross-regime: Ow-regels + Wro-bestemmingen.

    Wanneer zoektermen meegegeven worden, filtert de API server-side op
    relevante regelteksten. Zonder zoektermen worden alle regels geretourneerd.
    """
    resp = httpx.get(
        LOCATIESERVER,
        params={"q": q, "rows": 1, "fq": "type:adres"},
        timeout=10,
    )
    docs = resp.json().get("response", {}).get("docs", [])
    if not docs:
        raise HTTPException(404, "Adres niet gevonden")
    doc = docs[0]
    coords = doc["centroide_rd"].replace("POINT(", "").replace(")", "").split()
    x, y = float(coords[0]), float(coords[1])
    kw_list = [kw.strip() for kw in zoektermen.split(",") if kw.strip()] if zoektermen else None
    return {
        "adres": doc.get("weergavenaam", q),
        "rd": {"x": x, "y": y},
        **_wat_geldt_hier(x, y, zoektermen=kw_list),
    }


@app.get("/v1/locatie", dependencies=[Depends(verify_key)])
def locatie(
    x: float = Query(...),
    y: float = Query(...),
    zoektermen: str = Query("", description="Komma-gescheiden zoektermen"),
):
    """Wat geldt op RD-coordinaten?"""
    kw_list = [kw.strip() for kw in zoektermen.split(",") if kw.strip()] if zoektermen else None
    return _wat_geldt_hier(x, y, zoektermen=kw_list)


@app.get("/v1/zoek", dependencies=[Depends(verify_key)])
def zoek(q: str = Query(..., min_length=2), limit: int = Query(20, le=100)):
    """Full-text ILIKE zoek over Ow + Wro teksten."""
    pattern = f"%{q}%"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            (SELECT 'Ow' AS regime,
                    r.opschrift AS document,
                    ocd_artikel_label(te.opschrift, te.wid) AS artikel,
                    LEFT(te.inhoud_plain, 500) AS tekst
             FROM p2p.tekst_element te
             JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
             WHERE te.inhoud_plain ILIKE %s
             LIMIT %s)
            UNION ALL
            (SELECT 'Wro',
                    ri.naam,
                    wt.naam,
                    LEFT(wt.inhoud, 500)
             FROM wro.wro_tekst_object wt
             JOIN wro.ruimtelijk_instrument ri ON ri.idn = wt.instrument_idn
             WHERE wt.inhoud ILIKE %s
             LIMIT %s)
            """,
            (pattern, limit, pattern, limit),
        )
        return {"zoekterm": q, "resultaten": cur.fetchall()}


@app.get("/v1/gemeente/{code}/activiteiten", dependencies=[Depends(verify_key)])
def activiteiten(code: str):
    """Alle activiteiten van een gemeente (match op `gm{code}` in identificatie)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT a.naam, a.groep, ala.kwalificatie
            FROM p2p.activiteit a
            JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
            WHERE a.identificatie LIKE %s
            ORDER BY a.naam
            """,
            (f"%gm{code}%",),
        )
        return {"gemeente": code, "activiteiten": cur.fetchall()}


@app.get("/v1/gemeente/{code}/normen", dependencies=[Depends(verify_key)])
def normen(code: str):
    """Alle omgevingsnormen van een gemeente."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT n.identificatie, n.naam, n.type_norm, n.eenheid, n.groep,
                   count(nw.id) AS aantal_waarden
            FROM p2p.norm n
            JOIN p2p.normwaarde nw ON nw.norm_id = n.identificatie
            WHERE n.identificatie LIKE %s
            GROUP BY n.identificatie
            ORDER BY n.naam
            """,
            (f"%gm{code}%",),
        )
        return {"gemeente": code, "normen": cur.fetchall()}


@app.get("/v1/normwaarde", dependencies=[Depends(verify_key)])
def normwaarde(
    x: float = Query(..., description="RD x-coordinaat (EPSG:28992)"),
    y: float = Query(..., description="RD y-coordinaat (EPSG:28992)"),
    naam: str | None = Query(None, min_length=2, description="Detector-pad: substring-match op norm.naam, bv. 'bouwhoogte'"),
    zoektermen: list[str] | None = Query(None, description="Keyword-pad: brede OR-match op naam OF groep (repeated param)"),
    limit_detector: int = Query(5, le=100, description="Max hits in detector-bucket"),
    limit_keyword: int = Query(15, le=100, description="Max hits in keyword-bucket"),
):
    """Vraag-gestuurd: geef normwaarden op (x,y), gefilterd via twee buckets.

    - **Detector-bucket** (`naam`): exacte substring-match op `norm.naam` —
      hoge precisie, levert de hits die de bot-detector specifiek zocht.
    - **Keyword-bucket** (`zoektermen`): brede match op `norm.naam` OF
      `norm.groep` — vangnet wanneer de detector mist of breder begrip nodig is.
    - Beide tegelijk: detector-hits eerst (preferred bucket), keyword-hits
      eronder. Rij die in beide buckets matcht telt als `detector`.
    - Geen van beide: 400 (één van de twee is verplicht).

    Backward-compat: aanroep met enkel `naam=...` gedraagt zich identiek
    aan de oude API (substring-match, gesorteerd op waarde).
    """
    if not naam and not zoektermen:
        raise HTTPException(status_code=400, detail="Geef minimaal 'naam' of 'zoektermen' op.")

    naam_pattern = f"%{naam}%" if naam else None
    zoektermen_patterns = [f"%{kw}%" for kw in zoektermen] if zoektermen else None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH normwaarden_op_locatie AS (
                SELECT  n.identificatie                     AS norm_id,
                        n.naam                              AS norm_naam,
                        n.type_norm,
                        n.eenheid,
                        n.groep                             AS norm_groep,
                        nw.kwantitatieve_waarde,
                        nw.kwalitatieve_waarde,
                        l.identificatie                     AS locatie_id,
                        l.noemer                            AS locatie_naam,
                        l.locatie_type,
                        r.opschrift                         AS regeling,
                        r.frbr_expression,
                        -- V6.19: artikel via ocd_artikel_label() — opschrift indien gevuld,
                        -- anders 'Artikel X.Y' uit wid. Zie
                        -- dso-loader/scripts/2026-05-add-ocd-artikel-label-fn.sql.
                        ocd_artikel_label(te.opschrift, te.wid)                                   AS artikel,
                        te.wid                              AS artikel_wid,
                        LEFT(te.inhoud_plain, 800)          AS regeltekst_excerpt
                FROM    p2p.normwaarde                  nw
                JOIN    p2p.norm                        n   ON n.identificatie  = nw.norm_id
                JOIN    p2p.locatie                     l   ON l.identificatie  = nw.locatie_id
                LEFT JOIN p2p.juridische_regel_norm     jrn ON jrn.norm_id      = n.identificatie
                LEFT JOIN p2p.juridische_regel          jr  ON jr.identificatie = jrn.juridische_regel_id
                LEFT JOIN p2p.tekst_element             te  ON te.wid           = jr.regeltekst_wid
                LEFT JOIN p2p.regeling                  r   ON r.frbr_expression = te.regeling_expression
                WHERE   ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            ),
            bucketed AS (
                SELECT *,
                       CASE
                           WHEN %s::text IS NOT NULL AND norm_naam ILIKE %s
                               THEN 'detector'
                           WHEN %s::text[] IS NOT NULL
                                AND (norm_naam  ILIKE ANY(%s::text[])
                                     OR norm_groep ILIKE ANY(%s::text[]))
                               THEN 'keyword'
                           ELSE NULL
                       END AS match_bucket
                FROM normwaarden_op_locatie
            ),
            ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY match_bucket
                           ORDER BY kwantitatieve_waarde DESC NULLS LAST,
                                    norm_naam, locatie_id
                       ) AS rn
                FROM   bucketed
                WHERE  match_bucket IS NOT NULL
            )
            SELECT *
            FROM   ranked
            WHERE  (match_bucket = 'detector' AND rn <= %s)
               OR  (match_bucket = 'keyword'  AND rn <= %s)
            ORDER BY CASE match_bucket WHEN 'detector' THEN 0 ELSE 1 END,
                     kwantitatieve_waarde DESC NULLS LAST,
                     norm_naam, locatie_id
            """,
            (x, y,
             naam, naam_pattern,
             zoektermen_patterns, zoektermen_patterns, zoektermen_patterns,
             limit_detector, limit_keyword),
        )
        rows = cur.fetchall()
    for r in rows:
        r.pop("rn", None)
    count_detector = sum(1 for r in rows if r.get("match_bucket") == "detector")
    count_keyword  = sum(1 for r in rows if r.get("match_bucket") == "keyword")
    return {
        "x": x,
        "y": y,
        "naam_query": naam,
        "zoektermen_query": zoektermen,
        "count": len(rows),
        "count_detector": count_detector,
        "count_keyword": count_keyword,
        "matches": rows,
    }


@app.get("/v1/bestemming", dependencies=[Depends(verify_key)])
def bestemming(
    x: float = Query(..., description="RD x-coordinaat (EPSG:28992)"),
    y: float = Query(..., description="RD y-coordinaat (EPSG:28992)"),
    zoektermen: list[str] | None = Query(None, description="Optioneel keyword-pad: OR-match op planobject naam/hoofdgroep"),
    limit: int = Query(20, le=100),
):
    """Vraag-gestuurd: gestructureerde bestemmingen op (x,y) uit wro.planobject.

    Retourneert {bestemmingen[], dubbelbestemmingen[], gebiedsaanduidingen[]}
    met naam, hoofdgroep, regeling en artikelnummer per object. Bot kan
    direct de bestemmingsnaam (bv. 'Centrum-1', 'Dienstverlening') gebruiken
    in zijn antwoord, zonder LLM-extractie uit een blob aan tekstfragmenten.

    Ook lege response (count=0) is een eersterangs antwoord: deze locatie
    valt buiten elk planobject in de wro-laag (geen BP-bestemming hier).

    Optionele `zoektermen` (repeated param): filter de set planobjecten op
    `naam`, `bestemmingshoofdgroep` of `gebiedsaanduidinghoofdgroep` —
    handig wanneer een Wro-locatie meerdere bestemmingen heeft en je alleen
    de inhoudelijk relevante wil. Bestemmingen hebben geen detector-pad,
    dus alle resultaten krijgen `match_bucket='keyword'` (of `null` zonder
    filter).

    Backward-compat: aanroep zonder `zoektermen` gedraagt zich identiek
    aan de oude API.
    """
    zoektermen_patterns = [f"%{kw}%" for kw in zoektermen] if zoektermen else None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT  po.identificatie                    AS planobject_id,
                    po.object_type,
                    po.naam                             AS bestemming_naam,
                    po.bestemmingshoofdgroep            AS hoofdgroep,
                    po.artikelnummer,
                    po.gebiedsaanduidinghoofdgroep,
                    ri.idn                              AS instrument_idn,
                    ri.naam                             AS regeling_naam,
                    ri.type_plan,
                    ri.datum                            AS regeling_datum,
                    ri.bronhouder,
                    CASE WHEN %s::text[] IS NOT NULL THEN 'keyword' ELSE NULL END
                                                        AS match_bucket
            FROM    wro.planobject              po
            JOIN    wro.ruimtelijk_instrument   ri  ON ri.idn  = po.instrument_idn
            WHERE   ST_Intersects(po.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
              AND   LOWER(po.object_type) IN ('enkelbestemming', 'dubbelbestemming', 'gebiedsaanduiding', 'functieaanduiding')
              AND   ri.pons_status = 'actief'
              AND   (%s::text[] IS NULL
                     OR po.naam                          ILIKE ANY(%s::text[])
                     OR po.bestemmingshoofdgroep         ILIKE ANY(%s::text[])
                     OR po.gebiedsaanduidinghoofdgroep   ILIKE ANY(%s::text[]))
            ORDER BY
                    CASE LOWER(po.object_type)
                        WHEN 'enkelbestemming'    THEN 1
                        WHEN 'dubbelbestemming'   THEN 2
                        WHEN 'functieaanduiding'  THEN 3
                        WHEN 'gebiedsaanduiding'  THEN 4
                    END,
                    ri.datum DESC NULLS LAST
            LIMIT %s
            """,
            (zoektermen_patterns,
             x, y,
             zoektermen_patterns, zoektermen_patterns, zoektermen_patterns, zoektermen_patterns,
             limit),
        )
        rows = cur.fetchall()
    def _is(t: str, target: str) -> bool:
        return (t or "").lower() == target
    enkel  = [r for r in rows if _is(r["object_type"], "enkelbestemming")]
    dubbel = [r for r in rows if _is(r["object_type"], "dubbelbestemming")]
    functie = [r for r in rows if _is(r["object_type"], "functieaanduiding")]
    gebied = [r for r in rows if _is(r["object_type"], "gebiedsaanduiding")]
    count_keyword = sum(1 for r in rows if r.get("match_bucket") == "keyword")
    return {
        "x": x,
        "y": y,
        "zoektermen_query": zoektermen,
        "regime": "RO" if rows else None,
        "count": len(rows),
        "count_detector": 0,
        "count_keyword": count_keyword,
        "bestemmingen": enkel,
        "dubbelbestemmingen": dubbel,
        "functieaanduidingen": functie,
        "gebiedsaanduidingen": gebied,
    }


@app.get("/v1/activiteit", dependencies=[Depends(verify_key)])
def activiteit(
    x: float = Query(..., description="RD x-coordinaat (EPSG:28992)"),
    y: float = Query(..., description="RD y-coordinaat (EPSG:28992)"),
    soort: str | None = Query(None, min_length=2, description="Detector-pad: substring-match op activiteit.naam, bv. 'winkel'"),
    zoektermen: list[str] | None = Query(None, description="Keyword-pad: brede OR-match op naam OF groep (repeated param)"),
    limit_detector: int = Query(5, le=100, description="Max hits in detector-bucket"),
    limit_keyword: int = Query(15, le=100, description="Max hits in keyword-bucket"),
):
    """Vraag-gestuurd: 'mag ik hier een [soort]?' → structured kwalificatie.

    Retourneert {count, matches[]} met activiteit-naam, kwalificatie
    (toegestaan/verboden/vergunningplicht/meldingsplicht), regeling, artikel
    en regeltekst-excerpt. Bot kan bij count>0 direct formuleren zonder
    LLM-tekstextractie.

    Werkt alleen voor OW (`p2p.activiteit_locatieaanduiding`); voor RO/BP
    zit de activiteit-toets als vrije tekst in `wro.wro_tekst_object`.

    Twee filter-buckets met dezelfde semantiek als `/v1/normwaarde`:
    - **Detector-bucket** (`soort`): substring op `activiteit.naam`.
    - **Keyword-bucket** (`zoektermen`): OR op `activiteit.naam` OF `groep`.
    - Beide tegelijk: detector-hits eerst.
    - Geen van beide: 400.

    Backward-compat: aanroep met enkel `soort=...` gedraagt zich identiek
    aan de oude API.
    """
    if not soort and not zoektermen:
        raise HTTPException(status_code=400, detail="Geef minimaal 'soort' of 'zoektermen' op.")

    soort_pattern = f"%{soort}%" if soort else None
    zoektermen_patterns = [f"%{kw}%" for kw in zoektermen] if zoektermen else None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH activiteiten_op_locatie AS (
                SELECT  a.identificatie                         AS activiteit_id,
                        a.naam                                  AS activiteit_naam,
                        a.groep                                 AS activiteit_groep,
                        ala.kwalificatie,
                        l.identificatie                         AS locatie_id,
                        l.noemer                                AS locatie_naam,
                        l.locatie_type,
                        r.opschrift                             AS regeling,
                        r.frbr_expression,
                        ocd_artikel_label(te.opschrift, te.wid)                            AS artikel,
                        te.wid                                  AS artikel_wid,
                        LEFT(te.inhoud_plain, 800)              AS regeltekst_excerpt
                FROM    p2p.activiteit_locatieaanduiding ala
                JOIN    p2p.activiteit                   a    ON a.identificatie  = ala.activiteit_id
                JOIN    p2p.locatie                     l    ON l.identificatie  = ala.locatie_id
                JOIN    p2p.juridische_regel            jr   ON jr.identificatie = ala.juridische_regel_id
                LEFT JOIN p2p.tekst_element             te   ON te.wid           = jr.regeltekst_wid
                LEFT JOIN p2p.regeling                  r    ON r.frbr_expression = te.regeling_expression
                WHERE   ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            ),
            bucketed AS (
                SELECT *,
                       CASE
                           WHEN %s::text IS NOT NULL AND activiteit_naam ILIKE %s
                               THEN 'detector'
                           WHEN %s::text[] IS NOT NULL
                                AND (activiteit_naam  ILIKE ANY(%s::text[])
                                     OR activiteit_groep ILIKE ANY(%s::text[]))
                               THEN 'keyword'
                           ELSE NULL
                       END AS match_bucket
                FROM activiteiten_op_locatie
            ),
            ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY match_bucket
                           ORDER BY
                               CASE LOWER(COALESCE(kwalificatie, ''))
                                   WHEN 'verboden'         THEN 1
                                   WHEN 'vergunningplicht' THEN 2
                                   WHEN 'meldingsplicht'   THEN 3
                                   WHEN 'toegestaan'       THEN 4
                                   ELSE                          5
                               END,
                               activiteit_naam
                       ) AS rn
                FROM   bucketed
                WHERE  match_bucket IS NOT NULL
            )
            SELECT *
            FROM   ranked
            WHERE  (match_bucket = 'detector' AND rn <= %s)
               OR  (match_bucket = 'keyword'  AND rn <= %s)
            ORDER BY CASE match_bucket WHEN 'detector' THEN 0 ELSE 1 END,
                     CASE LOWER(COALESCE(kwalificatie, ''))
                         WHEN 'verboden'         THEN 1
                         WHEN 'vergunningplicht' THEN 2
                         WHEN 'meldingsplicht'   THEN 3
                         WHEN 'toegestaan'       THEN 4
                         ELSE                          5
                     END,
                     activiteit_naam
            """,
            (x, y,
             soort, soort_pattern,
             zoektermen_patterns, zoektermen_patterns, zoektermen_patterns,
             limit_detector, limit_keyword),
        )
        rows = cur.fetchall()
    for r in rows:
        r.pop("rn", None)
    count_detector = sum(1 for r in rows if r.get("match_bucket") == "detector")
    count_keyword  = sum(1 for r in rows if r.get("match_bucket") == "keyword")
    return {
        "x": x,
        "y": y,
        "soort_query": soort,
        "zoektermen_query": zoektermen,
        "count": len(rows),
        "count_detector": count_detector,
        "count_keyword": count_keyword,
        "matches": rows,
    }


@app.get("/v1/coverage", dependencies=[Depends(verify_key)])
def coverage(
    x: float = Query(..., description="RD x-coordinaat (EPSG:28992)"),
    y: float = Query(..., description="RD y-coordinaat (EPSG:28992)"),
    onderwerp: str = Query(None, min_length=2, description="Optioneel: filter op onderwerp"),
):
    """Vraag-gestuurd: kort antwoord op 'is hier überhaupt iets geregeld?'

    Retourneert {has_rules, ow_rules, ro_planobjecten, ow_gebiedsaanwijzingen}
    zodat de bot deterministisch 'geen regel hier' kan zeggen i.p.v.
    impliciet fall-through. Bij `onderwerp` filter ILIKE op naam-velden.
    """
    pat = f"%{onderwerp}%" if onderwerp else None
    with get_conn() as conn, conn.cursor() as cur:
        # Ow-regels (juridische_regel via locatie geo-intersect, optioneel onderwerp-filter)
        cur.execute(
            """
            SELECT COUNT(DISTINCT jr.identificatie) AS n
            FROM   p2p.juridische_regel jr
            LEFT JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
            LEFT JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
            LEFT JOIN p2p.juridische_regel_norm jrn ON jrn.juridische_regel_id = jr.identificatie
            LEFT JOIN p2p.norm n ON n.identificatie = jrn.norm_id
            LEFT JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.juridische_regel_id = jr.identificatie
            LEFT JOIN p2p.gebiedsaanwijzing ga ON ga.identificatie = jrg.gebiedsaanwijzing_id
            JOIN p2p.locatie l
                ON l.identificatie IN (ala.locatie_id, n.identificatie, ga.locatie_id)
            WHERE ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
              AND (%s::text IS NULL
                   OR a.naam ILIKE %s OR n.naam ILIKE %s OR ga.naam ILIKE %s)
            """,
            (x, y, onderwerp, pat, pat, pat),
        )
        ow_count = cur.fetchone()["n"] or 0

        # RO-planobjecten op geo-intersect
        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM   wro.planobject po
            JOIN   wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
            WHERE  ST_Intersects(po.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
              AND  ri.pons_status = 'actief'
              AND  (%s::text IS NULL OR po.naam ILIKE %s)
            """,
            (x, y, onderwerp, pat),
        )
        ro_count = cur.fetchone()["n"] or 0
    return {
        "x": x,
        "y": y,
        "onderwerp": onderwerp,
        "has_rules": (ow_count + ro_count) > 0,
        "ow_rules": ow_count,
        "ro_planobjecten": ro_count,
        "total": ow_count + ro_count,
    }


@app.get("/v1/onderwerp", dependencies=[Depends(verify_key)])
def onderwerp(
    x: float = Query(..., description="RD x-coordinaat (EPSG:28992)"),
    y: float = Query(..., description="RD y-coordinaat (EPSG:28992)"),
    q: str = Query(..., min_length=2, description="Komma-gescheiden zoektermen (bv. 'aalscholver,beschermd')"),
    limit: int = Query(20, le=100),
):
    """Topic-narrow: gebiedsaanwijzingen op (x,y) waarvan naam/groep/type
    matched met de keywords. Equivalent van DSO's 'Relevante onderwerpen voor
    de vraag' stap.

    Lost het grote-corpus-ranking-probleem op: bij een vraag over 'aalscholver
    beschermd' op een N2000-locatie kan de bot deze onderwerp-namen
    ('Vogelrichtlijngebied Alde Feanen' etc.) als extra zoektermen gebruiken
    in de bestaande tekst-rank, zodat regels mét die termen in de top-10
    belanden (i.p.v. tussen 100+ andere regels te verdwijnen).

    Retourneert {x, y, q, count, gebiedsaanwijzingen[]}. Per gebiedsaanwijzing:
    naam, type, groep, n_regels, match_veld.

    Note: de directe koppeling naar regelteksten is in de OCD-data ambigu
    (juridische_regel.regeltekst_wid is niet uniek over regelingen). De
    aanbevolen aanpak is daarom topic-naam-injectie als extra zoekterm,
    niet directe regeltekst-boost.
    """
    keywords = [k.strip() for k in q.split(",") if k.strip()]
    if not keywords:
        return {"x": x, "y": y, "count": 0, "gebiedsaanwijzingen": []}
    patterns = [f"%{k}%" for k in keywords]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT  g.identificatie                       AS gebiedsaanwijzing_id,
                    g.naam                                AS onderwerp_naam,
                    g.type                                AS onderwerp_type,
                    g.groep                               AS onderwerp_groep,
                    COUNT(jrg.juridische_regel_id)        AS n_regels,
                    CASE
                        WHEN g.naam  ILIKE ANY(%s) THEN 'naam'
                        WHEN g.groep ILIKE ANY(%s) THEN 'groep'
                        WHEN g.type  ILIKE ANY(%s) THEN 'type'
                        ELSE 'overig'
                    END                                   AS match_veld
            FROM    p2p.gebiedsaanwijzing            g
            JOIN    p2p.locatie                      l   ON l.identificatie = g.locatie_id
            LEFT JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.gebiedsaanwijzing_id = g.identificatie
            WHERE   ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
              AND   (g.naam ILIKE ANY(%s) OR g.groep ILIKE ANY(%s) OR g.type ILIKE ANY(%s))
            GROUP BY g.identificatie, g.naam, g.type, g.groep
            ORDER BY
                    CASE
                        WHEN g.naam  ILIKE ANY(%s) THEN 1
                        WHEN g.groep ILIKE ANY(%s) THEN 2
                        WHEN g.type  ILIKE ANY(%s) THEN 3
                        ELSE 4
                    END,
                    g.type, g.naam
            LIMIT %s
            """,
            (
                patterns, patterns, patterns,            # CASE labels in SELECT
                x, y,                                    # ST_Intersects
                patterns, patterns, patterns,            # WHERE OR-clause
                patterns, patterns, patterns,            # ORDER BY
                limit,
            ),
        )
        rows = cur.fetchall()
    return {
        "x": x,
        "y": y,
        "q": keywords,
        "count": len(rows),
        "gebiedsaanwijzingen": rows,
    }


@app.get("/v1/regeltekst", dependencies=[Depends(verify_key)])
def regeltekst(
    x: float = Query(..., description="RD x-coordinaat (EPSG:28992)"),
    y: float = Query(..., description="RD y-coordinaat (EPSG:28992)"),
    q: str = Query(..., min_length=2, description="Komma-gescheiden zoektermen (bv. 'aalscholver,beschermd')"),
    limit: int = Query(10, le=50),
):
    """Tekst-zoek: juridische regels op (x, y) waarvan de regeltekst-inhoud
    matched met de keywords (PostgreSQL FTS, dutch ts_config), gerankt op
    relevance.

    Complementair aan `/v1/onderwerp` (naam-matching op gebiedsaanwijzing) en
    `/v1/activiteit` (naam-matching op activiteit). Lost cases op waar het
    relevante concept *in de regeltekst* zit maar niet in metadata-namen
    (bv. R26: 'aalscholver' staat in de Vogelrichtlijngebied-regeltekst, niet
    in de gebiedsaanwijzing-naam).

    Retourneert {x, y, q, count, matches[]} met per match juridische_regel_id,
    artikel, artikel_wid, regeltekst_excerpt, regeling (best-effort), en
    match_score (ts_rank). Bot kan deze regels direct boost-en in de LLM-context.

    Note: PostgreSQL FTS gebruikt OR-semantiek tussen keywords (plainto_tsquery
    met implicit OR via to_tsquery '|'-split), zodat niet alle keywords hoeven
    te matchen. Bij geen FTS-match retourneert count=0.
    """
    keywords = [k.strip() for k in q.split(",") if k.strip()]
    if not keywords:
        return {"x": x, "y": y, "q": [], "count": 0, "matches": []}
    # Build OR-tsquery: 'aalscholver | beschermd | diersoort'. Sanitize per
    # keyword: only alphanumeric (NL letters) + hyphen → safe ts_query token.
    sanitized = []
    for k in keywords:
        tok = re.sub(r"[^\wëïüöäáéíóú\-]+", "", k, flags=re.IGNORECASE).strip()
        if tok and len(tok) >= 2:
            sanitized.append(tok)
    if not sanitized:
        return {"x": x, "y": y, "q": keywords, "count": 0, "matches": []}
    ts_query_str = " | ".join(sanitized)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH matched AS (
                SELECT  jr.identificatie                    AS juridische_regel_id,
                        ocd_artikel_label(te.opschrift, te.wid)                        AS artikel,
                        te.wid                              AS artikel_wid,
                        LEFT(te.inhoud_plain, 800)          AS regeltekst_excerpt,
                        te.regeling_expression,
                        ts_rank(
                            to_tsvector('dutch'::regconfig, COALESCE(te.inhoud_plain, '')),
                            to_tsquery('dutch'::regconfig, %s)
                        )                                   AS match_score
                FROM    p2p.juridische_regel               jr
                JOIN    p2p.tekst_element                  te  ON te.wid = jr.regeltekst_wid
                LEFT JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.juridische_regel_norm        jrn ON jrn.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.norm                         n   ON n.identificatie = jrn.norm_id
                LEFT JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.gebiedsaanwijzing            ga  ON ga.identificatie = jrg.gebiedsaanwijzing_id
                JOIN    p2p.locatie                        l
                        ON l.identificatie IN (ala.locatie_id, n.identificatie, ga.locatie_id)
                WHERE   ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                  AND   to_tsvector('dutch'::regconfig, COALESCE(te.inhoud_plain, '')) @@ to_tsquery('dutch'::regconfig, %s)
            ),
            best_per_jr AS (
                SELECT DISTINCT ON (juridische_regel_id)
                       juridische_regel_id, artikel, artikel_wid,
                       regeltekst_excerpt, regeling_expression, match_score
                FROM   matched
                ORDER  BY juridische_regel_id, match_score DESC
            )
            SELECT  b.juridische_regel_id,
                    b.artikel,
                    b.artikel_wid,
                    b.regeltekst_excerpt,
                    b.match_score,
                    r.opschrift                             AS regeling,
                    r.bronhouder
            FROM    best_per_jr                        b
            LEFT JOIN p2p.regeling                     r  ON r.frbr_expression = b.regeling_expression
            ORDER BY b.match_score DESC
            LIMIT %s
            """,
            (ts_query_str, x, y, ts_query_str, limit),
        )
        rows = cur.fetchall()
    return {
        "x": x,
        "y": y,
        "q": keywords,
        "ts_query": ts_query_str,
        "count": len(rows),
        "matches": rows,
    }


def _parse_scored_keyword(kw: str) -> tuple[str, float] | None:
    """V7: parse 'term:weight' input van /v1/objecten + /v1/regels.

    Voorbeelden: `bouwhoogte:1.00`, `hoogte:0.70`, `gebouw` (zonder ':weight'
    → default 1.0). Geeft None bij parse-fout zodat caller 'm kan overslaan.
    """
    if not kw:
        return None
    if ":" in kw:
        term, _, w = kw.rpartition(":")
        try:
            weight = float(w)
        except ValueError:
            return None
    else:
        term, weight = kw, 1.0
    term = term.strip()
    if not term or weight <= 0:
        return None
    return term, max(0.0, min(1.0, weight))


def _aggregate_objecten_per_object_id(scored: list[dict]) -> list[dict]:
    """V7: aggregeer scored cross-product rows naar één match per object_id.

    Input: lijst van {type, score, matched_keywords, object} waar dezelfde
    object_id meerdere keren voorkomt (één rij per artikel-lid).
    Output: één rij per object_id met:
    - score: max() over de groep
    - matched_keywords: union per (term, veld) met max gewicht_bijdrage
    - object.artikelen: array van {artikel, artikel_wid, regeltekst_excerpt}
      gesorteerd op artikel_wid; artikel/artikel_wid/regeltekst_excerpt
      verdwijnen van het object-niveau.

    Volgorde-stabiel: groepen verschijnen in volgorde van eerste optreden in
    `scored` (caller sorteert daarna alsnog op score).
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for item in scored:
        obj = item["object"]
        oid = obj["object_id"]
        if oid not in groups:
            order.append(oid)
            # Object-niveau payload zonder artikel-velden
            obj_level = {k: v for k, v in obj.items() if k not in (
                "artikel", "artikel_wid", "regeltekst_excerpt",
            )}
            obj_level["artikelen"] = []
            groups[oid] = {
                "type": item["type"],
                "score": item["score"],
                "matched_keywords": list(item["matched_keywords"]),
                "object": obj_level,
                # Tracking voor matched_keywords-merge
                "_mk_index": {
                    (mk["term"], mk["veld"]): mk for mk in item["matched_keywords"]
                },
                # Tracking voor artikel-dedupe binnen group
                "_art_wids": set(),
            }
        g = groups[oid]
        # Score: max over de groep
        if item["score"] > g["score"]:
            g["score"] = item["score"]
        # matched_keywords: union, max gewicht_bijdrage per (term, veld)
        for mk in item["matched_keywords"]:
            key = (mk["term"], mk["veld"])
            existing = g["_mk_index"].get(key)
            if existing is None or mk.get("gewicht_bijdrage", 0) > existing.get("gewicht_bijdrage", 0):
                g["_mk_index"][key] = mk
        # artikel-verwijzing toevoegen (uniek op artikel_wid)
        art_wid = obj.get("artikel_wid")
        if art_wid and art_wid not in g["_art_wids"]:
            g["_art_wids"].add(art_wid)
            g["object"]["artikelen"].append({
                "artikel": obj.get("artikel"),
                "artikel_wid": art_wid,
                "regeltekst_excerpt": obj.get("regeltekst_excerpt"),
            })

    # Finalize: rebuild matched_keywords from index, drop tracking-keys, sort artikelen
    result: list[dict] = []
    for oid in order:
        g = groups[oid]
        g["matched_keywords"] = list(g["_mk_index"].values())
        g["object"]["artikelen"].sort(key=lambda a: a.get("artikel_wid") or "")
        del g["_mk_index"]
        del g["_art_wids"]
        result.append(g)
    return result


# V7 — veld-gewichten voor /v1/objecten scoring. Zie objecten-regels-retrieve-endpoint.md §"Open punt 2"
_OBJ_FIELD_WEIGHT_NAAM         = 1.00  # primary_naam (norm_naam/activiteit_naam/bestemming_naam/onderwerp_naam)
_OBJ_FIELD_WEIGHT_GROEP        = 0.50  # secondary categorie
_OBJ_FIELD_WEIGHT_KWALIFICATIE = 0.70  # activiteit-kwalificatie of bestemming-hoofdgroep
_OBJ_FIELD_WEIGHT_REGELING     = 0.30  # regeling-naam
_OBJ_FIELD_WEIGHT_EXCERPT      = 0.30  # regeltekst_excerpt FTS-achtig
_OBJ_FIELD_WEIGHT_ARTIKEL      = 0.20  # artikel-naam (meestal toevallig)


def _score_object_against_keywords(
    obj_fields: dict[str, str | None],
    keywords: list[tuple[str, float]],
) -> tuple[float, list[dict]]:
    """V7: scoor een object tegen gewogen trefwoorden.

    `obj_fields` heeft sleutels die overeenkomen met de veld-gewichten (naam,
    groep, kwalificatie, regeling, excerpt, artikel) en string-waarden.
    Score = Σ over (keyword × field × match_strength).
    """
    field_weights = {
        "naam":         _OBJ_FIELD_WEIGHT_NAAM,
        "groep":        _OBJ_FIELD_WEIGHT_GROEP,
        "kwalificatie": _OBJ_FIELD_WEIGHT_KWALIFICATIE,
        "regeling":     _OBJ_FIELD_WEIGHT_REGELING,
        "excerpt":      _OBJ_FIELD_WEIGHT_EXCERPT,
        "artikel":      _OBJ_FIELD_WEIGHT_ARTIKEL,
    }
    score = 0.0
    matched: list[dict] = []
    for term, kw_weight in keywords:
        term_l = term.lower()
        if not term_l or len(term_l) < 2:
            continue
        for field_name, field_weight in field_weights.items():
            content = (obj_fields.get(field_name) or "").lower()
            if not content:
                continue
            if re.search(rf"\b{re.escape(term_l)}\b", content):
                ms = 1.0
            elif term_l in content:
                ms = 0.7
            else:
                continue
            contribution = kw_weight * field_weight * ms
            score += contribution
            matched.append({
                "term": term,
                "veld": field_name,
                "match_strength": ms,
                "gewicht_bijdrage": round(contribution, 4),
            })
    return score, matched


# V7 performance-bound: per type max N rows uit de DB pakken voor scoring.
# Adres-cases kunnen 8000+ rijen retourneren wat scoring in Python te traag maakt.
# 200/type × 4 types = 800 max objecten, ruim voldoende voor ranking.
_OBJ_FETCH_LIMIT_PER_TYPE = 200


def _fetch_objecten_normwaarde(cur, x: float, y: float) -> list[dict]:
    cur.execute(
        """
        SELECT  'normwaarde'::text                          AS type,
                n.identificatie                              AS object_id,
                n.naam                                       AS naam,
                n.groep                                      AS groep,
                NULL::text                                   AS kwalificatie,
                nw.kwantitatieve_waarde,
                nw.kwalitatieve_waarde,
                n.eenheid,
                l.identificatie                              AS locatie_id,
                l.noemer                                     AS locatie_naam,
                l.locatie_type,
                r.opschrift                                  AS regeling,
                r.frbr_expression,
                ocd_artikel_label(te.opschrift, te.wid)      AS artikel,
                te.wid                                       AS artikel_wid,
                LEFT(te.inhoud_plain, 800)                   AS regeltekst_excerpt
        FROM    p2p.normwaarde nw
        JOIN    p2p.norm n ON n.identificatie = nw.norm_id
        JOIN    p2p.locatie l ON l.identificatie = nw.locatie_id
        LEFT JOIN p2p.juridische_regel_norm jrn ON jrn.norm_id = n.identificatie
        LEFT JOIN p2p.juridische_regel jr ON jr.identificatie = jrn.juridische_regel_id
        LEFT JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
        LEFT JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        WHERE   ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
        LIMIT %s
        """,
        (x, y, _OBJ_FETCH_LIMIT_PER_TYPE),
    )
    return cur.fetchall()


def _fetch_objecten_activiteit(cur, x: float, y: float) -> list[dict]:
    cur.execute(
        """
        SELECT  'activiteit'::text                          AS type,
                a.identificatie                              AS object_id,
                a.naam                                       AS naam,
                a.groep                                      AS groep,
                ala.kwalificatie                             AS kwalificatie,
                NULL::numeric                                AS kwantitatieve_waarde,
                NULL::text                                   AS kwalitatieve_waarde,
                NULL::text                                   AS eenheid,
                l.identificatie                              AS locatie_id,
                l.noemer                                     AS locatie_naam,
                l.locatie_type,
                r.opschrift                                  AS regeling,
                r.frbr_expression,
                ocd_artikel_label(te.opschrift, te.wid)      AS artikel,
                te.wid                                       AS artikel_wid,
                LEFT(te.inhoud_plain, 800)                   AS regeltekst_excerpt
        FROM    p2p.activiteit_locatieaanduiding ala
        JOIN    p2p.activiteit a ON a.identificatie = ala.activiteit_id
        JOIN    p2p.locatie l ON l.identificatie = ala.locatie_id
        JOIN    p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
        LEFT JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
        LEFT JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        WHERE   ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
        LIMIT %s
        """,
        (x, y, _OBJ_FETCH_LIMIT_PER_TYPE),
    )
    return cur.fetchall()


def _fetch_objecten_bestemming(cur, x: float, y: float) -> list[dict]:
    cur.execute(
        """
        SELECT  'bestemming'::text                          AS type,
                po.identificatie                             AS object_id,
                po.naam                                      AS naam,
                po.bestemmingshoofdgroep                     AS groep,
                po.object_type                               AS kwalificatie,
                NULL::numeric                                AS kwantitatieve_waarde,
                NULL::text                                   AS kwalitatieve_waarde,
                NULL::text                                   AS eenheid,
                NULL::text                                   AS locatie_id,
                NULL::text                                   AS locatie_naam,
                NULL::text                                   AS locatie_type,
                ri.naam                                      AS regeling,
                NULL::text                                   AS frbr_expression,
                po.artikelnummer                             AS artikel,
                NULL::text                                   AS artikel_wid,
                NULL::text                                   AS regeltekst_excerpt
        FROM    wro.planobject po
        JOIN    wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
        WHERE   ST_Intersects(po.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
          AND   LOWER(po.object_type) IN ('enkelbestemming', 'dubbelbestemming', 'gebiedsaanduiding', 'functieaanduiding')
          AND   ri.pons_status = 'actief'
        LIMIT %s
        """,
        (x, y, _OBJ_FETCH_LIMIT_PER_TYPE),
    )
    return cur.fetchall()


def _fetch_objecten_gebiedsaanwijzing(cur, x: float, y: float) -> list[dict]:
    cur.execute(
        """
        SELECT  'gebiedsaanwijzing'::text                   AS type,
                ga.identificatie                             AS object_id,
                ga.naam                                      AS naam,
                ga.groep                                     AS groep,
                NULL::text                                   AS kwalificatie,
                NULL::numeric                                AS kwantitatieve_waarde,
                NULL::text                                   AS kwalitatieve_waarde,
                NULL::text                                   AS eenheid,
                l.identificatie                              AS locatie_id,
                l.noemer                                     AS locatie_naam,
                l.locatie_type,
                r.opschrift                                  AS regeling,
                r.frbr_expression,
                ocd_artikel_label(te.opschrift, te.wid)      AS artikel,
                te.wid                                       AS artikel_wid,
                LEFT(te.inhoud_plain, 800)                   AS regeltekst_excerpt
        FROM    p2p.gebiedsaanwijzing ga
        JOIN    p2p.locatie l ON l.identificatie = ga.locatie_id
        LEFT JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.gebiedsaanwijzing_id = ga.identificatie
        LEFT JOIN p2p.juridische_regel jr ON jr.identificatie = jrg.juridische_regel_id
        LEFT JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
        LEFT JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        WHERE   ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
        LIMIT %s
        """,
        (x, y, _OBJ_FETCH_LIMIT_PER_TYPE),
    )
    return cur.fetchall()


@app.get("/v1/objecten", dependencies=[Depends(verify_key)])
def objecten(
    x: float = Query(..., description="RD x-coordinaat (EPSG:28992)"),
    y: float = Query(..., description="RD y-coordinaat (EPSG:28992)"),
    keywords: list[str] = Query(
        ...,
        description="Gewogen trefwoorden als 'term:gewicht' (bv. keywords=bouwhoogte:1.00&keywords=hoogte:0.70)",
    ),
    min_score: float = Query(0.0, ge=0.0, description="Filter objecten met score < min_score weg"),
    limit: int = Query(20, le=100),
    include_types: str = Query(
        "normwaarde,activiteit,bestemming,gebiedsaanwijzing",
        description="Komma-gescheiden lijst objecttypes (default: alle vier)",
    ),
):
    """V7: verenigd objecten-endpoint. Vervangt /v1/normwaarde + /v1/activiteit +
    /v1/bestemming + /v1/onderwerp met één uniforme gewogen-scoring.

    Per object op deze locatie wordt elke trefwoord-term gematcht tegen meerdere
    velden (naam, groep, kwalificatie, regeling, excerpt, artikel). Score is een
    gewogen som van alle veld-matches × keyword-gewicht × match_strength.

    Match-strength heuristiek per veld:
    - 1.0: term staat als heel-woord substring in het veld
    - 0.7: term staat als substring (geen woordgrens)
    - 0.0: geen match
    """
    parsed = [p for p in (_parse_scored_keyword(k) for k in keywords) if p]
    if not parsed:
        raise HTTPException(status_code=400, detail="Geef minimaal één geldige `keywords=term:gewicht`-parameter mee.")
    types_set = {t.strip().lower() for t in include_types.split(",") if t.strip()}

    with get_conn() as conn, conn.cursor() as cur:
        rows: list[dict] = []
        if "normwaarde" in types_set:
            rows.extend(_fetch_objecten_normwaarde(cur, x, y))
        if "activiteit" in types_set:
            rows.extend(_fetch_objecten_activiteit(cur, x, y))
        if "bestemming" in types_set:
            rows.extend(_fetch_objecten_bestemming(cur, x, y))
        if "gebiedsaanwijzing" in types_set:
            rows.extend(_fetch_objecten_gebiedsaanwijzing(cur, x, y))

    # Dedupe op (type, object_id, locatie_id, artikel_wid) — JOIN's kunnen
    # dezelfde object/regel-combinatie meerdere keren teruggeven door multiple
    # locaties (bv. norm gekoppeld aan 5 paragrafen van hetzelfde artikel).
    seen_keys: set[tuple] = set()
    deduped: list[dict] = []
    for r in rows:
        key = (r["type"], r["object_id"], r.get("locatie_id"), r.get("artikel_wid"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(r)

    # Score each object
    scored: list[dict] = []
    for r in deduped:
        obj_fields = {
            "naam":         r.get("naam"),
            "groep":        r.get("groep"),
            "kwalificatie": r.get("kwalificatie"),
            "regeling":     r.get("regeling"),
            "excerpt":      r.get("regeltekst_excerpt"),
            "artikel":      r.get("artikel"),
        }
        score, matched = _score_object_against_keywords(obj_fields, parsed)
        if score < min_score:
            continue
        # Compose response item — `object` veld bevat type-specifieke payload
        obj_payload = {
            "object_id": r["object_id"],
            "naam": r["naam"],
            "groep": r.get("groep"),
            "regeling": r.get("regeling"),
            "artikel": r.get("artikel"),
            "artikel_wid": r.get("artikel_wid"),
            "regeltekst_excerpt": r.get("regeltekst_excerpt"),
        }
        if r["type"] == "normwaarde":
            obj_payload["kwantitatieve_waarde"] = r.get("kwantitatieve_waarde")
            obj_payload["kwalitatieve_waarde"] = r.get("kwalitatieve_waarde")
            obj_payload["eenheid"] = r.get("eenheid")
        elif r["type"] == "activiteit":
            obj_payload["kwalificatie"] = r.get("kwalificatie")
        elif r["type"] == "bestemming":
            obj_payload["object_type"] = r.get("kwalificatie")  # enkel/dubbel/functie/gebied
            obj_payload["hoofdgroep"] = r.get("groep")
        elif r["type"] == "gebiedsaanwijzing":
            obj_payload["onderwerp_groep"] = r.get("groep")

        scored.append({
            "type": r["type"],
            "score": round(score, 4),
            "matched_keywords": matched,
            "object": obj_payload,
        })

    # Aggregeer artikel-cross-product rijen naar één match per object_id.
    aggregated = _aggregate_objecten_per_object_id(scored)
    # Stable secondary sort op object_id zodat score-ties altijd dezelfde
    # volgorde geven (reproduceerbaarheid van top-N selectie).
    aggregated.sort(key=lambda r: (-r["score"], r["object"].get("object_id", "")))

    return {
        "x": x,
        "y": y,
        "keywords": [{"term": t, "gewicht": w} for t, w in parsed],
        "min_score": min_score,
        "include_types": sorted(types_set),
        "count": len(aggregated[:limit]),
        "matches": aggregated[:limit],
    }


@app.get("/v1/regels", dependencies=[Depends(verify_key)])
def regels(
    x: float = Query(..., description="RD x-coordinaat (EPSG:28992)"),
    y: float = Query(..., description="RD y-coordinaat (EPSG:28992)"),
    keywords: list[str] = Query(
        ...,
        description="Gewogen trefwoorden als 'term:gewicht' (bv. keywords=bouwhoogte:1.00&keywords=hoogte:0.70). "
                    "Zonder ':gewicht' wordt 1.0 verondersteld.",
    ),
    min_score: float = Query(0.0, ge=0.0, description="Filter regels met composite score < min_score weg"),
    limit: int = Query(10, le=50),
):
    """V7: gewogen FTS + keyword-match retrieval over juridische regels.

    Vervanger van /v1/regeltekst. Verschil:
    - Trefwoorden hebben individuele gewichten (relevantie uit /v1/keywords/extract)
    - Composite score: ts_rank * 0.5 + Σ(keyword.gewicht * match_strength) * 0.5
    - min_score-filter knipt zwakke matches weg vóór de top-N selectie

    Match-strength heuristiek:
    - 1.0 als de term als heel-woord substring in regeltekst-excerpt staat
    - 0.5 als de term alleen via FTS-token-match wordt geraakt (case van plurals,
      stemming) — wordt op rij-niveau geschat door substring-test op excerpt
    """
    parsed = [p for p in (_parse_scored_keyword(k) for k in keywords) if p]
    if not parsed:
        raise HTTPException(status_code=400, detail="Geef minimaal één geldige `keywords=term:gewicht`-parameter mee.")

    # FTS-tsquery — sanitize per term tot alphanumeric+hyphen
    sanitized_terms: list[tuple[str, float]] = []
    for term, weight in parsed:
        tok = re.sub(r"[^\wëïüöäáéíóú\-]+", " ", term, flags=re.IGNORECASE).strip()
        if tok and len(tok) >= 2:
            # Multi-word term → split tot losse FTS-tokens, behoud gewicht
            for t in tok.split():
                if len(t) >= 2:
                    sanitized_terms.append((t.lower(), weight))
    if not sanitized_terms:
        raise HTTPException(status_code=400, detail="Geen geldige FTS-tokens uit de trefwoorden te halen.")

    ts_query_str = " | ".join(sorted({t for t, _ in sanitized_terms}))

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH matched AS (
                SELECT  jr.identificatie                    AS juridische_regel_id,
                        ocd_artikel_label(te.opschrift, te.wid) AS artikel,
                        te.wid                              AS artikel_wid,
                        LEFT(te.inhoud_plain, 800)          AS regeltekst_excerpt,
                        te.regeling_expression,
                        ts_rank(
                            to_tsvector('dutch'::regconfig, COALESCE(te.inhoud_plain, '')),
                            to_tsquery('dutch'::regconfig, %s)
                        )                                   AS ts_rank_score
                FROM    p2p.juridische_regel               jr
                JOIN    p2p.tekst_element                  te  ON te.wid = jr.regeltekst_wid
                LEFT JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.juridische_regel_norm        jrn ON jrn.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.norm                         n   ON n.identificatie = jrn.norm_id
                LEFT JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.gebiedsaanwijzing            ga  ON ga.identificatie = jrg.gebiedsaanwijzing_id
                JOIN    p2p.locatie                        l
                        ON l.identificatie IN (ala.locatie_id, n.identificatie, ga.locatie_id)
                WHERE   ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                  AND   to_tsvector('dutch'::regconfig, COALESCE(te.inhoud_plain, '')) @@ to_tsquery('dutch'::regconfig, %s)
            ),
            best_per_jr AS (
                SELECT DISTINCT ON (juridische_regel_id)
                       juridische_regel_id, artikel, artikel_wid,
                       regeltekst_excerpt, regeling_expression, ts_rank_score
                FROM   matched
                ORDER  BY juridische_regel_id, ts_rank_score DESC
            )
            SELECT  b.juridische_regel_id,
                    b.artikel,
                    b.artikel_wid,
                    b.regeltekst_excerpt,
                    b.ts_rank_score,
                    r.opschrift                             AS regeling,
                    r.bronhouder
            FROM    best_per_jr                        b
            LEFT JOIN p2p.regeling                     r  ON r.frbr_expression = b.regeling_expression
            ORDER BY b.ts_rank_score DESC
            LIMIT %s
            """,
            (ts_query_str, x, y, ts_query_str, limit * 3),  # haal extra binnen, filter later
        )
        rows = cur.fetchall()

    # Composite score in Python: ts_rank * 0.5 + Σ(weight * match_strength) * 0.5
    scored: list[dict] = []
    for row in rows:
        excerpt_lower = (row.get("regeltekst_excerpt") or "").lower()
        matched: list[dict] = []
        keyword_score = 0.0
        for term, weight in sanitized_terms:
            term_lower = term.lower()
            if not term_lower or len(term_lower) < 2:
                continue
            # match_strength: 1.0 voor heel-woord substring, 0.5 als alleen FTS-token-match
            if re.search(rf"\b{re.escape(term_lower)}\b", excerpt_lower):
                ms = 1.0
            elif term_lower in excerpt_lower:
                ms = 0.7
            else:
                ms = 0.5  # FTS heeft 'm geraakt via stemming/conjugatie
            keyword_score += weight * ms
            matched.append({"term": term, "weight": weight, "match_strength": ms})
        ts_part = float(row.get("ts_rank_score") or 0.0)
        composite = ts_part * 0.5 + keyword_score * 0.5
        if composite < min_score:
            continue
        scored.append({
            "score": round(composite, 4),
            "ts_rank": round(ts_part, 4),
            "keyword_score": round(keyword_score, 4),
            "juridische_regel_id": row["juridische_regel_id"],
            "regeling": row["regeling"],
            "artikel": row["artikel"],
            "artikel_wid": row["artikel_wid"],
            "regeltekst_excerpt": row["regeltekst_excerpt"],
            "matched_keywords": matched,
        })

    # Stable secondary sort op artikel_wid voor deterministische top-N.
    scored.sort(key=lambda r: (-r["score"], r.get("artikel_wid") or ""))
    return {
        "x": x,
        "y": y,
        "keywords": [{"term": t, "gewicht": w} for t, w in parsed],
        "min_score": min_score,
        "ts_query": ts_query_str,
        "count": len(scored[:limit]),
        "matches": scored[:limit],
    }


@app.get("/v1/gemeente/{code}/pons", dependencies=[Depends(verify_key)])
def pons(code: str):
    """Pons-status: hoeveel Wro-plannen en ponsen voor deze gemeente?"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS wro_instrumenten
            FROM wro.ruimtelijk_instrument
            WHERE bronhouder = %s
            """,
            (code,),
        )
        wro_count = cur.fetchone()["wro_instrumenten"]
        cur.execute(
            """
            SELECT count(*) AS pons_count
            FROM p2p.pons p
            WHERE p.identificatie LIKE %s
            """,
            (f"%gm{code}%",),
        )
        pons_count = cur.fetchone()["pons_count"]
        return {
            "gemeente": code,
            "wro_instrumenten": wro_count,
            "pons_aanwezig": pons_count > 0,
            "pons_count": pons_count,
        }


@app.get("/v1/gezagen", dependencies=[Depends(verify_key)])
def gezagen():
    """Alle bevoegde gezagen met laad-status."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT overheidscode, naam, bestuurslaag,
                   ow_geladen, imtr_geladen, wro_geladen,
                   ow_regelingen, wro_instrumenten
            FROM core.bronhouder
            ORDER BY naam
            """
        )
        return {"bronhouders": cur.fetchall()}


# ── Viewer endpoints ──────────────────────────────────────────────


@app.get("/v1/viewer/regelingen", dependencies=[Depends(verify_key)])
def viewer_regelingen(x: float = Query(...), y: float = Query(...)):
    """Welke regelingen gelden op een RD-coördinaat? Retourneert een
    documentenlijst voor de viewer, gegroepeerd op bestuurslaag."""
    with get_conn() as conn, conn.cursor() as cur:
        # Dedupliceer op opschrift: zelfde titel = zelfde regeling voor de
        # gebruiker, zelfs als er 340 expressions zijn (bv. Voorbeschermings-
        # regels hyperscale datacentra per gemeente). Pak de nieuwste expression.
        cur.execute(
            """
            SELECT DISTINCT ON (r.opschrift)
                r.frbr_expression   AS expression,
                r.opschrift         AS titel,
                r.documenttype      AS type,
                r.bronhouder,
                b.naam              AS bronhouder_naam,
                b.bestuurslaag
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r       ON r.frbr_expression = te.regeling_expression
            JOIN core.bronhouder b    ON b.overheidscode = r.bronhouder
            WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            ORDER BY r.opschrift, r.frbr_expression DESC
            """,
            (x, y),
        )
        regelingen = cur.fetchall()
        laag_order = {'gemeente': 0, 'provincie': 1, 'waterschap': 2, 'rijk': 3}
        regelingen.sort(key=lambda r: (laag_order.get(r['bestuurslaag'] or '', 4), r['titel']))

        # Wro-plannen op dezelfde locatie — als volledige objecten
        cur.execute(
            """
            SELECT DISTINCT ON (ri.naam)
                ri.idn,
                ri.naam             AS titel,
                ri.type_plan        AS type,
                ri.planstatus,
                ri.datum,
                ri.pons_status,
                b.naam              AS bronhouder_naam,
                b.bestuurslaag
            FROM wro.ruimtelijk_instrument ri
            JOIN core.bronhouder b ON b.overheidscode = ri.bronhouder
            WHERE ST_Intersects(ri.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
              AND ri.pons_status = 'actief'
            ORDER BY ri.naam, ri.datum DESC NULLS LAST
            """,
            (x, y),
        )
        wro_plannen = cur.fetchall()

        # Pons-check: valt dit punt binnen een pons-geometrie?
        cur.execute(
            """
            SELECT count(*) AS n
            FROM p2p.pons p
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = p.locatie_id
            WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            """,
            (x, y),
        )
        pons_count = cur.fetchone()["n"]

    return {
        "locatie": {"x": x, "y": y},
        "regelingen": regelingen,
        "wro_plannen": wro_plannen,
        "pons_aanwezig": pons_count > 0,
    }


def _build_boom(rows: list[dict]) -> list[dict]:
    """Nest een platte lijst tekst_elementen (met parent_id) tot een boom.

    Twee-pass: eerst alle nodes aanmaken, dan pas nesten. Dit werkt
    ongeacht de volgorde van parent en child in de lijst.
    """
    by_id: dict[int, dict] = {}

    # Pass 1: maak alle nodes
    for row in rows:
        by_id[row["id"]] = {
            "id": row["id"],
            "wid": row["wid"],
            "eid": row.get("eid"),  # nodig voor IntRef-navigatie in de leestekst
            "type": row["element_type"],
            "nummer": row["nummer"],
            "opschrift": row["opschrift"],
            "tekst": row.get("tekst"),  # None wanneer lazy-loaded
            "heeft_tekst": (row.get("tekst_lengte") or 0) > 0,
            "kinderen": [],
            "annotaties": None,
            "_parent_id": row["parent_id"],
        }

    # Pass 2: nest kinderen onder hun parent
    roots: list[dict] = []
    for node in by_id.values():
        parent_id = node.pop("_parent_id")
        if parent_id is None or parent_id not in by_id:
            roots.append(node)
        else:
            by_id[parent_id]["kinderen"].append(node)

    return roots


def _annoteer_boom(boom: list[dict], annotaties: dict[str, dict]):
    """Hang annotaties (per regeltekst_wid) aan de juiste boom-nodes."""
    for node in boom:
        wid = node["wid"]
        if wid in annotaties:
            node["annotaties"] = annotaties[wid]
        if node["kinderen"]:
            _annoteer_boom(node["kinderen"], annotaties)


@app.get("/v1/viewer/regeling/{expression:path}/boom", dependencies=[Depends(verify_key)])
def viewer_boom(
    expression: str,
    x: float = Query(None, description="RD x-coördinaat (optioneel, voor locatie-filtering)"),
    y: float = Query(None, description="RD y-coördinaat (optioneel, voor locatie-filtering)"),
):
    """Documentstructuur als geneste boom + annotaties per artikel.

    Wanneer x/y zijn meegegeven, worden alleen annotaties geretourneerd
    waarvan de locatie het opgegeven punt raakt.
    """
    with get_conn() as conn, conn.cursor() as cur:
        # Regeling-metadata
        cur.execute(
            "SELECT frbr_expression, opschrift, documenttype, bronhouder "
            "FROM p2p.regeling WHERE frbr_expression = %s",
            (expression,),
        )
        regeling = cur.fetchone()
        if not regeling:
            raise HTTPException(404, "Regeling niet gevonden")

        # A: documentstructuur (platte lijst, genest in Python)
        cur.execute(
            """
            SELECT id, eid, wid, element_type, parent_id,
                   nummer, opschrift, volgorde,
                   CASE WHEN element_type IN ('Artikel', 'Lid', 'Divisietekst')
                        THEN length(coalesce(inhoud, ''))
                        ELSE 0 END AS tekst_lengte
            FROM p2p.tekst_element
            WHERE regeling_expression = %s
            ORDER BY volgorde
            """,
            (expression,),
        )
        boom = _build_boom(cur.fetchall())

        # B: annotaties — activiteiten, gebiedsaanwijzingen, normwaarden
        #
        # Optimalisatie: als x/y meegegeven, zoek eerst welke locatie_ids
        # het punt raken (GIST index), en filter daarna. Voorkomt dat
        # ST_Intersects op elke rij in de join wordt berekend.
        # Geen locatie-filtering op de boom-annotaties. De boom toont
        # alle annotaties van de regeling — het is aan de frontend om
        # bij klik op de kaart te highlighten welke locaties relevant zijn.
        # Dit bespaart een dure ST_Intersects query (~2s op grote gemeenten).

        cur.execute(
            f"""
            SELECT jr.regeltekst_wid,
                   a.naam           AS activiteit_naam,
                   a.groep          AS activiteit_groep,
                   ala.kwalificatie,
                   ala.locatie_id   AS ala_locatie_id
            FROM p2p.juridische_regel jr
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
                                     AND te.regeling_expression = %s
            LEFT JOIN p2p.activiteit_locatieaanduiding ala
                   ON ala.juridische_regel_id = jr.identificatie
            LEFT JOIN p2p.activiteit a
                   ON a.identificatie = ala.activiteit_id
            """,
            (expression,),
        )
        act_rows = cur.fetchall()

        cur.execute(
            """
            SELECT jr.regeltekst_wid,
                   ga.identificatie  AS ga_id,
                   ga.type           AS ga_type,
                   ga.naam           AS ga_naam,
                   ga.groep          AS ga_groep,
                   ga.locatie_id     AS ga_locatie_id
            FROM p2p.juridische_regel jr
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
                                     AND te.regeling_expression = %s
            JOIN p2p.juridische_regel_gebiedsaanwijzing jrga
                   ON jrga.juridische_regel_id = jr.identificatie
            JOIN p2p.gebiedsaanwijzing ga
                   ON ga.identificatie = jrga.gebiedsaanwijzing_id
            """,
            (expression,),
        )
        ga_rows = cur.fetchall()

        cur.execute(
            """
            SELECT jr.regeltekst_wid,
                   n.naam            AS norm_naam,
                   n.type_norm,
                   n.eenheid,
                   nw.kwantitatieve_waarde,
                   nw.kwalitatieve_waarde,
                   nw.locatie_id     AS nw_locatie_id
            FROM p2p.juridische_regel jr
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
                                     AND te.regeling_expression = %s
            JOIN p2p.juridische_regel_norm jrn
                   ON jrn.juridische_regel_id = jr.identificatie
            JOIN p2p.norm n
                   ON n.identificatie = jrn.norm_id
            LEFT JOIN p2p.normwaarde nw
                   ON nw.norm_id = n.identificatie
            """,
            (expression,),
        )
        nw_rows = cur.fetchall()

    # Groepeer annotaties per regeltekst_wid
    annot: dict[str, dict] = {}
    locatie_ids: set[str] = set()

    for row in act_rows:
        wid = row["regeltekst_wid"]
        annot.setdefault(wid, {"activiteiten": [], "gebiedsaanwijzingen": [], "normwaarden": []})
        if row["activiteit_naam"]:
            entry = {
                "naam": row["activiteit_naam"],
                "groep": row["activiteit_groep"],
                "kwalificatie": row["kwalificatie"],
            }
            if entry not in annot[wid]["activiteiten"]:
                annot[wid]["activiteiten"].append(entry)
        if row["ala_locatie_id"]:
            locatie_ids.add(row["ala_locatie_id"])

    for row in ga_rows:
        wid = row["regeltekst_wid"]
        annot.setdefault(wid, {"activiteiten": [], "gebiedsaanwijzingen": [], "normwaarden": []})
        entry = {
            "id": row["ga_id"],
            "type": row["ga_type"],
            "naam": row["ga_naam"],
            "groep": row["ga_groep"],
            "locatie_id": row["ga_locatie_id"],
        }
        if entry not in annot[wid]["gebiedsaanwijzingen"]:
            annot[wid]["gebiedsaanwijzingen"].append(entry)
        locatie_ids.add(row["ga_locatie_id"])

    for row in nw_rows:
        wid = row["regeltekst_wid"]
        annot.setdefault(wid, {"activiteiten": [], "gebiedsaanwijzingen": [], "normwaarden": []})
        entry = {
            "naam": row["norm_naam"],
            "type_norm": row["type_norm"],
            "eenheid": row["eenheid"],
            "waarde": (
                float(row["kwantitatieve_waarde"])
                if row["kwantitatieve_waarde"] is not None
                else row["kwalitatieve_waarde"]
            ),
        }
        if entry not in annot[wid]["normwaarden"]:
            annot[wid]["normwaarden"].append(entry)
        if row.get("nw_locatie_id"):
            locatie_ids.add(row["nw_locatie_id"])

    # Hang annotaties aan de boom
    _annoteer_boom(boom, annot)

    return {
        "regeling": {
            "expression": regeling["frbr_expression"],
            "titel": regeling["opschrift"],
            "type": regeling["documenttype"],
        },
        "boom": boom,
        "locatie_ids": sorted(locatie_ids),
    }


@app.get("/v1/viewer/tekst/{wid}", dependencies=[Depends(verify_key)])
def viewer_tekst(wid: str):
    """Tekst-inhoud (STOP-XML markup) van een enkel tekst_element (lazy loading).

    Geeft de XML met behoud van structuur (Lijst/Li/IntRef/Al) zodat de
    frontend lijsten en interne verwijzingen correct kan renderen.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT inhoud AS tekst FROM p2p.tekst_element WHERE wid = %s LIMIT 1",
            (wid,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Tekst niet gevonden")
    return {"wid": wid, "tekst": row["tekst"]}


def _csv_param(value: str | None) -> list[str] | None:
    """Parse een comma-separated query-parameter naar list[str].
    Leeg → None (filter wordt geskipt)."""
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


# Max aantal matched_artikelen per regeling — voorkomt dat één omgevingsplan
# met 200 lucht-norm-verwijzingen het hele response opblaast. UI kan later
# een "toon meer"-knop toevoegen.
_MAX_MATCHED_PER_REGELING = 20


def _collect_matched_artikelen(
    cur,
    expressions: list[str],
    *,
    activiteitengroepen: list[str] | None,
    typen_gebied: list[str] | None,
    groepen_gebied: list[str] | None,
    normgroepen: list[str] | None,
    themas: list[str] | None,
    soorten_hoofdlijn: list[str] | None,
    hoofdlijnen: list[str] | None,
) -> dict[str, list[dict]]:
    """Verzamelt matched artikelen per regeling-expression voor de actieve
    annotatie-filters. Returnt `{expression: [matched_artikel, ...]}`.

    Per artikel groepeert de functie alle annotaties_match die op dezelfde
    (regeling, wid) horen — een artikel kan dus tegelijk een norm-match én
    een activiteit-match hebben binnen één rij in de UI.
    """
    if not expressions:
        return {}

    by_key: dict[tuple[str, str], dict] = {}
    # Dedup van pills binnen één artikel: één activiteit kan via meerdere
    # ALA-rijen op hetzelfde artikel terugkomen (verschillende Locaties of
    # meerdere juridische_regels). Items met identieke pill-inhoud worden
    # samengevouwen; items die op locatie verschillen blijven gescheiden
    # zodra de locatie-noemer in de match zit.
    seen_by_key: dict[tuple[str, str], set[tuple]] = {}

    def upsert(expression: str, wid: str, element_type, nummer, opschrift, snippet, match: dict):
        key = (expression, wid)
        entry = by_key.get(key)
        if entry is None:
            entry = {
                "wid": wid,
                "element_type": element_type or None,
                "nummer": nummer or None,
                "opschrift": opschrift or "",
                "snippet": snippet or "",
                "annotaties_match": [],
            }
            by_key[key] = entry
            seen_by_key[key] = set()
        match_key = tuple(sorted(match.items()))
        if match_key in seen_by_key[key]:
            return
        seen_by_key[key].add(match_key)
        entry["annotaties_match"].append(match)

    # ── Activiteit-matches ──
    if activiteitengroepen:
        cur.execute(
            """
            SELECT te.regeling_expression, te.wid, te.element_type, te.nummer, te.opschrift,
                   LEFT(te.inhoud_plain, 300) AS snippet,
                   a.naam AS act_naam, a.groep AS act_groep,
                   ala.kwalificatie AS act_kwalificatie,
                   l.noemer AS act_locatie_noemer
            FROM p2p.tekst_element te
            JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
            JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
            JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
            LEFT JOIN p2p.locatie l ON l.identificatie = ala.locatie_id
            WHERE te.regeling_expression = ANY(%s)
              AND a.groep = ANY(%s)
            """,
            (expressions, activiteitengroepen),
        )
        for row in cur.fetchall():
            match: dict = {
                "type": "activiteit",
                "groep": row["act_groep"],
                "naam": row["act_naam"],
                "kwalificatie": row["act_kwalificatie"],
            }
            if row["act_locatie_noemer"]:
                match["locatie"] = row["act_locatie_noemer"]
            upsert(row["regeling_expression"], row["wid"], row["element_type"], row["nummer"], row["opschrift"], row["snippet"], match)

    # ── Gebiedsaanwijzing-matches ──
    if typen_gebied or groepen_gebied:
        ga_clauses = ["te.regeling_expression = ANY(%s)"]
        ga_params: list = [expressions]
        if typen_gebied:
            ga_clauses.append("g.type = ANY(%s)")
            ga_params.append(typen_gebied)
        if groepen_gebied:
            ga_clauses.append("g.groep = ANY(%s)")
            ga_params.append(groepen_gebied)
        cur.execute(
            f"""
            SELECT te.regeling_expression, te.wid, te.element_type, te.nummer, te.opschrift,
                   LEFT(te.inhoud_plain, 300) AS snippet,
                   g.type AS ga_type, g.groep AS ga_groep, g.naam AS ga_naam
            FROM p2p.tekst_element te
            JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
            JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.juridische_regel_id = jr.identificatie
            JOIN p2p.gebiedsaanwijzing g ON g.identificatie = jrg.gebiedsaanwijzing_id
            WHERE {' AND '.join(ga_clauses)}
            """,
            ga_params,
        )
        for row in cur.fetchall():
            upsert(row["regeling_expression"], row["wid"], row["element_type"], row["nummer"], row["opschrift"], row["snippet"], {
                "type": "gebiedsaanwijzing",
                "type_gebied": row["ga_type"],
                "groep": row["ga_groep"],
                "naam": row["ga_naam"],
            })

    # ── Norm-matches ──
    if normgroepen:
        cur.execute(
            """
            SELECT te.regeling_expression, te.wid, te.element_type, te.nummer, te.opschrift,
                   LEFT(te.inhoud_plain, 300) AS snippet,
                   n.naam AS norm_naam, n.groep AS norm_groep,
                   n.eenheid AS norm_eenheid
            FROM p2p.tekst_element te
            JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
            JOIN p2p.juridische_regel_norm jrn ON jrn.juridische_regel_id = jr.identificatie
            JOIN p2p.norm n ON n.identificatie = jrn.norm_id
            WHERE te.regeling_expression = ANY(%s)
              AND n.groep = ANY(%s)
            """,
            (expressions, normgroepen),
        )
        for row in cur.fetchall():
            match: dict = {
                "type": "norm",
                "groep": row["norm_groep"],
                "naam": row["norm_naam"],
            }
            if row["norm_eenheid"]:
                match["eenheid"] = row["norm_eenheid"]
            upsert(row["regeling_expression"], row["wid"], row["element_type"], row["nummer"], row["opschrift"], row["snippet"], match)

    # ── Thema-matches (via tekstdeel.thema TEXT[]) ──
    if themas:
        cur.execute(
            """
            SELECT te.regeling_expression, te.wid, te.element_type, te.nummer, te.opschrift,
                   LEFT(te.inhoud_plain, 300) AS snippet,
                   td.thema AS td_themas
            FROM p2p.tekst_element te
            JOIN p2p.tekstdeel td ON td.divisie_wid = te.wid
            WHERE te.regeling_expression = ANY(%s)
              AND td.thema && %s
            """,
            (expressions, themas),
        )
        for row in cur.fetchall():
            # Het tekstdeel kan meerdere thema's hebben; voeg per
            # matchend thema een aparte annotation toe.
            matched_themas = [t for t in (row["td_themas"] or []) if t in themas]
            for t in matched_themas:
                upsert(row["regeling_expression"], row["wid"], row["element_type"], row["nummer"], row["opschrift"], row["snippet"], {
                    "type": "thema",
                    "naam": t,
                })

    # ── Hoofdlijn-matches (canonical-soort + naam) ──
    if soorten_hoofdlijn or hoofdlijnen:
        hl_clauses = ["te.regeling_expression = ANY(%s)"]
        hl_params: list = [expressions]
        if soorten_hoofdlijn:
            hl_clauses.append("""COALESCE(m.canonical,
                CASE WHEN TRIM(h.soort) IN ('-', '') THEN 'Overig'
                     ELSE LOWER(TRIM(h.soort))
                END) = ANY(%s)""")
            hl_params.append(soorten_hoofdlijn)
        if hoofdlijnen:
            hl_clauses.append("h.naam = ANY(%s)")
            hl_params.append(hoofdlijnen)
        cur.execute(
            f"""
            SELECT te.regeling_expression, te.wid, te.element_type, te.nummer, te.opschrift,
                   LEFT(te.inhoud_plain, 300) AS snippet,
                   COALESCE(m.canonical,
                       CASE WHEN TRIM(h.soort) IN ('-', '') THEN 'Overig'
                            ELSE LOWER(TRIM(h.soort))
                       END) AS hl_soort,
                   h.naam AS hl_naam
            FROM p2p.tekst_element te
            JOIN p2p.tekstdeel td ON td.divisie_wid = te.wid
            JOIN p2p.tekstdeel_hoofdlijn tdh ON tdh.tekstdeel_id = td.identificatie
            JOIN p2p.hoofdlijn h ON h.identificatie = tdh.hoofdlijn_id
            LEFT JOIN core.hoofdlijn_soort_mapping m ON m.raw_value = h.soort
            WHERE {' AND '.join(hl_clauses)}
            """,
            hl_params,
        )
        for row in cur.fetchall():
            upsert(row["regeling_expression"], row["wid"], row["element_type"], row["nummer"], row["opschrift"], row["snippet"], {
                "type": "hoofdlijn",
                "soort": row["hl_soort"],
                "naam": row["hl_naam"],
            })

    # Aggregeer naar {expression: [artikel, ...]}, met cap per regeling.
    by_expression: dict[str, list[dict]] = {}
    for (expression, _wid), artikel in by_key.items():
        by_expression.setdefault(expression, []).append(artikel)
    for expression, artikelen in by_expression.items():
        # Sorteer: meer matches = hoger op de lijst, dan op opschrift
        artikelen.sort(
            key=lambda a: (-len(a["annotaties_match"]), a.get("nummer") or "", a["opschrift"]),
        )
        if len(artikelen) > _MAX_MATCHED_PER_REGELING:
            by_expression[expression] = artikelen[:_MAX_MATCHED_PER_REGELING]

    return by_expression


@app.get("/v1/regelingen/zoek", dependencies=[Depends(verify_key)])
def regelingen_zoek(
    q: str = Query("", description="Vrij-tekst zoekvraag"),
    bestuurslaag: str = Query("", description="Comma-separated: gemeente,provincie,waterschap,rijk"),
    regelingmodel: str = Query(""),
    documenttype: str = Query(""),
    bronhouder: str = Query("", description="Comma-separated overheidscodes"),
    activiteitengroep: str = Query("", description="Comma-separated activiteit-groepen"),
    type_gebiedsaanwijzing: str = Query("", description="Comma-separated gebiedsaanwijzing-types"),
    gebiedsaanwijzinggroep: str = Query("", description="Comma-separated gebiedsaanwijzing-groepen"),
    omgevingsnormgroep: str = Query("", description="Comma-separated norm-groepen"),
    thema: str = Query("", description="Comma-separated thema's (IMOW-waardelijst-labels)"),
    soort_hoofdlijn: str = Query("", description="Comma-separated hoofdlijn-soorten (canonical)"),
    hoofdlijn: str = Query("", description="Comma-separated hoofdlijn-namen"),
    wro: bool = Query(False, description="Wro-bestemmingsplannen meenemen"),
    sort_by: str = Query("titel", description="Sorteer-modus: relevantie | titel | datum"),
    limit: int = Query(20, le=100, ge=1),
    offset: int = Query(0, ge=0),
):
    """Zoek regelingen — Phase A + B.

    Phase A: regeling-eigenschappen + vrije tekst (q, bestuurslaag,
    regelingmodel, documenttype, bronhouder, wro).

    Phase B: annotatie-filters (activiteitengroep, type+groep
    gebiedsaanwijzing, normgroep, thema, soort+naam hoofdlijn).
    Per actief annotatie-filter komt een EXISTS-clause op de regeling.
    Filters zijn AND-gecombineerd tussen categorieën, OR binnen categorie.

    Wanneer ≥1 annotatie-filter actief is wordt per resulterende regeling
    een lijst van `matched_artikelen` opgehaald met snippet + match-context
    (welke specifieke annotatie matchte).

    Wro-bestemmingsplannen worden alleen meegegeven als `wro=true`. Ze
    krijgen nooit `matched_artikelen` (Wro kent geen IMOW-annotaties).
    """
    lagen = _csv_param(bestuurslaag)
    modellen = _csv_param(regelingmodel)
    types = _csv_param(documenttype)
    bronhouders = _csv_param(bronhouder)
    activiteitengroepen = _csv_param(activiteitengroep)
    typen_gebied = _csv_param(type_gebiedsaanwijzing)
    groepen_gebied = _csv_param(gebiedsaanwijzinggroep)
    normgroepen = _csv_param(omgevingsnormgroep)
    themas = _csv_param(thema)
    soorten_hoofdlijn = _csv_param(soort_hoofdlijn)
    hoofdlijnen = _csv_param(hoofdlijn)

    has_annotation_filter = any([
        activiteitengroepen, typen_gebied, groepen_gebied, normgroepen,
        themas, soorten_hoofdlijn, hoofdlijnen,
    ])

    with get_conn() as conn, conn.cursor() as cur:
        # ── Ow-regelingen ──────────────────────────────────────
        # Filter-clauses dynamisch opbouwen — alleen meegeven wat actief is.
        # Bestuurslaag-clause wordt apart bijgehouden zodat we 'm kunnen
        # weglaten bij de facet-count (per-chip preview semantiek).
        base_where: list[str] = ["1=1"]
        base_params: list = []

        bestuurslaag_clause: str | None = None
        bestuurslaag_params: list = []

        if lagen:
            bestuurslaag_clause = "b.bestuurslaag = ANY(%s)"
            bestuurslaag_params.append(lagen)
        if modellen:
            base_where.append("r.regelingmodel = ANY(%s)")
            base_params.append(modellen)
        if types:
            base_where.append("r.documenttype = ANY(%s)")
            base_params.append(types)
        if bronhouders:
            base_where.append("r.bronhouder = ANY(%s)")
            base_params.append(bronhouders)
        if q:
            # Match op regeling-metadata (opschrift, citeertitel, frbr_work)
            # OF op artikeltekst (inhoud_plain). EXISTS voor de tekst-tak is
            # sneller dan JOIN+DISTINCT bij regelingen met honderden artikelen.
            # frbr_work erbij zodat een gebruiker op identifier kan zoeken
            # ('AMS_OP', 'NL.IMRO...') i.p.v. alleen titels.
            base_where.append("""(
                r.opschrift ILIKE %s
                OR r.citeertitel ILIKE %s
                OR r.frbr_work ILIKE %s
                OR EXISTS (
                    SELECT 1 FROM p2p.tekst_element te
                     WHERE te.regeling_expression = r.frbr_expression
                       AND te.inhoud_plain ILIKE %s
                )
            )""")
            pattern = f"%{q}%"
            base_params.extend([pattern, pattern, pattern, pattern])

        # ── Annotatie-filters (Phase B) — EXISTS-clauses ─────
        # Elke actieve filter eist dat de regeling MINSTENS ÉÉN matchend
        # artikel heeft. AND-gecombineerd tussen categorieën, OR binnen
        # (via ANY(...)).
        if activiteitengroepen:
            base_where.append("""EXISTS (
                SELECT 1 FROM p2p.tekst_element te2
                JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te2.wid
                JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
                JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
                WHERE te2.regeling_expression = r.frbr_expression
                  AND a.groep = ANY(%s)
            )""")
            base_params.append(activiteitengroepen)

        if typen_gebied or groepen_gebied:
            ga_clauses = ["te2.regeling_expression = r.frbr_expression"]
            ga_params: list = []
            if typen_gebied:
                ga_clauses.append("g.type = ANY(%s)")
                ga_params.append(typen_gebied)
            if groepen_gebied:
                ga_clauses.append("g.groep = ANY(%s)")
                ga_params.append(groepen_gebied)
            base_where.append(f"""EXISTS (
                SELECT 1 FROM p2p.tekst_element te2
                JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te2.wid
                JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.juridische_regel_id = jr.identificatie
                JOIN p2p.gebiedsaanwijzing g ON g.identificatie = jrg.gebiedsaanwijzing_id
                WHERE {' AND '.join(ga_clauses)}
            )""")
            base_params.extend(ga_params)

        if normgroepen:
            base_where.append("""EXISTS (
                SELECT 1 FROM p2p.tekst_element te2
                JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te2.wid
                JOIN p2p.juridische_regel_norm jrn ON jrn.juridische_regel_id = jr.identificatie
                JOIN p2p.norm n ON n.identificatie = jrn.norm_id
                WHERE te2.regeling_expression = r.frbr_expression
                  AND n.groep = ANY(%s)
            )""")
            base_params.append(normgroepen)

        if themas:
            # Thema is TEXT[] op tekstdeel; `&&` is array-overlap operator.
            base_where.append("""EXISTS (
                SELECT 1 FROM p2p.tekst_element te2
                JOIN p2p.tekstdeel td ON td.divisie_wid = te2.wid
                WHERE te2.regeling_expression = r.frbr_expression
                  AND td.thema && %s
            )""")
            base_params.append(themas)

        if soorten_hoofdlijn or hoofdlijnen:
            hl_clauses = ["te2.regeling_expression = r.frbr_expression"]
            hl_params: list = []
            if soorten_hoofdlijn:
                # Hoofdlijn-soort gebruikt canonical-mapping (zie
                # core.hoofdlijn_soort_mapping).
                hl_clauses.append("""COALESCE(m.canonical,
                    CASE WHEN TRIM(h.soort) IN ('-', '') THEN 'Overig'
                         ELSE LOWER(TRIM(h.soort))
                    END) = ANY(%s)""")
                hl_params.append(soorten_hoofdlijn)
            if hoofdlijnen:
                hl_clauses.append("h.naam = ANY(%s)")
                hl_params.append(hoofdlijnen)
            base_where.append(f"""EXISTS (
                SELECT 1 FROM p2p.tekst_element te2
                JOIN p2p.tekstdeel td ON td.divisie_wid = te2.wid
                JOIN p2p.tekstdeel_hoofdlijn tdh ON tdh.tekstdeel_id = td.identificatie
                JOIN p2p.hoofdlijn h ON h.identificatie = tdh.hoofdlijn_id
                LEFT JOIN core.hoofdlijn_soort_mapping m ON m.raw_value = h.soort
                WHERE {' AND '.join(hl_clauses)}
            )""")
            base_params.extend(hl_params)

        # Volledige WHERE = base + bestuurslaag (als die er is)
        full_where = base_where + ([bestuurslaag_clause] if bestuurslaag_clause else [])
        full_params = base_params + bestuurslaag_params

        ow_query = f"""
            SELECT
                r.frbr_expression                         AS expression,
                r.opschrift                               AS titel,
                r.documenttype,
                r.regelingmodel,
                r.bronhouder                              AS bronhouder_code,
                b.naam                                    AS bronhouder_naam,
                b.bestuurslaag,
                (SELECT COUNT(*) FROM p2p.tekst_element te
                  WHERE te.regeling_expression = r.frbr_expression) AS totaal_artikelen,
                (CASE WHEN %s = '' THEN NULL ELSE
                    (SELECT COUNT(*) FROM p2p.tekst_element te
                      WHERE te.regeling_expression = r.frbr_expression
                        AND te.inhoud_plain ILIKE %s)
                END)                                      AS hits_in_tekst
            FROM p2p.regeling r
            JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
            WHERE {' AND '.join(full_where)}
            ORDER BY r.opschrift, r.frbr_expression DESC
        """
        # SELECT-clause heeft 2 extra params (q + pattern) voor de count
        select_params = [q, f"%{q}%" if q else ""]
        cur.execute(ow_query, select_params + full_params)
        ow_rows = cur.fetchall()

        # ── Bestuurslaag-facets ────────────────────────────────
        # Voor elke laag: hoeveel hits zou je krijgen als ALLEEN deze laag
        # geselecteerd was (met andere category-filters intact). Daarom
        # gebruiken we `base_where` zonder de bestuurslaag-clause.
        # NB: We tellen distinct frbr_expression — dezelfde regeling kan
        # niet in twee bestuurslagen tegelijk zitten dus DISTINCT is hier
        # eigenlijk overbodig, maar maakt 'm robuust voor toekomstige
        # multi-bronhouder-regelingen.
        cur.execute(
            f"""
            SELECT b.bestuurslaag, COUNT(*) AS n
            FROM p2p.regeling r
            JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
            WHERE {' AND '.join(base_where)}
              AND b.bestuurslaag IS NOT NULL
            GROUP BY b.bestuurslaag
            """,
            base_params,
        )
        facet_bestuurslaag = {row["bestuurslaag"]: row["n"] for row in cur.fetchall()}

        # ── Wro-bestemmingen (optioneel) ──────────────────────
        wro_rows: list[dict] = []
        if wro:
            wro_where = ["ri.pons_status = 'actief'"]
            wro_params: list = []
            if lagen:
                wro_where.append("b.bestuurslaag = ANY(%s)")
                wro_params.append(lagen)
            if bronhouders:
                wro_where.append("ri.bronhouder = ANY(%s)")
                wro_params.append(bronhouders)
            if q:
                # Wro: zoek alleen op naam (geen tekst-search; wro_tekst_object
                # is een optionele zware join).
                wro_where.append("ri.naam ILIKE %s")
                wro_params.append(f"%{q}%")

            cur.execute(
                f"""
                SELECT
                    ri.idn                                AS expression,
                    ri.naam                               AS titel,
                    ri.type_plan                          AS documenttype,
                    NULL::TEXT                            AS regelingmodel,
                    ri.bronhouder                         AS bronhouder_code,
                    b.naam                                AS bronhouder_naam,
                    b.bestuurslaag,
                    0                                     AS totaal_artikelen,
                    NULL::INTEGER                         AS hits_in_tekst
                FROM wro.ruimtelijk_instrument ri
                JOIN core.bronhouder b ON b.overheidscode = ri.bronhouder
                WHERE {' AND '.join(wro_where)}
                ORDER BY ri.naam
                """,
                wro_params,
            )
            wro_rows = cur.fetchall()

    # ── Combineren + sorteren + pagineren ─────────────────
    # Eerst Ow + Wro samenvoegen met regime-tag, dan sorteren volgens
    # `sort_by`, dan slicen voor pagination. Pas dáárna matched_artikelen
    # ophalen voor alleen de page-window — anders 1868 regelingen × N
    # match-queries voor niets.
    all_rows: list[tuple[dict, str]] = (
        [(r, "Ow") for r in ow_rows]
        + [(r, "Wro") for r in wro_rows]
    )

    def sort_key(item: tuple[dict, str]):
        row, _regime = item
        titel = (row["titel"] or "").lower()
        if sort_by == "relevantie":
            # Bij q-search: meer tekst-hits = hoger; NULL hits onderaan.
            # Bij geen q is hits_in_tekst NULL voor alle rijen, dus
            # effectief alfabetisch.
            hits = row.get("hits_in_tekst")
            return (-(hits or 0), titel)
        if sort_by == "datum":
            # Geen schone datum-kolom op p2p.regeling; voor Wro is er wel
            # ri.datum. v1: fallback op titel-sort tot loader/DDL-uitbreiding.
            # TODO: zodra p2p.regeling een vaststellings-/publicatie-datum
            # krijgt, hier sorteren op MAX(datum) DESC NULLS LAST.
            return (titel,)
        # default: 'titel'
        return (titel,)

    all_rows.sort(key=sort_key)
    totaal = len(all_rows)
    page_rows = all_rows[offset:offset + limit]

    # ── Phase B: matched_artikelen voor alleen de page-window ──
    matched_per_expression: dict[str, list[dict]] = {}
    if has_annotation_filter and page_rows:
        page_ow_expressions = [
            row["expression"] for row, regime in page_rows if regime == "Ow"
        ]
        if page_ow_expressions:
            with conn.cursor() as artikel_cur:
                matched_per_expression = _collect_matched_artikelen(
                    artikel_cur,
                    expressions=page_ow_expressions,
                    activiteitengroepen=activiteitengroepen,
                    typen_gebied=typen_gebied,
                    groepen_gebied=groepen_gebied,
                    normgroepen=normgroepen,
                    themas=themas,
                    soorten_hoofdlijn=soorten_hoofdlijn,
                    hoofdlijnen=hoofdlijnen,
                )

    # Reshape naar de RegelingHit-vorm die de frontend verwacht
    def to_hit(row: dict, regime: str) -> dict:
        matched = None
        if regime == "Ow" and has_annotation_filter:
            matched = matched_per_expression.get(row["expression"], [])
        return {
            "expression": row["expression"],
            "titel": row["titel"],
            "documenttype": row["documenttype"] or "Onbekend",
            "regelingmodel": row["regelingmodel"],
            "bronhouder": {
                "code": row["bronhouder_code"],
                "naam": row["bronhouder_naam"],
                "bestuurslaag": row["bestuurslaag"],
            },
            "regime": regime,
            "totaal_artikelen": row["totaal_artikelen"],
            "hits_in_tekst": row["hits_in_tekst"],
            "matched_artikelen": matched,
        }

    hits = [to_hit(row, regime) for row, regime in page_rows]

    return {
        "totaal": totaal,
        "regelingen": hits,
        "facets": {
            "bestuurslaag": facet_bestuurslaag,
        },
    }


@app.get("/v1/viewer/filter-options", dependencies=[Depends(verify_key)])
def viewer_filter_options():
    """Distinct waarden voor alle filter-dimensies van de zoeken-objecten-pagina.

    Vult de filter-sidebar van de viewer-zoekpagina met echte database-waarden
    in plaats van hard-coded mock-data. Bedoeld voor één call per page-load,
    dus alle queries lopen in dezelfde request/connection.

    Response:
      - regelingmodellen: list[str]            — distinct uit core.regelingmodel
      - documenttypen:    list[str]            — distinct uit core.documenttype
      - activiteitengroepen: list[str]         — distinct p2p.activiteit.groep (non-null)
      - omgevingsnormgroepen: list[str]        — distinct p2p.norm.groep (non-null)
      - themas: list[str]                      — distinct unnest(p2p.juridische_regel.thema)
      - gebiedsaanwijzingen: dict[str, list]   — type → groep[]
      - hoofdlijnen: dict[str, list]           — soort → naam[]

    Performance: alle queries in één roundtrip. De thema-query gebruikt een
    UNNEST + DISTINCT over een TEXT[]-kolom; bij grote datasets kan een GIN-
    index op `juridische_regel.thema` nodig zijn (TODO: meten en zo nodig
    een GIN(thema) toevoegen, eventueel materialized view).
    """
    with get_conn() as conn, conn.cursor() as cur:
        # Lookup-tabellen (klein, single column "code") ──
        cur.execute("SELECT code FROM core.regelingmodel ORDER BY code")
        regelingmodellen = [r["code"] for r in cur.fetchall()]

        cur.execute("SELECT code FROM core.documenttype ORDER BY code")
        documenttypen = [r["code"] for r in cur.fetchall()]

        # Activiteit-groepen — non-null, gesorteerd
        cur.execute(
            """
            SELECT DISTINCT groep
            FROM p2p.activiteit
            WHERE groep IS NOT NULL
            ORDER BY groep
            """
        )
        activiteitengroepen = [r["groep"] for r in cur.fetchall()]

        # Omgevingsnorm-groepen — non-null, gesorteerd
        cur.execute(
            """
            SELECT DISTINCT groep
            FROM p2p.norm
            WHERE groep IS NOT NULL
            ORDER BY groep
            """
        )
        omgevingsnormgroepen = [r["groep"] for r in cur.fetchall()]

        # Themas — distinct uit `tekstdeel.thema` (TEXT[]), gefilterd tegen
        # de IMOW Thema-waardelijst (`core.imow_thema`) zodat deprecated
        # thema's automatisch uit het filter verdwijnen. Inner join op label
        # met `deprecated = FALSE`.
        # `juridische_regel.thema` wordt door de loader nooit gevuld (zit
        # niet op regel-niveau in IMOW-praktijk); UNION laten staan voor het
        # geval een toekomstige loader-versie 'm wel gaat vullen.
        # TODO: bij trage response — overweeg GIN-index op tekstdeel(thema).
        cur.execute(
            """
            SELECT DISTINCT t.thema
            FROM (
                SELECT unnest(thema) AS thema FROM p2p.tekstdeel
                 WHERE thema IS NOT NULL
                UNION ALL
                SELECT unnest(thema) FROM p2p.juridische_regel
                 WHERE thema IS NOT NULL
            ) t
            JOIN core.imow_thema w ON w.label = t.thema AND NOT w.deprecated
            ORDER BY t.thema
            """
        )
        themas = [r["thema"] for r in cur.fetchall()]

        # Gebiedsaanwijzingen — type → groep[]-mapping
        cur.execute(
            """
            SELECT type, groep
            FROM p2p.gebiedsaanwijzing
            WHERE type IS NOT NULL
            GROUP BY type, groep
            ORDER BY type, groep
            """
        )
        gebiedsaanwijzingen: dict[str, list[str]] = {}
        for row in cur.fetchall():
            ga_type = row["type"]
            groep = row["groep"]
            bucket = gebiedsaanwijzingen.setdefault(ga_type, [])
            if groep is not None and groep not in bucket:
                bucket.append(groep)

        # Hoofdlijnen — soort → naam[]-mapping. We gebruiken de canonical
        # uit core.hoofdlijn_soort_mapping (zo schoner dan de rauwe IMOW-
        # soorten, die 47+ varianten hebben met case-verschillen en ad-hoc
        # beleidsteksten). Onbekende raw_values vallen terug op LOWER+TRIM
        # van zichzelf zodat het filter altijd werkt, ook voor net-geladen
        # documenten die nog niet in de mapping staan.
        cur.execute(
            """
            SELECT
                COALESCE(m.canonical,
                         CASE WHEN TRIM(h.soort) IN ('-', '')
                              THEN 'Overig'
                              ELSE LOWER(TRIM(h.soort))
                         END) AS soort_canonical,
                h.naam
            FROM p2p.hoofdlijn h
            LEFT JOIN core.hoofdlijn_soort_mapping m
                   ON m.raw_value = h.soort
            WHERE h.soort IS NOT NULL
            GROUP BY soort_canonical, h.naam
            ORDER BY soort_canonical, h.naam
            """
        )
        hoofdlijnen: dict[str, list[str]] = {}
        for row in cur.fetchall():
            soort = row["soort_canonical"]
            naam = row["naam"]
            bucket = hoofdlijnen.setdefault(soort, [])
            if naam is not None and naam not in bucket:
                bucket.append(naam)

    return {
        "regelingmodellen": regelingmodellen,
        "documenttypen": documenttypen,
        "activiteitengroepen": activiteitengroepen,
        "omgevingsnormgroepen": omgevingsnormgroepen,
        "themas": themas,
        "gebiedsaanwijzingen": gebiedsaanwijzingen,
        "hoofdlijnen": hoofdlijnen,
    }


@app.get("/v1/viewer/objecten", dependencies=[Depends(verify_key)])
def viewer_objecten(x: float = Query(...), y: float = Query(...)):
    """Alle OW-objecten op een RD-coördinaat, over alle regelingen heen.

    Retourneert vijf categorieën:
      - gebiedsaanwijzingen
      - activiteitlocatieaanduidingen (ALA's, dedup op naam+kwalificatie+groep)
      - omgevingsnormen (uniek via normwaarde-join)
      - normwaarden (concrete waarden)
      - ongetypeerde_locaties (locaties zonder GA/ALA/Normwaarde-binding)
      - wro_bestemmingen
    """
    point = "ST_SetSRID(ST_MakePoint(%s, %s), 28992)"
    with get_conn() as conn, conn.cursor() as cur:
        # Gebiedsaanwijzingen — incl. locatie_ids zodat de frontend de
        # geometrie kan ophalen voor hover/highlight én documentenlijst-kaart.
        cur.execute(
            f"""
            SELECT ga.type, ga.naam, ga.groep,
                   r.opschrift AS regeling, r.documenttype,
                   ARRAY_AGG(DISTINCT ga.locatie_id) AS locatie_ids
            FROM p2p.gebiedsaanwijzing ga
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = ga.locatie_id
            JOIN p2p.juridische_regel_gebiedsaanwijzing jrga
                   ON jrga.gebiedsaanwijzing_id = ga.identificatie
            JOIN p2p.juridische_regel jr ON jr.identificatie = jrga.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE ST_Intersects(ls.geometrie, {point})
            GROUP BY ga.type, ga.naam, ga.groep, r.opschrift, r.documenttype
            ORDER BY ga.type, ga.naam
            """,
            (x, y),
        )
        gebiedsaanwijzingen = cur.fetchall()

        # Activiteitlocatieaanduidingen — dedup op (naam, kwalificatie, groep)
        # met alle regelingen + locatie_ids voor hover-highlight op de kaart.
        cur.execute(
            f"""
            SELECT a.naam,
                   a.groep,
                   ala.kwalificatie,
                   ARRAY_AGG(DISTINCT r.opschrift) AS regelingen,
                   ARRAY_AGG(DISTINCT ala.locatie_id) AS locatie_ids
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = ala.locatie_id
            JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE ST_Intersects(ls.geometrie, {point})
              AND a.is_tophaak = FALSE
              AND a.naam NOT ILIKE '%%gereguleerd in%%'
              AND a.naam NOT ILIKE '%%gereguleerd bij%%'
            GROUP BY a.naam, a.groep, ala.kwalificatie
            ORDER BY a.groep, ala.kwalificatie, a.naam
            """,
            (x, y),
        )
        activiteitlocatieaanduidingen = cur.fetchall()

        # Omgevingsnormen — uniek per norm met regelingen-array
        cur.execute(
            f"""
            SELECT n.naam,
                   n.type_norm,
                   n.eenheid,
                   n.groep,
                   ARRAY_AGG(DISTINCT r.opschrift) AS regelingen
            FROM p2p.normwaarde nw
            JOIN p2p.norm n ON n.identificatie = nw.norm_id
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = nw.locatie_id
            JOIN p2p.juridische_regel_norm jrn ON jrn.norm_id = n.identificatie
            JOIN p2p.juridische_regel jr ON jr.identificatie = jrn.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE ST_Intersects(ls.geometrie, {point})
            GROUP BY n.identificatie, n.naam, n.type_norm, n.eenheid, n.groep
            ORDER BY n.naam
            """,
            (x, y),
        )
        omgevingsnormen = cur.fetchall()

        # Normwaarden (concrete waarden, geen dedup omdat de waarde zelf relevant is)
        cur.execute(
            f"""
            SELECT n.naam, n.type_norm, n.eenheid,
                   nw.kwantitatieve_waarde, nw.kwalitatieve_waarde,
                   r.opschrift AS regeling,
                   ARRAY_AGG(DISTINCT nw.locatie_id) AS locatie_ids
            FROM p2p.normwaarde nw
            JOIN p2p.norm n ON n.identificatie = nw.norm_id
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = nw.locatie_id
            JOIN p2p.juridische_regel_norm jrn ON jrn.norm_id = n.identificatie
            JOIN p2p.juridische_regel jr ON jr.identificatie = jrn.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE ST_Intersects(ls.geometrie, {point})
            GROUP BY n.naam, n.type_norm, n.eenheid,
                     nw.kwantitatieve_waarde, nw.kwalitatieve_waarde, r.opschrift
            ORDER BY n.naam
            """,
            (x, y),
        )
        normwaarden = [
            {
                **row,
                "waarde": (
                    float(row["kwantitatieve_waarde"])
                    if row["kwantitatieve_waarde"] is not None
                    else row["kwalitatieve_waarde"]
                ),
            }
            for row in cur.fetchall()
        ]

        # Ongetypeerde locaties — raken het punt maar hebben geen
        # GA/ALA/Normwaarde-binding. locatie_id == identificatie zodat de
        # frontend dezelfde geometrie-loader kan gebruiken.
        cur.execute(
            f"""
            SELECT DISTINCT l.identificatie, l.noemer, l.locatie_type
            FROM p2p.locatie_subdiv ls
            JOIN p2p.locatie l ON l.identificatie = ls.identificatie
            WHERE ST_Intersects(ls.geometrie, {point})
              AND NOT EXISTS (SELECT 1 FROM p2p.gebiedsaanwijzing g
                                WHERE g.locatie_id = l.identificatie)
              AND NOT EXISTS (SELECT 1 FROM p2p.activiteit_locatieaanduiding a
                                WHERE a.locatie_id = l.identificatie)
              AND NOT EXISTS (SELECT 1 FROM p2p.normwaarde nw
                                WHERE nw.locatie_id = l.identificatie)
            ORDER BY l.noemer NULLS LAST, l.identificatie
            """,
            (x, y),
        )
        ongetypeerde_locaties = [
            {**row, "locatie_ids": [row["identificatie"]]}
            for row in cur.fetchall()
        ]

        # Wro-bestemmingen
        cur.execute(
            f"""
            SELECT DISTINCT po.object_type, po.naam, po.bestemmingshoofdgroep,
                   ri.naam AS plan
            FROM wro.planobject po
            JOIN wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
            WHERE ST_Intersects(po.geometrie, {point})
              AND ri.pons_status = 'actief'
            ORDER BY ri.naam, po.object_type
            """,
            (x, y),
        )
        wro_bestemmingen = cur.fetchall()

    return {
        "locatie": {"x": x, "y": y},
        "gebiedsaanwijzingen": gebiedsaanwijzingen,
        "activiteitlocatieaanduidingen": activiteitlocatieaanduidingen,
        "omgevingsnormen": omgevingsnormen,
        "normwaarden": normwaarden,
        "ongetypeerde_locaties": ongetypeerde_locaties,
        "wro_bestemmingen": wro_bestemmingen,
    }


def _viewer_geometrie(ids: list[str]) -> dict:
    """Bouw FeatureCollection voor een lijst locatie-identificaties.

    Gedeeld door zowel de GET- als POST-variant van /viewer/geometrie. POST
    is bedoeld voor grote lijsten (>~100 IDs), waar de GET-URL anders > 8KB
    wordt en uvicorn 414 retourneert.
    """
    if not ids:
        return {"type": "FeatureCollection", "features": []}

    with get_conn() as conn, conn.cursor() as cur:
        # Geometrie + gebiedsaanwijzing-metadata voor kleuring per type
        cur.execute(
            """
            SELECT l.identificatie,
                   l.locatie_type,
                   l.noemer,
                   ga.type  AS ga_type,
                   ga.naam  AS ga_naam,
                   ga.groep AS ga_groep,
                   ST_AsGeoJSON(l.geometrie)::json AS geometry
            FROM p2p.locatie l
            LEFT JOIN p2p.gebiedsaanwijzing ga ON ga.locatie_id = l.identificatie
            WHERE l.identificatie = ANY(%s)
            """,
            (ids,),
        )
        features = [
            {
                "type": "Feature",
                "properties": {
                    # Uniforme keys voor de hele frontend (filter, hover, panel).
                    "naam": row["ga_naam"] or row["noemer"] or row["identificatie"],
                    "categorie": "gebiedsaanwijzing" if row["ga_naam"] else "ongetypeerd",
                    # Categorie-specifieke keys (kaart-styling/popup leunt hierop).
                    "identificatie": row["identificatie"],
                    "locatie_type": row["locatie_type"],
                    "noemer": row["noemer"],
                    "ga_type": row["ga_type"],
                    "ga_naam": row["ga_naam"],
                    "ga_groep": row["ga_groep"],
                },
                "geometry": row["geometry"],
            }
            for row in cur.fetchall()
        ]

    return {"type": "FeatureCollection", "features": features}


class GeometrieRequest(BaseModel):
    locatie_ids: list[str]


@app.get("/v1/viewer/geometrie", dependencies=[Depends(verify_key)])
def viewer_geometrie(
    locatie_ids: str = Query(..., description="Komma-gescheiden locatie-identificaties"),
):
    """GeoJSON FeatureCollection voor de opgegeven locaties (GET, kort)."""
    ids = [lid.strip() for lid in locatie_ids.split(",") if lid.strip()]
    return _viewer_geometrie(ids)


@app.post("/v1/viewer/geometrie", dependencies=[Depends(verify_key)])
def viewer_geometrie_post(req: GeometrieRequest = Body(...)):
    """GeoJSON FeatureCollection voor een (mogelijk grote) lijst locaties.

    POST-variant: gebruikt voor regelingen met honderden locatie-IDs waar de
    GET-URL te lang zou worden.
    """
    ids = [lid.strip() for lid in req.locatie_ids if lid and lid.strip()]
    return _viewer_geometrie(ids)


@app.get("/v1/viewer/regeling/{expression:path}/ala", dependencies=[Depends(verify_key)])
def viewer_ala(
    expression: str,
    x: float = Query(None),
    y: float = Query(None),
):
    """ActiviteitLocatieaanduidingen als GeoJSON voor kaartweergave.

    Elke feature is een locatie met als properties de activiteit-naam,
    kwalificatie, en het artikel waar de ALA uit komt. Dit maakt het
    mogelijk om op de kaart te tonen waar welke activiteit met welke
    kwalificatie geldt — vergelijkbaar met "Regels op de kaart".
    """
    loc_filter = ""
    loc_params: list = []
    if x is not None and y is not None:
        loc_filter = "AND ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))"
        loc_params = [x, y]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (a.naam, ala.kwalificatie, l.identificatie)
                a.naam              AS activiteit,
                a.groep             AS activiteit_groep,
                ala.kwalificatie,
                ocd_artikel_label(te.opschrift, te.wid)        AS artikel,
                te.wid              AS artikel_wid,
                l.identificatie     AS locatie_id,
                l.noemer            AS locatie_noemer,
                ST_AsGeoJSON(l.geometrie)::json AS geometry
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.activiteit a        ON a.identificatie = ala.activiteit_id
            JOIN p2p.locatie l            ON l.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr  ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te     ON te.wid = jr.regeltekst_wid
                                         AND te.regeling_expression = %s
            {("JOIN p2p.locatie_subdiv ls ON ls.identificatie = ala.locatie_id" if loc_filter else "")}
            WHERE TRUE {loc_filter}
            ORDER BY a.naam, ala.kwalificatie, l.identificatie
            """,
            (expression, *loc_params),
        )
        features = [
            {
                "type": "Feature",
                "properties": {
                    "naam": row["activiteit"],
                    "categorie": "activiteit",
                    "activiteit": row["activiteit"],
                    "activiteit_groep": row["activiteit_groep"],
                    "kwalificatie": row["kwalificatie"],
                    "artikel": row["artikel"],
                    "artikel_wid": row["artikel_wid"],
                    "locatie_id": row["locatie_id"],
                    "locatie_noemer": row["locatie_noemer"],
                },
                "geometry": row["geometry"],
            }
            for row in cur.fetchall()
        ]

    return {"type": "FeatureCollection", "features": features}


@app.get("/v1/viewer/wro/{idn}/detail", dependencies=[Depends(verify_key)])
def viewer_wro_detail(
    idn: str,
    x: float = Query(None),
    y: float = Query(None),
):
    """Wro-bestemmingsplan detail: planobjecten (bestemmingen) + teksten + geometrie.

    Retourneert bestemmingen als GeoJSON features + een teksten-array.
    Wanneer x/y meegegeven worden, worden alleen objecten geretourneerd
    die het opgegeven punt raken.
    """
    loc_filter = ""
    loc_params: list = []
    if x is not None and y is not None:
        loc_filter = "AND ST_Intersects(po.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))"
        loc_params = [x, y]

    with get_conn() as conn, conn.cursor() as cur:
        # Plan-metadata
        cur.execute(
            """
            SELECT ri.idn, ri.naam, ri.type_plan, ri.planstatus, ri.datum,
                   ri.pons_status, b.naam AS bronhouder
            FROM wro.ruimtelijk_instrument ri
            JOIN core.bronhouder b ON b.overheidscode = ri.bronhouder
            WHERE ri.idn = %s
            """,
            (idn,),
        )
        plan = cur.fetchone()
        if not plan:
            raise HTTPException(404, "Wro-plan niet gevonden")

        # Planobjecten als GeoJSON
        cur.execute(
            f"""
            SELECT po.identificatie, po.object_type, po.naam,
                   po.bestemmingshoofdgroep, po.artikelnummer,
                   po.maatvoering_info,
                   ST_AsGeoJSON(po.geometrie)::json AS geometry
            FROM wro.planobject po
            WHERE po.instrument_idn = %s {loc_filter}
            ORDER BY po.object_type, po.naam
            """,
            (idn, *loc_params),
        )
        features = [
            {
                "type": "Feature",
                "properties": {
                    "naam": row["naam"] or row["object_type"],
                    "categorie": "bestemming",
                    "identificatie": row["identificatie"],
                    "object_type": row["object_type"],
                    "bestemmingshoofdgroep": row["bestemmingshoofdgroep"],
                    "artikelnummer": row["artikelnummer"],
                    "maatvoering": row["maatvoering_info"],
                },
                "geometry": row["geometry"],
            }
            for row in cur.fetchall()
        ]

        # Teksten
        cur.execute(
            """
            SELECT wt.naam, wt.label, wt.nummer, wt.inhoud,
                   wt.object_type, wt.niveau
            FROM wro.wro_tekst_object wt
            WHERE wt.instrument_idn = %s
            ORDER BY wt.volgnummer
            """,
            (idn,),
        )
        teksten = cur.fetchall()

        # Check of er een conv-versie bestaat voor dit plan
        cur.execute(
            """
            SELECT cm.regeling_expression, cm.stap, cm.bron, cm.llm_model
            FROM conv.conversie_meta cm
            WHERE cm.instrument_idn = %s
            ORDER BY cm.stap DESC
            LIMIT 1
            """,
            (idn,),
        )
        conv_meta = cur.fetchone()

    return {
        "plan": {
            "idn": plan["idn"],
            "naam": plan["naam"],
            "type": plan["type_plan"],
            "status": plan["planstatus"],
            "datum": str(plan["datum"]) if plan["datum"] else None,
            "pons_status": plan["pons_status"],
            "bronhouder": plan["bronhouder"],
        },
        "bestemmingen": {"type": "FeatureCollection", "features": features},
        "teksten": teksten,
        "conv": {
            "beschikbaar": conv_meta is not None,
            "expression": conv_meta["regeling_expression"] if conv_meta else None,
            "stap": conv_meta["stap"] if conv_meta else None,
            "bron": conv_meta["bron"] if conv_meta else None,
            "model": conv_meta["llm_model"] if conv_meta else None,
        },
    }


@app.get("/v1/viewer/conv/{expression:path}/boom", dependencies=[Depends(verify_key)])
def viewer_conv_boom(expression: str):
    """Geconverteerde Wro→Ow boom uit het conv-schema.

    Zelfde structuur als /v1/viewer/regeling/{expression}/boom, maar
    leest uit conv.* in plaats van p2p.*. Dit maakt het mogelijk om
    een bestemmingsplan naast de geconverteerde Ow-variant te tonen.
    """
    with get_conn() as conn, conn.cursor() as cur:
        # Regeling-metadata
        cur.execute(
            "SELECT frbr_expression, opschrift, documenttype FROM conv.regeling WHERE frbr_expression = %s",
            (expression,),
        )
        regeling = cur.fetchone()
        if not regeling:
            raise HTTPException(404, "Geconverteerde regeling niet gevonden")

        # Documentstructuur
        cur.execute(
            """
            SELECT id, eid, wid, element_type, parent_id,
                   nummer, opschrift, inhoud AS tekst, volgorde
            FROM conv.tekst_element
            WHERE regeling_expression = %s
            ORDER BY volgorde
            """,
            (expression,),
        )
        boom = _build_boom(cur.fetchall())

        # Annotaties — activiteiten
        cur.execute(
            """
            SELECT jr.regeltekst_wid,
                   a.naam AS activiteit_naam,
                   a.groep AS activiteit_groep,
                   ala.kwalificatie,
                   ala.locatie_id AS ala_locatie_id
            FROM conv.juridische_regel jr
            JOIN conv.tekst_element te ON te.wid = jr.regeltekst_wid
                                      AND te.regeling_expression = %s
            LEFT JOIN conv.activiteit_locatieaanduiding ala
                   ON ala.juridische_regel_id = jr.identificatie
            LEFT JOIN conv.activiteit a
                   ON a.identificatie = ala.activiteit_id
            """,
            (expression,),
        )
        act_rows = cur.fetchall()

        # Gebiedsaanwijzingen
        cur.execute(
            """
            SELECT jr.regeltekst_wid,
                   ga.identificatie AS ga_id, ga.type AS ga_type,
                   ga.naam AS ga_naam, ga.groep AS ga_groep,
                   ga.locatie_id AS ga_locatie_id
            FROM conv.juridische_regel jr
            JOIN conv.tekst_element te ON te.wid = jr.regeltekst_wid
                                      AND te.regeling_expression = %s
            JOIN conv.juridische_regel_gebiedsaanwijzing jrga
                   ON jrga.juridische_regel_id = jr.identificatie
            JOIN conv.gebiedsaanwijzing ga
                   ON ga.identificatie = jrga.gebiedsaanwijzing_id
            """,
            (expression,),
        )
        ga_rows = cur.fetchall()

        # Normwaarden
        cur.execute(
            """
            SELECT jr.regeltekst_wid,
                   n.naam AS norm_naam, n.type_norm, n.eenheid,
                   nw.kwantitatieve_waarde, nw.kwalitatieve_waarde,
                   nw.locatie_id AS nw_locatie_id
            FROM conv.juridische_regel jr
            JOIN conv.tekst_element te ON te.wid = jr.regeltekst_wid
                                      AND te.regeling_expression = %s
            JOIN conv.juridische_regel_norm jrn
                   ON jrn.juridische_regel_id = jr.identificatie
            JOIN conv.norm n ON n.identificatie = jrn.norm_id
            LEFT JOIN conv.normwaarde nw ON nw.norm_id = n.identificatie
            """,
            (expression,),
        )
        nw_rows = cur.fetchall()

    # Groepeer annotaties per regeltekst_wid (zelfde logica als viewer_boom)
    annot: dict[str, dict] = {}
    locatie_ids: set[str] = set()

    for row in act_rows:
        wid = row["regeltekst_wid"]
        annot.setdefault(wid, {"activiteiten": [], "gebiedsaanwijzingen": [], "normwaarden": []})
        if row["activiteit_naam"]:
            entry = {"naam": row["activiteit_naam"], "groep": row["activiteit_groep"], "kwalificatie": row["kwalificatie"]}
            if entry not in annot[wid]["activiteiten"]:
                annot[wid]["activiteiten"].append(entry)
        if row.get("ala_locatie_id"):
            locatie_ids.add(row["ala_locatie_id"])

    for row in ga_rows:
        wid = row["regeltekst_wid"]
        annot.setdefault(wid, {"activiteiten": [], "gebiedsaanwijzingen": [], "normwaarden": []})
        entry = {"id": row["ga_id"], "type": row["ga_type"], "naam": row["ga_naam"], "groep": row["ga_groep"], "locatie_id": row["ga_locatie_id"]}
        if entry not in annot[wid]["gebiedsaanwijzingen"]:
            annot[wid]["gebiedsaanwijzingen"].append(entry)
        locatie_ids.add(row["ga_locatie_id"])

    for row in nw_rows:
        wid = row["regeltekst_wid"]
        annot.setdefault(wid, {"activiteiten": [], "gebiedsaanwijzingen": [], "normwaarden": []})
        entry = {
            "naam": row["norm_naam"], "type_norm": row["type_norm"], "eenheid": row["eenheid"],
            "waarde": float(row["kwantitatieve_waarde"]) if row["kwantitatieve_waarde"] is not None else row["kwalitatieve_waarde"],
        }
        if entry not in annot[wid]["normwaarden"]:
            annot[wid]["normwaarden"].append(entry)
        if row.get("nw_locatie_id"):
            locatie_ids.add(row["nw_locatie_id"])

    _annoteer_boom(boom, annot)

    # Conversie-metadata
    conv_meta_row = None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT instrument_idn, stap, bron, llm_model FROM conv.conversie_meta WHERE regeling_expression = %s ORDER BY stap DESC LIMIT 1",
            (expression,),
        )
        conv_meta_row = cur.fetchone()

    return {
        "regeling": {
            "expression": regeling["frbr_expression"],
            "titel": regeling["opschrift"],
            "type": regeling["documenttype"],
        },
        "boom": boom,
        "locatie_ids": sorted(locatie_ids),
        "conversie": {
            "instrument_idn": conv_meta_row["instrument_idn"] if conv_meta_row else None,
            "stap": conv_meta_row["stap"] if conv_meta_row else None,
            "bron": conv_meta_row["bron"] if conv_meta_row else None,
            "model": conv_meta_row["llm_model"] if conv_meta_row else None,
        } if conv_meta_row else None,
    }


# ─────────────────────────────────────────────────────────────────────
# Wijzigingen-overlay (Plan B)
# ─────────────────────────────────────────────────────────────────────

# Annotatie-types die in de viewer-overlay zichtbaar zijn. SKOS-pipeline-
# types als `regeltekst` en `juridische_regel` zijn technische koppelingen
# tussen tekst en object — geen IMOW-objecten waar de gebruiker als 'object'
# naar kijkt. Filteren scheelt ~95% van de annotatie-deltas zonder UI-verlies.
_WIJZIGING_IMOW_TYPES = (
    "activiteit",
    "gebiedsaanwijzing",
    "omgevingsnorm",
    "omgevingswaarde",
    "locatie",
    "tekstdeel",
)


def _strip_tekst_elementen(rows: list[dict]) -> list[dict]:
    """Houd alleen elementen met `wijzigactie`/`vervallen`/`bevatRenvooi` +
    hun parent-chain naar de root, zodat de boom-hiërarchie compleet blijft.

    Spiegelt de fixture-strip uit Plan A — zonder deze filter krijgt de
    frontend de volle-boom-mirror per bron (~1500 rijen voor een gemiddeld
    omgevingsplan × N bronnen). Met filter typisch <50 rijen per bron."""
    by_id = {r["id"]: r for r in rows}
    keep_ids: set[int] = set()
    for r in rows:
        if not (r["wijzigactie"] or r["vervallen"] or r["bevat_renvooi"]):
            continue
        current = r
        while current is not None:
            keep_ids.add(current["id"])
            pid = current.get("parent_id")
            if pid is None:
                break
            current = by_id.get(pid)
    return [r for r in rows if r["id"] in keep_ids]


def _row_to_tekst_element(r: dict) -> dict:
    """snake_case DB-rij → camelCase TS-shape (zie wijziging.model.ts)."""
    return {
        "id": r["id"],
        "parentId": r["parent_id"],
        "eid": r["eid"],
        "wid": r["wid"],
        "elementType": r["element_type"],
        "nummer": r["nummer"],
        "opschrift": r["opschrift"],
        "inhoud": r["inhoud"],
        "wijzigactie": r["wijzigactie"],
        "vervallen": r["vervallen"],
        "bevatRenvooi": r["bevat_renvooi"],
        "bevatOntwerpInformatie": r["bevat_ontwerp_informatie"],
        "volgorde": r["volgorde"],
    }


def _row_to_annotatie_delta(r: dict) -> dict:
    return {
        "type": r["type"],
        "identificatie": r["identificatie"],
        "bewerking": r["bewerking"],
        "naam": r["naam"],
        "payload": r["payload"],
    }


def _row_to_locatie_delta(r: dict) -> dict:
    # ST_AsGeoJSON levert een JSON-string; psycopg returnt 'm als str.
    # json.loads ééns hier zodat de response-laag een gestructureerd object
    # zonder dubbel-encoded JSON-string-veld krijgt.
    geom = r.get("geometrie_json")
    if isinstance(geom, str):
        import json
        geom = json.loads(geom)
    return {
        "locatieId": r["locatie_id"],
        "bewerking": r["bewerking"],
        "locatieType": r["locatie_type"],
        "noemer": r["noemer"],
        "geometrie": geom,
    }


def _row_to_besluit_meta(r: dict) -> dict:
    return {
        "ontwerpbesluitId": r["ontwerpbesluit_id"],
        "soort": r["soort"],
        "status": r["status"],
        "opschrift": r["opschrift"],
        "bekendOp": r["bekend_op"].isoformat() if r["bekend_op"] else None,
        "beginGeldigheid": r["begin_geldigheid"].isoformat() if r["begin_geldigheid"] else None,
        "beginInwerking": r["begin_inwerking"].isoformat() if r["begin_inwerking"] else None,
        "bronhouder": r["bronhouder"],
        "documenttype": r["documenttype"],
        "isVervangRegeling": r["is_vervang_regeling"],
    }


@app.get("/v1/viewer/regeling/{expression:path}/wijzigingen",
        dependencies=[Depends(verify_key)])
def viewer_wijzigingen(expression: str):
    """Aankomende wijzigingen (ontwerpen + besluitversies) op een regeling.

    Volgt de Plan A-TS-shape (`WijzigingenFixture`): één response met
    `regelingWork` + `wijzigingen[]`. Per bron de gestripte tekst-elementen
    (gewijzigd + parent-chain), IMOW-annotatie-deltas en locatie-deltas
    (incl. NULL-geometrie waar de backfill nog niet liep — Plan D)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT frbr_work FROM p2p.regeling WHERE frbr_expression = %s",
            (expression,),
        )
        reg = cur.fetchone()
        if not reg:
            raise HTTPException(404, "Regeling niet gevonden")
        regeling_work = reg["frbr_work"]

        # Bronnen — exclusief vervangRegeling-besluiten (die zijn geen
        # renvooi-overlay; volledige nieuwe regeling).
        cur.execute(
            """
            SELECT ontwerpbesluit_id, soort, status, opschrift,
                   bekend_op, begin_geldigheid, begin_inwerking,
                   bronhouder, documenttype, is_vervang_regeling
            FROM   p2pwijziging.besluit
            WHERE  regeling_work = %s
              AND  is_vervang_regeling = FALSE
            ORDER  BY bekend_op NULLS LAST
            """,
            (regeling_work,),
        )
        besluiten = cur.fetchall()

        wijzigingen = []
        for b in besluiten:
            ob_id = b["ontwerpbesluit_id"]

            cur.execute(
                """
                SELECT id, parent_id, eid, wid, element_type,
                       nummer, opschrift, inhoud, wijzigactie, vervallen,
                       bevat_renvooi, bevat_ontwerp_informatie, volgorde
                FROM   p2pwijziging.tekst_element
                WHERE  ontwerpbesluit_id = %s
                ORDER  BY volgorde
                """,
                (ob_id,),
            )
            tekst_rows = cur.fetchall()
            tekst_kept = _strip_tekst_elementen(tekst_rows)

            cur.execute(
                """
                SELECT type, identificatie, bewerking, naam, payload
                FROM   p2pwijziging.annotatie_delta
                WHERE  ontwerpbesluit_id = %s
                  AND  type = ANY(%s)
                """,
                (ob_id, list(_WIJZIGING_IMOW_TYPES)),
            )
            ann_rows = cur.fetchall()

            cur.execute(
                """
                SELECT locatie_id, bewerking, locatie_type, noemer,
                       ST_AsGeoJSON(geometrie) AS geometrie_json
                FROM   p2pwijziging.locatie_delta
                WHERE  ontwerpbesluit_id = %s
                """,
                (ob_id,),
            )
            loc_rows = cur.fetchall()

            wijziging = _row_to_besluit_meta(b)
            wijziging["tekstElementen"] = [_row_to_tekst_element(r) for r in tekst_kept]
            wijziging["annotatieDeltas"] = [_row_to_annotatie_delta(r) for r in ann_rows]
            wijziging["locatieDeltas"] = [_row_to_locatie_delta(r) for r in loc_rows]
            wijzigingen.append(wijziging)

    return {"regelingWork": regeling_work, "wijzigingen": wijzigingen}


@app.get("/v1/overzicht", dependencies=[Depends(verify_key)])
def overzicht():
    """Database-overzicht: totalen per tabel."""
    tables = [
        ("core", "bronhouder"),
        ("p2p", "regeling"), ("p2p", "tekst_element"), ("p2p", "juridische_regel"),
        ("p2p", "activiteit"), ("p2p", "locatie"), ("p2p", "gebiedsaanwijzing"),
        ("p2p", "norm"), ("p2p", "normwaarde"),
        ("i2a", "toepasbaar_regelbestand"), ("i2a", "dmn_element"), ("i2a", "werkzaamheid"),
        ("wro", "ruimtelijk_instrument"), ("wro", "planobject"), ("wro", "wro_tekst_object"),
    ]
    counts: dict[str, int] = {}
    with get_conn() as conn, conn.cursor() as cur:
        for schema, t in tables:
            cur.execute(f"SELECT count(*) AS n FROM {schema}.{t}")
            row = cur.fetchone()
            counts[t] = row["n"] if row else 0
    return {"tabellen": counts, "totaal": sum(counts.values())}
