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


def test_ontbrekende_generalisatietabel_geeft_503_met_uitleg(monkeypatch):
    """Het endpoint kan gedeployed zijn zonder dat de tabellen op die database
    gebouwd zijn — dat was op 2026-08-09 de situatie op productie. Dan hoort de
    aanroeper te horen wát er ontbreekt, niet een psycopg-stacktrace."""
    monkeypatch.setitem(
        mod.LAGEN["locaties"], "generalisatie", "p2p.bestaat_niet_generalisatie"
    )
    mod._BESTAAT.discard("p2p.bestaat_niet_generalisatie")

    x, y = _tegel_van(UTR_X, UTR_Y, 8)
    r = client.get(f"/v1/tiles/locaties/8/{x}/{y}.mvt")
    assert r.status_code == 503
    assert "p2p.bestaat_niet_generalisatie" in r.json()["detail"]
    assert "vul_locatie_generalisatie.py" in r.json()["detail"]


def test_zonder_generalisatietabel_werkt_z11_plus_wel(monkeypatch):
    """z11+ leest de brontabel, dus die blijft bruikbaar op een database waar
    de generalisatie nog niet gebouwd is."""
    monkeypatch.setitem(
        mod.LAGEN["locaties"], "generalisatie", "p2p.bestaat_niet_generalisatie"
    )
    x, y = _tegel_van(UTR_X, UTR_Y, 12)
    assert client.get(f"/v1/tiles/locaties/12/{x}/{y}.mvt").status_code == 200


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


# ── Filter op identificatie (?ids=) ────────────────────────────────────
#
# Uitgezoomd tekent de viewer alleen de locaties van de gekozen plek. Een
# ongefilterde tegel draagt dan honderdduizenden vlakken voor een handvol dat
# getekend wordt; met het filter komt alleen de selectie mee.

def test_ids_parser_sorteert_en_ontdubbelt():
    assert mod._ids_uit(None) is None
    assert mod._ids_uit("") == []
    assert mod._ids_uit("b, a,,b") == ["a", "b"]


def test_ids_parser_weigert_te_veel_en_te_lang():
    with pytest.raises(mod.HTTPException) as e:
        mod._ids_uit(",".join(f"id{i}" for i in range(mod.MAX_IDS + 1)))
    assert e.value.status_code == 400
    with pytest.raises(mod.HTTPException) as e:
        mod._ids_uit("x" * (mod.MAX_ID_LENGTE + 1))
    assert e.value.status_code == 400


def _ids_in_tegel(laag_tabel: str, niveau: int | None, z: int, n: int,
                  binnen: bool = False) -> list[str]:
    """n identificaties die in de tegel rond Utrecht op zoom z liggen.

    `binnen`: alleen objecten die helemaal in de tegel vallen -- nodig voor de
    randtests, want de omtrek van een object dat de hele tegel bedekt (een
    ambtsgebied) loopt er niet doorheen.

    Geen DISTINCT/ORDER BY: daarmee leest de query elke rij in de envelope, en
    met een koude lokale cache liep dat tegen de statement-timeout. Een ruime
    LIMIT en ontdubbelen in Python is genoeg."""
    x, y = _tegel_van(UTR_X, UTR_Y, z)
    minx, miny, maxx, maxy = mod.envelope(z, x, y)
    rel = "@" if binnen else "&&"
    niveau_filter = "TRUE" if niveau is None else f"niveau = {int(niveau)}"
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT identificatie FROM {laag_tabel} "  # noqa: S608 — vaste tabelnaam
            f"WHERE {niveau_filter} AND geometrie {rel} ST_MakeEnvelope(%s,%s,%s,%s,28992) "
            "LIMIT %s",
            (minx, miny, maxx, maxy, n * 20),
        )
        ids = [r["identificatie"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
    return list(dict.fromkeys(ids))[:n]


def test_filter_levert_alleen_de_selectie():
    """De identificatie staat als tekst in de MVT, dus dit is zonder decoder te
    toetsen: de gekozen ids zitten erin, een id buiten de selectie niet."""
    alle = _ids_in_tegel("p2p.locatie_generalisatie", 8, 8, 50, binnen=True)
    gekozen = alle[:3]
    # Een id die ook geen deel is van een gekozen id: de toets zoekt op bytes,
    # en `…BepGebHoofdspoorweg` zit letterlijk in `…BepGebHoofdspoorwegKernZ`.
    buiten = next(i for i in alle if all(i not in g and g not in i for g in gekozen))
    x, y = _tegel_van(UTR_X, UTR_Y, 8)

    r = client.get(f"/v1/tiles/locaties/8/{x}/{y}.mvt", params={"ids": ",".join(gekozen)})
    assert r.status_code == 200
    assert r.headers["X-Tegel-Filter"] == "3"
    for i in gekozen:
        assert i.encode() in r.content
    assert buiten.encode() not in r.content

    ongefilterd = client.get(f"/v1/tiles/locaties/8/{x}/{y}.mvt")
    assert buiten.encode() in ongefilterd.content
    assert len(r.content) < len(ongefilterd.content)


def test_filter_werkt_op_lage_zoom_zonder_de_hele_tegel_te_lezen():
    """z3 rond Utrecht: ongefilterd ruim 300.000 vlakken en minuten rekenwerk.
    Gefilterd moet dit binnen de gewone statement-timeout blijven, en dat lukt
    alleen als de selectie vóór de envelope komt (MATERIALIZED in _sql)."""
    gekozen = _ids_in_tegel("p2p.locatie_generalisatie", 6, 6, 5)
    x, y = _tegel_van(UTR_X, UTR_Y, 3)
    r = client.get(f"/v1/tiles/locaties/3/{x}/{y}.mvt", params={"ids": ",".join(gekozen)})
    assert r.status_code == 200
    assert r.headers["X-Tegel-Bron"] == "generalisatie-n6"
    assert len(r.content) > 0
    assert all(i.encode() in r.content for i in gekozen)


def test_filter_op_niets_geeft_een_lege_tegel():
    x, y = _tegel_van(UTR_X, UTR_Y, 8)
    r = client.get(f"/v1/tiles/locaties/8/{x}/{y}.mvt", params={"ids": ""})
    assert r.status_code == 200
    assert r.content == b""


def test_filter_werkt_ook_op_de_brontabel():
    """Vanaf z11 komt de geometrie uit locatie_subdiv; het filter moet daar net
    zo goed werken."""
    gekozen = _ids_in_tegel("p2p.locatie_generalisatie", 10, 12, 2)
    x, y = _tegel_van(UTR_X, UTR_Y, 12)
    r = client.get(f"/v1/tiles/locaties/12/{x}/{y}.mvt", params={"ids": ",".join(gekozen)})
    assert r.status_code == 200
    assert r.headers["X-Tegel-Bron"] == "subdiv"
    assert all(i.encode() in r.content for i in gekozen)


def test_filter_werkt_op_de_wro_laag():
    gekozen = _ids_in_tegel("wro.planobject_generalisatie", 8, 8, 3)
    x, y = _tegel_van(UTR_X, UTR_Y, 8)
    r = client.get(f"/v1/tiles/planobjecten/8/{x}/{y}.mvt", params={"ids": ",".join(gekozen)})
    assert r.status_code == 200
    assert all(i.encode() in r.content for i in gekozen)


def test_etag_hangt_af_van_de_selectie_niet_van_de_volgorde():
    x, y = _tegel_van(UTR_X, UTR_Y, 8)
    url = f"/v1/tiles/locaties/8/{x}/{y}.mvt"
    zonder = client.get(url).headers["ETag"]
    ab = client.get(url, params={"ids": "a,b"}).headers["ETag"]
    ba = client.get(url, params={"ids": "b,a"}).headers["ETag"]
    ac = client.get(url, params={"ids": "a,c"}).headers["ETag"]
    assert ab == ba
    assert len({zonder, ab, ac}) == 3


def test_selectie_eerst_alleen_uitgezoomd():
    """Uitgezoomd eerst de selectie (anders leest z5 852 MB aan pagina's),
    ingezoomd eerst de envelope (anders leest z11 elk stukje van elk
    provinciaal object: 49 MB tegen 7 MB). Zie SELECTIE_EERST_TOT_ZOOM."""
    laag = mod.LAGEN["locaties"]
    assert "MATERIALIZED" in mod._sql(laag, 6, gefilterd=True, selectie_eerst=True)
    ingezoomd = mod._sql(laag, None, gefilterd=True, selectie_eerst=False)
    assert "MATERIALIZED" not in ingezoomd
    assert "= ANY(%(ids)s)" in ingezoomd
    assert mod.SELECTIE_EERST_TOT_ZOOM == 8


# ── Randen (?rand=1) ───────────────────────────────────────────────────
#
# Een tweede MVT-laag `<laag>_rand` met de echte omtrek: uit p2p.locatie_rand op
# z0-z10, samengevoegd per tegel vanaf z11, en voor Wro de omtrek van de rij
# zelf. De laagnaam staat als tekst in de MVT, dus ook dit is zonder decoder te
# toetsen.

def _get(laag: str, z: int, ids: list[str], **params):
    x, y = _tegel_van(UTR_X, UTR_Y, z)
    return client.get(f"/v1/tiles/{laag}/{z}/{x}/{y}.mvt",
                      params={"ids": ",".join(ids), **params})


def test_rand_uit_de_randtabel_op_lage_zoom():
    gekozen = _ids_in_tegel("p2p.locatie_generalisatie", 8, 8, 5, binnen=True)
    r = _get("locaties", 8, gekozen, rand=1)
    assert r.status_code == 200
    assert r.headers["X-Tegel-Rand"] == "tabel"
    assert b"locaties_rand" in r.content
    assert b"locaties" in r.content.replace(b"locaties_rand", b"")


def test_rand_samengevoegd_vanaf_z11():
    # Objecten waarvan de omtrek door deze tegel loopt -- een object dat de hele
    # tegel bedekt heeft er terecht geen rand.
    gekozen = _ids_in_tegel("p2p.locatie_rand", 10, 12, 5)
    r = _get("locaties", 12, gekozen, rand=1)
    assert r.status_code == 200
    assert r.headers["X-Tegel-Rand"] == "samenvoegen"
    assert b"locaties_rand" in r.content


def test_rand_op_de_wro_laag_is_de_omtrek():
    # z10: in de z12-tegel van Utrecht-centrum (215 m) ligt geen planobject in
    # zijn geheel.
    gekozen = _ids_in_tegel("wro.planobject_generalisatie", 10, 10, 5, binnen=True)
    assert gekozen
    r = _get("planobjecten", 10, gekozen, rand=1)
    assert r.status_code == 200
    assert r.headers["X-Tegel-Rand"] == "omtrek"
    assert b"planobjecten_rand" in r.content


def test_zonder_rand_geen_randlaag():
    gekozen = _ids_in_tegel("p2p.locatie_generalisatie", 8, 8, 5)
    r = _get("locaties", 8, gekozen)
    assert b"locaties_rand" not in r.content
    assert "X-Tegel-Rand" not in r.headers


def test_rand_zonder_filter_wordt_genegeerd():
    """Ongefilterd zou het de omtrek van alles in beeld zijn."""
    x, y = _tegel_van(UTR_X, UTR_Y, 12)
    r = client.get(f"/v1/tiles/locaties/12/{x}/{y}.mvt", params={"rand": 1})
    assert r.status_code == 200
    assert r.headers["X-Tegel-Rand"] == "geen"
    assert b"locaties_rand" not in r.content


def test_ontbrekende_randtabel_geeft_tegel_zonder_rand(monkeypatch):
    """Een fout hier zou de viewer de hele tegel-weg laten opgeven. Dus: de
    vlakken gewoon leveren, en in de header zeggen dat de rand ontbreekt."""
    monkeypatch.setattr(mod, "RAND_TABEL", "p2p.bestaat_niet_rand")
    gekozen = _ids_in_tegel("p2p.locatie_generalisatie", 8, 8, 5)
    r = _get("locaties", 8, gekozen, rand=1)
    assert r.status_code == 200
    assert r.headers["X-Tegel-Rand"] == "ontbreekt"
    assert b"locaties_rand" not in r.content
    assert len(r.content) > 0


def test_etag_verschilt_met_en_zonder_rand():
    gekozen = _ids_in_tegel("p2p.locatie_generalisatie", 8, 8, 3)
    assert _get("locaties", 8, gekozen).headers["ETag"] != \
        _get("locaties", 8, gekozen, rand=1).headers["ETag"]
