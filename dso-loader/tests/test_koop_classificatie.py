"""Tests voor de tweede classificatietrap en de zaaknummer-extractie.

Draaien zonder pytest:  python tests/test_koop_classificatie.py

De cases zijn woordelijk ontleend aan echte publicaties in
vth.vergunningkennisgeving (Arnhem, Gouda, Enschede, Huizen, Noordenveld,
Hulst) — zie de vault-analyse "Doorlooptijd meewegen in de
DSO-implementatiemonitor-score" §3.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.loaders.koop_vergunning import (  # noqa: E402
    classify_type_besluit,
    classify_type_besluit_uit_tekst,
    extract_zaaknummer_bg,
)

# ---------------------------------------------------------------- tweede trap

TEKST_CASES = [
    # Arnhem — gelabelde 'Besluit:'-regel
    ("Burgemeester en wethouders maken bekend dat zij een besluit hebben genomen "
     "inzake een omgevingsvergunning, waarbij de reguliere procedure van "
     "toepassing is.\nZaakid: N26AB.1150\nOmschrijving: het realiseren van een "
     "uitbouw\nBesluit: Verleend\nVerzenddatum: 2026-07-14",
     "verleend"),
    # Gouda — vrije tekst
    ("Op 20 juli 2026 heeft de Omgevingsdienst Midden-Holland namens gemeente "
     "Gouda een besluit genomen op de aanvraag met kenmerk 2026-00012785. "
     "De vergunning is verleend.",
     "verleend"),
    # Enschede — 'toegekend'
    ("Wij hebben op 10 juli 2026 een besluit genomen op de aanvraag met "
     "zaaknummer 0153Z2025111300018. De vergunning is toegekend.",
     "verleend"),
    # Hulst — 'hebben verleend'
    ("Burgemeester en wethouders van de gemeente Hulst maken bekend dat zij een "
     "omgevingsvergunning hebben verleend voor een woning bouwen.",
     "verleend"),
    # Noordenveld — ', verleend op <datum>'
    ("Burgemeester en wethouders maken bekend dat zij in het kader van de "
     "Omgevingswet de volgende aanvraag hebben verleend Besluit aanvr. "
     "beschikking behandelen 2e Energieweg 10, verleend op 08 juli 2026",
     "verleend"),
    # Huizen — gelabelde aanvraag
    ("Zaaknummer: Z.467853\nOntvangstdatum: 30 juni 2026\nAanvraag inzien",
     "aanvraag"),
    # art. 4:5 Awb
    ("De aanvraag is buiten behandeling gesteld omdat de gegevens onvolledig waren.",
     "buiten_behandeling"),
    # Weigering
    ("Wij hebben een besluit genomen. De vergunning is geweigerd.", "geweigerd"),
    # Intrekking wint van verlening
    ("De vergunning is ingetrokken op verzoek van de aanvrager; hij was eerder "
     "verleend.", "ingetrokken"),
    # KRITISCH — voorwaardelijke bijzin is geen besluit
    ("Ingekomen verzoek om omgevingsvergunning Kenmerk: OV20230313. Tegen een "
     "aanvraag kunt u geen bezwaar maken. Dat kan pas als de vergunning is "
     "verleend.", None),
    # KRITISCH — testdata bij de bron telt niet mee
    ("Besluit: _afgebroken (telt niet voor productie!)", None),
    # Geen signaal
    ("De gemeente publiceert dit bericht om omwonenden te informeren.", None),
]

ZAAKNR_CASES = [
    ("Zaakid: N26AB.1150", "N26AB.1150"),                  # punt in het nummer
    ("Dossiernummer: OMV.24.31.12345", "OMV.24.31.12345"),  # Rotterdam
    ("Zaaknummer: Z.467853", "Z.467853"),                   # Huizen
    ("Kenmerk: OV20230313", "OV20230313"),                  # bestaand gedrag
    ("zaaknummer GU-Z2026-0047412 blabla", "GU-Z2026-0047412"),
    ("Referentienummer: 2026-00012785", "2026-00012785"),
    # KRITISCH — waarde op de volgende regel is geen zaaknummer
    ("Zaaknummer:\nOmschrijving: iets", None),
    # KRITISCH — 'kenmerk waarvan ...' leverde vroeger het zaaknummer 'waarvan'
    ("het kenmerk waarvan hierboven melding is gemaakt", None),
]

TITEL_CASES = [
    ("Verleende omgevingsvergunning Dorpsstraat 1", "verleend"),
    ("Besluit Omgevingsvergunning - Eindhovensingel 125 in Arnhem", "overig"),
    ("Ontvangen aanvraag omgevingsvergunning", "aanvraag"),
]


def _check(naam, cases, fn):
    fouten = []
    for invoer, verwacht in cases:
        gekregen = fn(invoer)
        if gekregen != verwacht:
            fouten.append(f"  {naam}: verwacht {verwacht!r}, kreeg {gekregen!r}\n"
                          f"    invoer: {invoer[:70]!r}")
    return fouten


def test_classify_type_besluit_uit_tekst():
    fouten = _check("tekst", TEKST_CASES, classify_type_besluit_uit_tekst)
    assert not fouten, "\n" + "\n".join(fouten)


def test_extract_zaaknummer_bg():
    fouten = _check("zaaknr", ZAAKNR_CASES, extract_zaaknummer_bg)
    assert not fouten, "\n" + "\n".join(fouten)


def test_classify_type_besluit_titel_ongewijzigd():
    """De eerste trap mag niet veranderd zijn — 'Besluit ...' blijft overig."""
    fouten = _check("titel", TITEL_CASES, classify_type_besluit)
    assert not fouten, "\n" + "\n".join(fouten)


if __name__ == "__main__":
    alle = []
    for fn in (test_classify_type_besluit_uit_tekst, test_extract_zaaknummer_bg,
               test_classify_type_besluit_titel_ongewijzigd):
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            print(f"FOUT {fn.__name__}{e}")
            alle.append(fn.__name__)
    sys.exit(1 if alle else 0)
