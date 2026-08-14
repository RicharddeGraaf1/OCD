"""Tests voor de Groq-provider in llm.py.

Puur unit — geen DB, geen draaiende API, geen netwerk. `httpx.post` wordt in de
llm-namespace gemockt.

Let op: `LLMService` leest de module-globals bij `__init__`, en die worden bij
import uit het env gezet. Tests die het env-gedrag zelf toetsen (de basis-URL
per provider) herladen de module daarom expliciet.

Run: pytest test_llm_groq.py -v
"""

import importlib

import pytest

import llm as mod


def _service(monkeypatch, provider="groq", key="gsk_test", model=None):
    """Bouw een LLMService met gepatchte module-globals."""
    monkeypatch.setattr(mod, "_PROVIDER", provider)
    monkeypatch.setattr(mod, "_API_KEY", key)
    svc = mod.LLMService()
    # __init__ leest _PROVIDER, maar het model komt uit de module-constante die
    # bij import is berekend — voor de tests zetten we hem expliciet.
    svc.model = model or mod._DEFAULT_MODELS.get(provider, "qwen2.5:14b")
    return svc


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ══════════════════════════════════════════════════════════
# Beschikbaarheid
# ══════════════════════════════════════════════════════════

class TestBeschikbaarheid:
    def test_groq_met_key_is_beschikbaar(self, monkeypatch):
        assert _service(monkeypatch).available is True

    def test_groq_zonder_key_is_niet_beschikbaar(self, monkeypatch):
        """Zonder sleutel moet het endpoint 503 geven i.p.v. een 401 van Groq."""
        assert _service(monkeypatch, key="").available is False

    def test_standaardmodel_voor_groq(self):
        assert mod._DEFAULT_MODELS["groq"] == "llama-3.3-70b-versatile"


# ══════════════════════════════════════════════════════════
# De call zelf
# ══════════════════════════════════════════════════════════

class TestChatGroq:
    def test_stuurt_bearer_en_openai_vorm(self, monkeypatch):
        svc = _service(monkeypatch)
        gezien = {}

        def _fake_post(url, headers=None, json=None, timeout=None):
            gezien["url"] = url
            gezien["headers"] = headers
            gezien["json"] = json
            return _FakeResponse(
                {"choices": [{"message": {"content": "  Dat mag.  "}}]}
            )

        monkeypatch.setattr(mod.httpx, "post", _fake_post)
        uit = svc._chat_groq("systeem", "gebruiker", "llama-3.3-70b-versatile")

        assert uit == "Dat mag."
        assert gezien["url"].endswith("/chat/completions")
        assert gezien["headers"]["Authorization"] == "Bearer gsk_test"
        assert gezien["json"]["model"] == "llama-3.3-70b-versatile"
        assert gezien["json"]["temperature"] == 0.0
        assert gezien["json"]["stream"] is False
        assert [m["role"] for m in gezien["json"]["messages"]] == ["system", "user"]

    def test_lege_choices_geeft_runtimeerror(self, monkeypatch):
        """Een 200 zonder choices is geen leeg antwoord maar een fout — anders
        toont de viewer een lege samenvatting alsof dat het resultaat is."""
        svc = _service(monkeypatch)
        monkeypatch.setattr(
            mod.httpx, "post",
            lambda *a, **k: _FakeResponse({"choices": []}),
        )
        with pytest.raises(RuntimeError):
            svc._chat_groq("s", "u", "m")

    def test_content_null_geeft_lege_string(self, monkeypatch):
        svc = _service(monkeypatch)
        monkeypatch.setattr(
            mod.httpx, "post",
            lambda *a, **k: _FakeResponse({"choices": [{"message": {"content": None}}]}),
        )
        assert svc._chat_groq("s", "u", "m") == ""

    def test_generate_answer_pelt_de_zekerheid_eraf(self, monkeypatch):
        svc = _service(monkeypatch)
        monkeypatch.setattr(
            mod.httpx, "post",
            lambda *a, **k: _FakeResponse({
                "choices": [{"message": {
                    "content": "Een dakkapel mag 1,5 m hoog zijn.\nZEKERHEID: HOOG",
                }}],
            }),
        )
        uit = svc.generate_answer("mag een dakkapel?", "Utrecht", "artikel 4.2 …")
        assert uit["confidence"] == "HOOG"
        assert "ZEKERHEID" not in uit["answer"]

    def test_niet_beschikbaar_raist_voor_de_call(self, monkeypatch):
        svc = _service(monkeypatch, key="")

        def _boom(*a, **k):
            raise AssertionError("er mag geen HTTP-call gedaan worden")

        monkeypatch.setattr(mod.httpx, "post", _boom)
        with pytest.raises(RuntimeError):
            svc.generate_answer("v", "l", "c")


# ══════════════════════════════════════════════════════════
# Basis-URL per provider
# ══════════════════════════════════════════════════════════

class TestBasisUrl:
    """De valkuil die deze splitsing bestaat: vóór Groq viel `OCD_LLM_BASE_URL`
    terug op de Ollama-ketting (OCD_EXPAND_URL → localhost:11434). Zou Groq die
    ketting delen, dan praatte hij zonder eigen env-var tegen een lokale poort."""

    def _herlaad(self, monkeypatch, **env):
        for k in ("OCD_LLM_BASE_URL", "OCD_EXPAND_URL"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(mod)

    def test_groq_default_is_de_groq_api(self, monkeypatch):
        m = self._herlaad(monkeypatch)
        assert m._GROQ_URL == "https://api.groq.com/openai/v1"

    def test_ollama_ketting_raakt_groq_niet(self, monkeypatch):
        m = self._herlaad(monkeypatch, OCD_EXPAND_URL="http://ollama-embed:11434")
        assert m._OLLAMA_URL == "http://ollama-embed:11434"
        assert m._GROQ_URL == "https://api.groq.com/openai/v1"

    def test_expliciete_base_url_wint_voor_beide(self, monkeypatch):
        m = self._herlaad(monkeypatch, OCD_LLM_BASE_URL="https://proxy.intern/v1")
        assert m._GROQ_URL == "https://proxy.intern/v1"
        assert m._OLLAMA_URL == "https://proxy.intern/v1"

    def test_teardown_herstelt_de_module(self, monkeypatch):
        """Laatste test in de klasse: herlaad zonder env zodat volgende
        testbestanden een schone module zien."""
        m = self._herlaad(monkeypatch)
        assert m._DEFAULT_MODELS["groq"] == "llama-3.3-70b-versatile"
