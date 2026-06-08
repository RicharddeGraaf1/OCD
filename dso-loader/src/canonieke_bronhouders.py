"""Canonieke code→naam-map voor bronhouders met een vaste landelijke standaard.

Achtergrond
-----------
De bronhouder-naam kwam historisch uit een handmatig CLI-argument
(`load-ow --overheid 'pvXX,Naam'`) en bleef door `ON CONFLICT DO NOTHING`
plakken — één typefout = blijvend fout. De bronhouder-audit van 2026-06-08
vond dat dit op grote schaal mis was: provincies, ministeries en waterschappen
hadden door elkaar gehusselde of code-only namen (bv. pv21 op "Groningen"
i.p.v. "Fryslân"; ws0636 "Amstel Gooi en Vecht" terwijl het De Stichtse
Rijnlanden is; tientallen waterschappen met `naam == overheidscode`).

Voor de drie **stabiele** bestuurslagen (provincie, rijk, waterschap) bestaat
een vaste, autoritatieve naam (TOOI-register, identifier.overheid.nl/tooi).
Die borgen we hier centraal en passen we toe met `ON CONFLICT … DO UPDATE`,
zodat een CLI-typefout niet meer blijft hangen.

Gemeenten staan hier bewust NIET in: hun naam komt autoritatief uit PDOK
(`loaders/gemeentegrens_pdok.py`, dat zelf al `DO UPDATE` doet). Voor gemeenten
geldt: een code-only placeholdernaam (kale cijfers) mag worden overschreven,
maar een echte naam laten we met rust.

Bron van de namen: TOOI `officieleNaamExclSoort` per code; de rijk-namen volgen
de afkortingsconventie die al in de database stond (BZK/IenW/LNV).
"""

import re


PROVINCIE_NAMEN = {
    "pv20": "Groningen", "pv21": "Fryslân", "pv22": "Drenthe",
    "pv23": "Overijssel", "pv24": "Flevoland", "pv25": "Gelderland",
    "pv26": "Utrecht", "pv27": "Noord-Holland", "pv28": "Zuid-Holland",
    "pv29": "Zeeland", "pv30": "Noord-Brabant", "pv31": "Limburg",
}

RIJK_NAMEN = {
    "mnre1018": "Ministerie van Defensie",
    "mnre1034": "Ministerie van BZK",
    "mnre1045": "Ministerie van Economische Zaken en Klimaat",
    "mnre1130": "Ministerie van IenW",
    "mnre1153": "Ministerie van LNV",
    "mnre1182": "Ministerie van Klimaat en Groene Groei",
}

# De 21 actuele waterschappen (TOOI officieleNaamExclSoort).
WATERSCHAP_NAMEN = {
    "ws0152": "Rijn en IJssel",
    "ws0155": "Amstel, Gooi en Vecht",
    "ws0372": "Hoogheemraadschap van Delfland",
    "ws0539": "De Dommel",
    "ws0616": "Hoogheemraadschap van Rijnland",
    "ws0621": "Rivierenland",
    "ws0636": "Hoogheemraadschap De Stichtse Rijnlanden",
    "ws0646": "Hunze en Aa's",
    "ws0647": "Noorderzijlvest",
    "ws0650": "Zuiderzeeland",
    "ws0651": "Hoogheemraadschap Hollands Noorderkwartier",
    "ws0652": "Brabantse Delta",
    "ws0653": "Wetterskip Fryslân",
    "ws0654": "Aa en Maas",
    "ws0655": "Hollandse Delta",
    "ws0656": "Hoogheemraadschap van Schieland en de Krimpenerwaard",
    "ws0661": "Scheldestromen",
    "ws0662": "Vallei en Veluwe",
    "ws0663": "Vechtstromen",
    "ws0664": "Drents Overijsselse Delta",
    "ws0665": "Limburg",
}

# Samengevoegde map voor de gecontroleerde lagen.
CANONIEKE_NAMEN: dict[str, str] = {**PROVINCIE_NAMEN, **RIJK_NAMEN, **WATERSCHAP_NAMEN}

# Een naam die niets toevoegt: kale 4-cijfercode of gelijk aan de overheidscode.
_CODE_ONLY = re.compile(r"^\d{4}$")


def is_code_only_naam(naam: str | None, overheidscode: str) -> bool:
    """True als `naam` een placeholder is (kale cijfers of == de code zelf)."""
    if not naam:
        return True
    return bool(_CODE_ONLY.match(naam)) or naam == overheidscode


def canonieke_bronhouder_naam(code: str, fallback: str) -> tuple[str, bool]:
    """Geef (naam, is_gecontroleerd) voor een bronhouder-code.

    Voor provincie/rijk/waterschap prevaleert de canonieke naam boven het
    CLI-/loader-argument. is_gecontroleerd=True betekent: bij ON CONFLICT mag
    de bestaande naam worden overschreven (de bron is autoritatief). Voor
    gemeenten houden we het meegegeven argument aan (False) — PDOK is daar de
    autoriteit — maar een code-only placeholder mag alsnog zelf-helen
    (zie `upsert_bronhouder`).
    """
    canon = CANONIEKE_NAMEN.get(code)
    if canon:
        return canon, True
    return fallback, False


def bestuurslaag_voor(code: str) -> str:
    """Leid de bestuurslaag af uit de overheidscode-prefix."""
    if code.startswith("gm"):
        return "gemeente"
    if code.startswith("pv"):
        return "provincie"
    if code.startswith("ws"):
        return "waterschap"
    return "rijk"


def upsert_bronhouder(cur, code: str, naam: str, bestuurslaag: str | None = None) -> None:
    """Insert-or-update één bronhouder met canonieke naamborging.

    Regels bij conflict op `overheidscode`:
    - gecontroleerde laag (provincie/rijk/waterschap): naam → canonieke naam.
    - overige laag (gemeente): bestaande échte naam blijft staan; een code-only
      placeholder wordt door de nieuwe (betere) naam overschreven.
    - bestuurslaag wordt alleen gevuld als hij nog NULL is (nooit overschreven).

    Eén centrale plek i.p.v. losse INSERT … DO NOTHING in elke loader, zodat
    typefouten niet meer blijven plakken (zie modulekop).
    """
    naam_to_use, gecontroleerd = canonieke_bronhouder_naam(code, naam)
    if bestuurslaag is None:
        bestuurslaag = bestuurslaag_voor(code)
    cur.execute(
        """INSERT INTO core.bronhouder (overheidscode, naam, bestuurslaag)
           VALUES (%s, %s, %s)
           ON CONFLICT (overheidscode) DO UPDATE
               SET naam = CASE
                       WHEN %s THEN EXCLUDED.naam
                       WHEN core.bronhouder.naam ~ '^[0-9]{4}$'
                            OR core.bronhouder.naam = core.bronhouder.overheidscode
                            THEN EXCLUDED.naam
                       ELSE core.bronhouder.naam END,
                   bestuurslaag = COALESCE(core.bronhouder.bestuurslaag, EXCLUDED.bestuurslaag)""",
        (code, naam_to_use, bestuurslaag, gecontroleerd),
    )
