"""Query-expansie endpoint voor OCD-API.

Geeft een gebruikersvraag aan een LLM, krijgt N alternatieve formuleringen
terug. Bedoeld om VOOR retrieval (zoek/objecten/locatie) aangeroepen te worden:
de aanroeper voert dan retrieval uit voor elke variant en deduplicaten zelf.

Provider-switch via env:
    OCD_EXPAND_PROVIDER  ollama | anthropic | disabled   (default ollama)
    OCD_EXPAND_MODEL     model-naam                       (provider-default)
    OCD_EXPAND_URL       Ollama base URL                  (http://localhost:11434)
    ANTHROPIC_API_KEY    nodig voor anthropic-provider

Gebruik:
    GET /v1/expand?q=mag+ik+hier+een+schuur+bouwen&n=3
    -> {"original": "...", "variants": ["...", "...", "..."], "model": "..."}
"""

import json
import logging
import os
import re
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

logger = logging.getLogger("ocd_api.expand")

_PROVIDER = os.getenv("OCD_EXPAND_PROVIDER", "ollama").lower()
_DEFAULT_MODEL = {
    "ollama": "qwen2.5:14b",
    "anthropic": "claude-haiku-4-5-20251001",
}
_MODEL = os.getenv("OCD_EXPAND_MODEL", _DEFAULT_MODEL.get(_PROVIDER, "qwen2.5:14b"))
_OLLAMA_URL = os.getenv("OCD_EXPAND_URL", "http://localhost:11434").rstrip("/")
_TIMEOUT = 60.0

router = APIRouter()


def _build_prompt(q: str, n: int) -> str:
    return f"""Je herschrijft Omgevingswet-vragen voor zoekverbetering. Geef {n} alternatieve formuleringen voor dezelfde vraag.

Vraag: {q}

KRITISCHE REGELS:
1. BEHOUD specifieke vakterm: als de vraag een Omgevingswet-vakterm bevat (bouwhoogte, bebouwingspercentage, bouwvlak, omgevingsplan, bestemming, gebiedsaanwijzing, omgevingsnorm, activiteit, dakkapel, dakopbouw, projectbesluit, etc.) — laat die EXACT staan in elke variant. Verzin GEEN synoniemen voor die termen.
2. Varieer in vraagvorm en bijwoorden, niet in vakterm-keuze.
3. Voeg waar zinvol een gerelateerde IMOW-objectsoort toe (omgevingsnorm, activiteit, gebiedsaanwijzing, bestemming) als de vraag dat impliceert.
4. Vermijd kromme uitdrukkingen, jargon dat geen vakterm is, of nonsens.

Voorbeelden voor "Wat is hier de maximale bouwhoogte?":
✓ "Welke maximale bouwhoogte geldt op deze locatie?"
✓ "Wat is de maximale bouwhoogte voor het bouwvlak hier?"
✓ "Welke omgevingsnorm bouwhoogte is van toepassing?"
✗ "Hoogste toegestane bouwlengte" (lengte ≠ hoogte)
✗ "Hoe hoog mag ik bouwen" (vermijdt vakterm)

Voorbeelden voor "Welke regels gelden voor het beginnen van een winkel?":
✓ "Welke activiteit-regels gelden voor het starten van een winkel?"
✓ "Mag ik op deze locatie een winkel beginnen?"
✓ "Welke vergunning is nodig voor een winkelactiviteit?"

Geef ALLEEN een JSON-object:
{{"variants": ["variant 1", "variant 2", "variant 3"]}}

Geen omhulling, alleen JSON."""


def _call_ollama(prompt: str) -> str:
    resp = httpx.post(
        f"{_OLLAMA_URL}/api/chat",
        json={
            "model": _MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_ctx": 2048},
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("message", {}).get("content", "")


def _call_anthropic(prompt: str) -> str:
    try:
        from anthropic import Anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed in OCD-API venv")
    client = Anthropic()
    resp = client.messages.create(
        model=_MODEL,
        temperature=0.0,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text if resp.content else ""


def _extract_variants(text: str, n: int) -> List[str]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    variants = data.get("variants", [])
    if not isinstance(variants, list):
        return []
    cleaned = [str(v).strip() for v in variants if isinstance(v, (str, int, float)) and str(v).strip()]
    return cleaned[:n]


def expand_query(q: str, n: int = 3) -> List[str]:
    """Roep LLM aan, parse variants, return lijst (max n items)."""
    if _PROVIDER == "disabled":
        return []
    prompt = _build_prompt(q, n)
    try:
        if _PROVIDER == "ollama":
            raw = _call_ollama(prompt)
        elif _PROVIDER == "anthropic":
            raw = _call_anthropic(prompt)
        else:
            raise RuntimeError(f"Onbekende OCD_EXPAND_PROVIDER: {_PROVIDER!r}")
    except Exception as e:
        logger.warning(f"expand_query LLM-call faalde: {e}")
        return []
    return _extract_variants(raw, n)


@router.get("/v1/expand")
def expand_endpoint(
    q: str = Query(..., min_length=2, max_length=500, description="Originele vraag"),
    n: int = Query(3, ge=1, le=6, description="Aantal varianten (1-6)"),
):
    """Herschrijf de vraag in n alternatieve formuleringen.

    Geeft de originele vraag terug + lijst varianten. Aanroeper doet zelf
    retrieval voor elke variant en deduplicaten.
    """
    variants = expand_query(q, n=n)
    return {
        "original": q,
        "variants": variants,
        "model": _MODEL if variants else None,
        "provider": _PROVIDER,
    }
