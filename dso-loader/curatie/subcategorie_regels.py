# -*- coding: utf-8 -*-
"""Curatie van de categorie/subcategorie-indeling — als regels, niet als lijst.

Waarom regels en geen handmatig ingevulde spreadsheet: de ruwe labels uit de
opschriftketen bevatten tientallen bijna-synoniemen (`bouwen`, `bouwwerken`,
`bouwvoorschriften`, `regels over bouwen`, `bouwen - algemeen`, `bouwen van een
bouwwerk`). Die één voor één afvinken is werk dat zich bij elke nieuwe
gemeente herhaalt. Een regel vangt ze allemaal, ook de varianten die er nu nog
niet zijn.

Eerste treffer wint, dus specifieke regels bovenaan.

Opgesteld door de agent op 2026-08-09, op verzoek van de gebruiker. Het zijn
oordelen, geen metingen — overschrijven mag en is de bedoeling zodra iets niet
klopt. Elke regel is één regel tekst, dus aanpassen is goedkoop.
"""

# ── Structuur, geen onderwerp ───────────────────────────────────────────────
# Deze kopjes beschrijven hoe het dócument in elkaar zit, niet waar de bepaling
# over gaat. Ze krijgen bewust géén categorie: "niet ingedeeld" is hier het
# juiste antwoord, niet een verzonnen etiket.
NIET_INDELEN = [
    r"bruidsschat", r"voormalige rijksregels", r"^regels van rijkswege",
    r"thema'?s", r"inhoudelijke regels", r"voorlopige regels",
    r"hoofdstukindeling", r"^algemene bepalingen", r"^omgevingsplannen",
    # Alleen de KALE containervormen. "activiteiten met betrekking tot
    # bouwwerken" en "voorbeschermingsregels hyperscale datacentra" noemen wél
    # een onderwerp en moeten dus doorvallen naar de regels hieronder.
    r"^activiteiten$", r"^activiteiten [(\['\"]", r"^activiteiten bruidsschat",
    r"^activiteiten tijdelijk deel", r"^activiteiten in de fysieke",
    r"^activitieiten", r"^regels$", r"^regels voor specifieke",
    r"^gebieden$", r"^ontwikkelingen$", r"^gebruik$", r"^vergunning$",
    r"^functies$", r"^tijdelijk deel", r"^voorbeschermingsregels$",
]

# ── Containers: kopjes die de BOEKDELEN van een plan benoemen ───────────────
# Ze zijn wél onderwerpdragend maar veel te grof om als label te dienen. Staat
# er onder zo'n container nog een specifieker kopje, dan wint dat; is de
# container het enige wat er is, dan valt het label terug op het diepste kopje.
CONTAINER = {
    "milieubelastende activiteiten",
    "activiteiten met betrekking tot bouwwerken, open erven en terreinen",
    "overige activiteiten", "thematische activiteiten",
    "aanleggen of wijzigen van wegen of spoorwegen zonder geluidproductieplafonds",
    "bruidsschat", "activiteiten bruidsschat", "thema's",
    "voormalige rijksregels", "inhoudelijke regels",
    "aanwijzingen in de fysieke leefomgeving",
    "regels tijdelijk deel omgevingsplan en andere gemeentelijke regelingen",
    "gebruiksactiviteiten", "bouwactiviteiten", "aanlegactiviteiten",
    "sloopactiviteiten", "algemene bepalingen", "hoofdstukindeling",
}

# ── categorie / subcategorie per onderwerp ──────────────────────────────────
# (patroon op het ruwe label, categorie, subcategorie)
REGELS = [
    # -- geluid, trillingen ---------------------------------------------------
    (r"geluid",                          "geluid",   "geluid"),
    (r"trilling",                        "geluid",   "trillingen"),

    # -- geur en agrarisch ----------------------------------------------------
    (r"geur",                            "milieu",   "geur"),
    (r"opslaan van (vaste mest|kuilvoer|drijfmest)|mestopslag|champost",
                                         "landbouw", "mestopslag en kuilvoer"),
    (r"fokken|houden of trainen|landbouwhuisdier|veehouderij|landbouw",
                                         "landbouw", "veehouderij"),
    (r"telen|kweken|gewas|glastuinbouw",  "landbouw", "teelt en tuinbouw"),

    # -- water ----------------------------------------------------------------
    (r"afvalwater",                      "water",    "afvalwater"),
    (r"lozen|lozing",                    "water",    "lozen"),
    (r"grondwater ?ont|wateronttrekking|onttrekk",
                                         "water",    "grondwateronttrekking"),
    (r"grondwaterbescherming|grondwaterverontreiniging|boringsvrije",
                                         "water",    "grondwaterbescherming"),
    (r"oppervlaktewater|waterstaatswerk|waterkering|beperkingengebied",
                                         "water",    "watersysteem en waterkeringen"),
    (r"\bwater\b",                       "water",    "water algemeen"),

    # -- bodem ----------------------------------------------------------------
    (r"bodembeheer|bodembescherming|ondergrond|saneren van de bodem|^bodem",
                                         "bodem",    "bodembeheer"),
    (r"graven|ontgraving|grondverzet",   "bodem",    "graven en grondverzet"),

    # -- milieubelastende activiteiten, per activiteit -----------------------
    (r"slachten van dieren|dierlijke bijproducten",
                                         "milieu",   "slachten en vleesbewerking"),
    (r"voedingsmiddelen|voedselbereiding|brood|bakker",
                                         "milieu",   "voedselbereiding en -industrie"),
    (r"uitwassen van beton|betonmortel|beton",
                                         "milieu",   "betonwerk"),
    (r"wassen van motorvoertuig|autowas", "milieu",  "wassen van voertuigen"),
    (r"acculader|accu",                  "milieu",   "acculaders"),
    (r"fotografisch",                    "milieu",   "fotografische bewerking"),
    (r"metaal|lassen|solderen|galvani",  "milieu",   "metaalbewerking"),
    (r"tanken|brandstof|lpg|propaan",    "milieu",   "tanken en brandstoffen"),
    (r"zwerfafval|afvalstof|\bafval\b",  "milieu",   "afval"),
    (r"stookinstallatie|verbranding|rookgas",
                                         "milieu",   "stoken en verbranding"),
    (r"opslaan|opslag",                  "milieu",   "opslag van stoffen"),
    (r"milieubelastende activiteit|milieuaspect|milieuvoorschrift|^milieu",
                                         "milieu",   "milieubelastende activiteiten overig"),

    # -- energie --------------------------------------------------------------
    (r"windturbine|windpark",            "energie",  "windturbines"),
    (r"zonne|zonnepanelen|opstelling zonne",
                                         "energie",  "zonne-energie"),
    (r"energiebespar|energietransitie|warmte",
                                         "energie",  "energiebesparing"),

    # -- bouwen ---------------------------------------------------------------
    (r"bouw- en sloop|slopen|sloopwerkzaamheden",
                                         "bouwen",   "bouwen en slopen"),
    (r"brandveilig|vluchtroute|brandcompartiment|bluswater|brandweer",
                                         "veiligheid", "brandveiligheid"),
    (r"gebruik van bouwwerk|gebruiksvoorschrift|open erven",
                                         "bouwen",   "gebruik van bouwwerken en erven"),
    (r"bouwen|bouwwerk|bouwvoorschrift|bouwactiviteit|nieuwbouw|hoofdgebouw|dakkapel",
                                         "bouwen",   "bouwen en verbouwen"),

    # -- erfgoed, natuur, landschap ------------------------------------------
    (r"erfgoed|monument|archeolog|stads- en dorpsgezicht",
                                         "erfgoed",  "cultureel erfgoed"),
    (r"natuur|flora|fauna|soortenbescherming|houtopstand|kappen|bomen",
                                         "natuur",   "natuur en bomen"),
    (r"beplanting|groen|park\b",         "natuur",   "groen en beplanting"),
    (r"landschap|kernkwaliteit|openheid", "landschap", "landschap"),

    # -- infrastructuur en mobiliteit ----------------------------------------
    (r"\bwegen\b|spoorweg|\bweg\b|verkeer|verhard",
                                         "infrastructuur", "wegen en spoorwegen"),
    (r"kabels|leiding",                  "infrastructuur", "kabels en leidingen"),
    (r"openbaar toegankelijk|openbare ruimte|straatmeubilair",
                                         "infrastructuur", "openbare ruimte"),
    (r"parkeer",                         "mobiliteit", "parkeren"),

    # -- gebruik van gronden en gebouwen -------------------------------------
    (r"wonen|woonactiviteit|woning",     "wonen",    "wonen"),
    (r"detailhandel|winkel",             "economie", "detailhandel"),
    (r"horeca",                          "economie", "horeca"),
    (r"bedrijf|bedrijven|industrie",     "economie", "bedrijven"),
    (r"maatschappelijk|onderwijs|zorg",  "planologisch gebruik", "maatschappelijke voorzieningen"),
    (r"sport|recreatie|visvijver|kampeer|evenement",
                                         "recreatie", "sport en recreatie"),
    (r"traditioneel schieten|schietbaan", "recreatie", "schietbanen"),
    (r"gebiedstype|ontwikkelgebied|gebiedsaanwijzing|bestemming",
                                         "planologisch gebruik", "gebiedstypen en functies"),

    (r"datacentra|datacenter",           "economie", "datacenters"),

    # -- veiligheid en gezondheid --------------------------------------------
    (r"externe veiligheid|kwetsbaar gebouw|explosie|gevaarlijke stoffen",
                                         "veiligheid", "externe veiligheid"),
    (r"gezondheid",                      "gezondheid", "gezondheid"),
    (r"lucht|stof|emissie",              "lucht",    "luchtkwaliteit"),

    # -- procedureel ----------------------------------------------------------
    (r"aanvraagvereisten",               "procedures", "aanvraagvereisten"),
    (r"vergunningplicht|omgevingsvergunning|binnenplanse vergunning",
                                         "procedures", "vergunningplicht"),
    (r"instructieregel",                 "procedures", "instructieregels"),
    (r"procedure|beoordelingsregel",     "procedures", "procedures"),
]
