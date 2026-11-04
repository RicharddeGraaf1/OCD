"""De plangrens uit de IHR (G-177): wat telt als een grens, en wat doet een fout?"""
import json

import httpx
import pytest

from src.loaders import wro_plangrens as wp


def _status(code):
    req = httpx.Request("GET", "https://ihr.test/plannen/x")
    return httpx.HTTPStatusError("fout", request=req, response=httpx.Response(code, request=req))


def test_grens_als_json():
    vlak = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    assert json.loads(wp.grens_als_json({"geometrie": vlak})) == vlak


@pytest.mark.parametrize("plan", [
    None,
    {},
    {"geometrie": None},
    {"geometrie": {"type": "Polygon", "coordinates": []}},
])
def test_geen_grens(plan):
    assert wp.grens_als_json(plan) is None


def test_vraagt_geometrie_in_rd(monkeypatch):
    gezien = {}

    def nep(pad, params=None, headers=None):
        gezien.update(pad=pad, params=params, headers=headers)
        return {"id": "x"}

    monkeypatch.setattr(wp, "_ihr_get", nep)
    assert wp.plan_met_grens("x") == {"id": "x"}
    assert gezien["pad"] == "/plannen/x"
    assert gezien["params"] == {"expand": "geometrie"}
    assert gezien["headers"]["Accept-Crs"] == "epsg:28992"
    assert gezien["headers"]["User-Agent"].startswith("ocd-loader/")


def test_weigert_buiten_het_venster(monkeypatch):
    def geen_database():
        raise AssertionError("mag geen verbinding openen")

    monkeypatch.setattr(wp, "in_voorkeursvenster", lambda: False)
    monkeypatch.setattr(wp, "get_conn", geen_database)
    with pytest.raises(RuntimeError, match="voorkeursvenster"):
        wp.neem_plangrenzen_over()


def test_onbekend_plan_is_geen_fout(monkeypatch):
    def nep(*a, **k):
        raise _status(404)

    monkeypatch.setattr(wp, "_ihr_get", nep)
    assert wp.plan_met_grens("x") is None


def test_opnieuw_bij_te_veel_verzoeken(monkeypatch):
    pogingen = []

    def nep(*a, **k):
        pogingen.append(1)
        if len(pogingen) < 3:
            raise _status(429)
        return {"id": "x"}

    monkeypatch.setattr(wp, "_ihr_get", nep)
    monkeypatch.setattr(wp.time, "sleep", lambda s: None)
    assert wp.plan_met_grens("x") == {"id": "x"}
    assert len(pogingen) == 3


def test_andere_fout_gaat_door(monkeypatch):
    def nep(*a, **k):
        raise _status(403)

    monkeypatch.setattr(wp, "_ihr_get", nep)
    with pytest.raises(httpx.HTTPStatusError):
        wp.plan_met_grens("x")
