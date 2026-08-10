"""Regressietests voor `_besluit_citeertitel` (ontwerp_loader).

Achtergrond: de Presenteren-API kent twee titel-niveaus op een ontwerpregeling.
Top-level `citeerTitel` hoort bij de REGELING, `besluitMetadata.citeerTitel` bij
het BESLUIT. De loader las alleen het eerste. Gevolg: de drie lopende ontwerpen
op het omgevingsplan van Putten heetten in de viewer alle drie "Omgevingsplan
gemeente Putten" en waren dus niet uit elkaar te houden.

Zie docs/citeertitel-uit-presenteren-api.md, sectie "Citeertitel van het besluit".
"""

from src.loaders.ontwerp_loader import _besluit_citeertitel


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
