"""Antwoord-endpoint: vraag + locatie -> natuurlijke-taal antwoord met bronnen.

POST /v1/antwoord-bij-vraag

De antwoord-tegenhanger van `/v1/regelteksten-bij-vraag`: dezelfde retrieval
(SKOS->activiteit-join->geo-filter via `killer_query`), maar met een extra
LLM-stap die de gevonden regelteksten samenvat tot een geaard antwoord met
zekerheidsniveau. De `bronnen` zijn exact de hits die de LLM als context kreeg,
zodat de frontend ze als bronverwijzing kan tonen.

Gedeeld brein: viewer én omgevingsbot consumeren dit endpoint (zie
`OCDviewer/docs/plans/20260603-ocd-gedeeld-brein.md`). De LLM is feature-flagged
(zie `llm.py`); zonder provider geeft dit endpoint 503 en valt de viewer terug op
zijn eigen samenvatting.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from db import get_conn
from keywords import match_skos_concepts
from llm import llm
from regelteksten_bij_vraag import (
    MatchedConceptSummary,
    RegeltekstHit,
    fetch_expanded_keywords,
    killer_query,
    resolve_address,
    tekst_fallback_query,
    verrijk_met_artikel,
)

logger = logging.getLogger("ocd_api.antwoord")

router = APIRouter(prefix="/v1", tags=["antwoord-bij-vraag"])

GEEN_CONCEPT_TEKST = (
    "Ik kon in je vraag geen specifiek onderwerp uit de omgevingsregelgeving "
    "herkennen. Probeer je vraag concreter te maken (bijvoorbeeld: \"mag ik hier "
    "een dakkapel bouwen?\")."
)
GEEN_REGELS_TEKST = (
    "Er gelden op deze locatie geen regels die bij je vraag passen, of de locatie "
    "valt buiten het beschikbare gebied."
)


# ─────────────────────────────────────────────────────────────────────
# Modellen
# ─────────────────────────────────────────────────────────────────────


class AntwoordRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    location: str | None = Field(None, description="Adres als string, of geef x/y.")
    x: float | None = Field(None, description="RD x-coordinaat (alternatief voor location)")
    y: float | None = Field(None, description="RD y-coordinaat")
    max_concepts: int = Field(5, ge=1, le=20)
    max_regelteksten: int = Field(20, ge=1, le=100)
    model: str | None = Field(None, description="Optionele model-override; anders server-default.")

    @model_validator(mode="after")
    def _location_or_xy(self):
        if not self.location and (self.x is None or self.y is None):
            raise ValueError("Geef ofwel `location`, ofwel `x` en `y`")
        return self


class AntwoordTrace(BaseModel):
    matched_concepts_count: int
    sql_query_ms: float
    llm_ms: float | None = None
    address_resolution_ms: float | None = None
    rd_x: float | None = None
    rd_y: float | None = None
    llm_provider: str | None = None
    llm_model: str | None = None


class AntwoordResponse(BaseModel):
    antwoord: str
    confidence: str = Field(..., description="HOOG | MIDDEN | LAAG")
    bronnen: list[RegeltekstHit] = Field(
        default_factory=list,
        description="De regelteksten die de LLM als context kreeg — bronverwijzing "
                    "voor de frontend (klik -> regelmix/leestekst).",
    )
    matched_concepts: list[MatchedConceptSummary] = Field(default_factory=list)
    trace: AntwoordTrace


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def build_context(rows: list[dict], max_chars: int = 6000) -> str:
    """Formatteer de hits tot één bron-blok voor de LLM-prompt. Capt op
    `max_chars` zodat de prompt niet ontploft; de sterkste hits staan vooraan
    (killer_query sorteert op activiteit/artikel)."""
    parts: list[str] = []
    total = 0
    for r in rows:
        regeling = (r.get("regeling") or "").strip()
        artikel = (r.get("artikel") or "").strip()
        activiteit = (r.get("activiteit_naam") or "").strip()
        body = (r.get("inhoud") or "").strip()
        if not body:
            continue
        header = f"[{regeling}] {artikel}".strip()
        if activiteit:
            header = f"{header} — {activiteit}" if header.strip("[] ") else activiteit
        block = f"{header}\n{body}".strip()
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n---\n".join(parts)


def _hit_models(rows: list[dict]) -> list[RegeltekstHit]:
    return [
        RegeltekstHit(
            activiteit_naam=r["activiteit_naam"],
            activiteit_id=r["activiteit_id"],
            artikel=r["artikel"],
            regeling=r["regeling"],
            regeling_expression=r["regeling_expression"],
            documenttype=r["documenttype"],
            inhoud=r["inhoud"],
            wid=r.get("wid"),
            artikel_nummer=r.get("artikel_nummer"),
            artikel_opschrift=r.get("artikel_opschrift"),
            hoofdstuk_nummer=r.get("hoofdstuk_nummer"),
            join_pad=r["join_pad"],
        )
        for r in rows
    ]


def _concept_models(matched_rows: list[dict]) -> list[MatchedConceptSummary]:
    return [
        MatchedConceptSummary(
            uri=r["uri"],
            naam=r["naam"],
            scheme=r["scheme_naam"],
            matched_terms=r["matched_terms"],
            werkzaamheid_urn=r["werkzaamheid_urn"],
            activiteit_imow_id=r["activiteit_imow_id"],
            score=round(r["score"], 2),
        )
        for r in matched_rows
    ]


# ─────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────


@router.post("/antwoord-bij-vraag", response_model=AntwoordResponse)
def antwoord_bij_vraag(req: AntwoordRequest):
    """Vraag + locatie -> geaard antwoord met bronnen + zekerheidsniveau."""
    address_ms = None
    rd_x, rd_y = req.x, req.y
    if req.location and (rd_x is None or rd_y is None):
        t0 = time.perf_counter()
        rd_x, rd_y, _ = resolve_address(req.location)
        address_ms = round((time.perf_counter() - t0) * 1000, 1)

    location_str = req.location or f"{rd_x}, {rd_y}"

    with get_conn() as conn, conn.cursor() as cur:
        matched_rows, _ngrams = match_skos_concepts(cur, req.question, req.max_concepts)

        if not matched_rows:
            return AntwoordResponse(
                antwoord=GEEN_CONCEPT_TEKST,
                confidence="LAAG",
                bronnen=[],
                matched_concepts=[],
                trace=AntwoordTrace(
                    matched_concepts_count=0, sql_query_ms=0.0,
                    address_resolution_ms=address_ms, rd_x=rd_x, rd_y=rd_y,
                ),
            )

        t0 = time.perf_counter()
        regel_rows = killer_query(cur, matched_rows, rd_x, rd_y, req.max_regelteksten)
        # Tekst-fallback wanneer de activiteit-join niets oplevert (dezelfde als
        # regelteksten-bij-vraag) — anders zou de LLM geen context krijgen op
        # locaties waar de werkzaamheid->activiteit-mapping faalt.
        if not regel_rows:
            keywords = fetch_expanded_keywords(cur, [r["uri"] for r in matched_rows])
            regel_rows = tekst_fallback_query(cur, keywords, rd_x, rd_y, req.max_regelteksten)
        verrijk_met_artikel(cur, regel_rows)
        sql_ms = round((time.perf_counter() - t0) * 1000, 1)

    bronnen = _hit_models(regel_rows)
    concepts = _concept_models(matched_rows)
    base_trace = dict(
        matched_concepts_count=len(matched_rows), sql_query_ms=sql_ms,
        address_resolution_ms=address_ms, rd_x=rd_x, rd_y=rd_y,
    )

    if not regel_rows:
        return AntwoordResponse(
            antwoord=GEEN_REGELS_TEKST, confidence="LAAG",
            bronnen=[], matched_concepts=concepts, trace=AntwoordTrace(**base_trace),
        )

    if not llm.available:
        # Bewust 503: de viewer valt dan terug op zijn eigen samenvatting i.p.v.
        # een nep-antwoord te tonen. De bronnen zijn al opgehaald maar zonder
        # LLM is er geen geaard antwoord.
        raise HTTPException(
            status_code=503,
            detail="Antwoord-generatie is niet beschikbaar (geen LLM geconfigureerd).",
        )

    dso_context = build_context(regel_rows)
    t1 = time.perf_counter()
    try:
        result = llm.generate_answer(
            req.question, location_str, dso_context, model_override=req.model,
        )
    except Exception as e:
        logger.warning("llm_failed: %s", e)
        raise HTTPException(status_code=503, detail=f"Antwoord-generatie mislukte: {e}")
    llm_ms = round((time.perf_counter() - t1) * 1000, 1)

    return AntwoordResponse(
        antwoord=result["answer"],
        confidence=result["confidence"],
        bronnen=bronnen,
        matched_concepts=concepts,
        trace=AntwoordTrace(
            **base_trace, llm_ms=llm_ms,
            llm_provider=llm.provider, llm_model=req.model or llm.model,
        ),
    )
