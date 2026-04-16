import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader

from db import get_conn, pool

load_dotenv()

API_KEY = os.environ.get("OCD_API_KEY", "")
LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    try:
        yield
    finally:
        pool.close()


app = FastAPI(
    title="OCD API",
    description="Omgevingswet Centraal Datamodel — alle regelgeving van Nederland",
    version="0.1.0",
    lifespan=lifespan,
)

api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


async def verify_key(key: str | None = Security(api_key_header)):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


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
        kw_filter, kw_params = _build_keyword_filter(kw, "te.inhoud")
        act_filter, act_params = _build_keyword_filter(kw, "a.naam")
        if kw_filter and act_filter:
            combined_filter = f"AND (({kw_filter[4:]}) OR ({act_filter[4:]}))"
            combined_params = kw_params + act_params
        else:
            combined_filter = ""
            combined_params = []

        cur.execute(
            f"""
            SELECT r.opschrift AS regeling, r.documenttype,
                   te.opschrift AS artikel, te.inhoud,
                   string_agg(DISTINCT a.naam, ' | ') AS activiteit,
                   string_agg(DISTINCT ala.kwalificatie, ' | ') AS kwalificatie
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.locatie l ON l.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
            WHERE ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            {combined_filter}
            GROUP BY r.opschrift, r.documenttype, te.opschrift, te.inhoud
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
                JOIN p2p.locatie l ON l.identificatie = ala.locatie_id
                JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
                JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
                JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
                WHERE ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
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
                               te.opschrift AS artikel, te.inhoud
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
                               te.opschrift AS artikel, te.inhoud,
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

        cur.execute(
            f"""
            SELECT r.opschrift AS regeling, r.documenttype,
                   te.opschrift AS artikel, te.inhoud
            FROM p2p.tekst_element te
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE r.documenttype IN ('Omgevingsvisie', 'Programma')
              AND r.bronhouder IN (
                  SELECT DISTINCT r2.bronhouder
                  FROM p2p.activiteit_locatieaanduiding ala2
                  JOIN p2p.locatie l2 ON l2.identificatie = ala2.locatie_id
                  JOIN p2p.juridische_regel jr2 ON jr2.identificatie = ala2.juridische_regel_id
                  JOIN p2p.tekst_element te2 ON te2.wid = jr2.regeltekst_wid
                  JOIN p2p.regeling r2 ON r2.frbr_expression = te2.regeling_expression
                  WHERE ST_Intersects(l2.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                    AND r2.documenttype = 'Omgevingsplan'
              )
              AND te.inhoud IS NOT NULL AND length(te.inhoud) > 50
            {visie_text_filter}
            LIMIT 50
            """,
            (x, y, *visie_text_params),
        )
        visies = cur.fetchall()

        # ── Query 4: Wro-bestemmingen ──
        wro_kw_filter, wro_kw_params = _build_keyword_filter(kw, "wt.inhoud")
        wro_name_filter, wro_name_params = _build_keyword_filter(kw, "po.naam")
        if wro_kw_filter and wro_name_filter:
            wro_combined = f"AND (({wro_kw_filter[4:]}) OR ({wro_name_filter[4:]}))"
            wro_combined_params = wro_kw_params + wro_name_params
        else:
            wro_combined = ""
            wro_combined_params = []

        cur.execute(
            f"""
            SELECT ri.naam AS plan, po.object_type, po.naam AS bestemming,
                   po.bestemmingshoofdgroep,
                   string_agg(DISTINCT wt.inhoud, ' ') FILTER (WHERE wt.inhoud IS NOT NULL) AS inhoud
            FROM wro.planobject po
            JOIN wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
            LEFT JOIN wro.wro_tekst_object wt ON wt.instrument_idn = po.instrument_idn
            WHERE ST_Intersects(po.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            {wro_combined}
            GROUP BY ri.naam, po.object_type, po.naam, po.bestemmingshoofdgroep
            """,
            (x, y, *wro_combined_params),
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
                    te.opschrift AS artikel,
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
