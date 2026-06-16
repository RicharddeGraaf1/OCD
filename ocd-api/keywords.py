"""
Trefwoord-extractie endpoints voor OCD.

Endpoints:
    POST /v1/keywords/extract — vraag → matched SKOS-concepten + zoektermen
    GET  /v1/keywords/define  — directe definitie-lookup

Gebruikt het `skos`-schema dat geladen is door
`tools/load_dso_skos_into_ocd.py` in omgevingsbot.nl.

Algoritme `/extract`:
    1. Tokenize de vraag (lowercase, stopwoorden eruit)
    2. Bouw 1-, 2- en 3-woord n-grams
    3. SQL: vind concepten waar LOWER(trefwoord) of LOWER(naam) of LOWER(synoniem)
       in de n-grams zit
    4. Score: langere n-gram-match telt zwaarder; meer matches op zelfde concept ook
    5. Per top-N matched concept: laad alle trefwoorden + 1 niveau broader
    6. Return matched_concepts + flat expanded_keywords + trace
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import get_conn

router = APIRouter(prefix="/v1/keywords", tags=["keywords"])


# ─────────────────────────────────────────────────────────────────────
# Stopwoorden
# ─────────────────────────────────────────────────────────────────────

STOP_WORDS: set[str] = {
    "de", "het", "een", "van", "in", "op", "is", "wat", "mag", "mogen", "mocht",
    "ik", "hier", "er", "kan", "kunnen", "kunt", "die", "dat", "voor", "met",
    "aan", "te", "en", "of", "wel", "niet", "hoe", "waar",
    "mijn", "jouw", "onze", "uw",
    "zijn", "worden", "moet", "moeten", "deze", "dit",
    "wil", "wilt", "wilde", "willen", "als", "dan", "nog", "al", "maar", "ook", "zo",
    "gebruiken", "doen", "gaan", "komen", "laten", "maken",
    "iets", "wel", "graag", "even", "eigenlijk",
    # Functie-/meta-woorden die typisch in "wat geldt hier qua ..."-vragen
    # opduiken: dragen geen domein-inhoud en vervuilen als losse substring
    # zowel de chip-rij als de objectmatch (bv. "over" matcht "overkapping").
    "over", "qua", "geldt", "gelden", "geldend", "geldende",
    "omtrent", "inzake", "betreft", "betreffende", "regelgeving",
}


# ─────────────────────────────────────────────────────────────────────
# Pydantic-modellen
# ─────────────────────────────────────────────────────────────────────


class ExtractRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500,
                          description="Gebruikersvraag in natuurlijke taal")
    max_concepts: int = Field(5, ge=1, le=20,
                              description="Maximum aantal teruggegeven concepten")
    include_broader: bool = Field(True,
                                  description="Voeg broader-context toe per match")


class BroaderRef(BaseModel):
    uri: str
    naam: str | None = None


class MatchedConcept(BaseModel):
    uri: str
    naam: str
    scheme: str
    matched_terms: list[str] = Field(
        ...,
        description="Welke n-grams uit de vraag op dit concept matchten "
                    "(via trefwoord/naam/synoniem)",
    )
    all_trefwoorden: list[str] = Field(default_factory=list)
    broader: list[BroaderRef] = Field(default_factory=list)
    werkzaamheid_urn: str | None = None
    activiteit_imow_id: str | None = None
    score: float


class ExtractTrace(BaseModel):
    tokens: list[str]
    ngrams_tried: int
    ngrams_matched: int
    sql_match_rows: int


class ScoredKeyword(BaseModel):
    """V7: trefwoord met relevantie-score en bron-tag.

    Bedoeld voor weighted scoring in `/v1/objecten` en `/v1/regels`. Termen
    met hoge `relevantie` moeten zwaarder wegen bij het ranken van objecten/
    regels dan termen met lage relevantie (synoniemen / broader / related).

    Bron-tags:
    - `letterlijk`: exact token uit de vraag (≥4 chars)
    - `letterlijk-kort`: token van 3 chars uit de vraag
    - `letterlijk-phrase`: multi-woord-fragment uit de vraag
    - `skos-exact`: concept-naam uit SKOS-match
    - `skos-trefwoord`: trefwoord/synoniem van matched concept
    - `skos-broader`: broader concept-naam (1 stap omhoog)
    - `skos-related`: related concept-naam
    """
    term: str
    relevantie: float = Field(..., ge=0.0, le=1.0)
    bron: str


class ExtractResponse(BaseModel):
    matched_concepts: list[MatchedConcept]
    expanded_keywords: list[str] = Field(
        ...,
        description="Vlakke deduped lijst zoektermen — klaar voor downstream "
                    "retrieval. Bevat namen + trefwoorden van alle matches.",
    )
    keywords: list[ScoredKeyword] = Field(
        default_factory=list,
        description="V7: gewogen trefwoorden met bron-tag. Bedoeld als input "
                    "voor /v1/objecten en /v1/regels (passeer als term:gewicht-paren).",
    )
    trace: ExtractTrace


class DefineResponse(BaseModel):
    naam: str
    definitie: str
    scheme: str
    uri: str
    eigenaar: str | None = None


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _tokenize(question: str) -> list[str]:
    """Lowercase, splits op niet-letter/cijfer, stopwoorden eruit."""
    cleaned = re.sub(r"[^\w\s\-&]", " ", question.lower())
    tokens = [t for t in cleaned.split() if t and t not in STOP_WORDS and len(t) > 1]
    return tokens


def _stem_variant(token: str) -> str | None:
    """Simpele Nederlandse meervoud→enkelvoud heuristiek.

    Strip `-en` of `-s` van het einde mits de stam ≥4 letters overhoudt.
    Bedoeld om matches op SKOS-trefwoorden ook te laten werken voor de
    meervoudsvariant uit een gebruikersvraag (bv. "recreatiewoningen" →
    extra n-gram "recreatiewoning").

    Geen volwaardige lemmatisering — accepteert false negatives (bv.
    "huizen" → "huize" matcht niet op "huis") in ruil voor zero-dependency
    eenvoud. Geeft `None` terug als er niets te strippen valt.
    """
    if len(token) < 6:
        return None
    if token.endswith("en"):
        return token[:-2]
    if token.endswith("s"):
        return token[:-1]
    return None


def _build_ngrams(tokens: list[str], max_n: int = 3) -> list[str]:
    """1-, 2-, 3-woord n-grams (langste eerst voor scoring-voorkeur).

    Voor elk woord wordt ook een meervoud→enkelvoud-stam-variant toegevoegd
    als losse n-gram, plus een variant waar het laatste woord van een multi-
    woord n-gram gestamd is. Dat dekt "recreatiewoningen worden gebouwd"
    → o.a. "recreatiewoning", "nieuwe recreatiewoning".

    Streepjes-tokens (bv. "antenne-installatie") worden óók opgedeeld in
    losse delen ("antenne", "installatie") en als spatie-variant
    ("antenne installatie") toegevoegd, zodat ze matchen ongeacht of de
    gebruiker met of zonder streepje typt.
    """
    ngrams: list[str] = []
    for n in range(min(max_n, len(tokens)), 0, -1):
        for i in range(len(tokens) - n + 1):
            chunk = tokens[i : i + n]
            ngrams.append(" ".join(chunk))
            # Stam-variant op het laatste woord — dekt meervoud van het
            # zelfstandig naamwoord aan het einde van een woordgroep.
            stem = _stem_variant(chunk[-1])
            if stem:
                variant = " ".join(chunk[:-1] + [stem])
                ngrams.append(variant)
    # Streepjes-varianten — voeg per koppelteken-token de losse delen + de
    # spatie-variant toe als extra n-grams.
    for t in tokens:
        if "-" in t and len(t) > 2:
            parts = [p for p in t.split("-") if len(p) > 1]
            if len(parts) >= 2:
                ngrams.append(" ".join(parts))  # spatie-variant
                for p in parts:
                    ngrams.append(p)
                    stem = _stem_variant(p)
                    if stem:
                        ngrams.append(stem)
    # Dedupe met behoud van volgorde (langste-eerst voor scoring).
    seen: set[str] = set()
    out: list[str] = []
    for g in ngrams:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _score_match_row(row: dict, freq_per_term: dict[str, int] | None = None) -> float:
    """Score voor één matched concept.

    Componenten (allemaal multiplicatief):
      1. Lengte-bonus: een 2-woord-match telt zwaarder dan twee losse woord-
         matches (`wlen²`: 1, 4, 9 voor 1-, 2-, 3-woord-matches).
      2. IDF-weging: termen die op weinig SKOS-concepten matchen ("antenne",
         freq=1) krijgen veel hoger gewicht dan termen die op veel matchen
         ("bouwen", freq=7+). Voorkomt dat generieke werkwoorden de top-N
         volstoppen ten koste van specifieke vraag-termen.
      3. Werkzaamheden-boost (1.5×): juridisch herkenbare werkzaamheden zijn
         meestal de bedoelde inhoud van de vraag.

    `freq_per_term` ontbreken (None of term niet in dict) → idf=1.0 als
    fallback, zodat oude callers/tests blijven werken.
    """
    import math

    matched = row["matched_terms"]
    score = 0.0
    for term in matched:
        wlen = len(term.split())
        if freq_per_term is None:
            idf = 1.0
        else:
            f = max(1, freq_per_term.get(term, 1))
            # f=1 → 1.0, f=2 → 0.59, f=5 → 0.38, f=10 → 0.30, f=50 → 0.20
            idf = 1.0 / (1.0 + math.log(f))
        score += wlen * wlen * idf
    if row["scheme_naam"] == "Werkzaamheden":
        score *= 1.5
    return score


def _fetch_freq_per_term(cur, terms: list[str]) -> dict[str, int]:
    """Tel per term hoe vaak die als trefwoord/naam/synoniem in SKOS staat.

    Voer alleen op de terms die daadwerkelijk gematcht zijn (kleine set), niet
    op alle n-grams uit de vraag. Gebruikt voor IDF-weging in `_score_match_row`.
    """
    if not terms:
        return {}
    cur.execute(
        """
        SELECT lc AS term, COUNT(DISTINCT concept_uri) AS freq
        FROM (
            SELECT concept_uri, LOWER(trefwoord) AS lc FROM skos.trefwoord
            UNION ALL
            SELECT uri,           LOWER(naam)      FROM skos.concept
            UNION ALL
            SELECT concept_uri, LOWER(synoniem)  FROM skos.synoniem
        ) all_lc
        WHERE lc = ANY(%s)
        GROUP BY lc
        """,
        (terms,),
    )
    return {r["term"]: r["freq"] for r in cur.fetchall()}


def extract_vraag_chips(cur, question: str, max_freq_for_1gram: int = 5) -> list[str]:
    """Specifieke termen uit de vraag voor de chip-rij in de UI.

    Selectie:
      - 2-/3-grams uit de vraag worden altijd opgenomen (multi-woord
        combinaties zijn doorgaans specifiek genoeg om als filter-chip
        te dienen).
      - 1-grams alleen als ze in ≤ `max_freq_for_1gram` SKOS-concepten
        voorkomen — generieke werkwoorden als "bouwen" (freq=7+) blijven zo
        weg, terwijl specialistische termen ("antenne", freq=1) en termen
        die helemaal niet in SKOS staan (freq=0, bv. "tiny") wél meegaan.
      - Stem-varianten en streepje-splits worden niet als chip getoond:
        die zijn voor matching, niet voor de UI.

    Volgorde: langste-eerst zodat de meest specifieke termen bovenaan
    komen wanneer de UI een limiet hanteert.
    """
    tokens = _tokenize(question)
    if not tokens:
        return []

    # Bouw kandidaten: multi-grams (langste eerst), dan 1-grams
    candidates: list[str] = []
    for n in range(min(3, len(tokens)), 1, -1):
        for i in range(len(tokens) - n + 1):
            candidates.append(" ".join(tokens[i : i + n]))
    candidates.extend(tokens)

    # Freq alleen voor de 1-grams nodig — multi-grams nemen we sowieso mee.
    freq = _fetch_freq_per_term(cur, list({t for t in tokens}))

    seen: set[str] = set()
    out: list[str] = []
    for term in candidates:
        key = term.lower()
        if key in seen:
            continue
        # 1-gram-drempel: te generieke werkwoorden vallen weg
        if " " not in term and freq.get(key, 0) > max_freq_for_1gram:
            continue
        seen.add(key)
        out.append(term)
    return out


# V7 weights — kalibreerbaar, zie objecten-regels-retrieve-endpoint.md §"Open punt 1"
_KW_WEIGHT_LETTERLIJK         = 1.00  # exact woord uit vraag, ≥4 chars
_KW_WEIGHT_LETTERLIJK_PHRASE  = 0.95  # multi-woord uit vraag
_KW_WEIGHT_LETTERLIJK_KORT    = 0.80  # 3-letter woord uit vraag
_KW_WEIGHT_SKOS_EXACT         = 0.80  # SKOS concept-naam (matched)
_KW_WEIGHT_SKOS_TREFWOORD     = 0.70  # trefwoord/synoniem van matched concept
_KW_WEIGHT_SKOS_BROADER       = 0.60  # broader concept-naam
_KW_WEIGHT_SKOS_RELATED       = 0.40  # related concept-naam


def build_scored_keywords(
    question: str,
    matched_rows: list[dict],
    trefw_by_uri: dict[str, list[str]],
    broader_by_uri: dict[str, list],
) -> list[dict]:
    """V7: bouw gewogen trefwoord-lijst uit vraag + SKOS-matches.

    Geeft een lijst dicts met `term`, `relevantie`, `bron` — gesorteerd op
    relevantie aflopend, gededupeerd op lowercase-term (eerste-wint).
    """
    keywords: list[dict] = []
    seen: set[str] = set()

    def add(term: str, relevantie: float, bron: str) -> None:
        term = term.strip()
        if not term:
            return
        key = term.lower()
        if key in seen:
            return
        seen.add(key)
        keywords.append({"term": term, "relevantie": relevantie, "bron": bron})

    # 1. Letterlijke woorden uit de vraag
    tokens = _tokenize(question)
    for t in tokens:
        if len(t) >= 4:
            add(t, _KW_WEIGHT_LETTERLIJK, "letterlijk")
        elif len(t) == 3:
            add(t, _KW_WEIGHT_LETTERLIJK_KORT, "letterlijk-kort")

    # 2. Letterlijke multi-woord-phrases (2- en 3-grams uit de vraag)
    for n in (3, 2):
        for i in range(len(tokens) - n + 1):
            phrase = " ".join(tokens[i : i + n])
            add(phrase, _KW_WEIGHT_LETTERLIJK_PHRASE, "letterlijk-phrase")

    # 3. SKOS concept-namen (exact match op de vraag)
    for r in matched_rows:
        naam = (r.get("naam") or "").strip()
        if naam:
            add(naam, _KW_WEIGHT_SKOS_EXACT, "skos-exact")

    # 4. Trefwoorden / synoniemen van de matched concepten
    for r in matched_rows:
        uri = r.get("uri")
        for tw in trefw_by_uri.get(uri, []) if uri else []:
            add(tw, _KW_WEIGHT_SKOS_TREFWOORD, "skos-trefwoord")

    # 5. Broader concept-namen
    for r in matched_rows:
        uri = r.get("uri")
        for br in broader_by_uri.get(uri, []) if uri else []:
            br_naam = br.naam if hasattr(br, "naam") else br.get("naam")
            if br_naam:
                add(br_naam, _KW_WEIGHT_SKOS_BROADER, "skos-broader")

    # Sort op relevantie aflopend; bij gelijkspel langere term eerst (specifieker)
    keywords.sort(key=lambda k: (k["relevantie"], len(k["term"])), reverse=True)
    return keywords


def match_skos_concepts(cur, question: str, max_concepts: int = 5) -> tuple[list[dict], list[str]]:
    """SKOS-match-helper, herbruikbaar door meerdere endpoints.

    Geeft (matched_rows, ngrams_tried) terug. Iedere row heeft de velden
    `uri, naam, scheme_naam, werkzaamheid_urn, activiteit_imow_id, matched_terms`
    plus een berekende `score`. Ranked op score, getrimd op `max_concepts`.
    """
    tokens = _tokenize(question)
    if not tokens:
        return [], []
    ngrams = _build_ngrams(tokens)
    if not ngrams:
        return [], tokens

    cur.execute(
        """
        WITH q(token) AS (SELECT UNNEST(%s::text[]))
        SELECT
            c.uri,
            c.naam,
            c.scheme_naam,
            c.werkzaamheid_urn,
            c.activiteit_imow_id,
            array_agg(DISTINCT q.token ORDER BY q.token) AS matched_terms
        FROM q
        JOIN (
            SELECT concept_uri, LOWER(trefwoord) AS lc FROM skos.trefwoord
            UNION ALL
            SELECT uri,           LOWER(naam)      FROM skos.concept
            UNION ALL
            SELECT concept_uri, LOWER(synoniem)  FROM skos.synoniem
        ) hits ON hits.lc = q.token
        JOIN skos.concept c ON c.uri = hits.concept_uri
        WHERE c.scheme_naam IN ('Werkzaamheden','Activiteiten','functie','bouw','erfgoed','natuur',
                                 'water en watersysteem','ruimtelijk gebruik','energievoorziening',
                                 'omgevingsnorm','type gebiedsaanwijzing','Gemeentelijk begrippenkader VNG')
        GROUP BY c.uri, c.naam, c.scheme_naam, c.werkzaamheid_urn, c.activiteit_imow_id
        ORDER BY array_length(array_agg(DISTINCT q.token), 1) DESC
        LIMIT %s
        """,
        (ngrams, max_concepts * 4),
    )
    rows = cur.fetchall()
    if not rows:
        return [], ngrams
    # IDF-weging: tel per gematchte term hoe vaak 'ie in de SKOS-graaf
    # voorkomt, zodat zeldzame (specifieke) termen zwaarder wegen dan
    # generieke werkwoorden als "bouwen".
    all_terms = sorted({t for r in rows for t in r["matched_terms"]})
    freq_per_term = _fetch_freq_per_term(cur, all_terms)
    for r in rows:
        r["score"] = _score_match_row(r, freq_per_term)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:max_concepts], ngrams


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


# Verify-key dependency komt uit main.py; we laten 'm injecteren via include_router
# i.p.v. hier herdefiniëren — dat houdt API-keys op één plek.


@router.post("/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest):
    """Vind SKOS-concepten die op de vraag passen, en geef zoektermen terug."""
    with get_conn() as conn, conn.cursor() as cur:
        rows, ngrams = match_skos_concepts(cur, req.question, req.max_concepts)
        tokens = _tokenize(req.question)

        if not rows:
            # V7: ook bij 0 SKOS-matches geven we de letterlijke woorden uit de
            # vraag terug — die zijn nog steeds bruikbaar als zoektermen voor
            # /v1/objecten en /v1/regels (FTS- en ILIKE-match op naam-velden).
            scored = build_scored_keywords(req.question, [], {}, {})
            return ExtractResponse(
                matched_concepts=[],
                expanded_keywords=[],
                keywords=[ScoredKeyword(**k) for k in scored],
                trace=ExtractTrace(
                    tokens=tokens, ngrams_tried=len(ngrams),
                    ngrams_matched=0, sql_match_rows=0,
                ),
            )

        # Voor de top: alle trefwoorden ophalen + (optioneel) broader 1 niveau
        uris = [r["uri"] for r in rows]
        cur.execute(
            "SELECT concept_uri, trefwoord FROM skos.trefwoord WHERE concept_uri = ANY(%s)",
            (uris,),
        )
        trefw_by_uri: dict[str, list[str]] = {u: [] for u in uris}
        for tr in cur.fetchall():
            trefw_by_uri[tr["concept_uri"]].append(tr["trefwoord"])

        broader_by_uri: dict[str, list[BroaderRef]] = {u: [] for u in uris}
        if req.include_broader and uris:
            cur.execute(
                """
                SELECT r.source_uri, r.target_uri, c.naam
                FROM skos.relation r
                LEFT JOIN skos.concept c ON c.uri = r.target_uri
                WHERE r.source_uri = ANY(%s) AND r.type = 'broader'
                """,
                (uris,),
            )
            for br in cur.fetchall():
                broader_by_uri[br["source_uri"]].append(
                    BroaderRef(uri=br["target_uri"], naam=br["naam"])
                )

        matched_concepts: list[MatchedConcept] = []
        for r in rows:
            mc = MatchedConcept(
                uri=r["uri"],
                naam=r["naam"],
                scheme=r["scheme_naam"],
                matched_terms=r["matched_terms"],
                all_trefwoorden=trefw_by_uri.get(r["uri"], []),
                broader=broader_by_uri.get(r["uri"], []) if req.include_broader else [],
                werkzaamheid_urn=r["werkzaamheid_urn"],
                activiteit_imow_id=r["activiteit_imow_id"],
                score=round(r["score"], 2),
            )
            matched_concepts.append(mc)

        # Vlakke deduped expanded_keywords
        seen: set[str] = set()
        expanded: list[str] = []
        for mc in matched_concepts:
            for term in [mc.naam] + mc.all_trefwoorden:
                key = term.lower()
                if key not in seen:
                    seen.add(key)
                    expanded.append(term)

        # V7: gewogen scored-keywords-output
        scored = build_scored_keywords(req.question, rows, trefw_by_uri, broader_by_uri)

        return ExtractResponse(
            matched_concepts=matched_concepts,
            expanded_keywords=expanded,
            keywords=[ScoredKeyword(**k) for k in scored],
            trace=ExtractTrace(
                tokens=tokens,
                ngrams_tried=len(ngrams),
                ngrams_matched=len(rows),
                sql_match_rows=len(rows),
            ),
        )


@router.get("/define", response_model=DefineResponse)
def define(
    term: str = Query(..., min_length=2, max_length=200,
                      description="Term om op te zoeken (case-insensitive op naam)"),
):
    """Lookup definitie van een term, direct uit SKOS — zonder retrieval."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT naam, definitie, scheme_naam, uri, eigenaar
            FROM skos.concept
            WHERE LOWER(naam) = LOWER(%s) AND definitie IS NOT NULL
            ORDER BY length(definitie) DESC  -- kies langste/rijkste definitie
            LIMIT 1
            """,
            (term,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Geen definitie gevonden voor '{term}'")
        return DefineResponse(
            naam=row["naam"],
            definitie=row["definitie"],
            scheme=row["scheme_naam"],
            uri=row["uri"],
            eigenaar=row["eigenaar"],
        )
