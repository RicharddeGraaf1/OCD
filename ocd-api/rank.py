"""Port van de bot-ranking (`chat_service._rank_by_relevance`) naar OCD.

Fase 2.0 van richting B (zie vault "Plan refactor gedeelde retrieval-laag bot en
viewer" §richting B): de bot-retrieval-engine wordt server-side gedeeld zodat de
viewer hem kan overnemen. Faithful port; de bot-versie blijft autoritair tot fase
2.4 (dan draait de bot óók op deze engine en is de duplicatie weg).

Werkt op de `_wat_geldt_hier`-rij-vorm (regeling / documenttype / artikel / inhoud /
fts_rank). Heuristiek + gewogen SKOS, identiek aan de bot. Bewust (nog) NIET geport:
BM25-bonus (optioneel, vereist rank_bm25) en source-priority (bot-`bron`-veld dat de
DB-rijen niet dragen) — beide klein/additief; meet of de poort (2.1) ze nodig heeft.
"""
from __future__ import annotations

import re

MIN_RELEVANCE_SCORE = 2.0

# Identiek aan chat_service._LOCATION_WORDS.
LOCATION_WORDS: set[str] = {
    "amsterdam", "rotterdam", "utrecht", "den haag", "eindhoven",
    "groningen", "almere", "breda", "nijmegen", "tilburg",
    "arnhem", "haarlem", "amersfoort", "zaanstad", "haarlemmermeer",
    "gemeente", "provincie", "wijk", "buurt", "stad", "dorp",
}

_OVERVIEW_TERMS = ("samenvatting", "publiekssamenvatting", "introductie",
                   "hoofdpunten", "grote opgaven", "groeien")


def _tekst(rt: dict) -> str:
    for field in ("documentTekst", "tekst", "inhoud", "content"):
        if rt.get(field):
            return str(rt[field])
    titel = rt.get("artikel") or rt.get("titel") or rt.get("opschrift")
    return f"Artikel: {titel}" if titel else ""


def _tokens(question: str) -> set[str]:
    return {t for t in re.findall(r"[a-zà-ÿ0-9]+", question.lower()) if len(t) >= 3}


def rank_regelteksten(
    rows: list[dict],
    question: str,
    skos_weights: dict[str, float] | None = None,
    min_score: float = MIN_RELEVANCE_SCORE,
) -> list[dict]:
    """Rangschik `_wat_geldt_hier`-rijen op relevantie; filter onder de drempel.

    `skos_weights` (term -> woordsoort × relevantie): gewogen-SKOS-bijdrage
    (×3 per gematchte term) i.p.v. platte telling — spiegelt de bot met
    BOT_USE_WEIGHTED_SKOS aan. Substring-match = bot-mechaniek.
    """
    qtokens = _tokens(question)
    location_kw = qtokens & LOCATION_WORDS
    topic_kw = qtokens - LOCATION_WORDS

    scored: list[tuple[float, dict]] = []
    for rt in rows:
        score = 0.0
        tekst = _tekst(rt).lower()

        if (rt.get("inhoud") or rt.get("tekst") or rt.get("documentTekst") or rt.get("content")) and len(tekst) > 50:
            score += 3.0

        if tekst:
            if skos_weights:
                score += sum(w * 3.0 for term, w in skos_weights.items() if term in tekst)
            else:
                score += sum(1 for kw in topic_kw if kw in tekst) * 3.0
            score += sum(1 for kw in location_kw if kw in tekst) * 1.0

        # Source-priority (zoals bot `_rank_by_relevance`): annotaties/structuur >
        # Presenteren > overig. Alleen bot-rijen dragen `bron`; _wat_geldt_hier-rijen
        # niet → dan geen bonus (ongewijzigd viewer-gedrag).
        bron = rt.get("bron") or ""
        if "annotaties" in bron or "documentstructuur" in bron:
            score += 2.0
        elif "Presenteren" in bron:
            score += 1.0

        # Bestuurslaag-bonus: lokaal > waterschap > provincie > rijk. Veld-tolerant:
        # _wat_geldt_hier-rijen dragen regeling/documenttype; bot-rijen document_titel
        # /opgesteldDoor — beide meenemen zodat de rank in beide contexten klopt.
        btext = (f"{rt.get('regeling') or ''} {rt.get('document_titel') or ''} "
                 f"{rt.get('documenttype') or ''} {rt.get('opgesteldDoor') or ''}").lower()
        if "gemeente" in btext or "omgevingsplan" in btext:
            score += 4.0
        elif "waterschap" in btext or "hoogheemraadschap" in btext:
            score += 3.0
        elif "provincie" in btext or "omgevingsverordening" in btext:
            score += 2.0

        titel = (rt.get("artikel") or rt.get("titel") or "").lower()
        if titel.startswith("begrip") or "begrippen" in titel:
            continue  # definities beantwoorden nooit de vraag

        if any(t in titel for t in _OVERVIEW_TERMS):
            score += 5.0

        fts = rt.get("fts_rank")
        if fts and isinstance(fts, (int, float)) and fts > 0:
            score += min(fts * 200, 8.0)

        rt["_relevance_score"] = round(score, 1)
        scored.append((score, rt))

    scored.sort(key=lambda x: x[0], reverse=True)
    kept = [rt for s, rt in scored if s >= min_score]
    # Fallback: als de drempel alles wegknipt, geef de top terug (bot-gedrag).
    return kept if kept else [rt for _, rt in scored]
