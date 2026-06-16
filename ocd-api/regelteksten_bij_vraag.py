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
import math
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from db import get_conn
from keywords import (
    match_skos_concepts, extract_vraag_chips, _stem_variant, _tokenize,
    _fetch_freq_per_term, _is_action_only, build_scored_keywords, ScoredKeyword,
)

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
    wid: str | None = Field(
        None, description="STOP-wid van de regeltekst (OW) — voor klik-naar-leestekst.",
    )
    artikel_nummer: str | None = Field(
        None, description="Nummer van de Artikel-voorouder, bv. '2.5'.",
    )
    artikel_opschrift: str | None = Field(
        None, description="Opschrift van de Artikel-voorouder.",
    )
    hoofdstuk_nummer: str | None = Field(
        None, description="Nummer van de Hoofdstuk-voorouder, bv. '2'.",
    )
    join_pad: str = Field(
        ...,
        description="Welke join-strategie deze regeltekst opleverde "
                    "(werkzaamheid_fk / werkzaamheid_naam / activiteit_uri / tekst_fallback)",
    )
    relevantie: float | None = Field(
        None,
        description="Begrippen-gewogen relevantiescore (Σ woordsoort × IDF × bron "
                    "over de aanwezige begrippen, objectnaam-match zwaarder). Hoger "
                    "= relevanter; bedoeld voor frontend-sortering. None als er geen "
                    "scoorbare begrippen waren (structurele join is dan het signaal).",
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
    keywords: list[ScoredKeyword] = Field(
        default_factory=list,
        description="Begrippen met relevantie (0-1) + bron-tag (letterlijk/skos-*; "
                    "woordsoort znw vs. actiewoord). Voor de frontend-objectfilter en "
                    "chip-weging — sterke begrippen (znw, letterlijk) zwaarder dan "
                    "zwakke (actiewoorden, broader/related). Spiegelt /v1/keywords/extract.",
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


def merge_question_tokens(cur, expanded: list[str], question: str) -> list[str]:
    """Voeg de oorspronkelijke tokens van de vraag (+ stem-varianten) toe aan
    expanded_keywords. Dedupe op lowercase, behoud volgorde.

    Reden: een gebruiker kan een specifieke term noemen ("recreatiewoning")
    die niet als SKOS-naam/trefwoord op álle semantisch relevante concepten
    staat, maar wel letterlijk in een object-naam voorkomt. Of: een term zit
    helemaal niet in SKOS (specialistische vakterm) maar wel in object-namen
    op deze locatie. In beide gevallen wil je dat de downstream object-filter
    daarop kan matchen, óók zonder dat SKOS er een concept aan koppelt.

    Filter (om over-brede substring-objectmatch te voorkomen):
      - tokens < 3 letters vallen altijd weg;
      - tokens die NIET in SKOS voorkomen (freq 0) moeten ≥ 4 letters zijn —
        zo lekken korte/generieke ruis-woorden niet de objectmatch in, terwijl
        vaktermen die buiten SKOS vallen ("recreatiewoning", "datacentrum")
        wél meegaan. Tokens die wél in SKOS staan gaan altijd mee, ook kort
        (bv. "vee"), want dan is het een bevestigde domein-term.

    Stopwoord-achtige functie-/meta-woorden ("over", "qua", "geldt",
    "regelgeving", …) zijn al door `_tokenize` (STOP_WORDS) weggefilterd.
    """
    tokens = _tokenize(question)
    if not tokens:
        return expanded
    seen = {t.lower() for t in expanded}
    # SKOS-frequentie per uniek token bepaalt of een niet-bevestigd token
    # specifiek genoeg is om de objectmatch in te gaan.
    freq = _fetch_freq_per_term(cur, list({t.lower() for t in tokens}))
    # Heeft de vraag een inhouds-token (znw)? Zo ja, weer dan de puur-actiewoord-
    # tokens ("veranderen") uit de objectmatch-keywords: anders matcht "veranderen"
    # op woordgrens élke "… bouwen of veranderen"-activiteit (de objectpaneel-ruis,
    # 131 → handvol). Spiegelt _is_action_only die de SKOS-conceptselectie al
    # beschermt. Geen inhouds-token (bv. "wat mag ik hier slopen") → actiewoorden
    # tóch toelaten als enige signaal (fallback).
    heeft_inhoud = any(not _is_action_only(t) for t in tokens if len(t) >= 3)
    for t in tokens:
        if len(t) < 3:
            continue
        if freq.get(t.lower(), 0) == 0 and len(t) < 4:
            continue
        if heeft_inhoud and _is_action_only(t):
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


def resolve_artikel_context(cur, te_ids: list[int]) -> dict[int, dict]:
    """Resolve per tekst_element-id de voorouder-Artikel (nummer + opschrift) en
    het Hoofdstuk-nummer via een single-pass tree-walk omhoog langs `parent_id`.

    Regelteksten zijn vaak op lid-niveau (leeg `opschrift`); dit haalt de kop van
    het omvattende Artikel op zodat de frontend "Artikel 2.5 <opschrift> (Hoofdstuk 2)"
    kan tonen i.p.v. een kale "Regel". Spiegelt de verrijking van het
    regelmix-detail-endpoint (`viewer_regelmix_document`). Alleen OW.
    """
    if not te_ids:
        return {}
    cur.execute(
        """
        WITH RECURSIVE walk AS (
            SELECT t.id AS origin, t.id, t.parent_id,
                CASE WHEN t.element_type = 'Artikel'   THEN t.nummer   END AS art_nr,
                CASE WHEN t.element_type = 'Artikel'   THEN t.opschrift END AS art_op,
                CASE WHEN t.element_type = 'Hoofdstuk' THEN t.nummer   END AS hfd_nr
            FROM p2p.tekst_element t
            WHERE t.id = ANY(%(ids)s)
            UNION ALL
            SELECT w.origin, p.id, p.parent_id,
                COALESCE(w.art_nr, CASE WHEN p.element_type = 'Artikel'   THEN p.nummer   END),
                COALESCE(w.art_op, CASE WHEN p.element_type = 'Artikel'   THEN p.opschrift END),
                COALESCE(w.hfd_nr, CASE WHEN p.element_type = 'Hoofdstuk' THEN p.nummer   END)
            FROM walk w
            JOIN p2p.tekst_element p ON p.id = w.parent_id
            WHERE w.art_nr IS NULL OR w.hfd_nr IS NULL
        )
        SELECT origin,
               max(art_nr) AS artikel_nummer,
               max(art_op) AS artikel_opschrift,
               max(hfd_nr) AS hoofdstuk_nummer
        FROM walk GROUP BY origin
        """,
        {"ids": te_ids},
    )
    return {r["origin"]: r for r in cur.fetchall()}


def verrijk_met_artikel(cur, rows: list[dict]) -> list[dict]:
    """Voeg artikel_nummer/artikel_opschrift/hoofdstuk_nummer toe aan retrieval-
    rijen (in-place) op basis van hun `te_id`. No-op als de rijen geen te_id
    dragen of leeg zijn."""
    te_ids = [r["te_id"] for r in rows if r.get("te_id") is not None]
    ctx = resolve_artikel_context(cur, te_ids)
    for r in rows:
        c = ctx.get(r.get("te_id"), {})
        r["artikel_nummer"] = c.get("artikel_nummer")
        r["artikel_opschrift"] = c.get("artikel_opschrift")
        r["hoofdstuk_nummer"] = c.get("hoofdstuk_nummer")
    return rows


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
            te.id         AS te_id,
            te.wid        AS wid,
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
            -- Pad A: wids van regels die op de locatie gelden via de activiteit-junction
            -- (alle artikelstructuur-regelingen: omgevingsplan, AMvB, waterschap, OvOv).
            SELECT DISTINCT jr.regeltekst_wid AS wid, jr.regeling_expression AS rexpr
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
            UNION
            -- Pad B: wids van alle tekst_elementen in regelingen waarvan
            -- regelingsgebied de coord raakt — vangt vrijetekst-instrumenten
            -- (omgevingsvisie, programma, N2000-besluit, projectbesluit) die geen
            -- juridische_regel hebben en dus via Pad A onbereikbaar zijn. Dezelfde
            -- scope-filosofie als /v1/semantisch sinds 2026-06-11. Cluster C-fix.
            SELECT te.wid, te.regeling_expression AS rexpr
            FROM p2p.regeling r
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = r.regelingsgebied_id
            JOIN p2p.tekst_element te ON te.regeling_expression = r.frbr_expression
            WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
              AND NOT r.inactief
              AND r.documenttype IN ('Omgevingsvisie','Programma',
                                     'Aanwijzingsbesluit N2000','Projectbesluit',
                                     'Toegangsbeperkingsbesluit')
        )
        SELECT DISTINCT ON (te.wid)
            te.id           AS te_id,
            te.wid          AS wid,
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
# Begrippen-gewogen score + cutoff (Fase 3)
# ─────────────────────────────────────────────────────────────────────

# Relatieve cutoff: houd regels met score ≥ α × topscore. Ruim startpunt (0,4):
# liever te veel tonen en later aanscherpen dan relevante regels wegknippen bij
# een vlakke score-verdeling. Tunen op de proefvragen-set (geen viewer-eval-set).
_ALPHA_CUTOFF = 0.4
# Een begrip in de objectnaam (annotatie) weegt zwaarder dan in de lopende tekst.
_NAAM_BONUS = 1.5


def fetch_trefw_broader(cur, uris: list[str]) -> tuple[dict[str, list[str]], dict[str, list[dict]]]:
    """Trefwoorden + broader-namen per concept-URI, voor build_scored_keywords."""
    trefw_by_uri: dict[str, list[str]] = {u: [] for u in uris}
    broader_by_uri: dict[str, list[dict]] = {u: [] for u in uris}
    if not uris:
        return trefw_by_uri, broader_by_uri
    cur.execute(
        "SELECT concept_uri, trefwoord FROM skos.trefwoord WHERE concept_uri = ANY(%s)",
        (uris,),
    )
    for r in cur.fetchall():
        trefw_by_uri.setdefault(r["concept_uri"], []).append(r["trefwoord"])
    cur.execute(
        """
        SELECT r.source_uri, c.naam
        FROM skos.relation r
        LEFT JOIN skos.concept c ON c.uri = r.target_uri
        WHERE r.source_uri = ANY(%s) AND r.type = 'broader'
        """,
        (uris,),
    )
    for r in cur.fetchall():
        broader_by_uri.setdefault(r["source_uri"], []).append({"naam": r["naam"]})
    return trefw_by_uri, broader_by_uri


def _term_regex(term: str) -> re.Pattern | None:
    """Linker-woordgrens-regex voor één term (Python re). Spiegelt de frontend
    `vraagTermRegex`: compounds matchen ("vee" → "veehouderij"), mid-woord niet.
    Termen < 3 tekens vallen weg (te generiek)."""
    t = term.lower().strip()
    if len(t) < 3:
        return None
    return re.compile(r"(?<![a-z0-9])" + re.escape(t))


def compute_term_weights(cur, scored: list[dict]) -> list[dict]:
    """Per scored keyword een gewicht = woordsoort × specificiteit(IDF) × bron.

      - woordsoort  : znw 1.0, puur-actiewoord 0.5 (_is_action_only)
      - specificiteit: IDF uit de SKOS-frequentie (zeldzaam = zwaarder); multi-woord
                       en niet-SKOS-termen krijgen idf 1.0
      - bron        : de `relevantie` uit build_scored_keywords (letterlijk 1.0 …
                       skos-related 0.4)

    Geeft list of {term, regex, gewicht, is_content}, sterkste eerst. Termen die
    geen bruikbare woordgrens-regex opleveren (< 3 tekens) vallen weg.
    """
    if not scored:
        return []
    freq = _fetch_freq_per_term(cur, [k["term"].lower() for k in scored])
    out: list[dict] = []
    for k in scored:
        term = k["term"]
        rx = _term_regex(term)
        if rx is None:
            continue
        is_action = _is_action_only(term)
        woordsoort = 0.5 if is_action else 1.0
        f = max(1, freq.get(term.lower(), 1))
        idf = 1.0 / (1.0 + math.log(f))
        out.append({
            "term": term,
            "regex": rx,
            "gewicht": woordsoort * idf * float(k["relevantie"]),
            "is_content": not is_action,
        })
    out.sort(key=lambda x: x["gewicht"], reverse=True)
    return out


def score_en_cutoff(rows: list[dict], term_weights: list[dict],
                    limit: int, alpha: float = _ALPHA_CUTOFF) -> list[dict]:
    """Scoor regelteksten op aanwezige begrippen en pas de hybride cutoff toe.

    Score per regel = Σ gewicht(begrip) voor elk begrip dat op woordgrens in de
    objectnaam (× _NAAM_BONUS) of in de tekst/opschrift voorkomt.

    Cutoff (zie [[Relevantiescoring en cutoff voor viewer-retrieval]] §3):
      1. harde bodem  — een regel die alléén op actiewoorden matcht valt weg
                        (tenzij er geen enkele inhouds-match is — dan behouden);
      2. relatief     — houd score ≥ alpha × topscore;
      3. vangrails    — altijd ≥1, nooit meer dan `limit`.

    Geen enkele scoorbare match → val terug op de oorspronkelijke (deterministische)
    volgorde: de structurele activiteit-join is dan zelf het relevantiesignaal.
    """
    if not rows:
        return rows
    if not term_weights:
        for r in rows:
            r["relevantie"] = None
        return rows[:limit]
    for r in rows:
        naam = (r.get("activiteit_naam") or "").lower()
        tekst = ((r.get("inhoud") or "") + " " + (r.get("artikel") or "")).lower()
        score = 0.0
        content_hit = False
        for tw in term_weights:
            in_naam = bool(tw["regex"].search(naam))
            in_tekst = in_naam or bool(tw["regex"].search(tekst))
            if not in_tekst:
                continue
            score += tw["gewicht"] * (_NAAM_BONUS if in_naam else 1.0)
            if tw["is_content"]:
                content_hit = True
        r["relevantie"] = round(score, 4)
        r["_content_hit"] = content_hit

    scoorbaar = [r for r in rows if (r.get("relevantie") or 0) > 0]
    if not scoorbaar:
        for r in rows:
            r["relevantie"] = None
        return rows[:limit]
    content = [r for r in scoorbaar if r.get("_content_hit")]
    pool = content if content else scoorbaar          # harde bodem (met fallback)
    pool.sort(key=lambda r: r["relevantie"], reverse=True)
    top = pool[0]["relevantie"]
    kept = [r for r in pool if r["relevantie"] >= alpha * top] or pool[:1]
    return kept[:limit]


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
            # SKOS gaf 0 matches (komt voor bij beleids-/visievragen waar het
            # vocabulaire buiten de SKOS-omgevingsnorm-/activiteit-schema's valt:
            # "hoogwaterbescherming", "Lelylijn", "broeikasgassen"). Toch een
            # tekst-fallback proberen met de vraag-tokens als keywords — dekt
            # vrijetekst-instrumenten (omgevingsvisie/programma) waar de relevante
            # term letterlijk in de proza staat. Cluster C-fix 2026-06-16.
            vraag_termen_leeg = extract_vraag_chips(cur, req.question)
            t0 = time.perf_counter()
            fallback_rows = tekst_fallback_query(
                cur, vraag_termen_leeg, rd_x, rd_y, req.max_regelteksten,
            )
            # Geen SKOS-concepten → scored keywords uit de vraag-tokens alleen
            # (letterlijk/phrase), dan begrippen-gewogen score + cutoff.
            scored_leeg = build_scored_keywords(req.question, [], {}, {})
            term_weights_leeg = compute_term_weights(cur, scored_leeg)
            fallback_rows = score_en_cutoff(
                fallback_rows, term_weights_leeg, req.max_regelteksten,
            )
            verrijk_met_artikel(cur, fallback_rows)
            sql_ms = round((time.perf_counter() - t0) * 1000, 1)
            return RegeltekstenResponse(
                regelteksten=[RegeltekstHit(**r) for r in fallback_rows],
                matched_concepts=[],
                expanded_keywords=merge_question_tokens(cur, [], req.question),
                vraag_termen=vraag_termen_leeg,
                keywords=[ScoredKeyword(**k) for k in scored_leeg],
                trace=RegeltekstenTrace(
                    tokens_count=len(ngrams),
                    matched_concepts_count=0,
                    sql_query_ms=sql_ms,
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

        # Stap 5 — begrippen-gewogen score + cutoff op de regelmix. De scored
        # keywords (relevantie + bron + woordsoort) komen uit dezelfde SKOS-match;
        # de cutoff zet de sterke begrippen (znw, letterlijk, zeldzaam) bovenaan en
        # snoeit de zwakke staart. Zie [[Relevantiescoring en cutoff voor viewer-retrieval]].
        trefw_by_uri, broader_by_uri = fetch_trefw_broader(cur, [r["uri"] for r in matched_rows])
        scored = build_scored_keywords(req.question, matched_rows, trefw_by_uri, broader_by_uri)
        term_weights = compute_term_weights(cur, scored)
        regel_rows = score_en_cutoff(regel_rows, term_weights, req.max_regelteksten)

        # Verrijk met de voorouder-Artikel + Hoofdstuk zodat lid-niveau teksten
        # als "Artikel 2.5 <opschrift> (Hoofdstuk 2)" getoond kunnen worden.
        verrijk_met_artikel(cur, regel_rows)
        sql_ms = round((time.perf_counter() - t0) * 1000, 1)

        # Stap 6 — expanded keywords voor frontend object-filter (legacy, additief
        # naast `keywords`): SKOS-trefwoorden + vraag-tokens (actiewoorden geweerd
        # in merge_question_tokens). De frontend prefereert `keywords` als die er is.
        expanded_keywords = merge_question_tokens(cur, list(domein_keywords), req.question)

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
            wid=r.get("wid"),
            artikel_nummer=r.get("artikel_nummer"),
            artikel_opschrift=r.get("artikel_opschrift"),
            hoofdstuk_nummer=r.get("hoofdstuk_nummer"),
            join_pad=r["join_pad"],
            relevantie=r.get("relevantie"),
        )
        for r in regel_rows
    ]

    return RegeltekstenResponse(
        regelteksten=regelteksten,
        matched_concepts=matched_summaries,
        expanded_keywords=expanded_keywords,
        vraag_termen=vraag_termen,
        keywords=[ScoredKeyword(**k) for k in scored],
        trace=RegeltekstenTrace(
            tokens_count=len(ngrams),
            matched_concepts_count=len(matched_rows),
            sql_query_ms=sql_ms,
            address_resolution_ms=address_resolution_ms,
            rd_x=rd_x, rd_y=rd_y,
        ),
    )
