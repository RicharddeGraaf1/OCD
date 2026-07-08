"""
Geo-scoped hybride semantische retrieval (dense ⊕ sparse) over v2a.tekst_embedding.

POST /v1/semantisch

Pipeline (0 live LLM behalve de vraag-embedding):
    1. locatie -> RD-coördinaat (adres via PDOK, of x/y direct)
    2. geo-scope: welke regelingen gelden op dit punt (locatie_subdiv ST_Intersects)
    3. vraag embedden via lokale Ollama (/api/embed, nomic-embed-text)
    4. dense (pgvector <=>) ∩ sparse (tsvector @@) binnen de scope-regelingen
    5. RRF-fusie (k=60) -> top-N regelteksten

Verschil met /v1/regelteksten-bij-vraag: dat pad joint via de IMOW-activiteit-
structuur (hol bij veel bronhouders); dit pad zoekt semantisch in de regelproza,
waar de discriminerende informatie wél zit.
"""
from __future__ import annotations

import os
import time
import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from db import get_conn
from regelteksten_bij_vraag import resolve_address

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["semantisch"])

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")

# OR-variant van de tsquery: één ontbrekend woord mag de match niet torpederen.
_TSQ = "nullif(replace(plainto_tsquery('dutch', %(q)s)::text, '&', '|'), '')::tsquery"

# Regeling-scope: elke chunk van een regeling die op het punt geldt (regelingsgebied).
_SCOPE_CTE = """
scope AS (
    -- Scope op regelingsgebied i.p.v. activiteit_locatieaanduiding-junction.
    -- Regelingsgebied is per TPOD verplicht op elke regeling en is in OCD 100%
    -- gevuld (1863/1863 regelingen). De activiteit-junction-route mist
    -- structureel alle vrijetekst-instrumenten (Omgevingsvisie, Programma,
    -- N2000-besluit) omdat die geen juridische_regel-elementen hebben — daar
    -- viel cluster C op stuk (r34/r37/r39). Geo-coverage van regelingsgebied-
    -- locaties in locatie_subdiv is volledig.
    SELECT DISTINCT r.frbr_expression AS expr
    FROM p2p.regeling r
    JOIN p2p.locatie_subdiv ls ON ls.identificatie = r.regelingsgebied_id
    WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
      AND NOT r.inactief
)"""

# Kandidaten — variant A (default): hele regeling-scope, grof.
_CAND_REGELING = """
cand AS (
    SELECT id, regeling_expression, bron_soort, kop_pad, inhoud_plain, embedding, fts
    FROM v2a.tekst_embedding
    WHERE regeling_expression IN (SELECT expr FROM scope)
)"""

# Kandidaten — variant B (werkingsgebied_filter): alleen chunks wiens EIGEN
# werkingsgebied het punt dekt (via v2a.chunk_annotatie, Fase 1). Chunks met
# alleen een regelingsgebied-fallback matchen via het ambtsgebied → degraderen
# naar regeling-scope; fijn-geannoteerde chunks doen alléén mee als hun
# specifieke locatie het punt dekt. Begrippen (geen gebied) blijven regeling-breed.
_CAND_WG = """
loc AS (
    SELECT DISTINCT ls.identificatie AS locatie_id
    FROM p2p.locatie_subdiv ls
    WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
),
cand AS (
    SELECT v.id, v.regeling_expression, v.bron_soort, v.kop_pad, v.inhoud_plain,
           v.embedding, v.fts
    FROM v2a.tekst_embedding v
    WHERE v.regeling_expression IN (SELECT expr FROM scope)
      AND (v.bron_soort = 'Begrip'
           OR EXISTS (SELECT 1 FROM v2a.chunk_annotatie ca
                      JOIN loc ON loc.locatie_id = ca.locatie_id
                      WHERE ca.chunk_id = v.id))
)"""

_TAIL = f"""
dense AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> %(qv)s::vector) AS rnk
    FROM cand ORDER BY embedding <=> %(qv)s::vector LIMIT 30
),
sparse AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank(fts, {_TSQ}) DESC) AS rnk
    FROM cand WHERE fts @@ {_TSQ} LIMIT 30
)
SELECT c.regeling_expression, r.opschrift AS regeling_titel, r.documenttype,
       b.bestuurslaag,
       c.bron_soort, c.kop_pad, c.inhoud_plain,
       d.rnk AS dense_rnk, s.rnk AS sparse_rnk,
       ((COALESCE(1.0/(60+d.rnk), 0) + COALESCE(1.0/(60+s.rnk), 0))
         -- scope-tiering: landelijke AMvB's (rijk) downwegen zodat ze de
         -- specifieke decentrale regels niet uit de top-k verdringen.
         * CASE WHEN b.bestuurslaag = 'rijk' THEN 0.55 ELSE 1.0 END)::float AS rrf
FROM cand c
LEFT JOIN dense d USING (id)
LEFT JOIN sparse s USING (id)
LEFT JOIN p2p.regeling r ON r.frbr_expression = c.regeling_expression
LEFT JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
WHERE d.id IS NOT NULL OR s.id IS NOT NULL
ORDER BY rrf DESC
LIMIT %(k)s
"""


def _build_sql(werkingsgebied_filter: bool) -> str:
    cand = _CAND_WG if werkingsgebied_filter else _CAND_REGELING
    return "WITH " + _SCOPE_CTE + ",\n" + cand + ",\n" + _TAIL


def _build_count_sql(werkingsgebied_filter: bool) -> str:
    """Aantal kandidaat-chunks in de scope — voor de A/B-trace (scope-versmalling)."""
    cand = _CAND_WG if werkingsgebied_filter else _CAND_REGELING
    return "WITH " + _SCOPE_CTE + ",\n" + cand + "\nSELECT count(*) AS n FROM cand"


class SemantischRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    location: str | None = Field(None, description="Adres. Of geef x/y.")
    x: float | None = Field(None, description="RD x-coördinaat")
    y: float | None = Field(None, description="RD y-coördinaat")
    k: int = Field(6, ge=1, le=30, description="Aantal regelteksten")
    werkingsgebied_filter: bool = Field(
        False,
        description="Fase 2: filter chunks op hun EIGEN werkingsgebied "
                    "(v2a.chunk_annotatie) i.p.v. op de grove regeling-scope.")

    @model_validator(mode="after")
    def _loc_or_xy(self):
        if not self.location and (self.x is None or self.y is None):
            raise ValueError("Geef ofwel `location`, ofwel `x` en `y`")
        return self


class SemantischHit(BaseModel):
    regeling_titel: str | None = None
    regeling_expression: str | None = None
    documenttype: str | None = None
    bestuurslaag: str | None = None
    bron_soort: str
    kop_pad: str | None = None
    inhoud: str
    dense_rnk: int | None = None
    sparse_rnk: int | None = None
    rrf: float


class SemantischResponse(BaseModel):
    hits: list[SemantischHit]
    rd_x: float
    rd_y: float
    weergavenaam: str | None = None
    scope_regelingen: int
    cand_chunks: int | None = None
    werkingsgebied_filter: bool = False
    sql_ms: float


def _embed(text: str) -> str:
    """Vraag-embedding via lokale Ollama; pgvector-literal terug."""
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/embed",
                       json={"model": EMBED_MODEL, "input": [text]}, timeout=30)
        r.raise_for_status()
        vec = r.json()["embeddings"][0]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"Embedding-service niet bereikbaar: {e}") from e
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


@router.post("/semantisch", response_model=SemantischResponse)
def semantisch(req: SemantischRequest) -> SemantischResponse:
    if req.location:
        x, y, naam = resolve_address(req.location)
    else:
        x, y, naam = req.x, req.y, None

    qv = _embed(req.question)

    wg = req.werkingsgebied_filter
    params = {"x": x, "y": y, "qv": qv, "q": req.question, "k": req.k}
    t0 = time.time()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(_build_sql(wg), params)
        rows = cur.fetchall()
        # aantal scope-regelingen (los, voor de trace)
        cur.execute(
            """SELECT count(DISTINCT r.frbr_expression) AS n
               FROM p2p.regeling r
               JOIN p2p.locatie_subdiv ls ON ls.identificatie = r.regelingsgebied_id
               WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                 AND NOT r.inactief""",
            (x, y))
        scope_n = cur.fetchone()["n"]
        # aantal kandidaat-chunks (toont de scope-versmalling van de WG-filter)
        cur.execute(_build_count_sql(wg), {"x": x, "y": y})
        cand_n = cur.fetchone()["n"]
    sql_ms = (time.time() - t0) * 1000

    hits = [SemantischHit(
        regeling_titel=r["regeling_titel"], regeling_expression=r["regeling_expression"],
        documenttype=r["documenttype"], bestuurslaag=r["bestuurslaag"],
        bron_soort=r["bron_soort"], kop_pad=r["kop_pad"],
        inhoud=r["inhoud_plain"], dense_rnk=r["dense_rnk"], sparse_rnk=r["sparse_rnk"],
        rrf=r["rrf"]) for r in rows]

    return SemantischResponse(hits=hits, rd_x=x, rd_y=y, weergavenaam=naam,
                              scope_regelingen=scope_n, cand_chunks=cand_n,
                              werkingsgebied_filter=wg, sql_ms=round(sql_ms, 1))
