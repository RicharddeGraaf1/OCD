"""Intent-classificatie voor de retrieval-kernel (convergentie bot ↔ viewer).

Faithful port van de detectoren in de omgevingsbot
(`omgevingsbot.nl/backend/services/chat_service.py`: `_detect_norm_question`,
`_detect_activiteit_question`, `_detect_bestemming_question`). Hier centraal in de
OCD-API zodat bot én viewer dezelfde intent-signalen krijgen — zie vault
`analysis/Plan refactor gedeelde retrieval-laag bot en viewer`.

LET OP — drift-bewaking: deze patronen zijn een kopie. Bij wijziging in de bot
moeten ze hier mee. Bedoeling van R2/R3 is dat de bot deze module gaat consumeren
i.p.v. zijn eigen kopie, zodat er nog maar één bron is.
"""
from __future__ import annotations

import re

# Norm-vraag patroon-classifier. Per patroon de zoekterm voor /v1/normwaarde?naam=.
# Volgorde van meest specifiek naar meest generiek; eerste match wint.
_NORM_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\boverstroming(?:s)?(?:kans)?\b", re.IGNORECASE), "overstroming"),
    (re.compile(r"\bwateroverlast\b", re.IGNORECASE), "wateroverlast"),
    (re.compile(r"\bbebouwingspercentage\b", re.IGNORECASE), "bebouwingspercentage"),
    (re.compile(r"\b(?:max\w*\s+)?bouwhoogte\b", re.IGNORECASE), "bouwhoogte"),
    (re.compile(r"\b(?:max\w*\s+)?bouwdiepte\b", re.IGNORECASE), "bouwdiepte"),
    (re.compile(r"\b(?:vloer\w*opp\w*|opp\w*\s+vloer)\b", re.IGNORECASE), "vloeroppervlak"),
    (re.compile(r"\bhoe\s+diep\b", re.IGNORECASE), "diepte"),
    (re.compile(r"\bgoothoogte\b", re.IGNORECASE), "goothoogte"),
    (re.compile(r"\bnokhoogte\b", re.IGNORECASE), "nokhoogte"),
]

_ACTIVITEIT_QUESTION_PATTERN = re.compile(
    r"\bmag\s+ik\s+hier\s+(?:een|de|het|mijn)?\s*"
    r"(?P<soort>[a-zA-Zëïü\-]{3,30})"
    r"(?:\s+(?:beginnen|bouwen|plaatsen|aanleggen|ontwikkelen|maken|aanvangen|starten|verhuren|openen))?"
    r"\b",
    re.IGNORECASE,
)
_ACTIVITEIT_STOPWORDS = {
    "boten", "vrij", "vrouw", "iets", "alles", "wat", "iemand",
}

_BESTEMMING_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bmag\s+ik\s+hier\b", re.IGNORECASE),
    re.compile(r"\bkan\s+ik\s+hier\b", re.IGNORECASE),
    re.compile(r"\bwat\s+(?:is|geldt)\s+(?:de|als)?\s*bestemming\b", re.IGNORECASE),
    re.compile(r"\bwelke\s+bestemming\b", re.IGNORECASE),
    re.compile(r"\bmag\s+(?:ik|mijn)\s+(?:hier|vrouw|huis|woning|pand)\b", re.IGNORECASE),
]


def detect_norm(question: str) -> str | None:
    for pattern, naam in _NORM_PATTERNS:
        if pattern.search(question):
            return naam
    return None


def detect_activiteit(question: str) -> str | None:
    m = _ACTIVITEIT_QUESTION_PATTERN.search(question)
    if not m:
        return None
    soort = (m.group("soort") or "").strip().lower()
    if not soort or len(soort) < 3 or soort in _ACTIVITEIT_STOPWORDS:
        return None
    return soort


def detect_bestemming(question: str) -> bool:
    return any(p.search(question) for p in _BESTEMMING_PATTERNS)


def detect_intent(question: str) -> dict:
    """Classificeer de vraag.

    Precedentie: norm > activiteit > bestemming > algemeen. Norm is het meest
    specifiek (kwantitatief). De activiteit- en bestemming-patronen overlappen op
    "mag ik hier"; activiteit wint als er een concrete soort uit te halen valt.

    Returnt: {intent, norm_naam, soort}.
    """
    norm_naam = detect_norm(question)
    if norm_naam:
        return {"intent": "norm", "norm_naam": norm_naam, "soort": None}
    soort = detect_activiteit(question)
    if soort:
        return {"intent": "activiteit", "norm_naam": None, "soort": soort}
    if detect_bestemming(question):
        return {"intent": "bestemming", "norm_naam": None, "soort": None}
    return {"intent": "algemeen", "norm_naam": None, "soort": None}
