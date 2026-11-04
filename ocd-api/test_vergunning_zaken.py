"""Tests voor het bundelen van kennisgevingen tot zaken (geen DB nodig).

De titels zijn echt, uit vth.vergunningkennisgeving (2026-11-02).

Run: pytest test_vergunning_zaken.py -v
"""

from datetime import date

import pytest

from vergunning_zaken import (
    bepaal_fase,
    bundel_tot_zaken,
    lees_rd_bbox,
    normaliseer_zaaknummer,
    onderwerp_uit_titel,
    onderwerp_van,
    soort_van,
)

VANDAAG = date(2026, 11, 2)


def test_rd_bbox_leest_vier_getallen():
    assert lees_rd_bbox("185000,320000,186000.5,321000", 15e6) == (185000, 320000, 186000.5, 321000)


@pytest.mark.parametrize(
    "tekst",
    ["", "1,2,3", "1,2,3,4,5", "a,b,c,d", "186000,320000,185000,321000", "185000,321000,186000,320000"],
)
def test_rd_bbox_weigert_onzin(tekst):
    with pytest.raises(ValueError):
        lees_rd_bbox(tekst, 15e6)


def test_rd_bbox_weigert_te_groot_beeld():
    # 4 x 4 km = 16 km2.
    with pytest.raises(ValueError, match="15 km2"):
        lees_rd_bbox("185000,320000,189000,324000", 15e6)


@pytest.mark.parametrize(
    "type_besluit, titel, soort",
    [
        ("aanvraag", "Kennisgeving ontvangst aanvraag voor het renoveren van de woning , Broekhem 1", "ontvangst"),
        # De kern: een besluit dat als aanvraag geclassificeerd staat.
        ("aanvraag", "Kennisgeving besluit op aanvraag het renoveren van de woning, Broekhem 1", "besluit"),
        ("aanvraag", "Besluit aangevraagde omgevingsvergunning flora- en fauna-activiteit - Ploegveld", "besluit"),
        ("kennisgeving", "Kennisgeving besluit op het vervangen van het dak, Broekhem 62", "besluit"),
        ("kennisgeving", "Kennisgeving WET BODEMBESCHERMING", None),
        ("overig", "Besluit voor het bouwen van een erker aan de Thorbeckestraat 1", "besluit"),
        ("overig", "Beslissing op bezwaar voor het project nieuwbouw woningen", None),
        ("overig", "Toestemming voor het plaatsen van een dakkapel aan de voorzijde van de gevel", "besluit"),
        ("aanvraag", "Aanvraag om toestemming voor het plaatsen van een dakkapel", "ontvangst"),
        ("aanvraag", "Buiten behandeling laten aanvraag omgevingsvergunning voor Madeliefpad", "buiten_behandeling"),
        ("buiten_behandeling", "Besluit buiten behandelingstelling, Broekhem 77", "buiten_behandeling"),
        ("ingetrokken", "Intrekken aanvraag omgevingsvergunning Lansiersveld 6", "ingetrokken"),
        ("verleend", "Verleende omgevingsvergunning Rhijnvis Feithlaan 7 in Bussum", "besluit"),
        ("verlenging_beslistermijn", "Kennisgeving verlenging beslistermijn, Broekhem 1", "verlenging"),
        ("rectificatie", "Rectificatie besluit omgevingsvergunning", None),
        (None, None, None),
    ],
)
def test_soort_van(type_besluit, titel, soort):
    assert soort_van(type_besluit, titel) == soort


@pytest.mark.parametrize(
    "titel, straat, onderwerp",
    [
        (
            "Kennisgeving besluit op aanvraag het renoveren van de woning (gemeentelijk monument), Broekhem 1, 6301 HD Valkenburg",
            "Broekhem",
            "Renoveren van de woning (gemeentelijk monument)",
        ),
        (
            "Kennisgeving besluit op aanvraag een aanpassing van de constructie , Koningswinkelstraat 2-4-6 , 6301 WJ Valkenburg",
            "Koningswinkelstraat",
            "Aanpassing van de constructie",
        ),
        (
            "Aanvraag omgevingsvergunning voor het uitbreiden van het hoogbouwvrieshuis Schuttersweg 17, 5443PR Haps",
            "Schuttersweg",
            "Uitbreiden van het hoogbouwvrieshuis",
        ),
        (
            "Besluit voor het bouwen van een erker aan de Thorbeckestraat 1, 7021AV Zelhem",
            "Thorbeckestraat",
            "Bouwen van een erker",
        ),
        (
            "Aanvraag Omgevingsvergunning Mariannalaan 7, 7316 DS Apeldoorn, het plaatsen van een schuur na sloop van de bestaande schuur",
            "Mariannalaan",
            "Plaatsen van een schuur na sloop van de bestaande schuur",
        ),
        ("Besluit op aanvraag omgevingsvergunning: kappen van 5 bomen", None, "Kappen van 5 bomen"),
        # Geen herkenbaar onderwerp: liever niets dan een half adres.
        ("Aanvraag omgevingsvergunning, Lage Witsiebaan 90, 5042DB Tilburg", "Lage Witsiebaan", None),
        ("Kennisgeving verlenging beslistermijn, Broekhem 1, 6301 HD Valkenburg", "Broekhem", None),
        (None, None, None),
    ],
)
def test_onderwerp_uit_titel(titel, straat, onderwerp):
    assert onderwerp_uit_titel(titel, straat) == onderwerp


def test_onderwerp_beschrijving_gaat_voor_de_titel():
    # Amsterdam: het onderwerp staat alleen in `beschrijving`.
    rij = {
        "titel": "Besluit omgevingsvergunning reguliere procedure verleend Nieuwe Jonkerstraat 43 1011CM Amsterdam",
        "beschrijving": "vernieuwen van de voorgevelpui",
    }
    assert onderwerp_van(rij) == "Vernieuwen van de voorgevelpui"
    assert onderwerp_van({"titel": rij["titel"], "beschrijving": "het  plaatsen van een  dakkapel."}) == "Plaatsen van een dakkapel"
    # Generiek of een lap tekst: terug naar de titel.
    titel = "Aanvraag omgevingsvergunning voor het kappen van een boom, Dorpsstraat 1"
    assert onderwerp_van({"titel": titel, "beschrijving": "Omgevingsvergunning"}) == "Kappen van een boom"
    assert onderwerp_van({"titel": titel, "beschrijving": "x" * 200}) == "Kappen van een boom"


@pytest.mark.parametrize(
    "beschrijving, straat, onderwerp",
    [
        # Sjablonen zonder onderwerp: door naar de titel.
        ("Betreft: Besluit op locatie Broekhem 77, 6301 HE Valkenburg", "Broekhem", None),
        ("Betreft: Beschikking op aanvraag op locatie Willem II-laan 4, 6371JC Landgraaf", "Willem II-laan", None),
        ("Betreft: Aanvraag op locatie Prinsessenweg 62, 3433AG Nieuwegein", "Prinsessenweg", None),
        ("Betreft: Beschikking verlenging beslistermijn op locatie Babelweg 15, 4357 BT Domburg", "Babelweg", None),
        ("Aanvraag omgevingsvergunning", None, None),
        ("(omgevingsvergunning)", None, None),
        # Onderwerp met rommel eromheen.
        (
            "Betreft: het vervangen van dakkapel op het voor- en achterdakvlak [Z2025-00000208], Willemstraat 211, 2983ES Ridderkerk",
            "Willemstraat",
            "Vervangen van dakkapel op het voor- en achterdakvlak",
        ),
        (
            "Betreft: het plaatsen van een dakkapel op locatie Galgeplek 11, 6662VS Elst, ontvangen op 2 maart 2026",
            "Galgeplek",
            "Plaatsen van een dakkapel",
        ),
        (
            "het plaatsen van vier dugouts over twee velden, Sportlaan 12A, 9781CL Bedum (5 maart 2026)",
            None,
            "Plaatsen van vier dugouts over twee velden",
        ),
        ("Z2025-00001725 - uitbreiding bedrijfshal", None, "Uitbreiding bedrijfshal"),
        ("Gemeente Huizen - Z.452847 - het tijdelijk inrichten van een bouwplaats", None, "Tijdelijk inrichten van een bouwplaats"),
        ("Aanvraag omgevingsvergunning voor het plaatsen van tussenwanden, Eurodreef 1, 5431VG Cuijk", "Eurodreef", "Plaatsen van tussenwanden"),
        # Komma's in het onderwerp zelf blijven staan.
        (
            "verbouwen van een bijgebouw naar mantelzorgwoning, plaatsen paardenverblijf",
            None,
            "Verbouwen van een bijgebouw naar mantelzorgwoning, plaatsen paardenverblijf",
        ),
    ],
)
def test_beschrijving_schoonmaken(beschrijving, straat, onderwerp):
    assert onderwerp_van({"beschrijving": beschrijving, "titel": None}, straat) == onderwerp


def test_sjabloon_in_beschrijving_valt_terug_op_de_titel():
    # Valkenburg: de titel heeft het onderwerp, de beschrijving alleen een sjabloon.
    rij = {
        "beschrijving": "Betreft: Besluit op locatie Broekhem 1, 6301 HD Valkenburg",
        "titel": "Kennisgeving besluit op aanvraag het renoveren van de woning (gemeentelijk monument), Broekhem 1, 6301 HD Valkenburg",
    }
    assert onderwerp_van(rij, "Broekhem") == "Renoveren van de woning (gemeentelijk monument)"


def test_adres_zonder_titelwoorden_uit_de_straatnaam():
    def adres(straat, nummer):
        z = bundel_tot_zaken(
            [_rij("a", date(2026, 1, 1), "aanvraag", "x", straatnaam=straat, huisnummer=nummer)], VANDAAG
        )
        return z[0]["adres"]

    assert adres("Aanvraag omgevingsvergunning Damrak", "47") == "Damrak 47"
    assert adres("Omgevingsvergunning 2e Wormenseweg", "3") == "2e Wormenseweg 3"
    assert adres("Aanvraag omgevingsvergunning", "12") is None
    assert adres("Vergunningstraat", "1") == "Vergunningstraat 1"


def test_zaaknummer_normaliseren():
    assert normaliseer_zaaknummer("Z2025-00001636voor") == "Z2025-00001636"
    assert normaliseer_zaaknummer("Z2026-00000410") == "Z2026-00000410"
    assert normaliseer_zaaknummer(None) is None


def _rij(koop_id, datum, type_besluit, titel, **kw):
    return {
        "koop_id": koop_id,
        "datum_publicatie": datum,
        "type_besluit": type_besluit,
        "titel": titel,
        "bg_naam": "Valkenburg aan de Geul",
        **kw,
    }


def test_fase_lopend_wordt_onbekend_na_een_jaar():
    rij = _rij("a", date(2025, 1, 1), "aanvraag", "Kennisgeving ontvangst aanvraag")
    assert bepaal_fase([rij], VANDAAG).fase == "onbekend"
    rij = _rij("a", date(2026, 8, 1), "aanvraag", "Kennisgeving ontvangst aanvraag")
    f = bepaal_fase([rij], VANDAAG)
    assert (f.fase, f.fase_label) == ("lopend", "In behandeling")


def test_fase_besloten_noemt_uitkomst_alleen_als_die_bekend_is():
    kaal = _rij("a", date(2026, 7, 31), "aanvraag", "Kennisgeving besluit op aanvraag")
    f = bepaal_fase([kaal], VANDAAG)
    assert (f.fase, f.fase_label, f.uitkomst) == ("besloten", "Besloten", None)

    verleend = _rij("b", date(2026, 7, 31), "verleend", "Verleende omgevingsvergunning")
    f = bepaal_fase([verleend], VANDAAG)
    assert (f.fase, f.fase_label, f.uitkomst) == ("besloten", "Verleend", "verleend")


def test_fase_intrekking_op_dezelfde_dag_wint_van_ontvangst():
    f = bepaal_fase(
        [
            _rij("a", date(2025, 8, 27), "aanvraag", "Kennisgeving ontvangst voor het renoveren"),
            _rij("b", date(2025, 8, 27), "ingetrokken", "Intrekking aanvraag"),
        ],
        VANDAAG,
    )
    assert (f.fase, f.fase_label) == ("gestopt", "Ingetrokken")


def test_bundelen_op_zaaknummer_met_tijdlijn_en_doorlooptijd():
    # De zaak uit het ontwerp: Broekhem 1, Z2026-00000410.
    gemeen = dict(
        zaaknummer_bg="Z2026-00000410",
        straatnaam="Broekhem",
        huisnummer="1",
        postcode="6301HD",
        x=185904.0,
        y=320095.0,
        afstand_m=308.4,
        activiteit_code="bouwen",
    )
    zaken = bundel_tot_zaken(
        [
            _rij("gmb-3", date(2026, 7, 31), "aanvraag", "Kennisgeving besluit op aanvraag het renoveren van de woning (gemeentelijk monument), Broekhem 1, 6301 HD Valkenburg", **gemeen),
            _rij("gmb-1", date(2026, 3, 17), "aanvraag", "Kennisgeving ontvangst aanvraag voor het renoveren van de woning , Broekhem 1, 6301 HD Valkenburg", **gemeen),
            _rij("gmb-2", date(2026, 7, 13), "verlenging_beslistermijn", "Kennisgeving verlenging beslistermijn, Broekhem 1, 6301 HD Valkenburg", **gemeen),
            _rij("gmb-9", date(2026, 1, 1), "kennisgeving", "Iets anders zonder zaaknummer", afstand_m=10.0),
        ],
        VANDAAG,
        postcode="6301 hd",
        huisnummer="1",
    )
    assert [z["zaak_id"] for z in zaken] == ["Valkenburg aan de Geul|Z2026-00000410", "gmb-9"]
    z = zaken[0]
    assert [s["koop_id"] for s in z["stappen"]] == ["gmb-1", "gmb-2", "gmb-3"]
    assert [s["soort"] for s in z["stappen"]] == ["ontvangst", "verlenging", "besluit"]
    assert z["fase"] == "besloten"
    assert z["doorlooptijd_dagen"] == 136
    assert z["onderwerp"] == "Renoveren van de woning (gemeentelijk monument)"
    assert z["adres"] == "Broekhem 1"
    assert z["afstand_m"] == 308
    assert z["op_adres"] is True
    assert zaken[1]["op_adres"] is False
    assert zaken[1]["zaaknummer"] is None


def test_zelfde_zaaknummer_bij_ander_bevoegd_gezag_is_een_andere_zaak():
    zaken = bundel_tot_zaken(
        [
            {**_rij("a", date(2026, 1, 1), "aanvraag", "x"), "zaaknummer_bg": "Z1-2", "bg_naam": "Ede"},
            {**_rij("b", date(2026, 1, 2), "aanvraag", "x"), "zaaknummer_bg": "Z1-2", "bg_naam": "Epe"},
        ],
        VANDAAG,
    )
    assert len(zaken) == 2
