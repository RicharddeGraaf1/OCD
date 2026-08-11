"""Tests voor GET /v1/leefomgeving/readout.

De extern-adapter query't PostGIS; voor deterministische endpoint-logica mocken
we de adapter. Eén smoke-test draait tegen de echte `lev.rev_risicobron`-tabel
(via de ingest geladen). De DB-pool moet open zijn (adapter opent een conn).

Run: pytest test_leefomgeving.py -v
"""

import leefomgeving as mod
from db import pool
from fastapi.testclient import TestClient
from main import app

pool.open()
client = TestClient(app)

# Locatie in de Rotterdamse haven — daar staan REV-risicobronnen (zie ingest-sanity).
RDAM_X, RDAM_Y = 90500, 435500


def _reset():
    """Wis de module-cache + per-key locks zodat tests elkaar niet beïnvloeden."""
    mod._cache.clear()
    mod._key_locks.clear()


def test_happy_path_extern(monkeypatch):
    _reset()
    monkeypatch.setitem(
        mod.ADAPTERS, "extern",
        lambda x, y: mod.Readout(value="Ja", unit="risicobron nabij",
                                 ctx="Geregistreerde risicobron op 85 m", status="stop"),
    )
    monkeypatch.setattr(mod, "_BRONNEN", {"extern"})
    r = client.get("/v1/leefomgeving/readout", params={"x": 1, "y": 2, "lagen": "extern"})
    assert r.status_code == 200
    body = r.json()
    assert body["locatie"] == {"x": 1, "y": 2}
    assert body["readouts"]["extern"]["value"] == "Ja"
    assert body["readouts"]["extern"]["status"] == "stop"


def test_uitgeschakeld_thema_geeft_null(monkeypatch):
    _reset()
    monkeypatch.setattr(mod, "_BRONNEN", {"extern"})  # lucht staat uit
    r = client.get("/v1/leefomgeving/readout", params={"x": 1, "y": 2, "lagen": "lucht"})
    assert r.status_code == 200
    assert r.json()["readouts"]["lucht"] is None


def test_fail_soft(monkeypatch):
    _reset()

    def _boom(x, y):
        raise RuntimeError("upstream down")

    monkeypatch.setitem(mod.ADAPTERS, "extern", _boom)
    monkeypatch.setattr(mod, "_BRONNEN", {"extern"})
    r = client.get("/v1/leefomgeving/readout", params={"x": 1, "y": 2, "lagen": "extern"})
    assert r.status_code == 200
    # Adapter-fout degradeert alleen dít thema tot null, geen 500.
    assert r.json()["readouts"]["extern"] is None


def test_single_flight_cache(monkeypatch):
    _reset()
    calls = {"n": 0}

    def _counting(x, y):
        calls["n"] += 1
        return mod.Readout(value="Nee", unit="risicobron nabij", ctx="", status="ok")

    monkeypatch.setitem(mod.ADAPTERS, "extern", _counting)
    monkeypatch.setattr(mod, "_BRONNEN", {"extern"})
    # Twee identieke requests (zelfde 100m-cel) → adapter maar één keer aangeroepen.
    client.get("/v1/leefomgeving/readout", params={"x": 1000, "y": 2000, "lagen": "extern"})
    client.get("/v1/leefomgeving/readout", params={"x": 1010, "y": 2010, "lagen": "extern"})
    assert calls["n"] == 1


def test_master_toggle_503(monkeypatch):
    _reset()
    monkeypatch.setattr(mod, "_ENABLED", False)
    r = client.get("/v1/leefomgeving/readout", params={"x": 1, "y": 2, "lagen": "extern"})
    assert r.status_code == 503


def test_lucht_jaargemiddelde_warn(monkeypatch):
    _reset()
    # GCN-jaargemiddelde: PM2.5 = 11 (> WHO 5, < EU 25 → warn), NO2 = 18 (warn).
    waarden = {f"conc_PM25_{mod._GCN_JAAR}": 11.0, f"conc_NO2_{mod._GCN_JAAR}": 18.0}
    monkeypatch.setattr(mod, "_wms_gfi", lambda base, layer, x, y, prop=None: waarden.get(layer))
    monkeypatch.setattr(mod, "_BRONNEN", {"lucht"})
    r = client.get("/v1/leefomgeving/readout", params={"x": 121000, "y": 487000, "lagen": "lucht"})
    assert r.status_code == 200
    ro = r.json()["readouts"]["lucht"]
    assert ro["value"] == "11"
    assert ro["unit"] == "µg/m³ PM2.5"
    assert ro["status"] == "warn"
    assert "NO₂ 18" in ro["ctx"]
    assert "Jaargemiddelde" in ro["ctx"]


def test_lucht_strengste_band_wint(monkeypatch):
    _reset()
    # PM2.5 = 8 (warn), maar NO2 = 45 (> EU 40 → stop) ⇒ status = stop.
    waarden = {f"conc_PM25_{mod._GCN_JAAR}": 8.0, f"conc_NO2_{mod._GCN_JAAR}": 45.0}
    monkeypatch.setattr(mod, "_wms_gfi", lambda base, layer, x, y, prop=None: waarden.get(layer))
    monkeypatch.setattr(mod, "_BRONNEN", {"lucht"})
    r = client.get("/v1/leefomgeving/readout", params={"x": 121000, "y": 487000, "lagen": "lucht"})
    ro = r.json()["readouts"]["lucht"]
    assert ro["status"] == "stop"


def test_lucht_schone_lucht_ok(monkeypatch):
    _reset()
    # Beide onder de WHO-advieswaarde → ok.
    waarden = {f"conc_PM25_{mod._GCN_JAAR}": 4.0, f"conc_NO2_{mod._GCN_JAAR}": 8.0}
    monkeypatch.setattr(mod, "_wms_gfi", lambda base, layer, x, y, prop=None: waarden.get(layer))
    monkeypatch.setattr(mod, "_BRONNEN", {"lucht"})
    r = client.get("/v1/leefomgeving/readout", params={"x": 121000, "y": 487000, "lagen": "lucht"})
    assert r.json()["readouts"]["lucht"]["status"] == "ok"


def test_gridwaarde_nodata_sentinel():
    # RIVM-grids geven -999 buiten het modelgebied (zee) → geen waarde.
    assert mod._gridwaarde(-999) is None
    assert mod._gridwaarde(None) is None
    assert mod._gridwaarde(10.4) == 10.4
    assert mod._gridwaarde("0") == 0.0


def test_lucht_zee_geeft_null(monkeypatch):
    _reset()
    # Beide stoffen nodata (-999 → None via _gridwaarde) ⇒ adapter geeft None.
    monkeypatch.setattr(mod, "_wms_gfi", lambda base, layer, x, y, prop=None: None)
    monkeypatch.setattr(mod, "_BRONNEN", {"lucht"})
    r = client.get("/v1/leefomgeving/readout", params={"x": 10000, "y": 600000, "lagen": "lucht"})
    assert r.json()["readouts"]["lucht"] is None


def test_lucht_geen_data_geeft_null(monkeypatch):
    _reset()
    monkeypatch.setattr(mod, "_wms_gfi", lambda base, layer, x, y, prop=None: None)
    monkeypatch.setattr(mod, "_BRONNEN", {"lucht"})
    r = client.get("/v1/leefomgeving/readout", params={"x": 1, "y": 2, "lagen": "lucht"})
    assert r.status_code == 200
    assert r.json()["readouts"]["lucht"] is None


def _mock_geluid(monkeypatch, totaal, weg):
    waarden = {mod._GELUID_TOTAAL_LAAG: totaal, mod._GELUID_WEG_LAAG: weg}
    monkeypatch.setattr(mod, "_wms_gfi", lambda base, layer, x, y, prop=None: waarden.get(layer))
    monkeypatch.setattr(mod, "_BRONNEN", {"geluid"})


def test_geluid_warn_wegverkeer(monkeypatch):
    _reset()
    # Lden 58 (> warn 55, <= stop 65 → warn); wegverkeer ~gelijk → dominant.
    _mock_geluid(monkeypatch, 58.0, 58.0)
    r = client.get("/v1/leefomgeving/readout", params={"x": 92000, "y": 437500, "lagen": "geluid"})
    assert r.status_code == 200
    ro = r.json()["readouts"]["geluid"]
    assert ro["value"] == "58"
    assert ro["unit"] == "dB Lden"
    assert ro["status"] == "warn"
    assert "wegverkeer dominant" in ro["ctx"]


def test_geluid_stop_hoge_belasting(monkeypatch):
    _reset()
    _mock_geluid(monkeypatch, 68.0, 40.0)  # 68 > stop 65
    r = client.get("/v1/leefomgeving/readout", params={"x": 92000, "y": 437500, "lagen": "geluid"})
    ro = r.json()["readouts"]["geluid"]
    assert ro["status"] == "stop"
    assert "meerdere bronnen" in ro["ctx"]  # weg 40 << totaal 68


def test_geluid_stil_ok(monkeypatch):
    _reset()
    _mock_geluid(monkeypatch, 0.0, 0.0)  # stil gebied = 0 dB → ok (geen None!)
    r = client.get("/v1/leefomgeving/readout", params={"x": 145000, "y": 605000, "lagen": "geluid"})
    ro = r.json()["readouts"]["geluid"]
    assert ro is not None
    assert ro["status"] == "ok"
    assert ro["value"] == "0"


def test_geluid_zee_geeft_null(monkeypatch):
    _reset()
    _mock_geluid(monkeypatch, None, None)  # geen feature → None
    r = client.get("/v1/leefomgeving/readout", params={"x": 10000, "y": 600000, "lagen": "geluid"})
    assert r.json()["readouts"]["geluid"] is None


def _mock_klimaat(monkeypatch, gray_index):
    monkeypatch.setattr(
        mod, "_wms_gfi",
        lambda base, layer, x, y, prop=None: (None if gray_index is None else float(gray_index)),
    )
    monkeypatch.setattr(mod, "_BRONNEN", {"klimaat"})


def test_klimaat_overstroomt_niet_ok(monkeypatch):
    _reset()
    _mock_klimaat(monkeypatch, 1)  # hoge zandgrond → overstroomt niet
    r = client.get("/v1/leefomgeving/readout", params={"x": 148463, "y": 466786, "lagen": "klimaat"})
    ro = r.json()["readouts"]["klimaat"]
    assert ro["value"] == "Overstroomt niet"
    assert ro["status"] == "ok"


def test_klimaat_uiterwaard_stop(monkeypatch):
    _reset()
    _mock_klimaat(monkeypatch, 4)  # 1x per 100 jaar (uiterwaard) → stop
    r = client.get("/v1/leefomgeving/readout", params={"x": 187500, "y": 432000, "lagen": "klimaat"})
    ro = r.json()["readouts"]["klimaat"]
    assert "100 jaar" in ro["value"]
    assert ro["status"] == "stop"


def test_klimaat_oppervlaktewater(monkeypatch):
    _reset()
    _mock_klimaat(monkeypatch, 6)  # open water
    r = client.get("/v1/leefomgeving/readout", params={"x": 155000, "y": 520000, "lagen": "klimaat"})
    ro = r.json()["readouts"]["klimaat"]
    assert ro["value"] == "Oppervlaktewater"
    assert "oppervlaktewater" in ro["ctx"].lower()
    assert ro["status"] == "ok"


def test_klimaat_buiten_modelgebied_null(monkeypatch):
    _reset()
    _mock_klimaat(monkeypatch, None)  # geen feature (zee/buitenland)
    r = client.get("/v1/leefomgeving/readout", params={"x": 10000, "y": 600000, "lagen": "klimaat"})
    assert r.json()["readouts"]["klimaat"] is None


class _FakeCur:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCur(self._row)


def _mock_natuur(monkeypatch, row):
    """Vervang get_conn zodat de nearest-query een vaste rij (of None) teruggeeft."""
    monkeypatch.setattr(mod, "get_conn", lambda: _FakeConn(row))
    monkeypatch.setattr(mod, "_BRONNEN", {"natuur"})


def test_natuur_in_gebied_stop(monkeypatch):
    _reset()
    _mock_natuur(monkeypatch, {"naam": "Veluwe", "dist": 0})
    r = client.get("/v1/leefomgeving/readout", params={"x": 185000, "y": 455000, "lagen": "natuur"})
    ro = r.json()["readouts"]["natuur"]
    assert ro["value"] == "In gebied"
    assert ro["status"] == "stop"
    assert "Veluwe" in ro["ctx"]


def test_natuur_nabij_warn(monkeypatch):
    _reset()
    _mock_natuur(monkeypatch, {"naam": "Coepelduynen", "dist": 500})
    r = client.get("/v1/leefomgeving/readout", params={"x": 90000, "y": 470000, "lagen": "natuur"})
    ro = r.json()["readouts"]["natuur"]
    assert ro["status"] == "warn"
    assert "500 m" in ro["ctx"]


def test_natuur_ver_ok(monkeypatch):
    _reset()
    _mock_natuur(monkeypatch, {"naam": "Veluwe", "dist": 2000})  # >1 km binnen zoekstraal
    r = client.get("/v1/leefomgeving/readout", params={"x": 180000, "y": 455000, "lagen": "natuur"})
    ro = r.json()["readouts"]["natuur"]
    assert ro["status"] == "ok"
    assert "2000 m" in ro["ctx"]


def test_natuur_geen_binnen_straal_ok(monkeypatch):
    _reset()
    _mock_natuur(monkeypatch, None)  # niets binnen de zoekstraal
    r = client.get("/v1/leefomgeving/readout", params={"x": 121000, "y": 487000, "lagen": "natuur"})
    ro = r.json()["readouts"]["natuur"]
    assert ro["value"] == "Nee"
    assert ro["status"] == "ok"
    assert "Geen Natura 2000" in ro["ctx"]


def test_natuur_adapter_echte_db():
    """Smoke: de echte adapter tegen lev.natura2000. Een punt midden op de Veluwe
    ligt ín een Natura 2000-gebied (afstand 0 → stop)."""
    _reset()
    r = client.get("/v1/leefomgeving/readout", params={"x": 185000, "y": 455000, "lagen": "natuur"})
    assert r.status_code == 200
    ro = r.json()["readouts"]["natuur"]
    assert ro is not None
    assert "Natura 2000" in ro["ctx"]
    assert ro["status"] in ("warn", "stop")


def _mock_cultuur(monkeypatch, row):
    monkeypatch.setattr(mod, "get_conn", lambda: _FakeConn(row))
    monkeypatch.setattr(mod, "_BRONNEN", {"cultuur"})


def test_cultuur_op_terrein_stop(monkeypatch):
    _reset()
    _mock_cultuur(monkeypatch, {"registernr": "10013", "soort": "vlak", "dist": 0})
    r = client.get("/v1/leefomgeving/readout", params={"x": 121300, "y": 487400, "lagen": "cultuur"})
    ro = r.json()["readouts"]["cultuur"]
    assert ro["value"] == "In gebied"
    assert ro["status"] == "stop"
    assert "10013" in ro["ctx"]


def test_cultuur_nabij_warn(monkeypatch):
    _reset()
    _mock_cultuur(monkeypatch, {"registernr": "10001", "soort": "punt", "dist": 30})
    r = client.get("/v1/leefomgeving/readout", params={"x": 121300, "y": 487400, "lagen": "cultuur"})
    ro = r.json()["readouts"]["cultuur"]
    assert ro["status"] == "warn"
    assert "30 m" in ro["ctx"]


def test_cultuur_ver_ok(monkeypatch):
    _reset()
    _mock_cultuur(monkeypatch, {"registernr": "10001", "soort": "punt", "dist": 300})  # >warn, binnen straal
    r = client.get("/v1/leefomgeving/readout", params={"x": 120000, "y": 486000, "lagen": "cultuur"})
    ro = r.json()["readouts"]["cultuur"]
    assert ro["status"] == "ok"
    assert "300 m" in ro["ctx"]


def test_cultuur_geen_binnen_straal_ok(monkeypatch):
    _reset()
    _mock_cultuur(monkeypatch, None)
    r = client.get("/v1/leefomgeving/readout", params={"x": 150000, "y": 500000, "lagen": "cultuur"})
    ro = r.json()["readouts"]["cultuur"]
    assert ro["value"] == "Nee"
    assert ro["status"] == "ok"
    assert "Geen rijksmonument" in ro["ctx"]


def test_cultuur_adapter_echte_db():
    """Smoke: de echte adapter tegen lev.rijksmonument op de Amsterdamse Herengracht
    (vol rijksmonumenten) → nabij/op-terrein (warn/stop)."""
    _reset()
    r = client.get("/v1/leefomgeving/readout", params={"x": 121300, "y": 487400, "lagen": "cultuur"})
    assert r.status_code == 200
    ro = r.json()["readouts"]["cultuur"]
    assert ro is not None
    assert ro["status"] in ("warn", "stop")


def test_extern_adapter_echte_db():
    """Smoke: de echte adapter tegen lev.rev_risicobron in de Rotterdamse haven
    geeft een risicobron nabij (warn/stop), niet 'Nee'."""
    _reset()
    r = client.get("/v1/leefomgeving/readout", params={"x": RDAM_X, "y": RDAM_Y, "lagen": "extern"})
    assert r.status_code == 200
    ro = r.json()["readouts"]["extern"]
    assert ro is not None
    assert ro["status"] in ("warn", "stop")
    assert "risicobron op" in ro["ctx"].lower()
