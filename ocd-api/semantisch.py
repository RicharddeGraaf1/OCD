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

# wro-scope (Fase 6a): oude bestemmingsplannen op het punt. wro-chunks dragen
# regeling_expression = instrument_idn en source_type='wro'; geo-anker is de
# plangebied-geometrie op wro.ruimtelijk_instrument (GiST-index, ~90 ms/punt).
_WRO_SCOPE_CTE = """
wro_scope AS (
    SELECT idn FROM wro.ruimtelijk_instrument
    WHERE geometrie IS NOT NULL
      AND ST_Intersects(geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
)"""

# de p2p-kandidaat-selects (inner, zonder cand-wrapper) — regeling-scope excludeert
# wro vanzelf (wro's instrument_idn zit niet in p2p.regeling).
_P2P_INNER_REGELING = """
    SELECT id, regeling_expression, bron_soort, kop_pad, inhoud_plain, embedding, fts
    FROM v2a.tekst_embedding
    WHERE regeling_expression IN (SELECT expr FROM scope)"""

# werkingsgebied_filter-variant: alleen chunks wiens EIGEN werkingsgebied het punt
# dekt (v2a.chunk_annotatie, Fase 1); grove chunks degraderen via het ambtsgebied,
# begrippen blijven regeling-breed. Vereist de _LOC_CTE.
_LOC_CTE = """
loc AS (
    SELECT DISTINCT ls.identificatie AS locatie_id
    FROM p2p.locatie_subdiv ls
    WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
)"""
_P2P_INNER_WG = """
    SELECT v.id, v.regeling_expression, v.bron_soort, v.kop_pad, v.inhoud_plain,
           v.embedding, v.fts
    FROM v2a.tekst_embedding v
    WHERE v.regeling_expression IN (SELECT expr FROM scope)
      AND (v.bron_soort = 'Begrip'
           -- NB (audit 2026-07-10): dit filter dropt ook alle chunks ZONDER
           -- enige annotatie — o.a. alle toelichting-chunks (292/292
           -- bij OV Zeeland). Regeling-breed meenemen van annotatie-loze
           -- chunks is geprobeerd (won r24-damherten) maar vergrootte de
           -- kandidaat-pool zodanig dat borderline-cases op randvariantie
           -- gingen wisselen (evalreeks 33-33-33 → 33-29-31); teruggedraaid.
           -- Structurele oplossing = toelichting-chunks alsnog annoteren of
           -- een aparte toelichting-tier met eigen cap.
           OR EXISTS (SELECT 1 FROM v2a.chunk_annotatie ca
                      JOIN loc ON loc.locatie_id = ca.locatie_id
                      WHERE ca.chunk_id = v.id))"""

# wro-chunks op het punt (alleen bij include_wro).
_WRO_INNER = """
    SELECT id, regeling_expression, bron_soort, kop_pad, inhoud_plain, embedding, fts
    FROM v2a.tekst_embedding
    WHERE source_type = 'wro' AND regeling_expression IN (SELECT idn FROM wro_scope)"""

# ontwerp-scope (Fase 6b): de frbr_works van de regelingen op het punt. Ontwerp-chunks
# dragen source_type='ontwerp' en regeling_expression=regeling_work (de gewijzigde
# regeling). NIET-vigerend — alleen mee bij expliciete include_ontwerp.
_ONTWERP_SCOPE_CTE = """
ontwerp_scope AS (
    SELECT DISTINCT r.frbr_work AS work
    FROM p2p.regeling r
    JOIN p2p.locatie_subdiv ls ON ls.identificatie = r.regelingsgebied_id
    WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
      -- hide-first-audit G6: consistent met _SCOPE_CTE — een inactieve
      -- (ingetrokken/verdrongen) regeling mag haar frbr_work niet bijdragen,
      -- anders lekken ontwerp-chunks van een niet-vigerende regeling (ze worden
      -- nergens later hergated omdat ze op frbr_work i.p.v. frbr_expression keyen).
      AND NOT r.inactief
)"""
_ONTWERP_INNER = """
    SELECT id, regeling_expression, bron_soort, kop_pad, inhoud_plain, embedding, fts
    FROM v2a.tekst_embedding
    WHERE source_type = 'ontwerp' AND regeling_expression IN (SELECT work FROM ontwerp_scope)"""

_TAIL = f"""
dense AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> %(qv)s::vector) AS rnk
    FROM cand ORDER BY embedding <=> %(qv)s::vector LIMIT %(cand_limit)s
),
sparse AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank(fts, {_TSQ}) DESC) AS rnk
    FROM cand WHERE fts @@ {_TSQ} LIMIT %(cand_limit)s
),
scored AS (
    SELECT c.regeling_expression,
           COALESCE(r.opschrift, wi.naam, ow.opschrift) AS regeling_titel,
           COALESCE(r.documenttype, wi.type_plan,
                    CASE WHEN ow.opschrift IS NOT NULL THEN 'Ontwerp: ' || ow.documenttype END) AS documenttype,
           b.bestuurslaag,
           c.bron_soort, c.kop_pad, c.inhoud_plain,
           d.rnk AS dense_rnk, s.rnk AS sparse_rnk,
           -- rrf_raw: ongewogen fusie-score, gebruikt in de round-robin-fase
           -- zodat rijks-documenten (bv. Programma IRM) daar eerlijk
           -- concurreren; de tiering geldt alleen voor de globale top-k/2.
           (COALESCE(1.0/(60+d.rnk), 0) + COALESCE(1.0/(60+s.rnk), 0))::float AS rrf_raw,
           ((COALESCE(1.0/(60+d.rnk), 0) + COALESCE(1.0/(60+s.rnk), 0))
             -- scope-tiering: landelijke AMvB's (rijk) downwegen zodat ze de
             -- specifieke decentrale regels niet uit de top-k verdringen.
             * CASE WHEN b.bestuurslaag = 'rijk' THEN 0.55 ELSE 1.0 END)::float AS rrf
    FROM cand c
    LEFT JOIN dense d USING (id)
    LEFT JOIN sparse s USING (id)
    LEFT JOIN p2p.regeling r ON r.frbr_expression = c.regeling_expression
    LEFT JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
    LEFT JOIN wro.ruimtelijk_instrument wi ON wi.idn = c.regeling_expression
    -- ontwerp-titel: keyt op frbr_work (alleen ontwerp-chunks dragen een work als
    -- regeling_expression), dus deze LATERAL vuurt vanzelf alleen voor die rijen.
    LEFT JOIN LATERAL (SELECT opschrift, documenttype FROM p2p.regeling
                       WHERE frbr_work = c.regeling_expression LIMIT 1) ow ON true
    WHERE d.id IS NOT NULL OR s.id IS NOT NULL
)
-- Diversiteits-cap (retrieval-v2): max %(per_regeling_cap)s chunks per regeling,
-- met HYBRIDE selectie: de globale rrf-top-(k/2) blijft onaangetast (diepte
-- van de best-scorende regeling behouden — pure round-robin kostte r31/r33
-- hun norm-chunk), de resterende slots worden round-robin gevuld (beste chunk
-- van élke regeling eerst), zodat doelgerichte chunks van kleine regelingen
-- (bv. Aanwijzingsbesluit N2000, r26) de kandidaat-set halen en de cross-
-- encoder-reranker downstream ze kan herwegen. NULL = geen cap én pure
-- rrf-orde (oud gedrag).
SELECT * FROM (
    SELECT sc.*,
           row_number() OVER (
               PARTITION BY sc.regeling_expression ORDER BY sc.rrf DESC) AS reg_rnk,
           row_number() OVER (ORDER BY sc.rrf DESC) AS glob_rnk
    FROM scored sc
) t
WHERE %(per_regeling_cap)s::int IS NULL OR t.reg_rnk <= %(per_regeling_cap)s::int
ORDER BY (CASE WHEN %(per_regeling_cap)s::int IS NULL THEN 0
               WHEN t.glob_rnk <= GREATEST(%(k)s / 2, 1) THEN 0
               ELSE t.reg_rnk END),
         (CASE WHEN %(per_regeling_cap)s::int IS NULL THEN t.rrf ELSE t.rrf_raw END) DESC
LIMIT %(k)s
"""


def _build_ctes(werkingsgebied_filter: bool, include_wro: bool, include_ontwerp: bool) -> str:
    ctes = [_SCOPE_CTE]
    if include_wro:
        ctes.append(_WRO_SCOPE_CTE)
    if include_ontwerp:
        ctes.append(_ONTWERP_SCOPE_CTE)
    if werkingsgebied_filter:
        ctes.append(_LOC_CTE)
    inner = _P2P_INNER_WG if werkingsgebied_filter else _P2P_INNER_REGELING
    if include_wro:
        inner += "\n    UNION ALL" + _WRO_INNER
    if include_ontwerp:
        inner += "\n    UNION ALL" + _ONTWERP_INNER
    ctes.append("cand AS (" + inner + "\n)")
    return "WITH " + ",\n".join(ctes)


def _build_sql(werkingsgebied_filter: bool, include_wro: bool = False,
               include_ontwerp: bool = False) -> str:
    return _build_ctes(werkingsgebied_filter, include_wro, include_ontwerp) + ",\n" + _TAIL


def _build_count_sql(werkingsgebied_filter: bool, include_wro: bool = False,
                     include_ontwerp: bool = False) -> str:
    """Aantal kandidaat-chunks in de scope — voor de A/B-trace (scope-versmalling)."""
    return _build_ctes(werkingsgebied_filter, include_wro, include_ontwerp) + "\nSELECT count(*) AS n FROM cand"


class SemantischRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    location: str | None = Field(None, description="Adres. Of geef x/y.")
    x: float | None = Field(None, description="RD x-coördinaat")
    y: float | None = Field(None, description="RD y-coördinaat")
    k: int = Field(6, ge=1, le=60, description="Aantal regelteksten")
    werkingsgebied_filter: bool = Field(
        False,
        description="Fase 2: filter chunks op hun EIGEN werkingsgebied "
                    "(v2a.chunk_annotatie) i.p.v. op de grove regeling-scope.")
    include_wro: bool = Field(
        False,
        description="Fase 6a: neem ook oude bestemmingsplannen (wro) op het punt mee "
                    "(source_type='wro', scope via wro.ruimtelijk_instrument-geometrie).")
    include_ontwerp: bool = Field(
        False,
        description="Fase 6b: neem ook ONTWERP/toekomstige regels (p2pwijziging) mee "
                    "(source_type='ontwerp', niet-vigerend — 'wat gaat hier veranderen').")
    cand_limit: int = Field(
        30, ge=10, le=200,
        description="Retrieval-v2: grootte van de dense- en sparse-kandidaatpool "
                    "vóór RRF-fusie. Default 30 = oud gedrag.")
    per_regeling_cap: int | None = Field(
        None, ge=1, le=30,
        description="Retrieval-v2: max chunks per regeling in de top-k, zodat één "
                    "chunk-rijke regeling de rest niet verdringt. NULL = geen cap.")

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
    include_wro: bool = False
    include_ontwerp: bool = False
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
    wro = req.include_wro
    ontw = req.include_ontwerp
    params = {"x": x, "y": y, "qv": qv, "q": req.question, "k": req.k,
              "cand_limit": req.cand_limit, "per_regeling_cap": req.per_regeling_cap}
    t0 = time.time()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(_build_sql(wg, wro, ontw), params)
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
        cur.execute(_build_count_sql(wg, wro, ontw), {"x": x, "y": y})
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
                              werkingsgebied_filter=wg, include_wro=wro,
                              include_ontwerp=ontw, sql_ms=round(sql_ms, 1))
