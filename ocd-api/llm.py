"""Minimale LLM-laag voor antwoord-generatie.

Geport uit omgevingsbot.nl (`backend/services/llm_service.py`): de
`SYSTEM_PROMPT`, `generate_answer` en de confidence-parsing. Hierdoor delen de
OCD-viewer en omgevingsbot straks één antwoord-pipeline (zie
`OCDviewer/docs/plans/20260603-ocd-gedeeld-brein.md`).

Provider via env, **feature-flagged** zodat de rest van OCD zonder LLM blijft
werken:
    OCD_LLM_PROVIDER   ollama (default) | anthropic | none
    OCD_LLM_MODEL      modelnaam (default per provider)
    OCD_LLM_BASE_URL   Ollama-URL (default: OCD_EXPAND_URL of localhost:11434)
    OCD_LLM_API_KEY    sleutel voor anthropic
    OCD_LLM_TIMEOUT    seconden (default 60)

`provider=none` → `llm.available is False`; het antwoord-endpoint geeft dan 503
en de viewer valt terug op zijn eigen samenvatting.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata

import httpx

logger = logging.getLogger("ocd_api.llm")

_PROVIDER = os.getenv("OCD_LLM_PROVIDER", "ollama").lower()
_OLLAMA_URL = os.getenv(
    "OCD_LLM_BASE_URL", os.getenv("OCD_EXPAND_URL", "http://localhost:11434")
).rstrip("/")
_API_KEY = os.getenv("OCD_LLM_API_KEY", "")
_TIMEOUT = float(os.getenv("OCD_LLM_TIMEOUT", "60"))

_DEFAULT_MODELS = {
    "ollama": "qwen2.5:14b",
    "anthropic": "claude-sonnet-4-6",
}
_MODEL = os.getenv("OCD_LLM_MODEL") or _DEFAULT_MODELS.get(_PROVIDER, "qwen2.5:14b")


SYSTEM_PROMPT = (
    "Je bent de Omgevingsbot, een assistent die vragen beantwoordt over de Nederlandse "
    "omgevingsregelgeving (Omgevingswet). Je baseert je antwoorden op de meegeleverde "
    "DSO-documentteksten.\n\n"
    "Regels:\n"
    "- Antwoord in het Nederlands, beknopt en helder.\n"
    "- Citeer de relevante regeltekst zo letterlijk mogelijk uit de context. "
    "Geef daarna een korte uitleg wat dit betekent voor de vraag van de gebruiker.\n"
    "- Verwijs naar specifieke artikelen of regelingen als die in de context staan.\n"
    "- Als de context voldoende informatie bevat, geef een concreet antwoord.\n"
    "- Als de context gedeeltelijk relevant is, geef aan wat je wél kunt zeggen "
    "en wat ontbreekt.\n"
    "- Gebruik NOOIT informatie die niet in de context staat.\n"
    "- Gebruik de exacte bewoordingen uit de regelgeving (bijv. 'verboden', "
    "'vergunningplicht') in plaats van eigen parafrases.\n\n"
    "Sluit je antwoord af met een regel:\n"
    "ZEKERHEID: [HOOG|MIDDEN|LAAG]\n"
    "- HOOG: de context bevat directe, duidelijke informatie over de vraag\n"
    "- MIDDEN: de context bevat gerelateerde informatie maar niet alles\n"
    "- LAAG: de context bevat weinig tot geen relevante informatie"
)


def parse_confidence(raw_answer: str) -> tuple[str, str]:
    """Splits een LLM-antwoord in (antwoordtekst, confidence).

    Geport uit llm_service.py: normaliseert Unicode-rommel (non-breaking spaces,
    full-width dubbele punt) en knipt de `ZEKERHEID: HOOG|MIDDEN|LAAG`-footer eraf.
    Default LAAG als er geen footer staat.
    """
    normalized = unicodedata.normalize("NFKC", raw_answer)
    normalized = re.sub(r"[^\S\n]", " ", normalized)

    confidence = "LAAG"
    answer_text = raw_answer
    match = re.search(
        r"[Zz][Ee][Kk][Ee][Rr][Hh][Ee][Ii][Dd]\s*:\s*(HOOG|MIDDEN|LAAG|hoog|midden|laag)",
        normalized,
    )
    if match:
        confidence = match.group(1).upper()
        answer_text = raw_answer[: match.start()].strip()
    else:
        fallback = re.search(r"\b(HOOG|MIDDEN|LAAG)\s*$", normalized)
        if fallback:
            confidence = fallback.group(1)
            answer_text = raw_answer[: fallback.start()].strip()
    return answer_text, confidence


class LLMService:
    """Provider-agnostische antwoord-generator. Eén module-singleton (`llm`)."""

    def __init__(self) -> None:
        self.provider = _PROVIDER
        self.model = _MODEL
        self.available = self.provider in ("ollama", "anthropic")

        if self.provider == "anthropic":
            try:
                import anthropic  # noqa: F401
            except ImportError:
                logger.warning("OCD_LLM_PROVIDER=anthropic maar de anthropic-SDK ontbreekt.")
                self.available = False
            if not _API_KEY:
                logger.warning("OCD_LLM_PROVIDER=anthropic maar OCD_LLM_API_KEY is leeg.")
                self.available = False

        logger.info(
            "llm init provider=%s model=%s available=%s", self.provider, self.model, self.available
        )

    def generate_answer(
        self,
        question: str,
        location: str,
        dso_context: str,
        model_override: str | None = None,
    ) -> dict[str, str]:
        """Genereer een antwoord, geaard in `dso_context`. Returnt
        {'answer', 'confidence'}. Raise RuntimeError als de LLM niet beschikbaar is."""
        if not self.available:
            raise RuntimeError("LLM is niet beschikbaar (OCD_LLM_PROVIDER=none of misconfig).")

        user_prompt = (
            f"Locatie: {location}\n"
            f"Vraag: {question}\n\n"
            f"Relevante DSO-documenttekst — DIT is je bron:\n---\n{dso_context[:6000]}\n---\n\n"
            "Beantwoord de vraag op basis van de DSO-documenttekst. "
            "Verwijs naar specifieke artikelen indien mogelijk."
        )
        model = model_override or self.model
        raw = self._chat(SYSTEM_PROMPT, user_prompt, model)
        answer, confidence = parse_confidence(raw)
        return {"answer": answer, "confidence": confidence}

    # ── Providers ──
    def _chat(self, system: str, user: str, model: str) -> str:
        if self.provider == "ollama":
            return self._chat_ollama(system, user, model)
        if self.provider == "anthropic":
            return self._chat_anthropic(system, user, model)
        raise RuntimeError(f"Onbekende LLM-provider: {self.provider}")

    def _chat_ollama(self, system: str, user: str, model: str) -> str:
        resp = httpx.post(
            f"{_OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"temperature": 0.0},
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return (resp.json().get("message") or {}).get("content", "").strip()

    def _chat_anthropic(self, system: str, user: str, model: str) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=_API_KEY)
        msg = client.messages.create(
            model=model,
            max_tokens=1024,
            temperature=0.0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in msg.content if block.type == "text").strip()


llm = LLMService()
