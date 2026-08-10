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
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, JSONResponse

from antwoord_bij_vraag import router as antwoord_router
from db import get_conn, pool
from expand import router as expand_router
from kennis import router as kennis_router
from keywords import router as keywords_router
from planvoorraad import router as planvoorraad_router
from ponsenkaart import router as ponsenkaart_router
from regelteksten_bij_vraag import router as regelteksten_router
from semantisch import router as semantisch_router
from tiles import router as tiles_router
from vergunningen import _tsquery_arg
from vergunningen import router as vergunningen_router
from mer import router as mer_router

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


# Gzip op alle responses > 1 KB. De viewer-endpoints leveren GeoJSON, dat
# 3-17x comprimeert (gemeten: /viewer/objecten 269 KB -> 15,5 KB,
# /viewer/geometrie 3,4 MB -> 1,1 MB). Vóór CORS toegevoegd zodat CORS de
# buitenste laag blijft en de headers ook op gecomprimeerde responses staan.
app.add_middleware(GZipMiddleware, minimum_size=1000)

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
        "https://mer-register.nl",                         # MER-register (kanaal A+B)
        "https://www.mer-register.nl",
        "https://mer-register.pages.dev",                  # Cloudflare Pages (pre-domein)
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
app.include_router(antwoord_router, dependencies=[Depends(verify_key)])
app.include_router(semantisch_router, dependencies=[Depends(verify_key)])
app.include_router(vergunningen_router, dependencies=[Depends(verify_key)])
app.include_router(mer_router, dependencies=[Depends(verify_key)])
app.include_router(planvoorraad_router, dependencies=[Depends(verify_key)])
app.include_router(ponsenkaart_router, dependencies=[Depends(verify_key)])
app.include_router(expand_router, dependencies=[Depends(verify_key)])
app.include_router(kennis_router, dependencies=[Depends(verify_key)])
app.include_router(tiles_router, dependencies=[Depends(verify_key)])


@app.get("/health")
def health():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
    return {"status": "ok"}


@app.get("/v1/data-health", dependencies=[Depends(verify_key)])
def data_health(
    bronhouder: str = Query(None, description="Optioneel: 1 bronhouder-code (bv. pv25)"),
    problemen: bool = Query(False, description="Alleen bronhouders met een integriteits-/load-flag"),
):
    """Datakwaliteit-rapportage uit core.mv_bronhouder_health + samenvattingen.

    Doel: in één call zien of een lage meting door de DATA komt of door de
    AANPAK. Zonder parameters → totaal-samenvatting (v_data_health) + geo-health.
    Met `bronhouder` → die ene rij. Met `problemen=true` → alleen bronhouders
    met code-only/duplicaat/pdok-mismatch-naam, regelingen zonder tekst, of
    een DSO-coverage-gat. De matview is een snapshot; refresh met
    `REFRESH MATERIALIZED VIEW core.mv_bronhouder_health`.
    """
    cols = ("overheidscode, naam, bestuurslaag, n_regelingen, n_regelingen_zonder_tekst, "
            "dso_mist, dso_over, is_code_only, is_duplicate_naam, pdok_mismatch, "
            "artikel_dekking_pct, pct_brede_scope, pct_anders_geduid")
    with get_conn() as conn, conn.cursor() as cur:
        if bronhouder:
            cur.execute(f"SELECT {cols} FROM core.mv_bronhouder_health WHERE overheidscode = %s",
                        (bronhouder,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"bronhouder {bronhouder} niet gevonden")
            return {"bronhouder": row}

        if problemen:
            cur.execute(
                f"""SELECT {cols} FROM core.mv_bronhouder_health
                    WHERE is_code_only OR is_duplicate_naam OR pdok_mismatch
                       OR n_regelingen_zonder_tekst > 0 OR COALESCE(dso_mist, 0) > 0
                    ORDER BY COALESCE(dso_mist,0) DESC, n_regelingen_zonder_tekst DESC,
                             overheidscode""")
            return {"problemen": cur.fetchall()}

        cur.execute("SELECT * FROM core.v_data_health")
        samenvatting = cur.fetchone()
        # mv_geo_health is de gematerialiseerde snapshot (v_geo_health kost ~26s →
        # timeout). Fallback op de live view als de matview nog niet bestaat
        # (bv. prod vóór de 2026-07-21-migratie).
        try:
            cur.execute("SELECT * FROM core.mv_geo_health")
        except Exception:
            conn.rollback()
            cur.execute("SELECT * FROM core.v_geo_health")
        geo = cur.fetchone()
    return {"samenvatting": samenvatting, "geo": geo}


@app.get("/v1/load-status", dependencies=[Depends(verify_key)])
def load_status(
    historie_limiet: int = Query(80, le=500, description="Max aantal runs in de historie-tijdlijn"),
):
    """Data-actualiteit: wanneer is welke bron voor het laatst bijgewerkt?

    Voedt het data-actualiteit-dashboard (standalone HTML + OCDviewer).
    `bronnen` = laatste run per bron (core.v_load_status), `totalen` = live
    totaal per bron, `lopend` = nu draaiende runs, `laatst_bijgewerkt` = meest
    recente geslaagde finished_at (glance), `bronhouders` = samenvatting van
    core.bronhouder.laatst_geladen.

    Uitgebreid met historie zodat je de vórige synchronisatie kunt terugzien:
    `historie` = laatste N runs per bron (tijdlijn + diff), `sync_runs` = de
    hele-sync-momenten uit audit.sync_run (dropdown-kiezer). Zie
    dso-loader/docs/bijwerken.md.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM core.v_load_status ORDER BY bron")
        bronnen = cur.fetchall()

        # Live exacte tellingen per bron. Deze view telt per bron-tabel en is bij
        # een koude buffercache (of gelijktijdige sync-writes) traag genoeg om
        # tegen de statement_timeout aan te tikken → dan zou de HELE monitor 500'en.
        # Daarom een korte eigen timeout + terugval op de laatste sync-snapshot
        # (audit.sync_run.totalen), zodat het dashboard altijd laadt.
        totalen_bron = "live"
        try:
            cur.execute("SET LOCAL statement_timeout = 8000")
            cur.execute("SELECT bron, totaal FROM core.v_bron_totalen")
            totalen = {r["bron"]: r["totaal"] for r in cur.fetchall()}
        except Exception:
            conn.rollback()
            totalen_bron = "snapshot"
            try:
                cur.execute(
                    "SELECT totalen FROM audit.sync_run "
                    "WHERE totalen IS NOT NULL ORDER BY gestart_op DESC LIMIT 1")
                row = cur.fetchone()
                totalen = dict(row["totalen"]) if row and row["totalen"] else {}
            except Exception:
                conn.rollback()
                totalen = {}
                totalen_bron = "onbekend"

        cur.execute(
            "SELECT bron, scope, started_at FROM core.load_run "
            "WHERE status = 'running' ORDER BY started_at")
        lopend = cur.fetchall()

        cur.execute(
            "SELECT max(finished_at) AS laatst_bijgewerkt FROM core.load_run "
            "WHERE status IN ('ok', 'deels')")
        laatst_bijgewerkt = cur.fetchone()["laatst_bijgewerkt"]

        cur.execute(
            "SELECT count(*) FILTER (WHERE laatst_geladen IS NOT NULL) AS met_laatst_geladen, "
            "count(*) AS totaal, min(laatst_geladen) AS oudste, max(laatst_geladen) AS nieuwste "
            "FROM core.bronhouder")
        bronhouders = cur.fetchone()

        # Historie-tijdlijn: laatste N runs (alle bronnen), incl. totaal-na uit
        # details, zodat de UI een 'toen'-toestand en een diff kan tonen.
        cur.execute(
            """SELECT run_id, bron, scope, started_at, finished_at, status,
                      n_verwerkt, n_fout,
                      round(extract(epoch from (coalesce(finished_at, now()) - started_at)))::bigint
                        AS duur_seconden,
                      (details->>'totaal_na')::bigint  AS totaal_na,
                      (details->>'totaal_voor')::bigint AS totaal_voor
               FROM core.load_run
               ORDER BY started_at DESC
               LIMIT %s""", (historie_limiet,))
        historie = cur.fetchall()

        # Hele-sync-momenten (audit-schema is nieuw; ontbreekt op oudere prod).
        # `totalen`/`metrics` zijn de per-run momentopname zodat het dashboard
        # per run het verschil met de vorige run kan tonen.
        sync_runs = []
        try:
            cur.execute(
                """SELECT run_id, label, gestart_op, klaar_op, opmerking,
                          round(extract(epoch from (coalesce(klaar_op, now()) - gestart_op)))::bigint
                            AS duur_seconden,
                          totalen, metrics
                   FROM audit.sync_run ORDER BY gestart_op DESC LIMIT 50""")
            sync_runs = cur.fetchall()
        except Exception:
            conn.rollback()

    return {
        "bronnen": bronnen,
        "totalen": totalen,
        "totalen_bron": totalen_bron,
        "lopend": lopend,
        "laatst_bijgewerkt": laatst_bijgewerkt,
        "bronhouders": bronhouders,
        "historie": historie,
        "sync_runs": sync_runs,
    }


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
        # ── Query 1: activiteit-based ──
        # Provinciale Omgevingsverordeningen en N2000-aanwijzingsbesluiten
        # ontsnappen aan het keyword-filter: hun activiteit-namen + artikel-
        # teksten matchen zelden de leek-zoektermen ("damherten", "wateroverlast"),
        # waardoor relevante regels ten onrechte werden uitgesloten.
        #
        # 2026-07-11: leest uit p2p.mv_regel_op_locatie (voorberekende
        # ala→jr→te→r-keten, zie dso-loader/scripts/2026-07-add-mv-regel-op-
        # locatie.sql). De live join kostte 16-37s per call op prod (fan-out
        # van 31k+ rijen, ~550k buffer-pages); de mv-variant is
        # resultaat-identiek geverifieerd (EXCEPT-diff = 0 op meerdere
        # punten) en raakt alleen de rijen op het punt.
        kw_filter, kw_params = _build_keyword_filter(kw, "te.inhoud")
        act_filter, act_params = _build_keyword_filter(kw, "m.a_naam")
        if kw_filter and act_filter:
            combined_filter = (
                f"AND (({kw_filter[4:]}) OR ({act_filter[4:]}) "
                f"OR m.documenttype IN ('Omgevingsverordening', 'Aanwijzingsbesluit N2000'))"
            )
            combined_params = kw_params + act_params
        else:
            combined_filter = ""
            combined_params = []

        cur.execute(
            f"""
            SELECT m.regeling, m.documenttype, m.artikel, te.inhoud,
                   string_agg(DISTINCT m.a_naam, ' | ') AS activiteit,
                   string_agg(DISTINCT m.kwalificatie, ' | ') AS kwalificatie
            FROM p2p.mv_regel_op_locatie m
            JOIN p2p.tekst_element te ON te.id = m.te_id
            WHERE m.locatie_id IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
            {combined_filter}
            GROUP BY m.regeling, m.documenttype, m.artikel, te.id, te.inhoud
            """,
            (x, y, *combined_params),
        )
        ow = cur.fetchall()

        # ── Query 2: enrichment per local regeling (opschrift search) ──
        # Find which regelingen are at this location
        if kw:
            # 2026-07-11: idem — uit de mv i.p.v. de live fan-out-join.
            cur.execute(
                """
                SELECT DISTINCT m.frbr_work, m.regeling AS opschrift, m.documenttype, m.bronhouder
                FROM p2p.mv_regel_op_locatie m
                WHERE m.locatie_id IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
                  AND m.documenttype IN ('Omgevingsplan', 'Waterschapsverordening', 'Omgevingsverordening')
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
                          AND NOT r.inactief
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
                          AND NOT r.inactief
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
        # Perf (2026-06-10): zoektermen-filter via geïndexeerde FTS op
        # inhoud_plain i.p.v. ILIKE op de XML-kolom `inhoud`. De ILIKE-variant
        # (`inhoud ~~* '%..%'`) forceerde een Parallel Seq Scan over alle ~666k
        # tekst_elementen (~5.7s lokaal / >10s timeout op prod) omdat het
        # documenttype-filter op `regeling` niet naar `tekst_element` te pushen
        # is. De tsvector-match gebruikt idx_tekst_element_inhoud_fts → ~0.5s.
        # Zelfde FTS als de Ow-tak (Query 2B). Opschrift-match valt onder de
        # FTS op inhoud_plain (de opschrifttekst zit in de regeltekst).
        visie_fts = _build_fts_query(kw)
        if visie_fts:
            visie_text_filter = (
                "AND to_tsvector('dutch', coalesce(te.inhoud_plain, '')) "
                "@@ to_tsquery('dutch', %s)"
            )
            # Relevantie-ordening VÓÓR de LIMIT 50: zonder deze ORDER BY kapt de
            # LIMIT willekeurig op DB-volgorde af, waardoor de relevantste
            # visie/programma-elementen (bv. Programma Integraal Riviermanagement
            # bij een hoogwaterbescherming-vraag) uit de top-50 vallen terwijl
            # minder relevante visies (bv. Mariene Strategie) de slots vullen.
            # ts_rank op dezelfde FTS-match; identieke index (idx_tekst_element_inhoud_fts).
            visie_order = (
                "ORDER BY ts_rank(to_tsvector('dutch', coalesce(te.inhoud_plain, '')), "
                "to_tsquery('dutch', %s)) DESC"
            )
            visie_text_params = [visie_fts, visie_fts]
        else:
            visie_text_filter = ""
            visie_order = ""
            visie_text_params = []

        # Twee paden naar relevantie:
        #   A) bronhouder voert ook een Omgevingsplan op deze coords (gemeenten)
        #   B) regelingsgebied van de visie/programma bevat zelf de coords
        #      (landelijke + provinciale Programma's zoals PIRM, NOVI,
        #      Natuurbeheerplan — die hebben geen Omgevingsplan-bronhouder)
        #
        # Perf: beide ST_Intersects draaien tegen p2p.locatie_subdiv i.p.v.
        # p2p.locatie. Regelingsgebieden zijn grote multipolygons (tot 326
        # bbox-kandidaten per punt); op de volledige geometrie kost
        # st_intersects ~3.4s/loop → ×3 parallelle loops over de 10s
        # statement_timeout (r13 Earnewâld / r14 Hilversum, EXPLAIN 31 mei).
        # locatie_subdiv bevat dezelfde geometrieën via ST_Subdivide(…,256)
        # opgedeeld in kleine stukjes, waardoor de GiST-index veel preciezer
        # pre-filtert. Identieke resultaatset, ~5-90x sneller per pad. Zelfde
        # truc als Query 1. DISTINCT op pad B omdat één locatie meerdere
        # subdiv-stukjes heeft.
        cur.execute(
            f"""
            SELECT r.opschrift AS regeling, r.documenttype,
                   ocd_artikel_label(te.opschrift, te.wid) AS artikel, te.inhoud
            FROM p2p.tekst_element te
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE r.documenttype IN ('Omgevingsvisie', 'Programma')
              AND NOT r.inactief
              AND (
                r.bronhouder IN (
                    SELECT DISTINCT r2.bronhouder
                    FROM p2p.activiteit_locatieaanduiding ala2
                    JOIN p2p.locatie_subdiv ls2 ON ls2.identificatie = ala2.locatie_id
                    JOIN p2p.juridische_regel jr2 ON jr2.identificatie = ala2.juridische_regel_id
                    JOIN p2p.tekst_element te2 ON te2.wid = jr2.regeltekst_wid
                        AND (te2.regeling_expression = jr2.regeling_expression OR jr2.regeling_expression IS NULL)
                    JOIN p2p.regeling r2 ON r2.frbr_expression = te2.regeling_expression
                    WHERE ST_Intersects(ls2.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                      AND NOT r2.inactief
                      AND r2.documenttype = 'Omgevingsplan'
                )
                OR r.regelingsgebied_id IN (
                    SELECT DISTINCT identificatie FROM p2p.locatie_subdiv
                    WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                )
              )
              AND te.inhoud IS NOT NULL AND length(te.inhoud) > 50
            {visie_text_filter}
            {visie_order}
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
               AND NOT r.inactief
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
                        LEFT(te.inhoud_plain, 800)          AS regeltekst_excerpt,
                        te.id                               AS te_element_id
                FROM    p2p.normwaarde                  nw
                JOIN    p2p.norm                        n   ON n.identificatie  = nw.norm_id
                JOIN    p2p.locatie                     l   ON l.identificatie  = nw.locatie_id
                LEFT JOIN p2p.juridische_regel_norm     jrn ON jrn.norm_id      = n.identificatie
                LEFT JOIN p2p.juridische_regel          jr  ON jr.identificatie = jrn.juridische_regel_id
                LEFT JOIN p2p.tekst_element             te  ON te.wid           = jr.regeltekst_wid
                LEFT JOIN p2p.regeling                  r   ON r.frbr_expression = te.regeling_expression
                WHERE   l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
                  AND   r.inactief IS NOT TRUE
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
            ),
            eind AS (
                SELECT *
                FROM   ranked
                WHERE  (match_bucket = 'detector' AND rn <= %s)
                   OR  (match_bucket = 'keyword'  AND rn <= %s)
            )
            -- 2026-07-10/11: IntRef-volg-stap. Bij waardeInRegeltekst staat de
            -- waarde vaak niet in het geannoteerde artikel zelf maar in een
            -- artikel waarnaar het verwijst (OV Fryslan art. 3.7 -> art. 3.9).
            -- De LATERAL staat bewust ACHTER de bucket-limieten: in de brede
            -- CTE liet de planner hem over de volledige join lopen
            -- (prod: 17,8s -> statement timeout); hier raakt hij max ~20 rijen.
            SELECT eind.*, verw.verwijzing_excerpt
            FROM   eind
            LEFT JOIN LATERAL (
                SELECT string_agg(DISTINCT LEFT(te2.inhoud_plain, 800), E'\n\n') AS verwijzing_excerpt
                FROM p2p.tekst_inline_referentie tir
                JOIN p2p.tekst_element te2
                  ON te2.regeling_expression = eind.frbr_expression
                 AND te2.eid = tir.target_ref
                WHERE tir.tekst_element_id = eind.te_element_id
                  AND tir.soort = 'IntRef'
                  AND COALESCE(te2.inhoud_plain, '') <> ''
            ) verw ON TRUE
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

        # 2026-07-11: Omgevingswaarderegel-fallback. Sommige bronhouders
        # (pv23 Overijssel) leveren omgevingswaarden ALLEEN als
        # Omgevingswaarderegel + regeltekst, zonder Omgevingsnorm/
        # normwaarde-objecten (geen omgevingsnormen.xml in de aanlevering
        # geverifieerd 2026-07-11). Dan is de normwaarde-tabel leeg
        # maar bestaat er wel een geo-gekoppelde regel met de waarde in de
        # tekst (OV Overijssel art. 2.6/2.7: 1/100 per jaar). Alleen bij
        # 0 reguliere hits én alleen op het detector-pad (naam): de brede
        # keyword-bucket zou hier valse hits geven ('maximale' matcht
        # elke waterkering-regel), de detector-naam is norm-specifiek.
        if not rows and naam_pattern:
            _pats = [naam_pattern]
            cur.execute(
                """
                WITH kandidaten AS (
                    SELECT DISTINCT
                            NULL::text                          AS norm_id,
                            ocd_artikel_label(te.opschrift, te.wid)             AS norm_naam,
                            NULL::text                          AS type_norm,
                            NULL::text                          AS eenheid,
                            'omgevingswaarderegel'::text        AS norm_groep,
                            NULL::numeric                       AS kwantitatieve_waarde,
                            'waardeInRegeltekst'::text          AS kwalitatieve_waarde,
                            NULL::text                          AS locatie_id,
                            NULL::text                          AS locatie_naam,
                            NULL::text                          AS locatie_type,
                            r.opschrift                         AS regeling,
                            r.frbr_expression,
                            ocd_artikel_label(te.opschrift, te.wid)             AS artikel,
                            te.wid                              AS artikel_wid,
                            LEFT(te.inhoud_plain, 800)          AS regeltekst_excerpt,
                            te.id                               AS te_element_id,
                            'omgevingswaarderegel'::text        AS match_bucket
                    FROM    p2p.activiteit_locatieaanduiding ala
                    JOIN    p2p.juridische_regel jr
                              ON jr.identificatie = ala.juridische_regel_id
                             AND jr.regel_type IN ('Omgevingswaardegel', 'Omgevingswaarderegel')
                    JOIN    p2p.tekst_element te
                              ON te.wid = jr.regeltekst_wid
                             AND (jr.regeling_expression IS NULL
                                  OR te.regeling_expression = jr.regeling_expression)
                    JOIN    p2p.regeling r ON r.frbr_expression = te.regeling_expression
                    WHERE   ala.locatie_id IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
                      AND   r.inactief IS NOT TRUE
                      AND   (te.inhoud_plain ILIKE ANY(%s::text[])
                             OR COALESCE(te.opschrift, '') ILIKE ANY(%s::text[]))
                    LIMIT 8
                )
                -- LATERAL achter de LIMIT (zie hoofdquery): max 8 rijen.
                SELECT k.*, verw.verwijzing_excerpt
                FROM   kandidaten k
                LEFT JOIN LATERAL (
                    SELECT string_agg(DISTINCT LEFT(te2.inhoud_plain, 800), E'\n\n') AS verwijzing_excerpt
                    FROM p2p.tekst_inline_referentie tir
                    JOIN p2p.tekst_element te2
                      ON te2.regeling_expression = k.frbr_expression
                     AND te2.eid = tir.target_ref
                    WHERE tir.tekst_element_id = k.te_element_id
                      AND tir.soort = 'IntRef'
                      AND COALESCE(te2.inhoud_plain, '') <> ''
                ) verw ON TRUE
                """,
                (x, y, _pats, _pats),
            )
            rows = cur.fetchall()

    for r in rows:
        r.pop("rn", None)
        r.pop("te_element_id", None)
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


@app.get("/v1/maatvoering", dependencies=[Depends(verify_key)])
def maatvoering(
    x: float = Query(..., description="RD x-coordinaat (EPSG:28992)"),
    y: float = Query(..., description="RD y-coordinaat (EPSG:28992)"),
    naam: str | None = Query(None, min_length=2, description="Detector: substring-match op maatvoering-key OF planobject-naam"),
    zoektermen: list[str] | None = Query(None, description="Keyword: brede OR-match (repeated param)"),
    limit_detector: int = Query(5, le=100),
    limit_keyword: int = Query(15, le=100),
):
    """Wro-analoog van /v1/normwaarde: structurele maatvoeringen uit
    bestemmingsplannen (bouwhoogte, goothoogte, bebouwingspercentage, ...).

    Bron: `wro.planobject` (object_type='Maatvoering') met JSONB
    `maatvoering_info`, uitgeklapt per key via `jsonb_each`. Eén planobject
    met 3 keys → 3 rijen. Eenheid afgeleid uit key-suffix (_m, _pct, _m2, ...).

    Lege response (count=0) is een eersterangs antwoord: geen Wro-maatvoering
    op deze coord — combineer met `/v1/normwaarde` voor de Ow-kant.
    """
    if not naam and not zoektermen:
        raise HTTPException(status_code=400, detail="Geef minimaal 'naam' of 'zoektermen' op.")

    naam_pattern = f"%{naam}%" if naam else None
    zoektermen_patterns = [f"%{kw}%" for kw in zoektermen] if zoektermen else None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH maatvoering_op_locatie AS (
                SELECT  po.identificatie                       AS planobject_id,
                        po.naam                                AS planobject_naam,
                        po.bestemmingshoofdgroep,
                        ri.idn                                 AS plan_idn,
                        ri.naam                                AS regeling,
                        kv.key                                 AS maatvoering_key,
                        CASE jsonb_typeof(kv.value)
                            WHEN 'number' THEN (kv.value)::text::numeric
                            ELSE NULL
                        END                                    AS kwantitatieve_waarde,
                        CASE jsonb_typeof(kv.value)
                            WHEN 'string' THEN kv.value #>> '{}'
                            ELSE NULL
                        END                                    AS kwalitatieve_waarde,
                        CASE
                            WHEN kv.key LIKE %s THEN 'procent'
                            WHEN kv.key LIKE %s  THEN 'vierkante meter'
                            WHEN kv.key LIKE %s  THEN 'kubieke meter'
                            WHEN kv.key LIKE %s  THEN 'hectare'
                            WHEN kv.key LIKE %s   THEN 'meter'
                            ELSE NULL
                        END                                    AS eenheid
                FROM    wro.planobject po
                JOIN    wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
                CROSS JOIN LATERAL jsonb_each(po.maatvoering_info) AS kv(key, value)
                WHERE   po.object_type = 'Maatvoering'
                  AND   po.maatvoering_info IS NOT NULL
                  AND   ST_Intersects(po.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            ),
            bucketed AS (
                SELECT *,
                       CASE
                           WHEN %s::text IS NOT NULL
                                AND (maatvoering_key  ILIKE %s
                                     OR planobject_naam ILIKE %s)
                               THEN 'detector'
                           WHEN %s::text[] IS NOT NULL
                                AND (maatvoering_key  ILIKE ANY(%s::text[])
                                     OR planobject_naam ILIKE ANY(%s::text[]))
                               THEN 'keyword'
                           ELSE NULL
                       END AS match_bucket
                FROM maatvoering_op_locatie
            ),
            ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY match_bucket
                           ORDER BY kwantitatieve_waarde DESC NULLS LAST,
                                    maatvoering_key, planobject_id
                       ) AS rn
                FROM   bucketed
                WHERE  match_bucket IS NOT NULL
            )
            SELECT planobject_id, planobject_naam, bestemmingshoofdgroep,
                   plan_idn, regeling,
                   maatvoering_key, eenheid,
                   kwantitatieve_waarde, kwalitatieve_waarde,
                   match_bucket
            FROM   ranked
            WHERE  (match_bucket = 'detector' AND rn <= %s)
               OR  (match_bucket = 'keyword'  AND rn <= %s)
            ORDER BY CASE match_bucket WHEN 'detector' THEN 0 ELSE 1 END,
                     kwantitatieve_waarde DESC NULLS LAST,
                     maatvoering_key, planobject_id
            """,
            ('%_pct', '%_m2', '%_m3', '%_ha', '%_m',
             x, y,
             naam, naam_pattern, naam_pattern,
             zoektermen_patterns, zoektermen_patterns, zoektermen_patterns,
             limit_detector, limit_keyword),
        )
        rows = cur.fetchall()

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
                WHERE   l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
                  AND   r.inactief IS NOT TRUE
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
            -- 2026-07-10: dedup vóór de ranking. De ala-joins geven een
            -- fan-out van duizenden rijen (locatieaanduidingen x regels)
            -- per activiteit; zonder dedup vullen near-duplicates van één
            -- activiteit de bucket-limieten en blijven andere regelingen
            -- op hetzelfde punt onzichtbaar (Wetterskip 'Oppervlaktewater
            -- onttrekken' achter 5x OV-'Grondwater onttrekken').
            dedup AS (
                SELECT DISTINCT ON (match_bucket, activiteit_naam, LOWER(COALESCE(kwalificatie, '')), regeling)
                       *
                FROM   bucketed
                WHERE  match_bucket IS NOT NULL
                ORDER BY match_bucket, activiteit_naam, LOWER(COALESCE(kwalificatie, '')), regeling
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
                FROM   dedup
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
        r.pop("te_element_id", None)
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
            WHERE l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
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
            WHERE   l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
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
                        AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
                LEFT JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.juridische_regel_norm        jrn ON jrn.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.norm                         n   ON n.identificatie = jrn.norm_id
                LEFT JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.gebiedsaanwijzing            ga  ON ga.identificatie = jrg.gebiedsaanwijzing_id
                JOIN    p2p.locatie                        l
                        ON l.identificatie IN (ala.locatie_id, n.identificatie, ga.locatie_id)
                WHERE   l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
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
            WHERE   r.inactief IS NOT TRUE
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
            AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
        LEFT JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        WHERE   l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
          AND   r.inactief IS NOT TRUE
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
            AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
        LEFT JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        WHERE   l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
          AND   r.inactief IS NOT TRUE
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
            AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
        LEFT JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        WHERE   l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
          AND   r.inactief IS NOT TRUE
        LIMIT %s
        """,
        (x, y, _OBJ_FETCH_LIMIT_PER_TYPE),
    )
    return cur.fetchall()


def _fetch_objecten_gio(cur, x: float, y: float) -> list[dict]:
    """GIO's die het punt dekken, via de basisgeo-junctieketen.

    Een GIO ís de FRBR (object_id = frbr_expression). `naam` is een leesbaar
    label (~35% van de GIO's; groep-label of locatie-naam), anders NULL —
    anonieme GIO's matchen dan geen trefwoorden en zakken vanzelf weg in de
    scoring. Geen artikel/regeltekst: een GIO is geometrie, geen regel.
    """
    cur.execute(
        """
        SELECT  'gio'::text                                 AS type,
                gio.frbr_expression                          AS object_id,
                gio.naam                                     AS naam,
                NULL::text                                   AS groep,
                NULL::text                                   AS kwalificatie,
                NULL::numeric                                AS kwantitatieve_waarde,
                NULL::text                                   AS kwalitatieve_waarde,
                NULL::text                                   AS eenheid,
                l.identificatie                              AS locatie_id,
                l.noemer                                     AS locatie_naam,
                l.locatie_type,
                r.opschrift                                  AS regeling,
                gio.regeling_expression                      AS frbr_expression,
                NULL::text                                   AS artikel,
                NULL::text                                   AS artikel_wid,
                NULL::text                                   AS regeltekst_excerpt,
                gio.frbr_work                                AS frbr_work
        FROM    p2p.locatie l
        JOIN    p2p.gio_locatie gl ON gl.locatie_id = l.identificatie
        JOIN    p2p.geo_informatieobject gio ON gio.frbr_expression = gl.gio_frbr
        LEFT JOIN p2p.regeling r ON r.frbr_expression = gio.regeling_expression
        WHERE   l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
          AND   r.inactief IS NOT TRUE
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
        description="Komma-gescheiden lijst objecttypes (default: alle vier). "
        "Optioneel: 'gio' (GeoInformatieObjecten via basisgeo-keten) — niet in "
        "de default zodat de bot-retrieval-baseline stabiel blijft.",
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
        if "gio" in types_set:
            rows.extend(_fetch_objecten_gio(cur, x, y))

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
        elif r["type"] == "gio":
            obj_payload["frbr_work"] = r.get("frbr_work")
            obj_payload["label"] = r.get("naam") or _gio_work_label(r.get("frbr_work"))

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
                        AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
                LEFT JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.juridische_regel_norm        jrn ON jrn.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.norm                         n   ON n.identificatie = jrn.norm_id
                LEFT JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.juridische_regel_id = jr.identificatie
                LEFT JOIN p2p.gebiedsaanwijzing            ga  ON ga.identificatie = jrg.gebiedsaanwijzing_id
                JOIN    p2p.locatie                        l
                        ON l.identificatie IN (ala.locatie_id, n.identificatie, ga.locatie_id)
                WHERE   l.identificatie IN (SELECT identificatie FROM p2p.locatie_subdiv WHERE ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992)))
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
            WHERE   r.inactief IS NOT TRUE
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
        # Leest p2p.ala_punt: daarin staat de regeling al per (locatie,
        # activiteit) ontdubbeld, dus de keten ALA -> juridische_regel ->
        # tekst_element is hier niet meer nodig. Was eerst een live keten van
        # ~14k tussenrijen voor ~12 regelingen.
        cur.execute(
            """
            WITH expr AS (
                SELECT DISTINCT ap.regeling_expression
                FROM p2p.locatie_subdiv ls
                JOIN p2p.ala_punt ap ON ap.locatie_id = ls.identificatie
                WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            )
            SELECT DISTINCT ON (r.opschrift)
                r.frbr_expression   AS expression,
                r.opschrift         AS titel,
                r.documenttype      AS type,
                r.bronhouder,
                b.naam              AS bronhouder_naam,
                b.bestuurslaag
            FROM expr
            JOIN p2p.regeling r    ON r.frbr_expression = expr.regeling_expression
            JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
            WHERE NOT r.inactief
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
            "SELECT frbr_expression, opschrift, documenttype, bronhouder, "
            "inactief, reden_inactief "
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
                "locatie_id": row["ala_locatie_id"],
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
            "locatie_id": row["nw_locatie_id"],
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
            # Soft-flag (hide-first-audit G3): endpoint blijft werken voor
            # historisch inzien, maar markeert een verdrongen/ingetrokken versie
            # zodat de frontend een badge kan tonen. Zoek/adres tonen 'm niet.
            "inactief": bool(regeling["inactief"]),
            "reden_inactief": regeling["reden_inactief"],
        },
        "boom": boom,
        "locatie_ids": sorted(locatie_ids),
    }


# ── Gedeelde frontend-asset: <ocd-regeltekst> STOP-weergavecomponent ──
# Publiek (géén API-key): afnemende (statische) sites laden dit via <script src>.
# Versie in de URL zodat een breaking change niet alle sites tegelijk raakt —
# sites migreren v1→v2 wanneer ze klaar zijn. Bron van waarheid: assets/.
# Zie vault: analysis/Generiek leesmodel en STOP-weergavecomponent.
_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")


@app.get("/assets/ocd-regeltekst.v1.js")
def serve_ocd_regeltekst_v1():
    """Gedeelde STOP-XML weergavecomponent (klassiek script, nul deps).
    Eén bron voor OCDviewer / instructieregels / omgevingsbot / RoM."""
    return FileResponse(
        os.path.join(_ASSETS_DIR, "ocd-regeltekst.v1.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# Hertaling-contract: één gepind (model, prompt_versie)-paar voor alle
# afnemers. De lookup gaat via v2a.element_hertaling (functioneel id →
# begrijpelijk); de content-hash blijft intern. Zie
# dso-loader/scripts/2026-07-add-element-hash-koppeling.sql.
HERTAAL_MODEL = "claude-sonnet-5"
HERTAAL_PROMPT_VERSIE = "v1"


@app.get("/v1/viewer/tekst/{wid}", dependencies=[Depends(verify_key)])
def viewer_tekst(wid: str):
    """Tekst-inhoud (STOP-XML markup) van een enkel tekst_element (lazy loading).

    Geeft de XML met behoud van structuur (Lijst/Li/IntRef/Al) zodat de
    frontend lijsten en interne verwijzingen correct kan renderen.
    Additief veld `begrijpelijk`: precomputed hertaling (of null) —
    geen juridische status.
    """
    with get_conn() as conn, conn.cursor() as cur:
        # Soft-flag (hide-first-audit G1): wid is niet uniek (wId-fan-out). Join
        # naar p2p.regeling voor de inactief-vlag en prefereer bij een fan-out de
        # ACTIEVE versie (ORDER BY r.inactief NULLS FIRST → false vóór true), zodat
        # een directe wid-call de vigerende tekst teruggeeft en anders 'inactief'
        # meldt i.p.v. stil de verdrongen versie te tonen.
        cur.execute(
            """
            SELECT te.inhoud AS tekst, eh.begrijpelijk,
                   coalesce(r.inactief, false) AS inactief
            FROM p2p.tekst_element te
            LEFT JOIN v2a.element_hertaling eh
                   ON eh.tekst_element_id = te.id
                  AND eh.model = %s AND eh.prompt_versie = %s
            LEFT JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE te.wid = %s
            ORDER BY r.inactief ASC NULLS FIRST
            LIMIT 1
            """,
            (HERTAAL_MODEL, HERTAAL_PROMPT_VERSIE, wid),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Tekst niet gevonden")
        # Ook hier `iorefs`, niet alleen op de batch-variant: een lui geladen
        # los artikel zou anders stilzwijgend geen klikbare verwijzingen
        # krijgen, afhankelijk van welk pad de frontend toevallig koos.
        iorefs = _iorefs_bij_wids(cur, [wid])
    return {"wid": wid, "tekst": row["tekst"], "begrijpelijk": row["begrijpelijk"],
            "inactief": bool(row["inactief"]), "iorefs": iorefs.get(wid, {})}


class TekstenRequest(BaseModel):
    wids: list[str]


@app.post("/v1/viewer/teksten", dependencies=[Depends(verify_key)])
def viewer_teksten(req: TekstenRequest = Body(...)):
    """Batch-variant van /v1/viewer/tekst — haalt de tekst van meerdere
    tekst_elementen in één round-trip op.

    De frontend laadt bij het openen van een leestekst-tab alle artikelen
    tegelijk; dat zou anders N losse GET-calls kosten (en op HTTP/1.1 ~6
    tegelijk, dus meerdere sequentiële golven). Eén POST met de wid-lijst →
    één SQL met `wid = ANY(...)` collapt dat naar één round-trip.

    Onbekende wids worden stil overgeslagen (geen 404) — de frontend toont
    daar zelf een fallback voor. Volgorde van de response is niet gegarandeerd;
    de caller mapt op `wid`.
    """
    wids = [w.strip() for w in req.wids if w and w.strip()]
    if not wids:
        return {"teksten": []}
    with get_conn() as conn, conn.cursor() as cur:
        # Soft-flag (hide-first-audit G2): zie /v1/viewer/tekst. DISTINCT ON (wid)
        # met ORDER BY wid, r.inactief kiest per wid de actieve versie; de vlag
        # gaat mee zodat de frontend een ingetrokken artikel kan badgen.
        cur.execute(
            """
            SELECT DISTINCT ON (te.wid) te.wid, te.inhoud AS tekst, eh.begrijpelijk,
                   coalesce(r.inactief, false) AS inactief
            FROM p2p.tekst_element te
            LEFT JOIN v2a.element_hertaling eh
                   ON eh.tekst_element_id = te.id
                  AND eh.model = %s AND eh.prompt_versie = %s
            LEFT JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE te.wid = ANY(%s)
            ORDER BY te.wid, r.inactief ASC NULLS FIRST
            """,
            (HERTAAL_MODEL, HERTAAL_PROMPT_VERSIE, wids),
        )
        rows = cur.fetchall()
        iorefs = _iorefs_bij_wids(cur, wids)
    return {"teksten": [
        {"wid": r["wid"], "tekst": r["tekst"], "begrijpelijk": r["begrijpelijk"],
         "inactief": bool(r["inactief"]),
         "iorefs": iorefs.get(r["wid"], {})}
        for r in rows
    ]}


def _iorefs_bij_wids(cur, wids: list[str]) -> dict[str, dict]:
    """Per wid: de IntIoRef'en in dat tekst_element, opgelost naar hun GIO.

    Additief veld op /v1/viewer/teksten. Reden om het hier te doen en niet in
    een eigen call: de leestekst laadt toch al per artikel, en de renderer moet
    bij het tékenen al weten of een verwijzing klikbaar is. Een knop die pas
    na de klik blijkt niets op te leveren is erger dan geen knop.

    Twee-traps: IntIoRef.@ref = wId van een ExtIoRef in HETZELFDE document;
    die ExtIoRef draagt de FRBR van het GIO. `target_gio_expression` heeft die
    keten al opgelost, maar staat leeg zodra de GIO-rij is opgeruimd
    (gaps.md G-106) — de LATERAL hieronder haalt de FRBR dan alsnog uit de
    ExtIoRef zelf. Zo kent het paneel altijd minstens de identiteit van waar
    de verwijzing heen wijst, ook als de geometrie ontbreekt.
    """
    if not wids:
        return {}
    cur.execute(
        """
        WITH te AS (
          SELECT DISTINCT ON (t.wid) t.id, t.wid, t.regeling_expression
          FROM p2p.tekst_element t
          LEFT JOIN p2p.regeling r ON r.frbr_expression = t.regeling_expression
          WHERE t.wid = ANY(%s)
          ORDER BY t.wid, r.inactief ASC NULLS FIRST
        ),
        ir AS (
          SELECT te.wid, te.regeling_expression, i.target_ref, i.target_gio_expression
          FROM te
          JOIN p2p.tekst_inline_referentie i ON i.tekst_element_id = te.id
          WHERE i.soort = 'IntIoRef'
        )
        SELECT ir.wid, ir.target_ref,
               coalesce(ir.target_gio_expression, e.target_ref) AS gio,
               g.naam,
               EXISTS (SELECT 1 FROM p2p.gio_locatie gl
                       WHERE gl.gio_frbr = coalesce(ir.target_gio_expression, e.target_ref)
                      ) AS heeft_geometrie
        FROM ir
        LEFT JOIN LATERAL (
          SELECT e2.target_ref
          FROM p2p.tekst_inline_referentie e2
          JOIN p2p.tekst_element t2 ON t2.id = e2.tekst_element_id
          WHERE e2.soort = 'ExtIoRef'
            AND e2.eigen_wid = ir.target_ref
            AND t2.regeling_expression = ir.regeling_expression
          LIMIT 1
        ) e ON TRUE
        LEFT JOIN p2p.geo_informatieobject g
               ON g.frbr_expression = coalesce(ir.target_gio_expression, e.target_ref)
        """,
        (wids,),
    )
    uit: dict[str, dict] = {}
    for r in cur.fetchall():
        if not r["gio"]:
            continue
        uit.setdefault(r["wid"], {})[r["target_ref"]] = {
            "gio": r["gio"],
            "naam": r["naam"],
            "heeft_geometrie": bool(r["heeft_geometrie"]),
        }
    return uit


class HertalingLookupRequest(BaseModel):
    """Óf teksten (server hasht met v2a.norm_hash) óf wids (via de koppeltabel)."""
    teksten: list[str] | None = None
    wids: list[str] | None = None


@app.post("/v1/hertaling/lookup", dependencies=[Depends(verify_key)])
def hertaling_lookup(req: HertalingLookupRequest = Body(...)):
    """Precomputed begrijpelijke hertaling opzoeken (geen LLM-call, geen
    juridische status).

    Twee varianten, één response-vorm:
    - `{"teksten": [...]}` — content-lookup: de server normaliseert+hasht
      (v2a.norm_hash, dé ene hash-implementatie) en zoekt in v2a.hertaling.
      Werkt alleen met de VOLLEDIGE element-tekst (inhoud_plain); getrunceerde
      snippets matchen per definitie niet.
    - `{"wids": [...]}` — id-lookup via v2a.element_hertaling.

    Response: {"hertalingen": [{"key": <tekst|wid>, "begrijpelijk": str|null}]}
    in de volgorde van de aanvraag. Cache-miss → null (caller kiest fallback).
    """
    if bool(req.teksten) == bool(req.wids):
        raise HTTPException(422, "Geef precies één van 'teksten' of 'wids' op.")

    with get_conn() as conn, conn.cursor() as cur:
        if req.teksten:
            items = [t for t in req.teksten if t and t.strip()][:200]
            cur.execute(
                """
                SELECT t.ord, h.tekst AS begrijpelijk
                FROM unnest(%s::text[]) WITH ORDINALITY AS t(tekst, ord)
                LEFT JOIN v2a.hertaling h
                       ON h.bron_hash = v2a.norm_hash(t.tekst)
                      AND h.model = %s AND h.prompt_versie = %s
                ORDER BY t.ord
                """,
                (items, HERTAAL_MODEL, HERTAAL_PROMPT_VERSIE),
            )
            found = {r["ord"]: r["begrijpelijk"] for r in cur.fetchall()}
            return {"hertalingen": [
                {"key": items[i], "begrijpelijk": found.get(i + 1)}
                for i in range(len(items))
            ]}

        wids = [w.strip() for w in req.wids if w and w.strip()][:200]
        cur.execute(
            """
            SELECT DISTINCT ON (wid) wid, begrijpelijk
            FROM v2a.element_hertaling
            WHERE wid = ANY(%s) AND model = %s AND prompt_versie = %s
            """,
            (wids, HERTAAL_MODEL, HERTAAL_PROMPT_VERSIE),
        )
        by_wid = {r["wid"]: r["begrijpelijk"] for r in cur.fetchall()}
        return {"hertalingen": [
            {"key": w, "begrijpelijk": by_wid.get(w)} for w in wids
        ]}


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

    # Tekstzoeken gaat via de GIN-index op tekst_element; None wanneer er na
    # sanitisatie geen bruikbaar woord overblijft. Zie de toelichting bij de
    # WHERE-clause hieronder.
    ts_arg = _tsquery_arg(q) if q else None

    has_annotation_filter = any([
        activiteitengroepen, typen_gebied, groepen_gebied, normgroepen,
        themas, soorten_hoofdlijn, hoofdlijnen,
    ])

    with get_conn() as conn, conn.cursor() as cur:
        # ── Ow-regelingen ──────────────────────────────────────
        # Filter-clauses dynamisch opbouwen — alleen meegeven wat actief is.
        # Bestuurslaag-clause wordt apart bijgehouden zodat we 'm kunnen
        # weglaten bij de facet-count (per-chip preview semantiek).
        base_where: list[str] = ["1=1", "NOT r.inactief"]
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
            # Metadata-takken blijven ILIKE: `p2p.regeling` is klein (~1.9k rijen)
            # en substring-match is daar juist gewenst ('IMRO', 'AMS_OP').
            #
            # De tekst-tak MOET full-text zijn. `inhoud_plain ILIKE '%…%'` kan
            # geen index gebruiken en scande 614k tekst-elementen; dat liep op
            # productie in `statement_timeout` → HTTP 500 op élke q-zoekvraag.
            #
            # Twee dingen zijn kritisch:
            #  1. De predicaat-expressie is LETTERLIJK gelijk aan die van
            #     `idx_tekst_element_inhoud_fts` (dso-loader/src/ddl.py). Wijkt
            #     hij af, dan valt de planner stilzwijgend terug op een seq scan
            #     en is de timeout terug. Controle:
            #       SELECT indexdef FROM pg_indexes
            #        WHERE indexname = 'idx_tekst_element_inhoud_fts';
            #  2. Prefix-match (`:*` via _tsquery_arg), niet websearch_to_tsquery.
            #     Voorheen deed ILIKE '%geluid%' ook 'geluidzone'; zonder prefix
            #     zou dat wegvallen, want de 'dutch'-stemmer splitst Nederlandse
            #     samenstellingen niet. Zelfde afweging als bij vergunningen
            #     (commit d0c7fab, 'kalver' moet Kalverstraat blijven vinden).
            pattern = f"%{q}%"
            if ts_arg:
                # De tekst-tak is GEEN gecorreleerde EXISTS meer. Die dwong een
                # aparte GIN-lookup per regeling af (~1.900 stuks), en bij een
                # prefix die over veel lexemen uitwaaiert liep dat op tot 12,4 s
                # voor 'geluid' — te dicht bij de timeout. `treffers` scant
                # tekst_element één keer en telt meteen; de join is daarna
                # gratis. Gemeten op prod: 'geluid' 12,43 s -> 0,17 s,
                # 'bouwen' 4,13 s -> 0,22 s, met identieke uitkomsten.
                base_where.append(
                    "(r.opschrift ILIKE %s OR r.citeertitel ILIKE %s "
                    " OR r.frbr_work ILIKE %s OR tr.e IS NOT NULL)"
                )
                base_params.extend([pattern, pattern, pattern])
            else:
                # Alleen leestekens ingetypt: tekst-tak weglaten in plaats van
                # op alles te matchen.
                base_where.append(
                    "(r.opschrift ILIKE %s OR r.citeertitel ILIKE %s OR r.frbr_work ILIKE %s)"
                )
                base_params.extend([pattern, pattern, pattern])

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

        # Tekst-treffers per regeling in één scan over tekst_element. Zowel de
        # resultaat- als de facet-query hangen eraan, dus beide dragen de CTE
        # én de join; het CTE-argument staat vooraan in de parameterlijst omdat
        # de WITH vóór de WHERE staat.
        if ts_arg:
            cte_sql = (
                "WITH treffers AS ("
                " SELECT te.regeling_expression AS e, COUNT(*) AS n"
                " FROM p2p.tekst_element te"
                " WHERE to_tsvector('dutch', coalesce(te.inhoud_plain, ''))"
                "       @@ to_tsquery('dutch', %s)"
                " GROUP BY te.regeling_expression)"
            )
            join_sql = "LEFT JOIN treffers tr ON tr.e = r.frbr_expression"
            hits_sql = "COALESCE(tr.n, 0)"
            cte_params: list = [ts_arg]
        else:
            cte_sql = join_sql = ""
            hits_sql = "NULL::bigint"
            cte_params = []

        ow_query = f"""
            {cte_sql}
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
                -- 0 bij een regeling die alleen op titel of identificatie
                -- matchte; NULL wanneer er niet op tekst gezocht is.
                {hits_sql}                                AS hits_in_tekst
            FROM p2p.regeling r
            JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
            {join_sql}
            WHERE {' AND '.join(full_where)}
            ORDER BY r.opschrift, r.frbr_expression DESC
        """
        cur.execute(ow_query, cte_params + full_params)
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
            {cte_sql}
            SELECT b.bestuurslaag, COUNT(*) AS n
            FROM p2p.regeling r
            JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
            {join_sql}
            WHERE {' AND '.join(base_where)}
              AND b.bestuurslaag IS NOT NULL
            GROUP BY b.bestuurslaag
            """,
            cte_params + base_params,
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


_LAAG_ORDER = {'gemeente': 0, 'provincie': 1, 'waterschap': 2, 'rijk': 3}


@app.get("/v1/viewer/regelmix", dependencies=[Depends(verify_key)])
def viewer_regelmix(x: float = Query(...), y: float = Query(...)):
    """Regelmix-overzicht: welke documenten gelden op een RD-punt, met per
    document het aantal regels — OW-regelingen én Wro-bestemmingsplannen.

    Dit is bewust lichtgewicht (een handvol documenten + tellingen), zodat er
    niets afgekapt hoeft te worden, ook op locaties met duizenden regels. De
    artikel-koppen per document laadt de frontend lui via
    GET /v1/viewer/regelmix/document, en de tekst via POST /v1/viewer/teksten.
    """
    with get_conn() as conn, conn.cursor() as cur:
        # OW: aantal distinct regels per regeling, dan dedupliceren op opschrift
        # (nieuwste expression) — net als viewer_regelingen, zodat de Documenten-
        # en Regelmix-tab hetzelfde documentenoverzicht tonen.
        cur.execute(
            """
            WITH per_expr AS (
                SELECT r.frbr_expression, r.opschrift, r.documenttype, b.bestuurslaag,
                       count(DISTINCT te.wid) AS aantal
                FROM p2p.activiteit_locatieaanduiding ala
                JOIN p2p.locatie_subdiv ls   ON ls.identificatie = ala.locatie_id
                JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
                JOIN p2p.tekst_element te     ON te.wid = jr.regeltekst_wid
                    AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
                JOIN p2p.regeling r          ON r.frbr_expression = te.regeling_expression
                JOIN core.bronhouder b       ON b.overheidscode = r.bronhouder
                WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
                  AND NOT r.inactief
                  AND te.inhoud IS NOT NULL AND length(te.inhoud) > 20
                GROUP BY r.frbr_expression, r.opschrift, r.documenttype, b.bestuurslaag
            )
            SELECT DISTINCT ON (opschrift)
                frbr_expression AS bron_id, opschrift AS regeling,
                documenttype, bestuurslaag, aantal
            FROM per_expr
            ORDER BY opschrift, frbr_expression DESC
            """,
            {"x": x, "y": y},
        )
        ow_docs = [dict(d, bron_type='ow') for d in cur.fetchall()]

        # Wro: aantal tekst-objecten per actief plan, dedupliceren op naam
        # (nieuwste datum) — net als viewer_regelingen.
        cur.execute(
            """
            WITH per_plan AS (
                SELECT ri.idn, ri.naam, ri.type_plan, ri.datum, b.bestuurslaag,
                       count(*) AS aantal
                FROM wro.ruimtelijk_instrument ri
                JOIN core.bronhouder b       ON b.overheidscode = ri.bronhouder
                JOIN wro.wro_tekst_object wt ON wt.instrument_idn = ri.idn
                WHERE ST_Intersects(ri.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
                  AND ri.pons_status = 'actief'
                  AND wt.inhoud IS NOT NULL AND length(wt.inhoud) > 20
                GROUP BY ri.idn, ri.naam, ri.type_plan, ri.datum, b.bestuurslaag
            )
            SELECT DISTINCT ON (naam)
                idn AS bron_id, naam AS regeling, type_plan AS documenttype,
                bestuurslaag, aantal
            FROM per_plan
            ORDER BY naam, datum DESC NULLS LAST
            """,
            {"x": x, "y": y},
        )
        wro_docs = [dict(d, bron_type='wro') for d in cur.fetchall()]

    documenten = ow_docs + wro_docs
    documenten.sort(key=lambda d: (_LAAG_ORDER.get(d['bestuurslaag'] or '', 4), d['regeling'] or ''))
    return {"locatie": {"x": x, "y": y}, "documenten": documenten}


@app.get("/v1/viewer/regelmix/document", dependencies=[Depends(verify_key)])
def viewer_regelmix_document(
    x: float = Query(...),
    y: float = Query(...),
    bron: str = Query(..., description="bron_id: frbr_expression (OW) of plan-idn (Wro)"),
    bron_type: str = Query(..., pattern="^(ow|wro)$"),
):
    """Artikel-koppen van één regelmix-document. OW: koppen (wid, artikel-
    nummer/opschrift, hoofdstuk, activiteit) zónder inhoud — nummer en hoofdstuk
    worden uit de `wid` geparst (`__chp_<n>__art_<x.y>__`), het opschrift via de
    Artikel-node (één indexed self-join, geen recursieve walk → snel, ongecapt).
    De inhoud laadt de frontend daarna lui via POST /v1/viewer/teksten.
    Wro: teksten inline (klein, geen p2p-`wid`).
    """
    with get_conn() as conn, conn.cursor() as cur:
        if bron_type == 'wro':
            cur.execute(
                """
                SELECT
                    'wro'             AS bron_type,
                    ri.idn            AS bron_id,
                    NULL              AS activiteit_naam,
                    NULL              AS activiteit_id,
                    COALESCE(wt.label, wt.naam, 'Artikel ' || wt.nummer) AS artikel,
                    NULL              AS wid,
                    REGEXP_REPLACE(COALESCE(wt.inhoud, ''), '<[^>]+>', '', 'g') AS inhoud,
                    wt.nummer         AS artikel_nummer,
                    COALESCE(wt.label, wt.naam) AS artikel_opschrift,
                    NULL              AS hoofdstuk_nummer
                FROM wro.ruimtelijk_instrument ri
                JOIN wro.wro_tekst_object wt ON wt.instrument_idn = ri.idn
                WHERE ri.idn = %(bron)s
                  AND wt.inhoud IS NOT NULL AND length(wt.inhoud) > 20
                ORDER BY wt.volgnummer
                """,
                {"bron": bron},
            )
        else:
            # OW: één document, dus de boom-walk is goedkoop. Single-pass: daal af
            # vanaf elke regel en draag artikel-/hoofdstuk-info mee (COALESCE houdt
            # de dichtstbijzijnde), stop een tak zodra beide gevonden zijn. Werkt
            # universeel (ongeacht wid-encoding) — sneller en completer dan wid-parse.
            cur.execute(
                """
                WITH RECURSIVE base AS (
                    SELECT DISTINCT ON (te.wid)
                        te.id AS te_id, te.wid AS wid, te.opschrift AS artikel,
                        a.naam AS activiteit_naam, a.identificatie AS activiteit_id
                    FROM p2p.activiteit_locatieaanduiding ala
                    JOIN p2p.activiteit a        ON a.identificatie = ala.activiteit_id
                    JOIN p2p.locatie_subdiv ls   ON ls.identificatie = ala.locatie_id
                    JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
                    JOIN p2p.tekst_element te     ON te.wid = jr.regeltekst_wid
                    WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
                      AND te.regeling_expression = %(bron)s
                      AND te.inhoud IS NOT NULL AND length(te.inhoud) > 20
                    ORDER BY te.wid, a.naam
                ),
                walk AS (
                    SELECT b.te_id AS origin, t.id, t.parent_id,
                        CASE WHEN t.element_type = 'Artikel'   THEN t.nummer   END AS art_nr,
                        CASE WHEN t.element_type = 'Artikel'   THEN t.opschrift END AS art_op,
                        CASE WHEN t.element_type = 'Hoofdstuk' THEN t.nummer   END AS hfd_nr
                    FROM base b
                    JOIN p2p.tekst_element t ON t.id = b.te_id
                    UNION ALL
                    SELECT w.origin, p.id, p.parent_id,
                        COALESCE(w.art_nr, CASE WHEN p.element_type = 'Artikel'   THEN p.nummer   END),
                        COALESCE(w.art_op, CASE WHEN p.element_type = 'Artikel'   THEN p.opschrift END),
                        COALESCE(w.hfd_nr, CASE WHEN p.element_type = 'Hoofdstuk' THEN p.nummer   END)
                    FROM walk w
                    JOIN p2p.tekst_element p ON p.id = w.parent_id
                    WHERE w.art_nr IS NULL OR w.hfd_nr IS NULL
                ),
                resolved AS (
                    SELECT origin,
                           max(art_nr) AS artikel_nummer,
                           max(art_op) AS artikel_opschrift,
                           max(hfd_nr) AS hoofdstuk_nummer
                    FROM walk GROUP BY origin
                )
                SELECT
                    'ow'              AS bron_type,
                    %(bron)s          AS bron_id,
                    b.activiteit_naam, b.activiteit_id,
                    b.artikel,
                    b.wid             AS wid,
                    NULL              AS inhoud,
                    rs.artikel_nummer, rs.artikel_opschrift, rs.hoofdstuk_nummer
                FROM base b
                JOIN resolved rs ON rs.origin = b.te_id
                ORDER BY b.wid
                """,
                {"x": x, "y": y, "bron": bron},
            )
        rows = cur.fetchall()

        # Soft-flag (hide-first-audit G4): markeer of de OW-bron een
        # verdrongen/ingetrokken versie is (Wro kent geen inactief). De frontend
        # kan dan badgen; retrieval/adres tonen deze bron sowieso niet.
        inactief = False
        if bron_type == 'ow':
            cur.execute("SELECT inactief FROM p2p.regeling WHERE frbr_expression = %s",
                        (bron,))
            r = cur.fetchone()
            inactief = bool(r["inactief"]) if r else False

    return {"regelmix": rows, "inactief": inactief}


def _meest_specifiek_cte(op_punt_sql: str) -> str:
    """Bouwt de WITH RECURSIVE-CTE's voor de 'meest-specifieke-wint'-regel.

    Activiteiten zitten in een functionele structuur (`p2p.activiteit.bovenliggende`,
    self-FK). Een koepel-activiteit ('Activiteit gereguleerd in het omgevingsplan',
    'Bouwactiviteit (omgevingsplan)', …) bestaat alleen om specifiekere activiteiten
    te groeperen en heeft zelf geen toegevoegde waarde zodra zo'n specifieke
    afstammeling óók op het punt geldt. Deze CTE's bepalen die overbodige koepels
    puur structureel — zonder naam-heuristiek (vroeger `is_tophaak`/ILIKE
    '%gereguleerd in%', wat zowel concrete bladeren wegfilterde als koepels miste).

    `op_punt_sql` moet één kolom `id` (activiteit-identificatie) leveren: de
    activiteiten die op het punt/in de scope gelden. De CTE 'scaffolding' bevat de
    deelverzameling daarvan die voorouder is van een andere op-punt activiteit —
    die filtert de caller weg met `NOT IN (SELECT id FROM scaffolding)`.

    Gebruik als: `f"WITH RECURSIVE {_meest_specifiek_cte(op_punt_sql)} SELECT …"`.
    De hoofd-query moet dezelfde parameters opnieuw binden (de CTE en de
    hoofd-query herhalen het locatie-/expressie-filter).
    """
    return f"""
    op_punt AS (
        {op_punt_sql}
    ),
    ancestors AS (
        -- voorouder-keten van elke op-punt activiteit (start bij directe bovenliggende)
        SELECT op.id AS leaf, a.bovenliggende AS anc, 1 AS d
        FROM op_punt op
        JOIN p2p.activiteit a ON a.identificatie = op.id
        WHERE a.bovenliggende IS NOT NULL
        UNION ALL
        SELECT anc.leaf, p.bovenliggende, anc.d + 1
        FROM ancestors anc
        JOIN p2p.activiteit p ON p.identificatie = anc.anc
        WHERE p.bovenliggende IS NOT NULL AND anc.d < 25
    ),
    scaffolding AS (
        -- op-punt activiteiten die voorouder zijn van een andere op-punt activiteit
        SELECT DISTINCT anc AS id FROM ancestors
        WHERE anc IN (SELECT id FROM op_punt)
    )
    """


def _gio_work_label(frbr_work: str | None) -> str:
    """Leesbaar label uit een GIO-FRBR-work voor GIO's zonder naam.

    `frbr_work` heeft door de loader een trailing taal-segment ('/nld'), bv.
    '/join/id/regdata/gm0014/2024/locatiegroep_<hash>/nld'. We strippen dat en
    nemen het laatste betekenisdragende segment ('locatiegroep_<hash>'). Beter
    dan een rauwe FRBR-URI, en het blijft de FRBR-identiteit die de gebruiker
    als 'de GIO' beschouwt.
    """
    if not frbr_work:
        return "(naamloos GIO)"
    segs = [s for s in frbr_work.rstrip("/").split("/") if s and s != "nld"]
    return segs[-1] if segs else frbr_work


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
                AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE ST_Intersects(ls.geometrie, {point})
              AND NOT r.inactief
            GROUP BY ga.type, ga.naam, ga.groep, r.opschrift, r.documenttype
            ORDER BY ga.type, ga.naam
            """,
            (x, y),
        )
        gebiedsaanwijzingen = cur.fetchall()

        # Activiteitlocatieaanduidingen — dedup op (naam, kwalificatie, groep)
        # met alle regelingen + locatie_ids voor hover-highlight op de kaart.
        # 'meest-specifieke-wint': koepel-activiteiten waarvan een specifiekere
        # afstammeling óók op dit punt geldt vallen weg (zie _meest_specifiek_cte).
        # Beide queries lezen p2p.ala_punt in plaats van de live keten
        # ALA -> juridische_regel -> tekst_element -> regeling. Die keten sleepte
        # ~13.700 tussenrijen mee om ~560 activiteiten op te leveren (297k
        # buffers per klik); de matview heeft het antwoord al ontdubbeld.
        # Zie dso-loader/scripts/2026-07-add-ala-punt-mv.sql.
        op_punt_sql = f"""
            SELECT DISTINCT ap.activiteit_id AS id
            FROM p2p.locatie_subdiv ls
            JOIN p2p.ala_punt ap ON ap.locatie_id = ls.identificatie
            WHERE ST_Intersects(ls.geometrie, {point})
        """
        cur.execute(
            f"""
            WITH RECURSIVE {_meest_specifiek_cte(op_punt_sql)}
            SELECT a.naam,
                   a.groep,
                   ap.kwalificatie,
                   ARRAY_AGG(DISTINCT r.opschrift) AS regelingen,
                   ARRAY_AGG(DISTINCT ap.locatie_id) AS locatie_ids
            FROM p2p.locatie_subdiv ls
            JOIN p2p.ala_punt ap ON ap.locatie_id = ls.identificatie
            JOIN p2p.activiteit a ON a.identificatie = ap.activiteit_id
            JOIN p2p.regeling r ON r.frbr_expression = ap.regeling_expression
            WHERE ST_Intersects(ls.geometrie, {point})
              AND NOT r.inactief
              AND a.identificatie NOT IN (SELECT id FROM scaffolding)
            GROUP BY a.naam, a.groep, ap.kwalificatie
            ORDER BY a.groep, ap.kwalificatie, a.naam
            """,
            (x, y, x, y),
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
                AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE ST_Intersects(ls.geometrie, {point})
              AND NOT r.inactief
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
                AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE ST_Intersects(ls.geometrie, {point})
              AND NOT r.inactief
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

        # Wro-bestemmingen. `planobject_ids` is de sleutel waarmee de
        # vector-tile-laag zijn features aan dit object koppelt — hetzelfde
        # patroon als `locatie_ids` aan de Ow-kant. Zonder die lijst weet de
        # kaart niet welke vlakken bij welke bestemming horen en blijft de
        # Wro-laag onzichtbaar. Groeperen op de kolommen die het object
        # identificeren; één bestemming beslaat vaak tientallen planobjecten.
        cur.execute(
            f"""
            SELECT po.object_type, po.naam, po.bestemmingshoofdgroep,
                   ri.naam AS plan,
                   ARRAY_AGG(DISTINCT po.identificatie) AS planobject_ids
            FROM wro.planobject po
            JOIN wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
            WHERE ST_Intersects(po.geometrie, {point})
              AND ri.pons_status = 'actief'
            GROUP BY po.object_type, po.naam, po.bestemmingshoofdgroep, ri.naam
            ORDER BY ri.naam, po.object_type
            """,
            (x, y),
        )
        wro_bestemmingen = cur.fetchall()

        # GeoInformatieObjecten — alle GIO's die het punt dekken, via de
        # basisgeo-junctieketen (locatie → basisgeo:id → GIO). Een GIO ís de
        # FRBR (work = hoofdobject, expression = versie); `naam` is een
        # leesbaar label dat ~35% van de GIO's heeft (groep-label of
        # locatie-naam), de rest valt terug op de FRBR-work-staart.
        # `gekoppeld` markeert of op dezelfde locatie ook een GA/ALA/normwaarde
        # zit: gekoppeld=TRUE dupliceert een al getoond object (GIO is de
        # geometrie eronder), gekoppeld=FALSE is nieuwe info — typisch een
        # omgevingsvisie/programma-GIO dat niet als gebiedsaanwijzing is
        # geannoteerd en anders onzichtbaar blijft.
        cur.execute(
            f"""
            WITH gio_loc AS (
                SELECT DISTINCT
                       gio.frbr_expression, gio.frbr_work, gio.naam,
                       gio.regeling_expression, gl.locatie_id
                FROM p2p.locatie_subdiv ls
                JOIN p2p.gio_locatie gl ON gl.locatie_id = ls.identificatie
                JOIN p2p.geo_informatieobject gio ON gio.frbr_expression = gl.gio_frbr
                WHERE ST_Intersects(ls.geometrie, {point})
            )
            SELECT gl.frbr_expression, gl.frbr_work, gl.naam,
                   r.opschrift AS regeling, r.documenttype,
                   ARRAY_AGG(DISTINCT gl.locatie_id) AS locatie_ids,
                   bool_or(
                       EXISTS (SELECT 1 FROM p2p.gebiedsaanwijzing ga
                                WHERE ga.locatie_id = gl.locatie_id)
                    OR EXISTS (SELECT 1 FROM p2p.activiteit_locatieaanduiding a
                                WHERE a.locatie_id = gl.locatie_id)
                    OR EXISTS (SELECT 1 FROM p2p.normwaarde nw
                                WHERE nw.locatie_id = gl.locatie_id)
                   ) AS gekoppeld
            FROM gio_loc gl
            LEFT JOIN p2p.regeling r ON r.frbr_expression = gl.regeling_expression
            WHERE r.inactief IS NOT TRUE
            GROUP BY gl.frbr_expression, gl.frbr_work, gl.naam, r.opschrift, r.documenttype
            ORDER BY gl.naam NULLS LAST, gl.frbr_work
            """,
            (x, y),
        )
        geo_informatieobjecten = [
            {**row, "label": row["naam"] or _gio_work_label(row["frbr_work"])}
            for row in cur.fetchall()
        ]

    return {
        "locatie": {"x": x, "y": y},
        "gebiedsaanwijzingen": gebiedsaanwijzingen,
        "activiteitlocatieaanduidingen": activiteitlocatieaanduidingen,
        "omgevingsnormen": omgevingsnormen,
        "normwaarden": normwaarden,
        "ongetypeerde_locaties": ongetypeerde_locaties,
        "geo_informatieobjecten": geo_informatieobjecten,
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
                   -- 0 decimalen: geometrie staat in RD (EPSG:28992, meters),
                   -- dus sub-meter-precisie is voor kaartweergave zinloos.
                   -- Default is 9 decimalen; dat scheelt hier bijna een derde
                   -- payload (3,44 MB -> 2,37 MB op een Utrechts omgevingsplan).
                   -- NB: geen procent-teken in SQL-commentaar -- psycopg leest
                   -- dat als placeholder en gooit ProgrammingError.
                   ST_AsGeoJSON(l.geometrie, 0)::json AS geometry
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


# ═══════════════════════════════════════════════════════════════════════
# GIO-paneel — een informatieobject achter een IntIoRef ontsluiten
#
# Ontwerp + metingen: vault_v1/analysis/GIO-paneel bij een IntIoRef.md
#
# Twee dingen zijn hier niet vrijblijvend:
#
# 1. GEOMETRIE GAAT NIET ALS GeoJSON DE DEUR UIT. Over alle 4.591 GIO's met
#    geometrie is de ruwe GeoJSON p95 4,45 MB en max 264 MB (gemiddeld 16,7
#    locaties per GIO, max 5.337). Simplificeren in meters lost dat niet op —
#    p95 blijft 1,16 MB bij een tolerantie van één beeldpixel, tegen ~0,4 s
#    per GIO. Duizenden losse vlakjes zijn een aantal-probleem, geen
#    precisie-probleem. Daarom reduceren we in PIXELRUIMTE: eerst affien naar
#    beeldpixels, dan snappen op een halve pixel (sub-pixel-vlakjes vallen
#    samen en verdwijnen), dan simplificeren, dan ST_AsSVG. Meting op 320
#    GIO's bij 380 px: 91,0 % onder 60 kB op de fijne pass, 97,5 % na de
#    grove, ~29 ms per GIO.
#
# 2. ST_AsSVG NEGEERT Y. `ST_AsSVG(POLYGON((0 0,10 0,10 10,0 10,0 0)))` geeft
#    `M 0 0 L 10 0 10 -10 0 -10 Z`. De affine hieronder flipt daarom ZELF NIET
#    (e = +s); de negatie van ST_AsSVG doet dat werk. Het pad loopt dus van
#    y=0 tot y=-hoogte, en daarom levert dit endpoint een `viewbox` mee in
#    plaats van de client die conventie te laten raden.
# ═══════════════════════════════════════════════════════════════════════

# Nederlandse WMTS-tilematrix (EPSG:28992), geverifieerd tegen de
# GetCapabilities van service.pdok.nl/brt/achtergrondkaart/wmts/v2_0:
# 256 px-tegels, oorsprong linksboven, macht-van-twee-piramide.
GIO_TEGEL_M0 = 3440.64  # meter per pixel op zoomniveau 0 (= scale 12288000 * 0.00028)
GIO_PLAAT_BREEDTE = 380
GIO_PLAAT_HOOGTE = 300
GIO_MARGE = 0.12          # lucht rond de vorm, als fractie van de extent
GIO_SVG_BUDGET = 60_000   # tekens; daarboven een grovere pass, dan opgeven


def _gio_zoom(dx: float, dy: float) -> int:
    """Fijnste zoomniveau waarop de extent nog volledig in de plaat past.

    Resolutie r(z) = 3440,64 / 2^z m/px, en de plaat is een vast aantal
    pixels. De extent past dus zolang r >= max(dx/breedte, dy/hoogte). We
    zoeken de grootste z (fijnste beeld) die daaraan nog voldoet — één stap
    verder en de vorm loopt buiten de plaat.

    Niet naar het eerste niveau springen dat fijn genoeg *lijkt*: r loopt
    omlaag met z, dus de test moet op z+1 staan, niet op z.
    """
    nodig = max(dx / GIO_PLAAT_BREEDTE, dy / GIO_PLAAT_HOOGTE, 1e-6)
    z = 0
    while z < 19 and GIO_TEGEL_M0 / (2 ** (z + 1)) >= nodig:
        z += 1
    return z


def _gio_kaart(cur, gio: str) -> dict | None:
    """Bouw het kaartblok: plaat-extent, zoomniveau en SVG-pad in pixels."""
    cur.execute(
        """
        SELECT ST_Extent(l.geometrie)::text AS bbox, count(*) AS n_loc
        FROM p2p.gio_locatie gl
        JOIN p2p.locatie l ON l.identificatie = gl.locatie_id
        WHERE gl.gio_frbr = %s
        """,
        (gio,),
    )
    row = cur.fetchone()
    if not row or not row["bbox"]:
        return None

    x0, y0, x1, y1 = (
        float(v) for v in row["bbox"][4:-1].replace(",", " ").split()
    )
    dx, dy = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    z = _gio_zoom(dx * (1 + GIO_MARGE), dy * (1 + GIO_MARGE))
    res = GIO_TEGEL_M0 / (2 ** z)

    # De plaat is altijd exact 380x300 px; de extent volgt uit de resolutie
    # van het gekozen zoomniveau, niet andersom. Zo blijft een tegel een tegel.
    half_x = GIO_PLAAT_BREEDTE * res / 2
    half_y = GIO_PLAAT_HOOGTE * res / 2
    plaat = [cx - half_x, cy - half_y, cx + half_x, cy + half_y]

    schaal = 1.0 / res
    xoff = -plaat[0] * schaal
    yoff = -plaat[1] * schaal
    # e = +schaal: geen eigen y-flip, ST_AsSVG negeert y al (zie kopcommentaar).
    affine = (schaal, 0, 0, schaal, xoff, yoff)

    # Drie tolerantie's in één query. Eén ST_Collect, drie goedkope reducties
    # (SnapToGrid + Douglas-Peucker), één round-trip; de fijnste die binnen het
    # budget past wint.
    #
    # Bewust GEEN ST_SimplifyPreserveTopology als vangnet. Dat stond er even in
    # op de aanname dat snappen dunne vormen wegperst — maar gemeten bleek een
    # 40 km lange dijkzone bij snap 0,5 px géén leeg pad te geven maar 3,2 MB.
    # De pass was dus een oplossing voor een probleem dat er niet was, en
    # kostte wél minutenlange queries op precies de zwaarste GIO's.
    #
    # Wat overblijft na 8 px is echt te fijn voor deze schaal. Dan liever de
    # bbox met een eerlijke telling dan een half getekende contour: een halve
    # contour liegt over waar het informatieobject ligt.
    cur.execute(
        """
        WITH px AS (
          SELECT ST_Affine(ST_Collect(l.geometrie), %s, %s, %s, %s, %s, %s) AS g
          FROM p2p.gio_locatie gl
          JOIN p2p.locatie l ON l.identificatie = gl.locatie_id
          WHERE gl.gio_frbr = %s
        )
        SELECT ST_AsSVG(ST_Simplify(ST_SnapToGrid(g, 0.5), 0.5), 0, 1) AS fijn,
               ST_AsSVG(ST_Simplify(ST_SnapToGrid(g, 2),   2),   0, 1) AS grof,
               ST_AsSVG(ST_Simplify(ST_SnapToGrid(g, 8),   8),   0, 1) AS grover,
               ST_NumGeometries(g) AS n_vlakken
        FROM px
        """,
        (*affine, gio),
    )
    passes = cur.fetchone() or {}
    pad, tol, afgekapt = None, None, True
    for kolom, tolerantie in (("fijn", 0.5), ("grof", 2.0), ("grover", 8.0)):
        kandidaat = passes.get(kolom)
        if kandidaat and len(kandidaat) <= GIO_SVG_BUDGET:
            pad, tol, afgekapt = kandidaat, tolerantie, False
            break

    return {
        "zoom": z,
        "resolutie_m_px": round(res, 4),
        "bbox_rd": [round(v, 2) for v in plaat],
        "extent_rd": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
        "breedte_px": GIO_PLAAT_BREEDTE,
        "hoogte_px": GIO_PLAAT_HOOGTE,
        "viewbox": f"0 -{GIO_PLAAT_HOOGTE} {GIO_PLAAT_BREEDTE} {GIO_PLAAT_HOOGTE}",
        "pad": pad,
        "tolerantie_px": tol,
        "afgekapt": afgekapt,
        "n_vlakken": passes.get("n_vlakken"),
        "n_locaties": row["n_loc"],
    }


def _gio_versiedatum(expression: str) -> str | None:
    """Datum uit de FRBR-expressie: `.../nld@2026-03-02;5-1` -> 2026-03-02.

    Dit is nadrukkelijk de VERSIEdatum, niet de vaststellingsdatum — die
    bestaat niet in deze data (p2p.procedurestap wordt alleen voor ontwerpen
    gevuld; vault gaps.md G-108). De frontend labelt hem ook zo.
    """
    m = re.search(r"@(\d{4}-\d{2}-\d{2})", expression or "")
    return m.group(1) if m else None


@app.get("/v1/viewer/gio/{expression:path}", dependencies=[Depends(verify_key)])
def viewer_gio(expression: str):
    """Alles wat het register achter één IntIoRef toont.

    `expression` is de FRBR-expressie van het GeoInformatieObject, zoals de
    ExtIoRef hem declareert.
    """
    gio = expression if expression.startswith("/") else "/" + expression

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.frbr_expression, g.frbr_work, g.naam, g.regeling_expression,
                   r.opschrift AS regeling_opschrift, r.citeertitel, r.bronhouder
            FROM p2p.geo_informatieobject g
            LEFT JOIN p2p.regeling r ON r.frbr_expression = g.regeling_expression
            WHERE g.frbr_expression = %s
            """,
            (gio,),
        )
        meta = cur.fetchone()

        # Geen rij in geo_informatieobject hoeft niet te betekenen dat het GIO
        # niet bestaat — pruning van verouderde regelingversies verwijdert
        # GIO-rijen waar de geldende versie nog naar verwijst (gaps.md G-106).
        # De basisgeo-keten kan er dan nog wél zijn, dus we proberen de kaart
        # hoe dan ook.
        kaart = _gio_kaart(cur, gio)

        objecten = {"gebiedsaanwijzingen": [], "activiteiten": [], "normwaarden": []}
        locaties = []
        if kaart:
            cur.execute(
                """
                SELECT DISTINCT l.identificatie, l.locatie_type, l.noemer
                FROM p2p.gio_locatie gl
                JOIN p2p.locatie l ON l.identificatie = gl.locatie_id
                WHERE gl.gio_frbr = %s
                ORDER BY l.noemer NULLS LAST
                LIMIT 50
                """,
                (gio,),
            )
            locaties = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT DISTINCT ga.type, ga.naam, ga.groep
                FROM p2p.gio_locatie gl
                JOIN p2p.gebiedsaanwijzing ga ON ga.locatie_id = gl.locatie_id
                WHERE gl.gio_frbr = %s
                ORDER BY ga.type, ga.naam
                LIMIT 40
                """,
                (gio,),
            )
            objecten["gebiedsaanwijzingen"] = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT DISTINCT a.naam, ala.kwalificatie
                FROM p2p.gio_locatie gl
                JOIN p2p.activiteit_locatieaanduiding ala ON ala.locatie_id = gl.locatie_id
                JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
                WHERE gl.gio_frbr = %s
                ORDER BY a.naam
                LIMIT 40
                """,
                (gio,),
            )
            objecten["activiteiten"] = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT DISTINCT n.naam AS norm, n.type_norm, n.eenheid,
                       coalesce(nw.kwalitatieve_waarde,
                                nw.kwantitatieve_waarde::text) AS waarde
                FROM p2p.gio_locatie gl
                JOIN p2p.normwaarde nw ON nw.locatie_id = gl.locatie_id
                JOIN p2p.norm n ON n.identificatie = nw.norm_id
                WHERE gl.gio_frbr = %s
                ORDER BY n.naam
                LIMIT 40
                """,
                (gio,),
            )
            objecten["normwaarden"] = [dict(r) for r in cur.fetchall()]

    if not meta and not kaart:
        raise HTTPException(404, "Informatieobject niet gevonden")

    return {
        "gio": {
            "frbr_expression": gio,
            "frbr_work": (meta or {}).get("frbr_work") or gio.split("@")[0],
            "naam": (meta or {}).get("naam"),
            "regeling_expression": (meta or {}).get("regeling_expression"),
            "regeling_opschrift": (meta or {}).get("citeertitel")
            or (meta or {}).get("regeling_opschrift"),
            "bronhouder": (meta or {}).get("bronhouder"),
            "versiedatum": _gio_versiedatum(gio),
            "geladen": meta is not None,
        },
        "kaart": kaart,
        "locaties": locaties,
        "objecten": objecten,
        # De koppeling GIO -> OW-object loopt over gedeelde basisgeo:id, dus
        # over gemeenschappelijke geometrie — niet over een verklaarde relatie
        # in de bron (p2p.juridische_borging is leeg; gaps.md G-107). De
        # frontend leest dit veld en formuleert de kop navenant.
        "koppeling": "basisgeo",
    }


@app.get("/v1/viewer/regeling/{expression:path}/onderwerpen", dependencies=[Depends(verify_key)])
def viewer_onderwerpen(expression: str):
    """Waar gaat dit document over, en wat voor bepalingen staan erin.

    Twee onafhankelijke assen, en dat onderscheid is de hele reden dat dit
    endpoint herbouwd is:

      categorie / subcategorie  waar gaat de bepaling OVER  (milieu > geur)
      type_bepaling             wat voor bepaling het IS    (toepassingsbereik)

    Tot 2026-08 zat dat op één as gepropt. Gevolg: "toepassingsbereik" werd een
    subcategorie onder de naam *Tanken en vloeibare brandstoffen*, en artikel
    22.96 van gm0358 (geur van landbouwhuisdieren en paarden) stond daarin —
    niet omdat het over brandstof ging maar omdat het een toepassingsbereik-
    bepaling is. Zie `OCD/docs/onderwerp-as-en-typebepaling-as.md`.

    Drie dingen die deze versie eenvoudiger maken dan de vorige:

    1. EEN OPZOEKING, GEEN MODEL. `v2a.artikel_indeling` is gevuld door een
       lookup op het genormaliseerde opschriftpad (categorie) en op het
       artikelopschrift (type_bepaling). Geen embeddings, dus ook geen
       argmax-muntworp: bij de vorige opzet had 45% van de toewijzingen een
       marge onder 0,01 tussen nummer 1 en nummer 2.

    2. GEEN WERK/EXPRESSIE-KUNSTGREEP MEER. De oude versie moest op het WERK
       joinen omdat de vector-laag achterliep op `p2p.regeling`, met een
       wId-zeef om te corrigeren voor elementen die intussen verdwenen waren.
       Deze tabel hangt rechtstreeks aan `p2p.tekst_element`, dus de expressie
       klopt per definitie.

    3. NULL = NIET INGEDEELD, en dat wordt geteld en getoond. Geen categorie is
       een geldig antwoord; een gok die als feit op het scherm komt niet
       (gebruikersbesluit 2026-08-09). Vandaar `niet_ingedeeld` als eersterangs
       veld in het antwoord en niet als stil verschil tussen twee tellingen.

    Wro-plannen zitten niet in deze tabel: die staan in het `wro`-schema en
    hebben hun eigen structuur. Leeg antwoord betekent daar "nog niet
    ingedeeld", niet "geen onderwerpen".
    """
    expr = expression if expression.startswith("/") else "/" + expression

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT categorie, subcategorie, type_bepaling, wid
            FROM v2a.artikel_indeling
            WHERE regeling_expression = %s AND wid IS NOT NULL
            """,
            (expr,),
        )
        rijen = cur.fetchall()

    # Categorie -> subcategorie, allebei op wId-verzamelingen zodat de
    # frontend knopen kan aanwijzen. Een artikel telt in zijn categorie ook mee
    # als het geen subcategorie heeft.
    cats: dict[str, dict] = {}
    types: dict[str, set] = {}
    niet_ingedeeld: set[str] = set()

    for r in rijen:
        wid = r["wid"]
        if r["categorie"]:
            c = cats.setdefault(r["categorie"], {"wids": set(), "sub": {}})
            c["wids"].add(wid)
            if r["subcategorie"]:
                c["sub"].setdefault(r["subcategorie"], set()).add(wid)
        else:
            niet_ingedeeld.add(wid)
        if r["type_bepaling"]:
            types.setdefault(r["type_bepaling"], set()).add(wid)

    def lijst(d):
        return sorted(
            ({"naam": naam, "n_elementen": len(wids), "wids": sorted(wids)}
             for naam, wids in d.items()),
            key=lambda x: -x["n_elementen"],
        )

    categorieen = sorted(
        (
            {
                "naam": naam,
                "n_elementen": len(v["wids"]),
                "wids": sorted(v["wids"]),
                "sub": lijst(v["sub"]),
            }
            for naam, v in cats.items()
        ),
        key=lambda c: -c["n_elementen"],
    )

    return {
        "frbr_expression": expr,
        "categorieen": categorieen,
        # Alias voor de frontend die nog op de oude sleutel staat. Weg zodra
        # het register op `categorieen` draait.
        "onderwerpen": categorieen,
        "type_bepalingen": lijst(types),
        "niet_ingedeeld": {
            "n_elementen": len(niet_ingedeeld),
            "wids": sorted(niet_ingedeeld),
        },
        "dekking": {
            "artikelen": len(rijen),
            "ingedeeld": len(rijen) - len(niet_ingedeeld),
            "met_type_bepaling": sum(1 for r in rijen if r["type_bepaling"]),
        },
    }


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

    **Geometrie staat NIET inline op de features.** Veel activiteiten delen
    dezelfde locatie — een Utrechts omgevingsplan levert 93 features die
    allemaal naar hetzelfde ambtsgebied wijzen. Inline betekende dan 93
    kopieën van dezelfde polygoon: 10,2 MB voor 4.900 unieke punten. Daarom
    komt de geometrie één keer terug in `geometrieen` (locatie_id -> GeoJSON
    geometry) en dragen de features alleen `properties.locatie_id`. Payload
    wordt daarmee ~0,17 MB, en de client kan dezelfde geometrie-referentie
    aan alle features hangen (scheelt ook parse- en geheugendruk in de kaart).

    De client hydrateert: `feature.geometry = geometrieen[locatie_id]`.
    Zie `GeometrieStore.laadAla` in de OCDviewer-frontend. Op dit moment is
    die viewer de enige consument van dit endpoint.
    """
    loc_filter = ""
    loc_params: list = []
    if x is not None and y is not None:
        loc_filter = "AND ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))"
        loc_params = [x, y]

    # 'meest-specifieke-wint': dezelfde structurele filter als viewer_objecten,
    # zodat de kaart-ALA-laag en het objecten-panel exact dezelfde activiteiten
    # tonen (geen koepels waarvan een specifiekere afstammeling ook geldt).
    # Scope = deze regeling (+ optioneel het punt), gelijk aan de hoofd-query.
    # Leest p2p.ala_punt in plaats van de ALA -> juridische_regel ->
    # tekst_element-keten; de scope-filter op de regeling zit daar al in als
    # kolom. De hóófd-query hieronder houdt die keten wél nodig, want die
    # levert per activiteit het artikel-label en de wId erbij.
    op_punt_ls_join = (
        "JOIN p2p.locatie_subdiv ls ON ls.identificatie = ap.locatie_id"
        if loc_filter else ""
    )
    op_punt_sql = f"""
        SELECT DISTINCT ap.activiteit_id AS id
        FROM p2p.ala_punt ap
        {op_punt_ls_join}
        WHERE ap.regeling_expression = %s {loc_filter}
    """

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH RECURSIVE {_meest_specifiek_cte(op_punt_sql)}
            SELECT DISTINCT ON (a.naam, ala.kwalificatie, l.identificatie)
                a.naam              AS activiteit,
                a.groep             AS activiteit_groep,
                ala.kwalificatie,
                ocd_artikel_label(te.opschrift, te.wid)        AS artikel,
                te.wid              AS artikel_wid,
                l.identificatie     AS locatie_id,
                l.noemer            AS locatie_noemer
                -- Geen geometrie hier: die zou per rij herhaald worden. Zie
                -- de tweede query hieronder, die 'm eenmalig per locatie haalt.
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.activiteit a        ON a.identificatie = ala.activiteit_id
            JOIN p2p.locatie l            ON l.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr  ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te     ON te.wid = jr.regeltekst_wid
                                         AND te.regeling_expression = %s
            {("JOIN p2p.locatie_subdiv ls ON ls.identificatie = ala.locatie_id" if loc_filter else "")}
            WHERE TRUE {loc_filter}
              AND a.identificatie NOT IN (SELECT id FROM scaffolding)
            ORDER BY a.naam, ala.kwalificatie, l.identificatie
            """,
            (expression, *loc_params, expression, *loc_params),
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
                # De client vult dit uit `geometrieen[locatie_id]`.
                "geometry": None,
            }
            for row in cur.fetchall()
        ]

        # Geometrie eenmalig per unieke locatie. Scheelt naast payload ook
        # DB-werk: ST_AsGeoJSON draaide voorheen 93x op dezelfde polygoon.
        locatie_ids = sorted({f["properties"]["locatie_id"] for f in features})
        geometrieen: dict[str, dict] = {}
        if locatie_ids:
            cur.execute(
                """
                SELECT identificatie,
                       -- 0 decimalen — RD is in meters, zie _viewer_geometrie.
                       ST_AsGeoJSON(geometrie, 0)::json AS geometry
                FROM p2p.locatie
                WHERE identificatie = ANY(%s)
                """,
                (locatie_ids,),
            )
            geometrieen = {r["identificatie"]: r["geometry"] for r in cur.fetchall()}

        # Soft-flag (hide-first-audit G5): markeer of deze regeling-versie
        # verdrongen/ingetrokken is, zodat de kaart-ALA-laag kan badgen.
        cur.execute("SELECT inactief FROM p2p.regeling WHERE frbr_expression = %s",
                    (expression,))
        r = cur.fetchone()
        inactief = bool(r["inactief"]) if r else False

    return {
        "type": "FeatureCollection",
        "features": features,
        "geometrieen": geometrieen,
        "inactief": inactief,
    }


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
                   -- 0 decimalen — RD is in meters, zie _viewer_geometrie.
                   ST_AsGeoJSON(po.geometrie, 0)::json AS geometry
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


def _row_to_annotatie_delta(r: dict, artikel_wids: list[str] | None = None) -> dict:
    return {
        "type": r["type"],
        "identificatie": r["identificatie"],
        "bewerking": r["bewerking"],
        "naam": r["naam"],
        "payload": r["payload"],
        # Artikel-ankers waar deze annotatie via een juridische regel of
        # tekstdeel-verwijzing aan hangt. Kan meerdere zijn (annotatie via
        # meerdere regels aan verschillende artikelen). Leeg = geen artikel-
        # koppeling gevonden — annotatie valt in de "Algemeen"-bucket in
        # de tour (bv. omdat de bijbehorende regel niet in dit ontwerp is
        # gewijzigd of bij een tekstdeel zonder divisietekstRef).
        "artikelWids": artikel_wids or [],
    }


def _artikel_wids_per_annotatie(cur, ontwerpbesluit_id: str) -> dict[str, list[str]]:
    """Voor elke annotatie-delta in dit ontwerp: bepaal aan welke artikelen
    hij hangt. Drie paden:

      1. **Delta-bindingen**: annotaties waarvoor de koppeling in dit
         ontwerp WIJZIGT — via `p2pwijziging.juridische_regel_*_delta`
         → juridische_regel_delta.regeltekst_wid.
      2. **P2P-fallback**: annotaties waarvan het OBJECT wijzigt maar de
         koppeling aan een bestaande, ongewijzigde regel is — via
         `p2p.juridische_regel_*` en `p2p.activiteit_locatieaanduiding`.
         Zonder deze fallback zouden bv. hernoemde gebiedsaanwijzingen
         die aan bestaande regels hangen in de "Algemeen"-bucket vallen.
      3. **Tekstdeel-annotaties**: via payload.divisietekstRef/divisieRef
         → wid direct.

    Alle drie paden komen uit op een regeltekst-wid, waarna één recursive
    parent-walk in p2pwijziging.tekst_element de dichtstbijzijnde Artikel-
    parent bepaalt. p2p-wids en p2pwijziging-wids zijn identiek (STOP-
    invariant: wids zijn stabiel over versies), dus de walk werkt voor
    beide bronnen.

    Return: {annotatie_identificatie: [artikel_wid, ...]} — dedupliceerd."""
    cur.execute(
        """
        WITH RECURSIVE
        -- Pad 1: bindingen die in dit ontwerp wijzigen.
        delta_bindings AS (
          SELECT bd.activiteit_identificatie AS ann_id, jrd.regeltekst_wid
          FROM   p2pwijziging.juridische_regel_activiteit_delta bd
          JOIN   p2pwijziging.juridische_regel_delta jrd
            ON   jrd.identificatie = bd.juridische_regel_identificatie
           AND   jrd.ontwerpbesluit_id = bd.ontwerpbesluit_id
          WHERE  bd.ontwerpbesluit_id = %(ob)s
          UNION ALL
          SELECT bd.norm_identificatie, jrd.regeltekst_wid
          FROM   p2pwijziging.juridische_regel_norm_delta bd
          JOIN   p2pwijziging.juridische_regel_delta jrd
            ON   jrd.identificatie = bd.juridische_regel_identificatie
           AND   jrd.ontwerpbesluit_id = bd.ontwerpbesluit_id
          WHERE  bd.ontwerpbesluit_id = %(ob)s
          UNION ALL
          SELECT bd.gebiedsaanwijzing_identificatie, jrd.regeltekst_wid
          FROM   p2pwijziging.juridische_regel_gebiedsaanwijzing_delta bd
          JOIN   p2pwijziging.juridische_regel_delta jrd
            ON   jrd.identificatie = bd.juridische_regel_identificatie
           AND   jrd.ontwerpbesluit_id = bd.ontwerpbesluit_id
          WHERE  bd.ontwerpbesluit_id = %(ob)s
        ),
        -- Pad 2: annotaties in dit ontwerp die via een BESTAANDE (p2p)
        -- regel aan een artikel hangen. Alleen relevant voor annotaties
        -- die zelf een _delta hebben (anders zouden we ongewijzigde bindings
        -- ook meepakken). Restricted to annotaties in dit ontwerp.
        p2p_bindings AS (
          SELECT ala.activiteit_id AS ann_id, jr.regeltekst_wid
          FROM   p2p.activiteit_locatieaanduiding ala
          JOIN   p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
          WHERE  ala.activiteit_id IN (
                   SELECT identificatie FROM p2pwijziging.annotatie_delta
                   WHERE ontwerpbesluit_id = %(ob)s AND type = 'activiteit'
                 )
          UNION ALL
          SELECT jrn.norm_id, jr.regeltekst_wid
          FROM   p2p.juridische_regel_norm jrn
          JOIN   p2p.juridische_regel jr ON jr.identificatie = jrn.juridische_regel_id
          WHERE  jrn.norm_id IN (
                   SELECT identificatie FROM p2pwijziging.annotatie_delta
                   WHERE ontwerpbesluit_id = %(ob)s
                     AND type IN ('omgevingsnorm','omgevingswaarde')
                 )
          UNION ALL
          SELECT jrg.gebiedsaanwijzing_id, jr.regeltekst_wid
          FROM   p2p.juridische_regel_gebiedsaanwijzing jrg
          JOIN   p2p.juridische_regel jr ON jr.identificatie = jrg.juridische_regel_id
          WHERE  jrg.gebiedsaanwijzing_id IN (
                   SELECT identificatie FROM p2pwijziging.annotatie_delta
                   WHERE ontwerpbesluit_id = %(ob)s AND type = 'gebiedsaanwijzing'
                 )
        ),
        -- Pad 3: tekstdeel-annotaties via payload.divisietekstRef → wid
        -- (divisietekst-annotatie geeft die wid als eigen wId)
        tekstdeel_wids AS (
          SELECT ad_td.identificatie AS ann_id, dt.payload->>'wId' AS regeltekst_wid
          FROM   p2pwijziging.annotatie_delta ad_td
          JOIN   p2pwijziging.annotatie_delta dt
            ON   dt.ontwerpbesluit_id = ad_td.ontwerpbesluit_id
           AND   dt.type = 'divisietekst'
           AND   dt.identificatie = COALESCE(
                   ad_td.payload->>'divisietekstRef',
                   ad_td.payload->>'divisieRef'
                 )
          WHERE  ad_td.ontwerpbesluit_id = %(ob)s
            AND  ad_td.type = 'tekstdeel'
        ),
        starts AS (
          SELECT * FROM delta_bindings
          UNION SELECT * FROM p2p_bindings     -- UNION (niet ALL) — dedupliceert overlap tussen delta+p2p
          UNION SELECT * FROM tekstdeel_wids
        ),
        -- Klim in de tekst-element-boom naar de dichtstbijzijnde Artikel-parent.
        -- Ontwerpbesluit-scope: we lopen alleen door tekst_elementen van dit
        -- ontwerp (wids zijn stabiel over versies).
        walk AS (
          SELECT s.ann_id, te.id AS current_id, te.parent_id, te.wid, te.element_type,
                 CASE WHEN te.element_type = 'Artikel' THEN te.wid END AS artikel_wid
          FROM   starts s
          JOIN   p2pwijziging.tekst_element te
            ON   te.wid = s.regeltekst_wid
           AND   te.ontwerpbesluit_id = %(ob)s
          UNION ALL
          SELECT w.ann_id, p.id, p.parent_id, p.wid, p.element_type,
                 COALESCE(w.artikel_wid,
                          CASE WHEN p.element_type = 'Artikel' THEN p.wid END)
          FROM   walk w
          JOIN   p2pwijziging.tekst_element p ON p.id = w.parent_id
          WHERE  w.artikel_wid IS NULL
        )
        SELECT ann_id, ARRAY_AGG(DISTINCT artikel_wid) AS artikel_wids
        FROM   walk
        WHERE  artikel_wid IS NOT NULL
        GROUP  BY ann_id
        """,
        {"ob": ontwerpbesluit_id},
    )
    return {row["ann_id"]: row["artikel_wids"] for row in cur.fetchall()}


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
        # Citeertitel van het besluit zelf ("Wijziging omgevingsplan … t.b.v.
        # ontwikkeling Stenenkamerseweg 38/38a"). `opschrift` is de naam van de
        # regeling en dus gelijk voor elk besluit op die regeling; hiermee kan
        # de viewer bronnen uit elkaar houden. Alleen ontwerpen leveren dit —
        # bij besluitversies valt de loader terug op de regeling-citeertitel.
        "citeertitel": r.get("citeertitel"),
        "bekendOp": r["bekend_op"].isoformat() if r["bekend_op"] else None,
        "beginGeldigheid": r["begin_geldigheid"].isoformat() if r["begin_geldigheid"] else None,
        "beginInwerking": r["begin_inwerking"].isoformat() if r["begin_inwerking"] else None,
        "bronhouder": r["bronhouder"],
        "documenttype": r["documenttype"],
        "isVervangRegeling": r["is_vervang_regeling"],
    }


def _artikel_categorieen(cur, regeling_work: str,
                         artikel_wids: set[str]) -> dict[str, dict]:
    """Onderwerp én typeBepaling per artikel-wid, voor de categorie-as van de tour.

    Leest `v2a.wijziging_indeling`, gevuld door dso-loader/scripts/bouw_indeling.py
    met exact de regels waarop het register draait. Twee assen:

        hoofd / sub    waar de bepaling OVER gaat  <- opschriftpad
        typeBepaling   wat voor bepaling het IS    <- artikelopschrift

    Tot 2026-08-10 kwam dit uit `v2a.wijziging_artikel_categorie`, en daarmee uit
    de centroïde-taxonomie die beide assen op één hoop gooide: de grootste
    "categorie" op wijzigingen was "Tanken en vloeibare brandstoffen" (6.026
    artikelen), gevolgd door drie waarden die in werkelijkheid typeBepaling zijn.

    `hoofd` mag NULL zijn — dat is "niet ingedeeld", een geldig antwoord, en de
    rij zit hier dan alleen om zijn typeBepaling. `categorieDekking` telt daarom
    op `hoofd`, niet op het aantal rijen.

    De overervingsstap die hier stond is vervallen. Die bestond omdat de oude
    classificatie op de artikéltekst draaide en een tekstloos artikel dus niets
    kreeg; nu komt het onderwerp uit de voorouderketen, die broers en zussen
    delen. Gemeten 2026-08-10: overerving zou nog 11 van de 3.705 niet-ingedeelde
    artikelen raken — geen boomwandeling waard.
    """
    if not artikel_wids:
        return {}

    cur.execute(
        """
        SELECT artikel_wid, categorie, subcategorie, type_bepaling, herkomst
        FROM   v2a.wijziging_indeling
        WHERE  regeling_work = %s AND artikel_wid = ANY(%s)
          AND  (categorie IS NOT NULL OR type_bepaling IS NOT NULL)
        """,
        (regeling_work, list(artikel_wids)),
    )
    return {
        r["artikel_wid"]: {
            "hoofd": r["categorie"],
            "sub": r["subcategorie"],
            "typeBepaling": r["type_bepaling"],
            "herkomst": r["herkomst"],
        }
        for r in cur.fetchall()
    }


@app.get("/v1/viewer/regeling/{expression:path}/wijzigingen",
        dependencies=[Depends(verify_key)])
def viewer_wijzigingen(expression: str, include_verouderd: bool = False):
    """Aankomende wijzigingen (ontwerpen + besluitversies) op een regeling.

    Volgt de Plan A-TS-shape (`WijzigingenFixture`): één response met
    `regelingWork` + `wijzigingen[]`. Per bron de gestripte tekst-elementen
    (gewijzigd + parent-chain), IMOW-annotatie-deltas en locatie-deltas
    (incl. NULL-geometrie waar de backfill nog niet liep — Plan D).

    Verouderde ontwerpen (basis-expression niet meer in `p2p.regeling` voor
    deze work — ingehaald door een nieuwere vaststelling in de tijdreis)
    worden default verborgen; `verouderdVerborgen` in de response telt
    hoeveel er verborgen zijn. `include_verouderd=true` toont ze alsnog."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT frbr_work, opschrift FROM p2p.regeling WHERE frbr_expression = %s",
            (expression,),
        )
        reg = cur.fetchone()
        if not reg:
            raise HTTPException(404, "Regeling niet gevonden")
        regeling_work = reg["frbr_work"]
        regeling_opschrift = reg["opschrift"]

        # Bronnen — exclusief vervangRegeling-besluiten (die zijn geen
        # renvooi-overlay; volledige nieuwe regeling).
        # `is_verouderd` = basis-expression zit niet (meer) in p2p.regeling
        # voor deze work → ontwerp is achterhaald door een nieuwere vaststelling.
        cur.execute(
            """
            SELECT b.ontwerpbesluit_id, b.soort, b.status, b.opschrift,
                   b.citeertitel,
                   b.bekend_op, b.begin_geldigheid, b.begin_inwerking,
                   b.bronhouder, b.documenttype, b.is_vervang_regeling,
                   NOT EXISTS (
                     SELECT 1 FROM p2p.regeling r
                     WHERE  r.frbr_work = b.regeling_work
                       AND  r.frbr_expression = b.wijzigt_expression
                   ) AS is_verouderd
            FROM   p2pwijziging.besluit b
            WHERE  b.regeling_work = %s
              AND  b.is_vervang_regeling = FALSE
            ORDER  BY b.bekend_op NULLS LAST
            """,
            (regeling_work,),
        )
        alle_besluiten = cur.fetchall()

        if include_verouderd:
            besluiten = alle_besluiten
            verouderd_verborgen = 0
        else:
            besluiten = [b for b in alle_besluiten if not b["is_verouderd"]]
            verouderd_verborgen = len(alle_besluiten) - len(besluiten)

        wijzigingen = []
        # Alle artikel-wids die ergens in de tour genoemd worden — via de
        # tekst-mirror OF via de artikel-ankers van annotaties. Aan het eind
        # halen we nummer/opschrift op uit p2p.tekst_element voor de wids
        # die in de p2pwijziging-mirror nummer/opschrift missen (artikelen
        # die zelf niet wijzigen maar wel een annotatie geraakt zien).
        alle_artikel_wids: set[str] = set()
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

            # Fase 1 sub 1.2: artikel-ankers per annotatie ophalen. Eén
            # recursive CTE die zowel binding-annotaties (activiteit/norm/
            # gebiedsaanwijzing) als tekstdeel-annotaties naar hun artikel-
            # wid klimt. Frontend groepeert de tour op deze wids.
            artikel_wids_per_ann = _artikel_wids_per_annotatie(cur, ob_id)

            cur.execute(
                """
                SELECT locatie_id, bewerking, locatie_type, noemer,
                       -- 0 decimalen — RD is in meters, zie _viewer_geometrie.
                       ST_AsGeoJSON(geometrie, 0) AS geometrie_json
                FROM   p2pwijziging.locatie_delta
                WHERE  ontwerpbesluit_id = %s
                """,
                (ob_id,),
            )
            loc_rows = cur.fetchall()

            wijziging = _row_to_besluit_meta(b)
            wijziging["tekstElementen"] = [_row_to_tekst_element(r) for r in tekst_kept]
            wijziging["annotatieDeltas"] = [
                _row_to_annotatie_delta(r, artikel_wids_per_ann.get(r["identificatie"]))
                for r in ann_rows
            ]
            wijziging["locatieDeltas"] = [_row_to_locatie_delta(r) for r in loc_rows]
            wijzigingen.append(wijziging)

            for r in tekst_rows:
                if r["element_type"] == "Artikel":
                    alle_artikel_wids.add(r["wid"])
            for wids in artikel_wids_per_ann.values():
                alle_artikel_wids.update(wids)

        # Titel-fallback uit p2p.tekst_element (fase 1 sub 1.5). Wids zijn
        # STOP-stabiel over versies; label ophalen bij de geldende expression
        # dekt alle artikelen — ook die in dit ontwerp niet wijzigen. UI
        # gebruikt dit als fallback wanneer de p2pwijziging-mirror nummer/
        # opschrift missen (wat bij anker-artikelen normaal is).
        artikel_titels: dict[str, dict] = {}
        if alle_artikel_wids:
            cur.execute(
                """
                SELECT wid, nummer, opschrift
                FROM   p2p.tekst_element
                WHERE  regeling_expression = %s
                  AND  element_type = 'Artikel'
                  AND  wid = ANY(%s)
                """,
                (expression, list(alle_artikel_wids)),
            )
            for r in cur.fetchall():
                artikel_titels[r["wid"]] = {
                    "nummer": r["nummer"],
                    "opschrift": r["opschrift"],
                }

        # Onderwerp per artikel — zijkanaal in dezelfde vorm als artikelTitels,
        # zodat de tour er één call voor nodig heeft. Leeg wanneer
        # bouw_indeling.py nog niet over deze regeling is gelopen; de
        # frontend valt dan terug op de artikel-as.
        artikel_categorieen = _artikel_categorieen(
            cur, regeling_work, alle_artikel_wids,
        )
        cur.execute("SELECT DISTINCT curatie_versie FROM v2a.wijziging_indeling "
                    "WHERE regeling_work = %s LIMIT 1", (regeling_work,))
        rij = cur.fetchone()
        taxonomie = rij["curatie_versie"] if rij else None

    # Tellen op `hoofd`: een rij die alleen een typeBepaling draagt is niet
    # ingedeeld, en de UI beslist op dit getal of de categorie-as zinvol is.
    met_onderwerp = [v for v in artikel_categorieen.values() if v["hoofd"]]
    uit_register = sum(1 for v in met_onderwerp if v["herkomst"] == "register")
    return {
        "regelingWork": regeling_work,
        "regelingOpschrift": regeling_opschrift,
        "wijzigingen": wijzigingen,
        "verouderdVerborgen": verouderd_verborgen,
        "artikelTitels": artikel_titels,
        "artikelCategorieen": artikel_categorieen,
        "categorieDekking": {
            "artikelen": len(alle_artikel_wids),
            "geclassificeerd": len(met_onderwerp),
            "uitRegister": uit_register,
            "uitRenvooi": len(met_onderwerp) - uit_register,
            "curatieVersie": taxonomie,
        },
    }


@app.get("/v1/viewer/regeling/{expression:path}/artikel/{wid}/inhoud",
        dependencies=[Depends(verify_key)])
def viewer_artikel_inhoud(expression: str, wid: str):
    """Inhoud van één artikel uit de geldende regeling, voor de uitklap in
    de verval-lijst van de wijzigingen-tour. Concatenatie van alle descendant-
    inhoud (Leden/Al's) in leesvolgorde, plus meta (nummer/opschrift).

    Gebruik dit alleen voor artikelen die als vervallen in de tour verschijnen —
    voor de leestekst-view heeft de viewer al z'n eigen document-endpoints."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE walk AS (
              SELECT id, parent_id, wid, element_type, nummer, opschrift, inhoud,
                     volgorde, 0 AS diepte
              FROM   p2p.tekst_element
              WHERE  regeling_expression = %s AND wid = %s
              UNION ALL
              SELECT c.id, c.parent_id, c.wid, c.element_type, c.nummer, c.opschrift,
                     c.inhoud, c.volgorde, w.diepte + 1
              FROM   p2p.tekst_element c
              JOIN   walk w ON c.parent_id = w.id
            )
            SELECT diepte, element_type, nummer, opschrift, inhoud
            FROM   walk
            ORDER  BY diepte, volgorde
            """,
            (expression, wid),
        )
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(404, "Artikel niet gevonden")
        root = rows[0]
        body_parts = [r["inhoud"] for r in rows if r["inhoud"]]
        body = "\n".join(body_parts)
        return {
            "wid": wid,
            "nummer": root["nummer"],
            "opschrift": root["opschrift"],
            "inhoud": body,
            "isLeeg": body.strip() == "",
        }


@app.get("/v1/viewer/regeling/{expression:path}/tekstelement/{wid}/kinderen",
        dependencies=[Depends(verify_key)])
def viewer_tekstelement_kinderen(expression: str, wid: str, ontwerp_id: str | None = None):
    """Artikel-descendants van een container-tekstelement (Afdeling, Hoofdstuk,
    Paragraaf, …). Voor de uitklap in de "Algemene wijzigingen"-bucket van de
    tour: gebruiker klikt op een gewijzigde container en ziet welke artikelen
    er onder hangen.

    Bron-keuze: probeert eerst `p2p.tekst_element` (geldende regeling — voor
    vervallen of gewijzigde containers). Als daar niets staat en er is een
    `ontwerp_id` meegegeven, valt terug op `p2pwijziging.tekst_element` voor
    dat besluit (nodig voor nieuwe containers die nog niet in het geldende
    plan bestaan). Retour is dus wat er *nu* onder valt of *straks* onder komt,
    afhankelijk van of de container vervalt of nieuw is.

    Returned volgorde: tree-walk (breadth-first). Frontend sorteert desnoods
    op nummer met natural sort."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE walk AS (
              SELECT id, parent_id, wid, element_type, nummer, opschrift, volgorde,
                     0 AS diepte
              FROM   p2p.tekst_element
              WHERE  regeling_expression = %s AND wid = %s
              UNION ALL
              SELECT c.id, c.parent_id, c.wid, c.element_type, c.nummer, c.opschrift,
                     c.volgorde, w.diepte + 1
              FROM   p2p.tekst_element c
              JOIN   walk w ON c.parent_id = w.id
            )
            SELECT wid, nummer, opschrift
            FROM   walk
            WHERE  element_type = 'Artikel' AND diepte > 0
            ORDER  BY diepte, volgorde
            """,
            (expression, wid),
        )
        rows = cur.fetchall()
        bron = "p2p"

        if not rows and ontwerp_id:
            cur.execute(
                """
                WITH RECURSIVE walk AS (
                  SELECT id, parent_id, wid, element_type, nummer, opschrift, volgorde,
                         0 AS diepte
                  FROM   p2pwijziging.tekst_element
                  WHERE  ontwerpbesluit_id = %s AND wid = %s
                  UNION ALL
                  SELECT c.id, c.parent_id, c.wid, c.element_type, c.nummer, c.opschrift,
                         c.volgorde, w.diepte + 1
                  FROM   p2pwijziging.tekst_element c
                  JOIN   walk w ON c.parent_id = w.id
                )
                SELECT wid, nummer, opschrift
                FROM   walk
                WHERE  element_type = 'Artikel' AND diepte > 0
                ORDER  BY diepte, volgorde
                """,
                (ontwerp_id, wid),
            )
            rows = cur.fetchall()
            bron = "p2pwijziging"

        return {
            "wid": wid,
            "bron": bron,
            "kinderen": [
                {"wid": r["wid"], "nummer": r["nummer"], "opschrift": r["opschrift"]}
                for r in rows
            ],
        }


@app.get("/v1/register/landelijk", dependencies=[Depends(verify_key)])
def register_landelijk():
    """Landelijk beeld voor omgevingsdocumentenregister.nl — fase 6.

    Eén call in plaats van een handvol: de pagina toont uitsluitend
    aggregaten, en die apart ophalen zou tientallen round-trips kosten
    (per documenttype één telling).

    **Geen tijdreeksen.** Bewust: de database is een momentopname en er is
    voor de Ow-kant geen betrouwbare tijdas. Alles hier beschrijft de
    tóestand, niet de ontwikkeling. Zie het uitvoeringsplan, fase 7 is
    buiten scope gezet.

    Alle tellingen respecteren `NOT r.inactief`, zodat verdrongen
    regelingversies niet meetellen.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM p2p.regeling WHERE NOT inactief")
        ow_totaal = cur.fetchone()["n"]

        cur.execute(
            "SELECT count(*) FILTER (WHERE ow_regelingen > 0 OR wro_instrumenten > 0) AS n "
            "FROM core.bronhouder"
        )
        bronhouders = cur.fetchone()["n"]

        # Uit de tabel tellen, niet uit `core.bronhouder.wro_instrumenten`:
        # die gedenormaliseerde kolom is verlopen (55.085 tegen 63.062 in de
        # tabel, gemeten 2026-08-04). Het filter op pons_status kost ~3,3 s
        # doordat er geen index op staat; acceptabel omdat de proxy dit
        # antwoord een dag lang cachet, en het is het enige eerlijke getal —
        # zoeken filtert er ook op.
        cur.execute(
            "SELECT count(*) FILTER (WHERE pons_status = 'actief') AS n "
            "FROM wro.ruimtelijk_instrument"
        )
        wro_totaal = cur.fetchone()["n"]

        cur.execute(
            """
            SELECT b.bestuurslaag, count(*) AS n
            FROM p2p.regeling r
            JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
            WHERE NOT r.inactief AND b.bestuurslaag IS NOT NULL
            GROUP BY b.bestuurslaag ORDER BY n DESC
            """
        )
        per_bestuurslaag = cur.fetchall()

        # Alleen gemeentelijke documenten: een provincie- of waterschapsdocument
        # hoort niet bij één provincie-gebied, en het Rijk al helemaal niet.
        # De frontend zegt dat er expliciet bij.
        cur.execute(
            """
            SELECT g.provincie,
                   count(DISTINCT g.overheidscode) AS gemeenten,
                   count(r.frbr_expression)        AS ow,
                   coalesce((SELECT count(*) FROM wro.ruimtelijk_instrument ri
                             JOIN core.gemeentegrens g2 ON g2.overheidscode = ri.bronhouder
                             WHERE g2.provincie = g.provincie
                               AND ri.pons_status = 'actief'), 0) AS wro
            FROM core.gemeentegrens g
            LEFT JOIN p2p.regeling r
                   ON r.bronhouder = g.overheidscode AND NOT r.inactief
            WHERE g.provincie IS NOT NULL
            GROUP BY g.provincie ORDER BY ow DESC
            """
        )
        per_provincie = cur.fetchall()

        cur.execute(
            """
            SELECT coalesce(r.documenttype, 'onbekend') AS documenttype,
                   count(*) AS n
            FROM p2p.regeling r
            WHERE NOT r.inactief
            GROUP BY 1 ORDER BY n DESC
            """
        )
        per_documenttype = cur.fetchall()

        # ── Opvallend ────────────────────────────────────────
        # Vier uitschieters die zonder tijdas te bepalen zijn. De vier uit het
        # oorspronkelijke ontwerp die datums nodig hebben (drukste
        # bekendmakingsdag, meeste wijzigingen, kortste geldigheidsduur,
        # snelste bronhouder) zitten er bewust niet in.
        opvallend = []

        cur.execute(
            """
            SELECT r.frbr_expression, r.opschrift, count(*) AS n
            FROM p2p.tekst_element te
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE NOT r.inactief
            GROUP BY 1, 2 ORDER BY n DESC LIMIT 1
            """
        )
        if (r := cur.fetchone()):
            opvallend.append({
                "kop": "Grootste omgevingsdocument",
                "waarde": r["opschrift"] or "(zonder opschrift)",
                "noot": f"{r['n']:,} tekstelementen".replace(",", "."),
                "expression": r["frbr_expression"],
            })

        cur.execute(
            "SELECT naam, count(*) AS n FROM p2p.activiteit "
            "WHERE naam IS NOT NULL GROUP BY 1 ORDER BY n DESC LIMIT 1"
        )
        if (r := cur.fetchone()):
            opvallend.append({
                "kop": "Meest geannoteerde activiteit",
                "waarde": r["naam"],
                "noot": f"in {r['n']} omgevingsdocumenten geannoteerd",
                "expression": None,
            })

        cur.execute(
            "SELECT type, count(*) AS n FROM p2p.gebiedsaanwijzing "
            "WHERE type IS NOT NULL GROUP BY 1 ORDER BY n DESC LIMIT 1"
        )
        if (r := cur.fetchone()):
            opvallend.append({
                "kop": "Meest gebruikte gebiedsaanwijzing",
                "waarde": r["type"],
                "noot": f"{r['n']} keer aangewezen",
                "expression": None,
            })

        cur.execute(
            """
            SELECT n.type_norm, n.eenheid, nw.kwantitatieve_waarde AS waarde,
                   r.frbr_expression, r.opschrift
            FROM p2p.normwaarde nw
            JOIN p2p.norm n ON n.identificatie = nw.norm_id
            JOIN p2p.juridische_regel_norm jrn ON jrn.norm_id = n.identificatie
            JOIN p2p.juridische_regel jr ON jr.identificatie = jrn.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE nw.kwantitatieve_waarde IS NOT NULL
              AND n.type_norm ILIKE '%hoogte%'
              AND NOT r.inactief
            ORDER BY nw.kwantitatieve_waarde DESC LIMIT 1
            """
        )
        if (r := cur.fetchone()):
            opvallend.append({
                "kop": "Hoogste genormeerde hoogte",
                "waarde": f"{r['waarde']:g} {r['eenheid'] or ''}".strip(),
                "noot": f"{r['type_norm']} — {r['opschrift'] or '(zonder opschrift)'}",
                "expression": r["frbr_expression"],
            })

    return {
        "totalen": {
            "ow": ow_totaal,
            "wro": wro_totaal,
            "bronhouders": bronhouders,
        },
        "per_bestuurslaag": per_bestuurslaag,
        "per_provincie": per_provincie,
        "per_documenttype": per_documenttype,
        "opvallend": opvallend,
    }


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
    # Geschatte rij-tellingen uit de planner-statistieken (pg_class.reltuples):
    # exact count(*) op wro.planobject (16 GB) kostte ~29s → timeout. Voor een
    # overzicht-totaal is de ANALYZE-schatting ruim voldoende en milliseconden-snel.
    counts: dict[str, int] = {}
    with get_conn() as conn, conn.cursor() as cur:
        for schema, t in tables:
            cur.execute(
                "SELECT GREATEST(reltuples, 0)::bigint AS n "
                "FROM pg_class WHERE oid = %s::regclass",
                (f"{schema}.{t}",))
            row = cur.fetchone()
            counts[t] = row["n"] if row else 0
    return {"tabellen": counts, "totaal": sum(counts.values()), "geschat": True}
