"""Tests voor GET /v1/tiles/{laag}/{z}/{x}/{y}.mvt.

De piramide-rekenkunde wordt puur getest (geen DB nodig); de endpoint-tests
draaien tegen de echte database, want een MVT-tegel is precies de plek waar een
mock niets bewijst — de vraag is juist of PostGIS de goede bytes teruggeeft.

Run: pytest test_tiles.py -v
"""

import pytest
import tiles as mod
from db import pool
from fastapi.testclient import TestClient
from main import app

pool.open()
client = TestClient(app)

# Utrecht-centrum, het meetpunt uit docs/plans/vector-tiles.md.
UTR_X, UTR_Y = 136827, 455914


def _tegel_van(x: float, y: float, z: int) -> tuple[int, int]:
    """Tegelindex waarin een RD-punt valt — het omgekeerde van mod.envelope."""
    breedte = mod.RES_Z0 / 2**z * mod.TEGEL_PX
    return int((x - mod.RD_MINX) // breedte), int((mod.RD_MAXY - y) // breedte)


# ── De piramide ────────────────────────────────────────────────────────

def test_z0_is_precies_de_rd_extent():
    """Op z0 beslaat één tegel de hele RD-extent. Klopt dit niet, dan sluiten
    alle tegels scheef aan op de PDOK-achtergrondkaart."""
    minx, miny, maxx, maxy = mod.envelope(0, 0, 0)
    assert (minx, maxy) == (mod.RD_MINX, mod.RD_MAXY)
    assert maxx == pytest.approx(595401.92)
    assert miny == pytest.approx(22598.08)


def test_y_telt_van_boven_naar_beneden():
    """OpenLayers gebruikt sinds v6 tegelcoordinaten met de oorsprong
    linksboven. Tegel y=1 ligt dus ONDER y=0."""
    _, _, _, maxy_boven = mod.envelope(1, 0, 0)
    _, _, _, maxy_onder = mod.envelope(1, 0, 1)
    assert maxy_boven > maxy_onder


def test_tegels_sluiten_naadloos_aan():
    _, _, maxx, _ = mod.envelope(8, 100, 100)
    minx_rechts, _, _, _ = mod.envelope(8, 101, 100)
    assert maxx == pytest.approx(minx_rechts)


def test_punt_valt_binnen_zijn_eigen_tegel():
    for z in (6, 8, 10, 12):
        x, y = _tegel_van(UTR_X, UTR_Y, z)
        minx, miny, maxx, maxy = mod.envelope(z, x, y)
        assert minx <= UTR_X < maxx
        assert miny <= UTR_Y < maxy


def test_niveaukeuze_volgt_de_zoom():
    assert [mod._niveau_voor(z) for z in range(0, 14)] == [
        6, 6, 6, 6, 6, 6, 6,      # z0-z6
        8, 8,                      # z7-z8
        10, 10,                    # z9-z10
        None, None, None,          # z11+ rechtstreeks uit de bron
    ]


# ── Het endpoint ───────────────────────────────────────────────────────

def test_onbekende_laag_geeft_404():
    assert client.get("/v1/tiles/verzonnen/12/2000/1500.mvt").status_code == 404


def test_tegel_buiten_de_piramide_geeft_404():
    # Op z2 zijn er 4 tegels per as (0..3).
    assert client.get("/v1/tiles/locaties/2/4/0.mvt").status_code == 404


def test_zoom_boven_het_maximum_wordt_geweigerd():
    assert client.get("/v1/tiles/locaties/15/0/0.mvt").status_code == 422


def test_tegel_met_data_is_een_mvt():
    x, y = _tegel_van(UTR_X, UTR_Y, 12)
    r = client.get(f"/v1/tiles/locaties/12/{x}/{y}.mvt")
    assert r.status_code == 200
    assert r.headers["content-type"] == mod.MEDIATYPE
    assert len(r.content) > 0
    # Laagnaam staat als string in de protobuf — goedkope inhoudscontrole
    # zonder een MVT-parser als afhankelijkheid.
    assert b"locaties" in r.content


def test_lage_zoom_komt_uit_de_generalisatietabel():
    x, y = _tegel_van(UTR_X, UTR_Y, 8)
    r = client.get(f"/v1/tiles/locaties/8/{x}/{y}.mvt")
    assert r.status_code == 200
    assert r.headers["X-Tegel-Bron"] == "generalisatie-n8"


def test_hoge_zoom_komt_uit_de_brontabel():
    x, y = _tegel_van(UTR_X, UTR_Y, 12)
    r = client.get(f"/v1/tiles/locaties/12/{x}/{y}.mvt")
    assert r.headers["X-Tegel-Bron"] == "subdiv"


def test_wro_laag_levert_planobjecten():
    x, y = _tegel_van(UTR_X, UTR_Y, 12)
    r = client.get(f"/v1/tiles/planobjecten/12/{x}/{y}.mvt")
    assert r.status_code == 200
    assert r.headers["X-Tegel-Bron"] == "subdiv"   # z11+ = rechtstreeks uit wro.planobject
    assert b"planobjecten" in r.content


def test_wro_laag_gebruikt_ook_de_generalisatie():
    x, y = _tegel_van(UTR_X, UTR_Y, 8)
    r = client.get(f"/v1/tiles/planobjecten/8/{x}/{y}.mvt")
    assert r.status_code == 200
    assert r.headers["X-Tegel-Bron"] == "generalisatie-n8"


def test_beide_lagen_zijn_los_van_elkaar():
    """Ow en Wro delen de piramide maar niet hun inhoud — een tegel van de ene
    laag mag nooit features van de andere bevatten."""
    x, y = _tegel_van(UTR_X, UTR_Y, 12)
    ow = client.get(f"/v1/tiles/locaties/12/{x}/{y}.mvt").content
    wro = client.get(f"/v1/tiles/planobjecten/12/{x}/{y}.mvt").content
    assert b"locaties" in ow and b"planobjecten" not in ow
    assert b"planobjecten" in wro and b"locaties" not in wro


def test_leeg_gebied_geeft_een_lege_tegel_geen_404():
    """Noordzee ver ten noordwesten van de kust. Voor een tegelbron is 'hier is
    niets' een geldig antwoord — een 404 zou OpenLayers als laadfout tonen.

    Let op bij het kiezen van zo'n punt: de Noordzee is niet leeg. Op RD
    20000/600000 liggen elf vlakken (rijksregelingen reiken tot ver op zee)."""
    x, y = _tegel_van(-200000, 800000, 12)
    r = client.get(f"/v1/tiles/locaties/12/{x}/{y}.mvt")
    assert r.status_code == 200
    assert r.content == b""


def test_etag_geeft_304_bij_herhaald_verzoek():
    x, y = _tegel_van(UTR_X, UTR_Y, 12)
    eerste = client.get(f"/v1/tiles/locaties/12/{x}/{y}.mvt")
    etag = eerste.headers["ETag"]
    tweede = client.get(
        f"/v1/tiles/locaties/12/{x}/{y}.mvt", headers={"If-None-Match": etag}
    )
    assert tweede.status_code == 304
    assert tweede.content == b""


def test_etag_verschilt_per_tegel():
    a = client.get("/v1/tiles/locaties/12/2000/1500.mvt").headers["ETag"]
    b = client.get("/v1/tiles/locaties/12/2001/1500.mvt").headers["ETag"]
    assert a != b
