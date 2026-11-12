"""Tests voor de transitie-export (transitie.py) en de tegels van indeling H (tegels_h.py).

De regels (bronhouder, TAM, tegels) worden puur getest. De endpoint-tests draaien tegen de echte
database, want de vraag is juist of de selectie op de echte voorraad klopt. Ze gebruiken Nunspeet
(gm0302) en Haarlem (gm0392), de proefgemeenten van Locus Legis.

Run: pytest test_transitie.py -v
"""
import importlib.util
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import tegels_h
import transitie
from db import pool

BENCHMARK = os.path.join(os.path.dirname(__file__), "..", "..", "omgevingsdocumentenregister.nl",
                         "tests", "rom-benchmark", "run_benchmark.py")


# ── regels zonder database ───────────────────────────────────────────────────────────────────────

def test_echte_bronhouder_van_inpassingsplan():
    assert transitie._echte_bronhouder("NL.IMRO.9928.DOSx2010x0009772IP-VA02", "gm9928") == "pv28"
    assert transitie._echte_bronhouder("NL.IMRO.0000.ipxyz-VA01", "gm0000") == "rijk"
    assert transitie._echte_bronhouder("NL.IMRO.0302.BP01101-vg01", "gm0302") == "gm0302"


def test_tam_op_naam_en_identificatie():
    assert transitie.RE_TAM_NAAM.match("TAM - omgevingsplan Schotkampweg 179")
    assert not transitie.RE_TAM_NAAM.match("Tamboerijnstraat")
    assert transitie.RE_TAM.search("NL.IMRO.0302.TAM0022n-vg01")


def test_structuur_hangt_aan_de_dichtstbijzijnde_structuur():
    rijen = [
        {"id": 1, "parent_id": None, "element_type": "Lichaam", "nummer": None, "opschrift": None, "wid": "x__body", "volgorde": 1},
        {"id": 3, "parent_id": 1, "element_type": "Hoofdstuk", "nummer": "2", "opschrift": "Twee", "wid": "x__chp_2", "volgorde": 2},
        {"id": 2, "parent_id": 1, "element_type": "Hoofdstuk", "nummer": "1", "opschrift": "Een", "wid": "x__chp_1", "volgorde": 1},
        {"id": 4, "parent_id": 2, "element_type": "Afdeling", "nummer": "1.1", "opschrift": "A", "wid": "x__chp_1__subchp_1.1", "volgorde": 1},
        {"id": 5, "parent_id": 4, "element_type": "Artikel", "nummer": "1.1", "opschrift": None, "wid": "x__art_1.1", "volgorde": 1},
        {"id": 6, "parent_id": 2, "element_type": "Artikel", "nummer": "1.2", "opschrift": None, "wid": "x__art_1.2", "volgorde": 2},
    ]
    uit = transitie._structuur(rijen)
    assert [o["wId"] for o in uit] == ["x__chp_1", "x__chp_1__subchp_1.1", "x__chp_2"]
    assert [o["parent"] for o in uit] == [None, "x__chp_1", None]
    assert [o["diepte"] for o in uit] == [0, 1, 0]
    assert [o["aantalArtikelen"] for o in uit] == [2, 1, 0]


def test_plangrens():
    assert transitie._plangrens(None) == "pdok"
    assert transitie._plangrens("ihr-plangrens") == "ihr"
    assert transitie._plangrens("ambtsgebied-imro2006") == "ambtsgebied"


def test_tegels_wro_via_bestemmingsomschrijving():
    tegels, herkomst = tegels_h.tegels_e(
        pad={"categorie": "wonen", "sub": "wonen"}, opschrift="3.1 Bestemmingsomschrijving",
        inhoud="De voor Wonen aangewezen gronden zijn bestemd voor wonen en een bedrijf aan huis; "
               "ondergeschikt: parkeervoorzieningen")
    assert tegels == ["d-bouwen", "d-werken"]
    assert herkomst == "pad"


def test_procedures_naar_kleine_tegels():
    assert tegels_h.tegel_d("procedures", "instructieregels") == "overheid"
    assert tegels_h.tegel_d("procedures", "aanvraagvereisten") == "aanvragen"


@pytest.mark.skipif(not os.path.exists(BENCHMARK), reason="register-repo niet naast deze repo")
def test_gelijk_aan_de_benchmark():
    """tegels_h moet dezelfde tegels geven als tegels_e() in de RoM-benchmark."""
    spec = importlib.util.spec_from_file_location("rom_benchmark", BENCHMARK)
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)
    gevallen = [
        dict(pad={"categorie": "erfgoed", "sub": "cultureel erfgoed"}, imow=["erfgoed"], ga=[], act=[],
             opschrift="Artikel 18 Waarde - Archeologie", inhoud=None),
        dict(pad=None, imow=["milieu", "geluid"], ga=[{"type": "geluid", "naam": "geluidzone - industrie"}],
             act=[{"groep": "milieubelastende activiteit", "naam": "Bodemenergiesystemen aanleggen"}],
             opschrift=None, inhoud=None),
        dict(pad={"categorie": "procedures", "sub": "instructieregels"}, imow=[], ga=[], act=[],
             opschrift="3.1 Bestemmingsomschrijving", inhoud="bestemd voor agrarische bedrijven en wonen"),
        dict(pad=None, imow=[], ga=[], act=[], opschrift=None, inhoud=None),
    ]
    for g in gevallen:
        verwacht, _, _ = bm.tegels_e({"pad": g["pad"], "imow": g["imow"], "ann": {"ga": g["ga"], "act": g["act"]},
                                      "opschrift": g["opschrift"], "inhoud": g["inhoud"]})
        uit, _ = tegels_h.tegels_e(pad=g["pad"], imow=g["imow"], gebiedsaanwijzingen=g["ga"],
                                   activiteiten=g["act"], opschrift=g["opschrift"], inhoud=g["inhoud"])
        assert (uit or ["niet-ingedeeld"]) == verwacht, g


# ── endpoints tegen de database ──────────────────────────────────────────────────────────────────

def _client(tier: str) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def zet_tier(request: Request, call_next):
        request.state.tier = tier
        return await call_next(request)

    app.include_router(transitie.router)
    pool.open()
    return TestClient(app)


def test_publieke_sleutel_wordt_geweigerd():
    assert _client("public").get("/v1/transitie/gm0302/voorraad").status_code == 403


def test_ongeldige_gemeentecode():
    assert _client("private").get("/v1/transitie/0302/voorraad").status_code == 400


@pytest.mark.parametrize("gm", ["gm0302", "gm0392"])
def test_voorraad_contract(gm):
    d = _client("private").get(f"/v1/transitie/{gm}/voorraad").json()
    assert d["contractversie"] == transitie.CONTRACTVERSIE
    assert d["plannen"] and d["artikelen"]
    idns = {p["idn"] for p in d["plannen"]}
    assert all(a["idn"] in idns for a in d["artikelen"]), "artikel zonder plan"
    sleutels = [(a["idn"], a["tekstobjectId"]) for a in d["artikelen"]]
    assert len(sleutels) == len(set(sleutels)), "dubbele artikelsleutel"
    assert not any(".toelichting" in a["tekstobjectId"].lower() for a in d["artikelen"])
    assert not any(transitie.RE_TAM_NAAM.match(p["naam"] or "") for p in d["plannen"])
    assert all(p["geldendM2"] <= p["oppervlakteM2"] + 1 for p in d["plannen"])
    for p in d["plannen"]:
        assert ("tekst ontbreekt" in {s["soort"] for s in p["signalen"]}) == (not p["tekstenVolledig"])


def test_haarlem_artikel10_zonder_tekst_en_deels_geponst():
    d = _client("private").get("/v1/transitie/gm0392/voorraad").json()
    art10 = [p for p in d["plannen"] if p["typePlan"].startswith("gemeentelijk plan")]
    assert art10 and all(not p["tekstenVolledig"] for p in art10)
    assert any(p["geldendM2"] < p["oppervlakteM2"] for p in d["plannen"]), "Haarlem heeft een pons"
    assert d["bron"]["ponsIdentificaties"]


def test_geometrie_exact_vraagt_idn():
    c = _client("private")
    assert c.get("/v1/transitie/gm0302/geometrie?precisie=exact").status_code == 400
    r = c.get("/v1/transitie/gm0302/geometrie?precisie=exact&idn=NL.IMRO.0302.BP01101-vg01")
    assert r.status_code == 200 and len(r.json()["features"]) == 1


# ── pons-geschiedenis ────────────────────────────────────────────────────────────────────────────

def test_ponshistorie_publieke_sleutel_wordt_geweigerd():
    assert _client("public").get("/v1/transitie/gm0392/ponshistorie").status_code == 403


def test_ponshistorie_contract_haarlem():
    d = _client("private").get("/v1/transitie/gm0392/ponshistorie").json()
    assert d["contractversie"] == transitie.CONTRACTVERSIE and d["gemeentecode"] == "gm0392"
    assert d["gepeild"] is True and d["gepeildOp"]
    data = [s["geldigVanaf"] for s in d["stappen"]]
    assert data == sorted(data) and data[0] == "2024-01-01"
    assert d["stappen"][0]["geometrie"] is None, "Haarlem had op 1-1-2024 nog geen pons"
    laatste = d["stappen"][-1]
    assert laatste["geometrieIdentificatie"] and laatste["geometrie"]["type"] in ("Polygon", "MultiPolygon")


def test_ponshistorie_nunspeet_zonder_pons():
    d = _client("private").get("/v1/transitie/gm0302/ponshistorie").json()
    assert d["gepeild"] is True
    assert all(s["geometrie"] is None for s in d["stappen"])


def test_ponshistorie_niet_gepeilde_gemeente():
    d = _client("private").get("/v1/transitie/gm9999/ponshistorie").json()
    assert d["gepeild"] is False and d["stappen"] == [] and d["gepeildOp"] is None


# ── structuur van het omgevingsplan ──────────────────────────────────────────────────────────────

def test_omgevingsplan_publieke_sleutel_wordt_geweigerd():
    assert _client("public").get("/v1/transitie/gm0392/omgevingsplan").status_code == 403


@pytest.mark.parametrize("gm", ["gm0302", "gm0392"])
def test_omgevingsplan_structuur(gm):
    d = _client("private").get(f"/v1/transitie/{gm}/omgevingsplan").json()
    assert d["contractversie"] == transitie.CONTRACTVERSIE and d["expressie"].startswith(f"/akn/nl/act/{gm}/")
    onderdelen = d["onderdelen"]
    wids = [o["wId"] for o in onderdelen]
    assert len(wids) == len(set(wids)), "dubbele wId"
    gezien = set()
    for o in onderdelen:
        assert o["parent"] is None or o["parent"] in gezien, "ouder na kind"
        gezien.add(o["wId"])
    assert any("__chp_22" in w for w in wids), "hoofdstuk 22 (bruidsschat) ontbreekt"
    assert all(o["type"] in transitie.STRUCTUUR for o in onderdelen)


def test_omgevingsplan_onbekende_gemeente():
    assert _client("private").get("/v1/transitie/gm9999/omgevingsplan").status_code == 404
