"""Regressietests voor de keuze van de annotatieroute.

Achtergrond (vault gaps.md G-145 en G-148): `load_via_api` koos de
annotatieroute op `doc_type`. `Projectbesluit` staat in
`ARTIKELSTRUCTUUR_TYPES`, maar alle 27 vigerende projectbesluiten in het DSO
zijn vrijetekst. Die kregen dus `load_regeltekstannotaties`.

Op 2026-09-05 is daarvoor een terugval ingebouwd die afging op een leeg
resultaat:

    stats = load_regeltekstannotaties(...)
    if not stats["regels"] and not stats["activiteiten"]:
        load_divisieannotaties(...)

Die kon niet vuren. De API antwoordt op deze documenten met **400** ("Deze
regeling ondersteunt geen Regeltekst-objecten"), `is_transient()` geeft voor
4xx terecht False, dus de call raist en de regel eronder wordt nooit bereikt.

Sinds 2026-09-09 wijst de regeling zelf de route aan via `_links.annotaties`,
met het documenttype als terugval en de 400 als vangnet. Deze tests bewaken
alle drie.
"""

import httpx
import pytest

from src.loaders import api_loader


# ── _route_uit_links ─────────────────────────────────────────────────

def _detail(href: str) -> dict:
    return {"_links": {"self": {"href": "/regelingen/x"},
                       "annotaties": {"href": href}}}


def test_divisielink_geeft_divisieroute():
    d = _detail("/regelingen/_akn_nl_act_ws0636_2025_PBCUB/divisieannotaties"
                "?geldigOp=2026-09-09&inWerkingOp=2026-09-09")
    assert api_loader._route_uit_links(d) == "divisie"


def test_regeltekstlink_geeft_regeltekstroute():
    d = _detail("/regelingen/_akn_nl_act_gm0344_2020_omgevingsplan/regeltekstannotaties"
                "?geldigOp=2026-09-09&inWerkingOp=2026-09-09")
    assert api_loader._route_uit_links(d) == "regeltekst"


@pytest.mark.parametrize("data", [
    {},                                   # geen _links
    {"_links": {}},                       # geen annotaties-link
    {"_links": {"annotaties": {}}},       # link zonder href
    {"_links": {"annotaties": {"href": "/regelingen/x/ietsanders"}}},
])
def test_ontbrekende_of_onbekende_link_geeft_none(data):
    """None betekent: val terug op het documenttype, raad niet."""
    assert api_loader._route_uit_links(data) is None


def test_projectbesluit_kiest_divisie_ondanks_artikelstructuur_type():
    """De kern van G-145: de link wint van de typelijst.

    `Projectbesluit` staat nog steeds in ARTIKELSTRUCTUUR_TYPES — bewust, als
    terugval — maar een divisie-link moet dat overrulen.
    """
    assert "Projectbesluit" in api_loader.ARTIKELSTRUCTUUR_TYPES
    d = _detail("/regelingen/x/divisieannotaties")
    assert api_loader._route_uit_links(d) == "divisie"


# ── bepaal_annotatie_route (herstelpad) ──────────────────────────────

def test_bepaal_route_haalt_de_link_op(monkeypatch):
    monkeypatch.setattr(api_loader, "_get",
                        lambda url, params=None: _detail("/x/divisieannotaties"))
    assert api_loader.bepaal_annotatie_route("/akn/nl/act/ws0636/2025/PBCUB") == "divisie"


def test_bepaal_route_slikt_een_falende_call():
    """Faalt de detail-call, dan None — de aanroeper valt terug op het type,
    en een remediatiepad mag niet omvallen op een hik."""
    def _boem(url, params=None):
        raise httpx.ConnectError("stuk")

    origineel = api_loader._get
    api_loader._get = _boem
    try:
        assert api_loader.bepaal_annotatie_route("/akn/nl/act/x/y") is None
    finally:
        api_loader._get = origineel


# ── de 400-vangst ────────────────────────────────────────────────────

def test_vierhonderd_is_niet_transient():
    """Het vangnet is nodig juist omdat de retry-laag 4xx doorlaat.

    Zou `is_transient` True geven, dan werd de 400 herhaald in plaats van
    afgevangen — en dat is precies waarom de fix van 05-09 dode code was.
    """
    resp = httpx.Response(400, request=httpx.Request("GET", "https://x/y"))
    fout = httpx.HTTPStatusError("400", request=resp.request, response=resp)
    from src.http_retry import is_transient
    assert is_transient(fout) is False


def test_lege_stats_dekken_alle_tellers():
    """De 400-terugval levert deze dict; hij moet dezelfde sleutels hebben als
    een echte run, anders klapt de teller-print erna op een KeyError."""
    for sleutel in ("regels", "activiteiten", "ala", "ga", "normen",
                    "normwaarden", "kaarten", "kaartlagen", "locaties",
                    "geometrieen"):
        assert sleutel in api_loader.LEGE_REGELTEKST_STATS
        assert api_loader.LEGE_REGELTEKST_STATS[sleutel] == 0


# ── het label ────────────────────────────────────────────────────────

def test_projectbesluit_is_vrijetekst_in_de_regelingmodelmap():
    """G-148. Het aparte artikelstructuur-deel houdt zijn eigen documenttype."""
    assert api_loader.REGELINGMODEL_MAP["Projectbesluit"] == "RegelingVrijetekst"
    assert api_loader.REGELINGMODEL_MAP.get(
        "Omgevingsplanregels Projectbesluit", "RegelingCompact") == "RegelingCompact"
