"""Tests voor POST /v1/antwoord-bij-vraag.

De retrieval (match_skos_concepts / killer_query) en de LLM worden gemockt zodat
de endpoint-logica deterministisch getest wordt, los van DB-inhoud en zonder een
draaiende Ollama. De DB-pool moet wel open zijn (het endpoint opent een conn).

Run: pytest test_antwoord_bij_vraag.py -v
"""

import antwoord_bij_vraag as mod
from db import pool
from fastapi.testclient import TestClient
from llm import parse_confidence
from main import app
from regelteksten_bij_vraag import _woordgrens_regex, tekst_fallback_query

pool.open()
client = TestClient(app)

AMS_X, AMS_Y = 121687, 487316


# ── Mock-data ──
def _fake_concepts(cur, question, max_concepts):
    return ([{
        "uri": "u1", "naam": "bouwactiviteit", "scheme_naam": "Activiteiten",
        "matched_terms": ["bouwen"], "werkzaamheid_urn": None,
        "activiteit_imow_id": "act-1", "score": 0.9,
    }], ["bouwen"])


def _fake_regels(cur, matched_rows, x, y, limit):
    return [{
        "activiteit_naam": "bouwactiviteit", "activiteit_id": "act-1",
        "artikel": "Artikel 4.2", "regeling": "Omgevingsplan A",
        "regeling_expression": "/akn/expr-a", "documenttype": "Omgevingsplan",
        "inhoud": "Voor een dakkapel geldt een maximale hoogte van 1,5 meter.",
        "join_pad": "activiteit_uri",
    }]


# ══════════════════════════════════════════════════════════
# Validatie
# ══════════════════════════════════════════════════════════

class TestValidatie:
    def test_geen_locatie_of_xy_geeft_422(self):
        r = client.post("/v1/antwoord-bij-vraag", json={"question": "mag ik bouwen?"})
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════
# Fallback: geen herkende concepten
# ══════════════════════════════════════════════════════════

class TestFallback:
    def test_geen_concepten_geeft_uitleg_zonder_llm(self, monkeypatch):
        monkeypatch.setattr(mod, "match_skos_concepts", lambda cur, q, n: ([], []))
        calls = []
        monkeypatch.setattr(mod.llm, "generate_answer",
                            lambda *a, **k: calls.append(1) or {"answer": "x", "confidence": "HOOG"})

        r = client.post("/v1/antwoord-bij-vraag",
                        json={"question": "xyzzy onzin", "x": AMS_X, "y": AMS_Y})
        assert r.status_code == 200
        data = r.json()
        assert data["confidence"] == "LAAG"
        assert data["bronnen"] == []
        assert "geen specifiek onderwerp" in data["antwoord"].lower()
        assert calls == []  # LLM niet aangeroepen


# ══════════════════════════════════════════════════════════
# LLM niet beschikbaar -> 503
# ══════════════════════════════════════════════════════════

class TestLLMUit:
    def test_503_als_llm_niet_beschikbaar(self, monkeypatch):
        monkeypatch.setattr(mod, "match_skos_concepts", _fake_concepts)
        monkeypatch.setattr(mod, "killer_query", _fake_regels)
        monkeypatch.setattr(mod.llm, "available", False)

        r = client.post("/v1/antwoord-bij-vraag",
                        json={"question": "mag ik een dakkapel bouwen?", "x": AMS_X, "y": AMS_Y})
        assert r.status_code == 503


# ══════════════════════════════════════════════════════════
# Happy path: antwoord met bronnen
# ══════════════════════════════════════════════════════════

class TestAntwoord:
    def test_antwoord_met_bronnen_en_confidence(self, monkeypatch):
        monkeypatch.setattr(mod, "match_skos_concepts", _fake_concepts)
        monkeypatch.setattr(mod, "killer_query", _fake_regels)
        monkeypatch.setattr(mod.llm, "available", True)
        monkeypatch.setattr(
            mod.llm, "generate_answer",
            lambda q, loc, ctx, model_override=None: {
                "answer": "Een dakkapel mag, mits maximaal 1,5 m hoog (Artikel 4.2).",
                "confidence": "HOOG",
            },
        )

        r = client.post("/v1/antwoord-bij-vraag",
                        json={"question": "mag ik een dakkapel bouwen?", "x": AMS_X, "y": AMS_Y})
        assert r.status_code == 200
        data = r.json()
        assert data["confidence"] == "HOOG"
        assert data["antwoord"].startswith("Een dakkapel mag")
        assert len(data["bronnen"]) == 1
        bron = data["bronnen"][0]
        assert bron["artikel"] == "Artikel 4.2"
        assert bron["regeling_expression"] == "/akn/expr-a"
        assert data["matched_concepts"][0]["naam"] == "bouwactiviteit"
        assert data["trace"]["llm_provider"] == mod.llm.provider
        assert data["trace"]["matched_concepts_count"] == 1

    def test_model_override_in_trace(self, monkeypatch):
        monkeypatch.setattr(mod, "match_skos_concepts", _fake_concepts)
        monkeypatch.setattr(mod, "killer_query", _fake_regels)
        monkeypatch.setattr(mod.llm, "available", True)
        monkeypatch.setattr(mod.llm, "generate_answer",
                            lambda *a, **k: {"answer": "ok", "confidence": "MIDDEN"})

        r = client.post("/v1/antwoord-bij-vraag",
                        json={"question": "dakkapel bouwen", "x": AMS_X, "y": AMS_Y,
                              "model": "claude-sonnet-4-6"})
        assert r.status_code == 200
        assert r.json()["trace"]["llm_model"] == "claude-sonnet-4-6"

    def test_geen_regels_geeft_uitleg(self, monkeypatch):
        monkeypatch.setattr(mod, "match_skos_concepts", _fake_concepts)
        monkeypatch.setattr(mod, "killer_query", lambda *a, **k: [])
        monkeypatch.setattr(mod.llm, "available", True)

        r = client.post("/v1/antwoord-bij-vraag",
                        json={"question": "dakkapel bouwen", "x": AMS_X, "y": AMS_Y})
        assert r.status_code == 200
        data = r.json()
        assert data["confidence"] == "LAAG"
        assert data["bronnen"] == []
        assert data["matched_concepts"][0]["naam"] == "bouwactiviteit"

    def test_llm_exceptie_geeft_503(self, monkeypatch):
        monkeypatch.setattr(mod, "match_skos_concepts", _fake_concepts)
        monkeypatch.setattr(mod, "killer_query", _fake_regels)
        monkeypatch.setattr(mod.llm, "available", True)

        def boom(*a, **k):
            raise RuntimeError("ollama down")

        monkeypatch.setattr(mod.llm, "generate_answer", boom)
        r = client.post("/v1/antwoord-bij-vraag",
                        json={"question": "dakkapel bouwen", "x": AMS_X, "y": AMS_Y})
        assert r.status_code == 503


# ══════════════════════════════════════════════════════════
# build_context
# ══════════════════════════════════════════════════════════

class TestBuildContext:
    def test_formatteert_header_en_scheidt_blokken(self):
        rows = [
            {"regeling": "Doc A", "artikel": "Art 1", "activiteit_naam": "bouwen", "inhoud": "tekst 1"},
            {"regeling": "Doc B", "artikel": "Art 2", "activiteit_naam": "", "inhoud": "tekst 2"},
        ]
        ctx = mod.build_context(rows)
        assert "[Doc A] Art 1 — bouwen" in ctx
        assert "tekst 1" in ctx and "tekst 2" in ctx
        assert "\n---\n" in ctx

    def test_capt_op_max_chars(self):
        rows = [
            {"regeling": "D", "artikel": "A", "activiteit_naam": "", "inhoud": "x" * 5000},
            {"regeling": "E", "artikel": "B", "activiteit_naam": "", "inhoud": "y" * 5000},
        ]
        ctx = mod.build_context(rows, max_chars=6000)
        assert "x" * 5000 in ctx
        assert "y" * 5000 not in ctx  # tweede blok valt buiten de cap

    def test_slaat_lege_inhoud_over(self):
        rows = [{"regeling": "D", "artikel": "A", "activiteit_naam": "", "inhoud": "   "}]
        assert mod.build_context(rows) == ""


# ══════════════════════════════════════════════════════════
# parse_confidence (llm.py)
# ══════════════════════════════════════════════════════════

class TestWoordgrensRegex:
    def test_filtert_korte_termen_en_dedupt_en_sorteert(self):
        rx = _woordgrens_regex(["veehouderij", "vee", "Melkbedrijf", "melkbedrijf"])
        # 'vee' (3) valt af; dedupe op lowercase; gesorteerd; woordgrens-prefix
        assert rx == r"\m(melkbedrijf|veehouderij)"

    def test_geen_bruikbare_termen_geeft_none(self):
        assert _woordgrens_regex(["a", "vee", "mest"]) is None
        assert _woordgrens_regex([]) is None

    def test_escapet_regex_metakarakters(self):
        rx = _woordgrens_regex(["a.b(c)"])
        assert rx == r"\m(a\.b\(c\))"


class TestTekstFallback:
    def test_lege_keywords_doen_geen_query(self):
        class Boom:
            def execute(self, *a, **k):
                raise AssertionError("execute had niet aangeroepen mogen worden")
        # Geen bruikbare termen -> early return [] zonder de cursor te raken.
        assert tekst_fallback_query(Boom(), ["a", "vee"], 1.0, 2.0, 20) == []


class TestParseConfidence:
    def test_hoog_footer_wordt_afgeknipt(self):
        ans, conf = parse_confidence("Het antwoord is ja.\nZEKERHEID: HOOG")
        assert conf == "HOOG"
        assert ans == "Het antwoord is ja."

    def test_default_laag_zonder_footer(self):
        ans, conf = parse_confidence("Geen footer hier.")
        assert conf == "LAAG"
        assert ans == "Geen footer hier."

    def test_unicode_rommel_en_lowercase(self):
        # non-breaking space + lowercase 'midden'
        ans, conf = parse_confidence("Antwoord.\nzekerheid : midden")
        assert conf == "MIDDEN"
        assert ans == "Antwoord."
