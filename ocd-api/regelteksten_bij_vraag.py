"""
Killer-endpoint: vraag + locatie -> regelteksten via SKOS-aktiviteit-join.

POST /v1/regelteksten-bij-vraag

Pipeline (in één SQL-roundtrip, geen tekst-ranker):
    1. SKOS-extractie (hergebruik van keywords.match_skos_concepts)
    2. Matched concepten -> p2p.activiteit via 3 paden:
         a) i2a.werkzaamheid.activiteit_id (FK, ~51%)
         b) i2a.werkzaamheid.naam == p2p.activiteit.naam (fallback voor 49% NULL FK)
         c) skos.activiteit_imow_id == p2p.activiteit.identificatie (Activiteiten, 99%)
    3. JOIN met p2p.activiteit_locatieaanduiding -> juridische_regel -> tekst_element
    4. Geo-filter op p2p.locatie_subdiv (ST_Intersects, SRID 28992)
    5. Top-N regelteksten retourneren

Verschil met /v1/adres:
    - /v1/adres: ILIKE op tekst, fts_rank ranking, ow_regels + wro + visies
    - /regelteksten-bij-vraag: structurele activity-FK-join, geen tekst-search
      noodzakelijk, deterministische volgorde van resultaten

Doel: regelteksten die *over de juridische activiteit van de vraag* gaan,
niet alleen regelteksten waar de leek-termen toevallig in voorkomen.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from db import get_conn
from keywords import match_skos_concepts, extract_vraag_chips, _stem_variant, _tokenize

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["regelteksten-bij-vraag"])

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"


# ─────────────────────────────────────────────────────────────────────
# Pydantic-modellen
# ─────────────────────────────────────────────────────────────────────


class RegeltekstenRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    location: str | None = Field(
        None, description="Adres als string. Of geef x/y in plaats hiervan."
    )
    x: float | None = Field(None, description="RD x-coordinaat (alternatief voor location)")
    y: float | None = Field(None, description="RD y-coordinaat")
    max_concepts: int = Field(5, ge=1, le=20)
    max_regelteksten: int = Field(20, ge=1, le=100)

    @model_validator(mode="after")
    def _location_or_xy(self):
        if not self.location and (self.x is None or self.y is None):
            raise ValueError("Geef ofwel `location`, ofwel `x` en `y`")
        return self


class MatchedConceptSummary(BaseModel):
    uri: str
    naam: str
    scheme: str
    matched_terms: list[str]
    werkzaamheid_urn: str | None = None
    activiteit_imow_id: str | None = None
    score: float


class RegeltekstHit(BaseModel):
    activiteit_naam: str
    activiteit_id: str
    artikel: str | None = None
    regeling: str | None = None
    regeling_expression: str | None = Field(
        None,
        description="FRBR-expression van de regeling — sleutel voor frontend-"
                    "filtering tegen RegelingSamenvatting.expression.",
    )
    documenttype: str | None = None
    inhoud: str
    join_pad: str = Field(
        ...,
        description="Welke join-strategie deze regeltekst opleverde "
                    "(werkzaamheid_fk / werkzaamheid_naam / activiteit_uri)",
    )


class RegeltekstenTrace(BaseModel):
    tokens_count: int
    matched_concepts_count: int
    sql_query_ms: float
    address_resolution_ms: float | None = None
    rd_x: float | None = None
    rd_y: float | None = None


class RegeltekstenResponse(BaseModel):
    regelteksten: list[RegeltekstHit]
    matched_concepts: list[MatchedConceptSummary]
    expanded_keywords: list[str] = Field(
        default_factory=list,
        description="Vlakke deduped lijst van concept-namen + trefwoorden van "
                    "alle matched concepten — bedoeld voor downstream object-"
                    "filtering aan de frontend (substring-match op object-namen).",
    )
    vraag_termen: list[str] = Field(
        default_factory=list,
        description="Specifieke termen uit de vraag voor de chip-rij in de UI: "
                    "alle 2-/3-grams + zeldzame 1-grams (freq ≤ 5 in SKOS-graaf). "
                    "Generieke werkwoorden als 'bouwen' worden weggefilterd "
                    "zodat de chips de gebruiker concrete filter-context geven.",
    )
    trace: RegeltekstenTrace


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def resolve_address(q: str) -> tuple[float, float, str]:
    """PDOK Locatieserver: adres -> (x_rd, y_rd, weergavenaam)."""
    try:
        resp = httpx.get(
            LOCATIESERVER,
            params={"q": q, "rows": 1, "fq": "type:adres"},
            timeout=10,
        )
    except httpx.HTTPError as e:
        raise HTTPException(503, f"Locatieserver onbereikbaar: {e}")
    docs = resp.json().get("response", {}).get("docs", [])
    if not docs:
        raise HTTPException(404, f"Adres niet gevonden: '{q}'")
    doc = docs[0]
    coords = doc["centroide_rd"].replace("POINT(", "").replace(")", "").split()
    return float(coords[0]), float(coords[1]), doc.get("weergavenaam", q)


# ─────────────────────────────────────────────────────────────────────
# Killer-query
# ─────────────────────────────────────────────────────────────────────


def merge_question_tokens(expanded: list[str], question: str) -> list[str]:
    """Voeg de oorspronkelijke tokens van de vraag (+ stem-varianten) toe aan
    expanded_keywords. Dedupe op lowercase, behoud volgorde.

    Reden: een gebruiker kan een specifieke term noemen ("recreatiewoning")
    die niet als SKOS-naam/trefwoord op álle semantisch relevante concepten
    staat, maar wel letterlijk in een object-naam voorkomt. Of: een term zit
    helemaal niet in SKOS (specialistische vakterm) maar wel in object-namen
    op deze locatie. In beide gevallen wil je dat de downstream object-filter
    daarop kan matchen, óók zonder dat SKOS er een concept aan koppelt.

    Tokens < 3 letters worden weggefilterd om false positives te beperken.
    """
    tokens = _tokenize(question)
    seen = {t.lower() for t in expanded}
    for t in tokens:
        if len(t) < 3:
            continue
        if t.lower() not in seen:
            expanded.append(t)
            seen.add(t.lower())
        stem = _stem_variant(t)
        if stem and stem.lower() not in seen:
            expanded.append(stem)
            seen.add(stem.lower())
    return expanded


def fetch_expanded_keywords(cur, concept_uris: list[str]) -> list[str]:
    """Vlakke deduped lijst van concept-namen + trefwoorden voor de gegeven URIs.

    Wordt aan de response toegevoegd zodat de frontend object-namen kan filteren
    via substring-match (lowercase) zonder een extra round-trip naar
    /v1/keywords/extract. Volgorde: concept-naam eerst, dan trefwoorden.
    """
    if not concept_uris:
        return []
    cur.execute(
        """
        SELECT uri, naam, NULL::text AS trefwoord, 0 AS rang
        FROM skos.concept WHERE uri = ANY(%s)
        UNION ALL
        SELECT concept_uri, NULL, trefwoord, 1 AS rang
        FROM skos.trefwoord WHERE concept_uri = ANY(%s)
        ORDER BY rang
        """,
        (concept_uris, concept_uris),
    )
    seen: set[str] = set()
    out: list[str] = []
    for row in cur.fetchall():
        term = row["naam"] or row["trefwoord"]
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def killer_query(cur, matched_concept_rows: list[dict],
                 x: float, y: float, limit: int) -> list[dict]:
    """Voer de drie-pads activity-join uit met geo-filter, retourneer regelteksten."""
    werkz_urns = [r["werkzaamheid_urn"] for r in matched_concept_rows
                  if r.get("scheme_naam") == "Werkzaamheden" and r.get("werkzaamheid_urn")]
    activiteit_imow_ids = [r["activiteit_imow_id"] for r in matched_concept_rows
                           if r.get("scheme_naam") == "Activiteiten" and r.get("activiteit_imow_id")]

    if not werkz_urns and not activiteit_imow_ids:
        return []

    # Drie-pads UNION zorgt dat alle bekende routes meedoen.
    cur.execute(
        """
        WITH matched_act AS (
            -- Pad a: Werkzaamheden via FK
            SELECT a.identificatie, 'werkzaamheid_fk'::text AS join_pad
            FROM i2a.werkzaamheid w
            JOIN p2p.activiteit a ON a.identificatie = w.activiteit_id
            WHERE w.urn = ANY(%(urns)s) AND w.activiteit_id IS NOT NULL

            UNION

            -- Pad b: Werkzaamheden via naam-match (fallback voor de helft NULL FK)
            SELECT a.identificatie, 'werkzaamheid_naam'::text
            FROM i2a.werkzaamheid w
            JOIN p2p.activiteit a ON LOWER(a.naam) = LOWER(w.naam)
            WHERE w.urn = ANY(%(urns)s) AND w.activiteit_id IS NULL

            UNION

            -- Pad c: Activiteiten via URI-transform (vrijwel volledige match)
            SELECT a.identificatie, 'activiteit_uri'::text
            FROM p2p.activiteit a
            WHERE a.identificatie = ANY(%(act_imow)s)
        )
        SELECT
            a.naam        AS activiteit_naam,
            a.identificatie AS activiteit_id,
            te.opschrift  AS artikel,
            r.opschrift   AS regeling,
            r.frbr_expression AS regeling_expression,
            r.documenttype,
            REGEXP_REPLACE(te.inhoud, '<[^>]+>', '', 'g') AS inhoud,
            ma.join_pad
        FROM matched_act ma
        JOIN p2p.activiteit a ON a.identificatie = ma.identificatie
        JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
        JOIN p2p.locatie_subdiv ls ON ls.identificatie = ala.locatie_id
        JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
        JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
        JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
          AND te.inhoud IS NOT NULL
          AND length(te.inhoud) > 20
        ORDER BY a.naam, te.opschrift NULLS LAST
        LIMIT %(limit)s
        """,
        {
            "urns": werkz_urns or [None],
            "act_imow": activiteit_imow_ids or [None],
            "x": x, "y": y, "limit": limit,
        },
    )
    return cur.fetchall()


def _woordgrens_regex(keywords: list[str]) -> str | None:
    """Bouw één case-insensitieve POSIX-regex die op woordgrens (`\\m`) matcht op
    een van de domein-trefwoorden. Woordgrens i.p.v. substring voorkomt dat een
    korte term als 'vee' matcht in 'verveelend' of 'mest' in 'domesticatie' —
    FTS-achtige precisie zonder een tsvector-kolom. Termen < 5 tekens vallen af
    (te generiek). Geeft None als er niets bruikbaars overblijft."""
    META = r"([.\\^$*+?()\[\]{}|])"
    alts = sorted({k.lower().strip() for k in keywords if len(k.strip()) >= 5})
    if not alts:
        return None
    escaped = [re.sub(META, r"\\\1", k) for k in alts]
    return r"\m(" + "|".join(escaped) + r")"


def tekst_fallback_query(cur, keywords: list[str], x: float, y: float,
                         limit: int) -> list[dict]:
    """Tekst-fallback voor wanneer de activiteit-join niets oplevert.

    De killer-query is structureel (SKOS-concept -> activiteit -> ALA): scherp,
    maar leeg zodra de werkzaamheid->activiteit-mapping op deze locatie ontbreekt
    of naar een irrelevante (andere gemeente) activiteit wijst. Deze fallback laat
    de activiteit-eis los en zoekt de op de locatie geldende regelteksten waarvan
    de tekst/opschrift een domein-trefwoord op woordgrens bevat.

    Zo krijgt "boerderijen" alsnog de echte veehouderij-regels van het lokale
    omgevingsplan, ook zonder kloppende activiteit-FK. `join_pad='tekst_fallback'`
    maakt in de respons zichtbaar dat dit via tekst-match kwam.

    Performance: een `loc_wids`-CTE dedupt eerst de wids op de locatie (via de
    geo-join), daarna filtert de woordgrens-regex met early-stop `LIMIT`. ~0,2-0,7s.
    Geen activiteit-kolom (die hoort bij de activiteit-join, niet bij tekst-match)
    — `activiteit_naam/id` zijn leeg.
    """
    rx = _woordgrens_regex(keywords)
    if rx is None:
        return []
    cur.execute(
        """
        WITH loc_wids AS (
            SELECT DISTINCT jr.regeltekst_wid AS wid, jr.regeling_expression AS rexpr
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
        )
        SELECT DISTINCT ON (te.wid)
            ''::text        AS activiteit_naam,
            ''::text        AS activiteit_id,
            te.opschrift    AS artikel,
            r.opschrift     AS regeling,
            r.frbr_expression AS regeling_expression,
            r.documenttype,
            REGEXP_REPLACE(te.inhoud, '<[^>]+>', '', 'g') AS inhoud,
            'tekst_fallback'::text AS join_pad
        FROM loc_wids lw
        JOIN p2p.tekst_element te ON te.wid = lw.wid
            AND (te.regeling_expression = lw.rexpr OR lw.rexpr IS NULL)
        JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        WHERE te.inhoud IS NOT NULL
          AND length(te.inhoud) > 20
          AND (te.inhoud ~* %(rx)s OR COALESCE(te.opschrift, '') ~* %(rx)s)
        ORDER BY te.wid
        LIMIT %(limit)s
        """,
        {"x": x, "y": y, "rx": rx, "limit": limit},
    )
    return cur.fetchall()


# ─────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────


@router.post("/regelteksten-bij-vraag", response_model=RegeltekstenResponse)
def regelteksten_bij_vraag(req: RegeltekstenRequest):
    """Vraag + locatie -> regelteksten via SKOS-activity-join met geo-filter."""
    address_resolution_ms = None
    rd_x, rd_y = req.x, req.y
    if req.location and (rd_x is None or rd_y is None):
        t0 = time.perf_counter()
        rd_x, rd_y, _ = resolve_address(req.location)
        address_resolution_ms = round((time.perf_counter() - t0) * 1000, 1)

    with get_conn() as conn, conn.cursor() as cur:
        # Stap 1 — SKOS-extractie
        matched_rows, ngrams = match_skos_concepts(cur, req.question, req.max_concepts)

        if not matched_rows:
            # SKOS gaf 0 matches — toch de oorspronkelijke vraag-tokens
            # meesturen zodat de frontend op de letterlijke woorden uit de
            # vraag kan filteren (specialistische termen zonder SKOS-concept).
            vraag_termen_leeg = extract_vraag_chips(cur, req.question)
            return RegeltekstenResponse(
                regelteksten=[],
                matched_concepts=[],
                expanded_keywords=merge_question_tokens([], req.question),
                vraag_termen=vraag_termen_leeg,
                trace=RegeltekstenTrace(
                    tokens_count=len(ngrams),
                    matched_concepts_count=0,
                    sql_query_ms=0,
                    address_resolution_ms=address_resolution_ms,
                    rd_x=rd_x, rd_y=rd_y,
                ),
            )

        # Stap 2-4 — killer query
        t0 = time.perf_counter()
        regel_rows = killer_query(cur, matched_rows, rd_x, rd_y, req.max_regelteksten)

        # Domein-trefwoorden (concept-namen + SKOS-trefwoorden) + vraag-chips:
        # basis voor zowel de tekst-fallback als het frontend object-filter.
        domein_keywords = fetch_expanded_keywords(cur, [r["uri"] for r in matched_rows])
        vraag_termen = extract_vraag_chips(cur, req.question)

        # Stap 4b — tekst-fallback: vond de activiteit-join niets, zoek dan de op
        # de locatie geldende regelteksten waarvan de tekst/opschrift een domein-
        # trefwoord op woordgrens bevat. Vangt de gevallen waarin de werkzaamheid->
        # activiteit-mapping ontbreekt of naar een andere gemeente wijst (zie
        # tekst_fallback_query). Alleen domein-trefwoorden — vraag-chips als
        # "geldt" zouden te breed matchen.
        if not regel_rows:
            regel_rows = tekst_fallback_query(
                cur, domein_keywords, rd_x, rd_y, req.max_regelteksten,
            )
        sql_ms = round((time.perf_counter() - t0) * 1000, 1)

        # Stap 5 — expanded keywords voor frontend object-filter:
        # SKOS-trefwoorden + de oorspronkelijke vraag-tokens (+ stem-varianten).
        expanded_keywords = merge_question_tokens(list(domein_keywords), req.question)

    matched_summaries = [
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

    regelteksten = [
        RegeltekstHit(
            activiteit_naam=r["activiteit_naam"],
            activiteit_id=r["activiteit_id"],
            artikel=r["artikel"],
            regeling=r["regeling"],
            regeling_expression=r["regeling_expression"],
            documenttype=r["documenttype"],
            inhoud=r["inhoud"],
            join_pad=r["join_pad"],
        )
        for r in regel_rows
    ]

    return RegeltekstenResponse(
        regelteksten=regelteksten,
        matched_concepts=matched_summaries,
        expanded_keywords=expanded_keywords,
        vraag_termen=vraag_termen,
        trace=RegeltekstenTrace(
            tokens_count=len(ngrams),
            matched_concepts_count=len(matched_rows),
            sql_query_ms=sql_ms,
            address_resolution_ms=address_resolution_ms,
            rd_x=rd_x, rd_y=rd_y,
        ),
    )
