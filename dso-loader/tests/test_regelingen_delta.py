"""Regressietests voor de p2p-delta-sweep (`find_regelingen_delta`).

Achtergrond: de sweep vroeg `_sort=-registratietijdstip` en brak af bij het
eerste item ouder dan `sinds`. De DSO-lijst is niet strikt gesorteerd — op
2026-08-01 stond op positie 2 van pagina 1 een registratie uit 2024 tussen twee
van eind juli. Gevolg: de sweep stopte na één item en miste 16 regelingen,
terwijl de sync "0 fouten" rapporteerde (vault gaps.md G-98).

De pagina-fixture hieronder heeft exact die vorm.
"""

import pytest

from src.loaders import api_loader


def _reg(code: str, ts: str, work: str, expr: str = "", titel: str = ""):
    return {
        "identificatie": work,
        "expressionId": expr or f"{work}/nld@{ts[:10]}",
        "officieleTitel": titel or f"Regeling {code}",
        "type": {"waarde": "Omgevingsplan"},
        "aangeleverdDoorEen": {"code": code, "naam": f"gemeente {code}"},
        "geregistreerdMet": {"tijdstipRegistratie": ts},
    }


# Let op de volgorde: item 2 is jaren ouder dan item 1 en 3 — precies de
# echte respons die de oude implementatie liet afbreken.
PAGINA_1 = [
    _reg("gm0779", "2026-07-30T07:51:05Z", "/akn/nl/act/gm0779/2026/A"),
    _reg("gm0984", "2024-08-07T11:16:26Z", "/akn/nl/act/gm0984/2024/B"),
    _reg("gm1963", "2026-07-29T07:19:04Z", "/akn/nl/act/gm1963/2026/C"),
    _reg("gm1681", "2026-07-28T07:39:45Z", "/akn/nl/act/gm1681/2026/D"),
]
PAGINA_2 = [
    _reg("gm0202", "2026-06-29T07:37:49Z", "/akn/nl/act/gm0202/2020/E"),
    _reg("gm0363", "2021-12-14T06:31:43Z", "/akn/nl/act/gm0363/2021/F"),
]


@pytest.fixture
def dso(monkeypatch):
    """Vervang de HTTP-laag door twee pagina's met een `next`-link."""
    calls = []

    def nep_get(url, params=None, **kw):
        params = params or {}
        calls.append(params)
        page = params.get("page", 1)
        regs = {1: PAGINA_1, 2: PAGINA_2}.get(page, [])
        links = {"next": {"href": "…"}} if page < 2 else {}
        return {"_embedded": {"regelingen": regs}, "_links": links}

    monkeypatch.setattr(api_loader, "_get", nep_get)
    return calls


def _codes(resultaat):
    return {r["bronhouder_code"] for r in resultaat}


def test_ongesorteerde_lijst_breekt_de_sweep_niet_af(dso):
    """De oude implementatie stopte bij gm0984 (2024) en gaf alleen gm0779."""
    uit = api_loader.find_regelingen_delta("2026-07-01T00:00:00Z")
    assert _codes(uit) == {"gm0779", "gm1963", "gm1681"}, (
        "een oudere registratie tussen nieuwere in mag de sweep niet afbreken")


def test_pagineert_door_na_een_oude_registratie(dso):
    """Ook pagina 2 moet gelezen worden, niet alleen pagina 1."""
    uit = api_loader.find_regelingen_delta("2026-01-01T00:00:00Z")
    assert "gm0202" in _codes(uit)


def test_sinds_filtert_oudere_registraties_weg(dso):
    uit = api_loader.find_regelingen_delta("2026-07-29T00:00:00Z")
    assert _codes(uit) == {"gm0779", "gm1963"}


def test_zonder_sinds_komt_alles_terug(dso):
    uit = api_loader.find_regelingen_delta(None)
    assert len(uit) == len(PAGINA_1) + len(PAGINA_2)


def test_scope_filter_op_bronhouder(dso):
    uit = api_loader.find_regelingen_delta(None, bronhouder_codes={"gm0779", "gm0202"})
    assert _codes(uit) == {"gm0779", "gm0202"}


def test_nieuwste_expressie_wint_bij_meerdere_versies(dso, monkeypatch):
    """Twee expressies van hetzelfde work → de nieuwst geregistreerde."""
    work = "/akn/nl/act/gm0999/2026/X"
    pagina = [
        _reg("gm0999", "2026-05-01T00:00:00Z", work, expr=f"{work}/nld@2026-05-01"),
        _reg("gm0999", "2026-07-01T00:00:00Z", work, expr=f"{work}/nld@2026-07-01"),
    ]
    monkeypatch.setattr(api_loader, "_get",
                        lambda url, params=None, **kw: {
                            "_embedded": {"regelingen": pagina if (params or {}).get("page", 1) == 1 else []},
                            "_links": {}})
    uit = api_loader.find_regelingen_delta(None)
    assert len(uit) == 1
    assert uit[0]["expressionId"].endswith("2026-07-01")


def test_vraagt_geen_sortering_meer_aan(dso):
    """`_sort` is bewust weg: de volgorde is niet betrouwbaar en een instabiele
    sortering kan over paginagrenzen items dubbelen of overslaan."""
    api_loader.find_regelingen_delta(None)
    assert all("_sort" not in params for params in dso)
