"""Tegels van indeling H van Regels op maat, in Python.

Indeling H (keuze 2026-11-04) gebruikt de tegels van variant E: negen tegels uit het voorstel van
Brigitte, plus twee kleine voor procedures. Een artikel staat onder élke tegel waarover het iets
regelt. Op het tweede niveau staan de subonderwerpen van de tegel.

Bron van deze logica:
  - omgevingsdocumentenregister.nl/public/regels-op-maat/thema.js (`stelselVan`, variant 'meervoudig')
  - omgevingsdocumentenregister.nl/tests/rom-benchmark/run_benchmark.py (`tegels_d`, `tegels_e`)
Deze module neemt die tabellen letterlijk over. Wijzig ze samen; `test_tegels_h.py` vergelijkt deze
module met de benchmark zodra die naast deze repo staat.

Gebruikt door de transitie-export (`transitie.py`). Een script in dso-loader kan hem importeren via
het pad, zoals `bouw_wro_indeling.py` dat met `curatie/subcategorie_regels.py` doet.
"""
from __future__ import annotations

import re
import unicodedata

INDELING = "H"
INDELING_VERSIE = "2026-11-04"

# id, naam, volgorde. De negen tegels van E plus de kleine tegels; 'niet-ingedeeld' als vaste bak.
TEGELS = [
    {"id": "d-bouwen", "naam": "Bouwen en gebruik", "volgorde": 1},
    {"id": "d-werken", "naam": "Werken en recreëren", "volgorde": 2},
    {"id": "d-landbouw", "naam": "Landbouw en veehouderij", "volgorde": 3},
    {"id": "d-erfgoed", "naam": "Erfgoed", "volgorde": 4},
    {"id": "d-natuur", "naam": "Natuur en landschap", "volgorde": 5},
    {"id": "d-bodem-water", "naam": "Bodem en water", "volgorde": 6},
    {"id": "d-milieu", "naam": "Milieu, gezondheid en veiligheid", "volgorde": 7},
    {"id": "d-energie", "naam": "Energie en klimaat", "volgorde": 8},
    {"id": "d-verkeer", "naam": "Verkeer en infrastructuur", "volgorde": 9},
    {"id": "aanvragen", "naam": "Aanvragen en vergunningen", "volgorde": 10},
    {"id": "overheid", "naam": "Regels voor de overheid", "volgorde": 11},
    {"id": "niet-ingedeeld", "naam": "Niet ingedeeld", "volgorde": 12},
]

KOEPELTHEMAS = {"milieu", "procedures", "planologisch gebruik"}

THEMA_D = {
    "bouwen": "d-bouwen", "wonen": "d-bouwen", "planologisch gebruik": "d-bouwen",
    "economie": "d-werken", "recreatie": "d-werken", "landbouw": "d-landbouw", "erfgoed": "d-erfgoed",
    "natuur": "d-natuur", "landschap": "d-natuur", "bodem": "d-bodem-water", "water": "d-bodem-water",
    "milieu": "d-milieu", "geluid": "d-milieu", "lucht": "d-milieu", "veiligheid": "d-milieu", "gezondheid": "d-milieu",
    "energie": "d-energie", "duurzaamheid": "d-energie", "infrastructuur": "d-verkeer", "mobiliteit": "d-verkeer",
}

GA_TYPE_D = {
    "water en watersysteem": "d-bodem-water", "bodem": "d-bodem-water", "mijnbouw": "d-bodem-water",
    "natuur": "d-natuur", "landschap": "d-natuur", "energievoorziening": "d-energie", "erfgoed": "d-erfgoed",
    "bouw": "d-bouwen", "geluid": "d-milieu", "lucht": "d-milieu", "geur": "d-milieu",
    "externe veiligheid": "d-milieu", "defensie": "d-milieu", "verkeer": "d-verkeer", "leiding": "d-verkeer",
    "recreatie": "d-werken",
}

ACTGROEP_THEMA = {g: t for t, gs in {
    "bodem": ["bodemactiviteit", "mijnbouwlocatieactiviteit", "ontgrondingsactiviteit"],
    "bouwen": ["bouwactiviteit ruimtelijk", "bouwactiviteit technisch", "gebruiken-van-bouwwerkenactiviteit",
               "in-stand-houden-van-bouwwerkenactiviteit", "sloopactiviteit"],
    "economie": ["cultuuractiviteit", "dienstverleningsactiviteit", "evenementactiviteit", "exploitatieactiviteit bedrijf",
                 "exploitatieactiviteit detailhandel", "exploitatieactiviteit horeca", "exploitatieactiviteit kantoor",
                 "festiviteitactiviteit", "maatschappelijke activiteit", "reclameactiviteit", "standplaatsactiviteit"],
    "erfgoed": ["cultureel-erfgoedactiviteit", "monumentenactiviteit", "rijksmonumentenactiviteit", "werelderfgoedactiviteit"],
    "infrastructuur": ["aansluitactiviteit", "beperkingengebiedactiviteit m.b.t. een leiding"],
    "landbouw": ["agrarische activiteit", "landinrichtingsactiviteit"],
    "milieu": ["lozingsactiviteit", "milieubelastende activiteit", "milieubelastende activiteit met beperkt effect",
               "milieubelastende activiteit overig", "opslagactiviteit", "stortingsactiviteit"],
    "mobiliteit": ["beperkingengebiedactiviteit m.b.t. een luchthaven", "beperkingengebiedactiviteit m.b.t. een spoorweg",
                   "beperkingengebiedactiviteit m.b.t. een weg", "parkeeractiviteit", "uitritactiviteit", "wegactiviteit"],
    "natuur": ["dierenactiviteit", "flora- en fauna-activiteit", "jachtgeweeractiviteit", "kapactiviteit",
               "natura 2000-activiteit", "natuuractiviteit", "toegangsactiviteit", "valkeniersactiviteit"],
    "planologisch gebruik": ["herstructureringsactiviteit", "moderniseringsactiviteit", "planologische gebruiksactiviteit"],
    "recreatie": ["gelegenheid-tot-zwemmen-en-baden-biedenactiviteit", "kampeeractiviteit", "ontspanningsactiviteit",
                  "recreatieactiviteit", "sportactiviteit"],
    "veiligheid": ["alarminstallatieactiviteit"],
    "water": ["beperkingengebiedactiviteit m.b.t. een bergingsgebied",
              "beperkingengebiedactiviteit m.b.t. een installatie in een waterstaatswerk",
              "beperkingengebiedactiviteit m.b.t. een oppervlaktewaterlichaam",
              "beperkingengebiedactiviteit m.b.t. een waterkering", "beperkingengebiedactiviteit m.b.t. een waterstaatswerk",
              "beperkingengebiedactiviteit m.b.t. een zuiveringtechnisch werk", "beperkingengebiedactiviteit m.b.t. grondwater",
              "beperkingengebiedactiviteit m.b.t. zeegebied", "grondwateractiviteit", "hemelwateractiviteit",
              "noordzee-activiteit", "wateronttrekkingsactiviteit", "waterstaatswerkenactiviteit"],
    "wonen": ["ligplaatsactiviteit", "woonactiviteit"],
}.items() for g in gs}

FUNCTIE_E = [
    (r"bedrij|winkel|detailhandel|horeca|kantoor|dienstverlen|verhuur|pension|bed (?:en|&) breakfast|logies",
     "d-werken"),
    (r"recreati|sport|kampe|jachthaven|ligplaats", "d-werken"),
    (r"agrari|landbouw|veehouder|glastuin|paardenhouder", "d-landbouw"),
    (r"natuur|bos\b|landschapswaarde", "d-natuur"),
    (r"waterstaat|waterkering|waterberging", "d-bodem-water"),
    (r"verkeer|parkeer|spoor", "d-verkeer"),
    (r"archeolog|monument|cultuurhistor", "d-erfgoed"),
    (r"energie|windturbine|zonne", "d-energie"),
    (r"wonen|woning", "d-bouwen"),
]

NAAM_E = [
    (r"energie|warmte|zonne|windturbine|windpark|laadpaal", "d-energie"),
    (r"monument", "d-bouwen"),
    (r"(?<!niet zijnde )\bbouw(?:en|werk|activiteit)|\bverbouw|\bslopen", "d-bouwen"),
    (r"archeolog|monument|cultuurhistor|erfgoed", "d-erfgoed"),
    (r"\bkap(?:pen)?\b|houtopstand|\bbomen\b|flora|fauna|natura 2000|natuurnetwerk", "d-natuur"),
    (r"grondwater|onttrek|boringsvrij|oppervlaktewater|waterkering|\bgraven\b", "d-bodem-water"),
    (r"\buitweg|parkeer|\bweg(?:en)?\b|spoorweg", "d-verkeer"),
    (r"veehouderij|landbouw|agrari|mest", "d-landbouw"),
]


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[‘’“”'\"]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def tegel_d(categorie, sub):
    if not categorie:
        return None
    if categorie == "procedures":
        return "overheid" if sub == "instructieregels" else "aanvragen"
    return THEMA_D.get(categorie)


def tegels_e(pad=None, imow=None, gebiedsaanwijzingen=(), activiteiten=(), opschrift=None, inhoud=None):
    """Tegels van variant E voor één tekstdeel. Geeft (tegels, herkomst).

    pad: {'categorie', 'sub'} uit de onderwerp-indeling, of None.
    imow: lijst IMOW-thema's (Ow), of None.
    gebiedsaanwijzingen: [{'type', 'naam'}], activiteiten: [{'groep', 'naam'}] (Ow).
    opschrift/inhoud: voor de bestemmingsomschrijving (Wro).
    Gelijk aan tegels_e() in de benchmark, zonder de chips."""
    uit, herkomst = [], None
    themas = list(imow or [])

    def voeg(t, h):
        nonlocal herkomst
        if t and t not in uit:
            uit.append(t)
            herkomst = herkomst or h

    if pad:
        voeg(tegel_d(pad.get("categorie"), pad.get("sub")), "pad")
    elif themas:
        specifiek = [t for t in themas if t not in KOEPELTHEMAS]
        for th in (specifiek or themas):
            voeg(THEMA_D.get(th), "annotatie")
    for ga in gebiedsaanwijzingen or ():
        voeg(GA_TYPE_D.get(norm(ga.get("type"))), "gebiedsaanwijzing")
    for ac in activiteiten or ():
        voeg(THEMA_D.get(ACTGROEP_THEMA.get(norm(ac.get("groep")), "")), "activiteitengroep")
    for th in themas:
        voeg(THEMA_D.get(th), "annotatie")
    if re.search(r"bestemmingsomschrijving", opschrift or "", re.I) and inhoud:
        hoofd = re.split(r"ondergeschikt", inhoud, maxsplit=1, flags=re.I)[0].lower()
        for patroon, t in FUNCTIE_E:
            if re.search(patroon, hoofd):
                voeg(t, "bestemmingsomschrijving")
    for naam in [x.get("naam") for x in activiteiten or ()] + [x.get("naam") for x in gebiedsaanwijzingen or ()]:
        for patroon, t in NAAM_E:
            if re.search(patroon, norm(naam)):
                voeg(t, "naam activiteit/gebiedsaanwijzing")
    return uit, herkomst
