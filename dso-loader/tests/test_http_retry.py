"""Tests voor de retry-helper op transiënte DSO-fouten (`src/http_retry.py`).

Aanleiding: op 2026-08-01 brak één losse 503 de BOPA-snapshot af op pagina 62
van 78, en viel één gemeente uit de i2a-fase. Geen van beide was een echte
storing.
"""

import httpx
import pytest

from src import http_retry


@pytest.fixture(autouse=True)
def _geen_echte_wachttijd(monkeypatch):
    monkeypatch.setattr(http_retry.time, "sleep", lambda s: None)


def _http_fout(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.test/x")
    return httpx.HTTPStatusError("boem", request=req,
                                 response=httpx.Response(code, request=req))


def test_slaagt_zonder_retry():
    assert http_retry.met_retry(lambda: "ok") == "ok"


def test_herprobeert_503_en_slaagt_daarna():
    pogingen = {"n": 0}

    def fn():
        pogingen["n"] += 1
        if pogingen["n"] < 3:
            raise _http_fout(503)
        return "ok"

    assert http_retry.met_retry(fn) == "ok"
    assert pogingen["n"] == 3


def test_geeft_op_na_alle_pogingen():
    pogingen = {"n": 0}

    def fn():
        pogingen["n"] += 1
        raise _http_fout(503)

    with pytest.raises(httpx.HTTPStatusError):
        http_retry.met_retry(fn, backoff=(1, 1))
    assert pogingen["n"] == 3, "len(backoff) + 1 pogingen"


def test_4xx_wordt_niet_herprobeerd():
    """Een 404 is een echte fout — meteen doorgooien, niet blijven kloppen."""
    pogingen = {"n": 0}

    def fn():
        pogingen["n"] += 1
        raise _http_fout(404)

    with pytest.raises(httpx.HTTPStatusError):
        http_retry.met_retry(fn)
    assert pogingen["n"] == 1


def test_timeout_is_transient():
    pogingen = {"n": 0}

    def fn():
        pogingen["n"] += 1
        if pogingen["n"] == 1:
            raise httpx.ConnectTimeout("traag")
        return "ok"

    assert http_retry.met_retry(fn) == "ok"


def test_niet_http_fout_gaat_direct_door():
    with pytest.raises(ValueError):
        http_retry.met_retry(lambda: (_ for _ in ()).throw(ValueError("bug")))
