"""Tests voor de gebiedsmarkering-parser van het KOOP-vergunningenregister.

Draaien zonder pytest:  python tests/test_koop_gebiedsmarkering.py

Achtergrond (2026-09-20): de parser accepteerde alleen Adres/Punt/Vlak/
Postcodegebied als kind van <ow:gebiedsmarkering> en liet elk ander
locatietype stil vallen — 39.045 records zonder geometrie terwijl de WKT
gewoon in de opgeslagen raw_xml zat. Deze tests spijkeren twee dingen vast:

  1. elk kind mét <ow:geometrie> levert een kandidaat, ongeacht zijn tag;
  2. de nieuwe locatietypen verdringen nooit een klassieke kandidaat.

De XML-fragmenten zijn woordelijk ontleend aan echte records
(gmb-2024-132098 De Fryske Marren, gmb-2025-158299 Apeldoorn).
"""
import os
import sys
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.loaders.koop_vergunning import (  # noqa: E402
    NS,
    parse_gebiedsmarkering,
    wkt_centroid,
)

OW = "http://standaarden.overheid.nl/wetgeving/"


def _gm(inner: str) -> list:
    xml = f'<root xmlns:ow="{OW}">{inner}</root>'
    return ET.fromstring(xml).findall(".//ow:gebiedsmarkering", NS)


GEOMETRIEREF = """
<ow:gebiedsmarkering scheme="OVERHEIDop.locatietype"><ow:GeometrieRef>
  <ow:geometrie>MULTIPOLYGON (((169730.701 544982.266,169734.391 544976.767,
    169792.69 544985.606,169730.701 544982.266)))</ow:geometrie>
  <ow:locatiegebied>POLYGON((5.6060847 52.891768,5.6061393 52.891718,
    5.6070059 52.891796,5.6060847 52.891768))</ow:locatiegebied>
</ow:GeometrieRef></ow:gebiedsmarkering>
"""

PERCEEL = """
<ow:gebiedsmarkering scheme="OVERHEIDop.locatietype"><ow:Perceel>
  <ow:geometrie>POLYGON ((197070.81 472786.72, 197019.655 472778.339,
    197041.842 472721.734, 197070.81 472786.72))</ow:geometrie>
  <ow:geometrielabel>Perceel Apeldoorn (APD04) sectie: AF nummer: 5812</ow:geometrielabel>
</ow:Perceel></ow:gebiedsmarkering>
"""

WEG = """
<ow:gebiedsmarkering scheme="OVERHEIDop.locatietype"><ow:Weg>
  <ow:geometrie>MULTILINESTRING((119594 539725,119627 539742),
    (117899 541627,117927 541601))</ow:geometrie>
</ow:Weg></ow:gebiedsmarkering>
"""

ADRES = """
<ow:gebiedsmarkering scheme="OVERHEIDop.locatietype"><ow:Adres>
  <ow:geometrie>POINT (150000 450000)</ow:geometrie>
  <ow:straatnaam>Heusdensebaan</ow:straatnaam>
  <ow:huisnummer>14</ow:huisnummer>
  <ow:woonplaats>Oisterwijk</ow:woonplaats>
</ow:Adres></ow:gebiedsmarkering>
"""

ZONDER_GEOMETRIE = """
<ow:gebiedsmarkering scheme="OVERHEIDop.locatietype"><ow:Onbekend>
  <ow:noemer>iets zonder geometrie</ow:noemer>
</ow:Onbekend></ow:gebiedsmarkering>
"""


def test_geometrieref_levert_geometrie():
    """Het vergunningsgebied van een BOPA — vóór de fix viel dit stil."""
    geom = parse_gebiedsmarkering(_gm(GEOMETRIEREF))
    assert geom["geometrie_type"] == "GeometrieRef"
    assert geom["rd_wkt"].startswith("MULTIPOLYGON")
    assert geom["rd_x"] is not None and geom["rd_y"] is not None
    assert 0 < geom["rd_x"] < 300000 and 300000 < geom["rd_y"] < 620000
    assert geom["lat"] is not None and geom["lon"] is not None


def test_perceel_met_kadastraal_label():
    geom = parse_gebiedsmarkering(_gm(PERCEEL))
    assert geom["geometrie_type"] == "Perceel"
    assert "sectie: AF nummer: 5812" in geom["geometrielabel"]


def test_weg_levert_lijngeometrie():
    geom = parse_gebiedsmarkering(_gm(WEG))
    assert geom["geometrie_type"] == "Weg"
    assert geom["rd_wkt"].startswith("MULTILINESTRING")
    assert geom["rd_x"] is not None


def test_kind_zonder_geometrie_telt_niet_mee():
    geom = parse_gebiedsmarkering(_gm(ZONDER_GEOMETRIE))
    assert geom["geometrie_type"] is None
    assert geom["rd_wkt"] is None


def test_klassiek_type_houdt_voorrang():
    """Een nieuw locatietype mag een bestaande adrespin nooit verdringen.

    Zonder deze regel verschoof 0,47% van de ~884k bestaande pins, tot 8 km,
    puur omdat de documentvolgorde een ander eerste blok opleverde.
    """
    geom = parse_gebiedsmarkering(_gm(WEG + ADRES))
    assert geom["geometrie_type"] == "Adres"
    assert geom["straatnaam"] == "Heusdensebaan"

    geom_omgekeerd = parse_gebiedsmarkering(_gm(ADRES + WEG))
    assert geom_omgekeerd["geometrie_type"] == "Adres"


def test_wkt_centroid_laat_polygon_ongemoeid():
    """POLYGON blijft via polygon_centroid lopen — identieke coördinaat."""
    from src.loaders.koop_vergunning import polygon_centroid
    wkt = "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))"
    assert wkt_centroid(wkt) == polygon_centroid(wkt) == (5.0, 5.0)


def test_wkt_centroid_multi_en_lijn():
    assert wkt_centroid("LINESTRING (100 200, 300 400)") == (200.0, 300.0)
    c = wkt_centroid("MULTIPOINT ((0 0), (10 10))")
    assert c == (5.0, 5.0)


if __name__ == "__main__":
    for naam, fn in sorted(globals().items()):
        if naam.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {naam}")
    print("alle tests geslaagd")
