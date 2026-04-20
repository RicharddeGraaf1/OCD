import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
        "http://localhost:4201",
        "http://localhost:4202",
    ],
    allow_methods=["GET"],
    allow_headers=["X-Api-Key"],
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
            JOIN p2p.locatie l        ON l.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r       ON r.frbr_expression = te.regeling_expression
            JOIN core.bronhouder b    ON b.overheidscode = r.bronhouder
            WHERE ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
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
            JOIN p2p.locatie l ON l.identificatie = p.locatie_id
            WHERE ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
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
                        THEN length(coalesce(inhoud_plain, ''))
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
    """Tekst-inhoud van een enkel tekst_element (lazy loading)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT inhoud_plain AS tekst FROM p2p.tekst_element WHERE wid = %s LIMIT 1",
            (wid,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Tekst niet gevonden")
    return {"wid": wid, "tekst": row["tekst"]}


@app.get("/v1/viewer/geometrie", dependencies=[Depends(verify_key)])
def viewer_geometrie(
    locatie_ids: str = Query(..., description="Komma-gescheiden locatie-identificaties"),
):
    """GeoJSON FeatureCollection voor de opgegeven locaties.

    Geometrie wordt direct uit PostGIS geleverd via ST_AsGeoJSON.
    """
    ids = [lid.strip() for lid in locatie_ids.split(",") if lid.strip()]
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
                   ST_AsGeoJSON(ST_Transform(l.geometrie, 4326))::json AS geometry
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
        loc_filter = "AND ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))"
        loc_params = [x, y]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (a.naam, ala.kwalificatie, l.identificatie)
                a.naam              AS activiteit,
                a.groep             AS activiteit_groep,
                ala.kwalificatie,
                te.opschrift        AS artikel,
                te.wid              AS artikel_wid,
                l.identificatie     AS locatie_id,
                l.noemer            AS locatie_noemer,
                ST_AsGeoJSON(ST_Transform(l.geometrie, 4326))::json AS geometry
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.activiteit a        ON a.identificatie = ala.activiteit_id
            JOIN p2p.locatie l            ON l.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr  ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te     ON te.wid = jr.regeltekst_wid
                                         AND te.regeling_expression = %s
            WHERE TRUE {loc_filter}
            ORDER BY a.naam, ala.kwalificatie, l.identificatie
            """,
            (expression, *loc_params),
        )
        features = [
            {
                "type": "Feature",
                "properties": {
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
                   ST_AsGeoJSON(ST_Transform(po.geometrie, 4326))::json AS geometry
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
                    "identificatie": row["identificatie"],
                    "object_type": row["object_type"],
                    "naam": row["naam"],
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
