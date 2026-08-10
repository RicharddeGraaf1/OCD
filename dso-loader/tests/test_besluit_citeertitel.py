"""Regressietests voor de besluit-citeertitel (ontwerp_loader).

Achtergrond: de Presenteren-API kent twee titel-niveaus op een ontwerpregeling.
Top-level `citeerTitel` hoort bij de REGELING, `besluitMetadata.citeerTitel` bij
het BESLUIT. De loader las alleen het eerste. Gevolg: de drie lopende ontwerpen
op het omgevingsplan van Putten heetten in de viewer alle drie "Omgevingsplan
gemeente Putten" en waren dus niet uit elkaar te houden.

Voor besluitversies zet Presenteren `besluitMetadata` helemaal niet; daar komt
de naam uit de Ontsluiten-API (`_ontsluiten_citeertitel`).

Zie docs/citeertitel-uit-presenteren-api.md, sectie "Citeertitel van het besluit".
"""

import pytest

from src.loaders import ontwerp_loader
from src.loaders.ontwerp_loader import _besluit_citeertitel, _ontsluiten_citeertitel


# Precies zoals de API het levert voor /akn/nl/bill/gm0273/2026/besluit4e68…
PUTTEN = {
    "identificatie": "/akn/nl/act/gm0273/2020/omgevingsplan",
    "opschrift": "Omgevingsplan gemeente Putten",
    "citeerTitel": "Omgevingsplan gemeente Putten",
    "besluitMetadata": {
        "citeerTitel": "Wijziging omgevingsplan gemeente Putten "
                       "t.b.v. ontwikkeling Stenenkamerseweg 38/38a",
    },
}


def test_besluitnaam_wint_van_regelingnaam():
    assert _besluit_citeertitel(PUTTEN) == (
        "Wijziging omgevingsplan gemeente Putten "
        "t.b.v. ontwikkeling Stenenkamerseweg 38/38a"
    )


def test_valt_terug_op_regeling_zonder_besluitmetadata():
    """223 van de 1028 ontwerpen missen het blok; besluitversies allemaal.

    Terugvallen op de regeling-citeertitel houdt de kolom gevuld — beter een
    generieke naam dan NULL, want de viewer toont dit veld als bron-label.

    Voor besluitversies is die terugval voorlopig; de Kadaster-BFF heeft voor
    93% van hen wél een besluitnaam. Zie de docstring van
    `_besluit_citeertitel`.
    """
    item = {k: v for k, v in PUTTEN.items() if k != "besluitMetadata"}
    assert _besluit_citeertitel(item) == "Omgevingsplan gemeente Putten"


def test_lege_besluitmetadata_telt_niet_als_naam():
    """Een aanwezig maar leeg veld mag de terugval niet blokkeren."""
    assert _besluit_citeertitel({**PUTTEN, "besluitMetadata": {}}) == \
        "Omgevingsplan gemeente Putten"
    assert _besluit_citeertitel({**PUTTEN, "besluitMetadata": {"citeerTitel": "   "}}) == \
        "Omgevingsplan gemeente Putten"


def test_zonder_enige_titel_none():
    """NULL is correct als de bron niets levert; de viewer valt dan terug op
    `opschrift`. Een lege string zou daar als naam worden gerenderd."""
    assert _besluit_citeertitel({"opschrift": "Omgevingsplan gemeente X"}) is None
    assert _besluit_citeertitel({"citeerTitel": "  "}) is None


def test_witruimte_wordt_getrimd():
    """De bron levert regelmatig een naslepende spatie ("Voorbeschermingsregels
    Provincie Noord-Brabant "), wat anders als 'afwijkend van opschrift' telt."""
    item = {"citeerTitel": "Voorbeschermingsregels Provincie Noord-Brabant "}
    assert _besluit_citeertitel(item) == "Voorbeschermingsregels Provincie Noord-Brabant"


# ── Ontsluiten-API (besluitversies) ──────────────────────────────────

# Zoals de Ontsluiten-API het levert voor de besluitversie van Raalte, waar
# Presenteren alleen "Omgevingsplan gemeente Raalte" geeft.
RAALTE_ONTSLUITEN = {
    "titel": "Elshagenweg 3 Wesepe",
    "omgevingsdocumentMetadata": {
        "besluitCiteertitel": "Elshagenweg 3 Wesepe",
        "isBesluit": True,
    },
}


@pytest.fixture
def vang_get(monkeypatch):
    """Vervang de HTTP-call; geeft de laatst opgevraagde URL terug."""
    gezien = {}

    def zet(antwoord):
        def nep(url, params=None, max_retries=3):
            gezien["url"] = url
            if isinstance(antwoord, Exception):
                raise antwoord
            return antwoord
        monkeypatch.setattr(ontwerp_loader, "_get", nep)
        return gezien

    return zet


def test_ontsluiten_leest_besluitciteertitel(vang_get):
    gezien = vang_get(RAALTE_ONTSLUITEN)
    assert _ontsluiten_citeertitel("_akn_nl_act_gm0177_x") == "Elshagenweg 3 Wesepe"
    assert gezien["url"].endswith("/documenten/_akn_nl_act_gm0177_x")


def test_ontsluiten_slikt_fouten():
    """Best-effort: een besluitnaam is een siersel, geen dragend gegeven. Een
    hikkende tweede API mag geen besluitversie-load stukmaken."""
    def stuk(url, params=None, max_retries=3):
        raise RuntimeError("503 Service Unavailable")

    import src.loaders.ontwerp_loader as ol
    origineel, ol._get = ol._get, stuk
    try:
        assert _ontsluiten_citeertitel("_wat_dan_ook") is None
    finally:
        ol._get = origineel


def test_ontsluiten_zonder_technisch_id_doet_geen_call(vang_get):
    gezien = vang_get(RAALTE_ONTSLUITEN)
    assert _ontsluiten_citeertitel(None) is None
    assert "url" not in gezien


def test_ontsluiten_lege_of_ontbrekende_naam(vang_get):
    vang_get({"omgevingsdocumentMetadata": {"besluitCiteertitel": "  "}})
    assert _ontsluiten_citeertitel("_x") is None
    vang_get({"omgevingsdocumentMetadata": {}})
    assert _ontsluiten_citeertitel("_x") is None
    vang_get({})
    assert _ontsluiten_citeertitel("_x") is None
