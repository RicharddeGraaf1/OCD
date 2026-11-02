"""
Kennisgevingen bundelen tot zaken — voor de vergunningen-tab in de OCD-viewer.

Eén aanvraag levert meerdere kennisgevingen op (ontvangst, verlenging, besluit).
Wie op een plek wil weten wat er speelt, wil de zaak zien, niet de losse
berichten. Op Broekhem 33 Valkenburg (2026-10): 60 kennisgevingen, 32 zaken.

Pure functies: geen DB, geen FastAPI. Het endpoint staat in vergunningen.py.

**De fase komt niet alleen uit `type_besluit`.** Dat veld is op de titel
geclassificeerd en "Kennisgeving besluit op aanvraag ..." valt daar door naar de
regel `aanvraag`, omdat er geen uitkomstwoord in staat. Gemeten 2026-11-02 op de
lokale DB (965.510 records): 43.767 records met het woord "besluit" in de titel
staan als `aanvraag`, en 14.163 als `kennisgeving`. Hier toetsen we de titel
daarom zelf nog een keer na; zo klopt de tab ook zonder herclassificatie, en er
verandert niets aan de data.

**"Besloten" is niet "verleend".** De titel van zulke berichten noemt geen uitkomst.
De tekst doet dat vaak wel, maar `type_besluit` weet dat pas na de herclassificatie
(dso-loader/scripts/2026-11-herclassificeer-besluit-uit-tekst.py). De uitkomst noemen
we alleen als `type_besluit` hem kent; daarna verschijnt hij hier vanzelf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

# Na een jaar zonder vervolg zeggen we niet meer "in behandeling": de
# beslistermijn is dan ruim voorbij en het besluit is kennelijk niet (met
# hetzelfde zaaknummer) gepubliceerd.
LOPEND_MAX_DAGEN = 365

_BESLUIT = re.compile(r"\bbesluit\b", re.IGNORECASE)
# Alleen als het bericht nog niet als aanvraag staat: "Aanvraag om toestemming"
# is geen besluit, "Toestemming voor het plaatsen van een dakkapel" wel.
_TOESTEMMING = re.compile(r"\btoestemming\b", re.IGNORECASE)
# 'aangevraagd' hoort hier bewust niet bij: "Besluit aangevraagde
# omgevingsvergunning" is een besluit.
_ONTVANGST = re.compile(r"\b(ontvang\w*|ingekomen|ingediend\w*|binnengekomen)\b", re.IGNORECASE)
_BUITEN_BEHANDELING = re.compile(r"\bbuiten\s+behandeling", re.IGNORECASE)

# type_besluit dat zelf de uitkomst noemt → (uitkomst, label)
_UITKOMST: dict[str, tuple[str, str]] = {
    "verleend": ("verleend", "Verleend"),
    "van_rechtswege": ("van_rechtswege", "Van rechtswege verleend"),
    "geweigerd": ("geweigerd", "Geweigerd"),
    "vergunningvrij": ("vergunningvrij", "Vergunningvrij"),
    "melding_geaccepteerd": ("melding_geaccepteerd", "Melding geaccepteerd"),
}

# Gewicht binnen één publicatiedag: het zwaarste bericht bepaalt de fase.
_GEWICHT = {
    "ontvangst": 1,
    "ontwerp": 2,
    "verlenging": 3,
    "melding": 4,
    "besluit": 5,
    "ingetrokken": 6,
    "buiten_behandeling": 6,
}

_FASE_VAN_SOORT = {
    "ontvangst": "lopend",
    "ontwerp": "lopend",
    "verlenging": "lopend",
    "melding": "besloten",
    "besluit": "besloten",
    "ingetrokken": "gestopt",
    "buiten_behandeling": "gestopt",
}


def soort_van(type_besluit: str | None, titel: str | None) -> str | None:
    """Wat voor bericht is dit? None = zegt niets over de fase (rectificatie e.d.)."""
    t = titel or ""
    # Vóór de type-check: "Buiten behandeling laten aanvraag ..." staat als `aanvraag`.
    if type_besluit == "buiten_behandeling" or _BUITEN_BEHANDELING.search(t):
        return "buiten_behandeling"
    if type_besluit == "ingetrokken":
        return "ingetrokken"
    if type_besluit in _UITKOMST:
        return "besluit"
    if type_besluit == "verlenging_beslistermijn":
        return "verlenging"
    if type_besluit == "ontwerp":
        return "ontwerp"
    if type_besluit == "melding":
        return "melding"
    heeft_besluit = bool(_BESLUIT.search(t)) and not _ONTVANGST.search(t)
    if type_besluit == "aanvraag":
        return "besluit" if heeft_besluit else "ontvangst"
    # kennisgeving, overig, NULL: alleen een besluit als de titel het zegt.
    if type_besluit == "rectificatie":
        return None
    return "besluit" if heeft_besluit or _TOESTEMMING.search(t) else None


# ── Onderwerp en adres uit de titel ──────────────────────────────────────

_POSTCODE = re.compile(r"\b\d{4}\s?[A-Z]{2}\b")
# Eerste lidwoord na een scheidingsteken of inleidend woord: daar begint de
# handeling. "... voor het plaatsen van een dakkapel, Kerkstraat 1".
_START_LIDWOORD = re.compile(r"(?:^|[\s,:/-])(?:het|een)\s+(?=[^\W\d_]{3})", re.IGNORECASE)
_START_DUBBELE_PUNT = re.compile(r":\s*(?=[^\W\d_]{3})")
_START_ACTIVITEIT = re.compile(r"\b(?:voor|t\.?b\.?v\.?)\s+(?=[^\W\d_]{4,}en\b)", re.IGNORECASE)
_EINDE = re.compile(r",|\s[-–/]\s|\s\(?(?:Z|OLO|V)?\d{5,}")
_SLOT_VOORZETSEL = re.compile(
    r"[\s,;:.-]*(?:\b(?:aan de|aan|op de|op|te|in|nabij|bij|ter hoogte van|locatie))?[\s,;:.-]*$",
    re.IGNORECASE,
)
# Blijft er alleen dit soort tekst over, dan weten we het onderwerp niet.
_GENERIEK = re.compile(
    r"^(?:omgevings)?(?:vergunning|aanvraag|besluit|beschikking|kennisgeving)\b"
    r"|^(?:ontvangst|verzoek|melding)\b",
    re.IGNORECASE,
)


def onderwerp_uit_titel(titel: str | None, straatnaam: str | None = None) -> str | None:
    """Best-effort onderwerp uit vrije tekst, of None als het niet herkenbaar is.

    Titels zijn vrije tekst van honderden bronhouders; we mikken op de
    veelvoorkomende vormen en geven liever niets terug dan een half adres. De
    viewer valt bij None terug op de volledige titel.
    """
    if not titel:
        return None
    t = " ".join(titel.split())
    m = _START_LIDWOORD.search(t) or _START_DUBBELE_PUNT.search(t) or _START_ACTIVITEIT.search(t)
    if not m:
        return None
    rest = t[m.end():]
    einde = _EINDE.search(rest)
    if einde:
        rest = rest[: einde.start()]
    pc = _POSTCODE.search(rest)
    if pc:
        rest = rest[: pc.start()]
    if straatnaam:
        i = rest.lower().find(straatnaam.lower())
        if i >= 0:
            rest = rest[:i]
    rest = _SLOT_VOORZETSEL.sub("", rest).strip()
    if len(rest) < 6 or _GENERIEK.match(rest):
        return None
    return rest[0].upper() + rest[1:]


# `beschrijving` is de eerste keus, maar geen schoon veld. Amsterdam zet er het
# kale onderwerp in ("vernieuwen van de voorgevelpui") en heeft dat nergens anders;
# andere bronhouders zetten er een sjabloon in ("Betreft: Beschikking op aanvraag
# op locatie Willem II-laan 4, 6371JC Landgraaf") of plakken er van alles omheen
# ("Gemeente Huizen - Z.452847 - het inrichten van een bouwplaats"). In een
# steekproef van 7.564 records begon zo'n 25% met "Betreft", en had 30% een postcode.
_MAX_ONDERWERP = 160
_LIDWOORD_VOORAAN = re.compile(r"^(?:het|een|de)\s+", re.IGNORECASE)
_BESCHRIJVING_PREFIX = re.compile(
    r"^(?:betreft\s*:|toelichting\s*:|onderwerp\s*:|omschrijving\s*:"
    r"|gemeente\s+[^-–:]{2,40}[-–:]"
    r"|[A-Z]{0,3}[.\s]?\d[\w./-]*\d\s*[-–:]|\d{5,}\s)\s*",
    re.IGNORECASE,
)
_BESCHRIJVING_SLOT = re.compile(
    r"\s*(?:\[[^\]]*\]|\(\s*\d{1,2}\s+\w+\s+\d{4}\s*\)|,?\s*ontvangen\s+op\b.*)\s*$",
    re.IGNORECASE,
)
_LOCATIEWOORD = re.compile(r"\s+(?:op|voor|aan|nabij)\s+(?:de\s+)?locatie\b|\s+locatie\s+", re.IGNORECASE)
# Een procesbericht, geen onderwerp: dan moet het onderwerp er nog ergens in zitten.
_PROCESWOORD = re.compile(
    r"^(?:bekendmaking|verleend\w*|binnengekomen|ingekomen|ingediend\w*|gedeputeerde|gemeente"
    r"|owo\b|de\s+volgende|rectificatie|intrekk\w*|verlenging|\()",
    re.IGNORECASE,
)


def _schone_beschrijving(tekst: str | None, straatnaam: str | None) -> str | None:
    t = " ".join((tekst or "").split())
    if not t:
        return None
    # Twee keer: "Betreft: Gemeente Huizen - het plaatsen ...".
    t = _BESCHRIJVING_PREFIX.sub("", _BESCHRIJVING_PREFIX.sub("", t))
    while (kort := _BESCHRIJVING_SLOT.sub("", t)) != t:
        t = kort
    if _GENERIEK.match(t) or _PROCESWOORD.match(t):
        # "Aanvraag omgevingsvergunning voor het plaatsen van ..."
        return onderwerp_uit_titel(t, straatnaam)
    loc = _LOCATIEWOORD.search(t)
    if loc:
        t = t[: loc.start()]
    if straatnaam:
        i = t.lower().find(straatnaam.lower())
        if i > 0:
            t = t[:i]
    pc = _POSTCODE.search(t)
    if pc:
        # Het stuk vóór de postcode is het adres: terug naar de komma ervoor.
        t = t[: pc.start()].rstrip(" ,")
        if "," in t and re.search(r"\d", t.rsplit(",", 1)[1]):
            t = t.rsplit(",", 1)[0]
    t = _BESCHRIJVING_SLOT.sub("", t.rstrip(" ,;:"))
    t = _LIDWOORD_VOORAAN.sub("", _SLOT_VOORZETSEL.sub("", t).strip())
    # Het adres kan midden in een haakje zijn afgeknipt.
    if t.count("(") > t.count(")"):
        t = t[: t.rindex("(")].rstrip()
    if len(t) < 6 or len(t) > _MAX_ONDERWERP or _GENERIEK.match(t):
        return None
    return t[0].upper() + t[1:]


def onderwerp_van(rij: dict[str, Any], straatnaam: str | None = None) -> str | None:
    """Onderwerp uit `beschrijving`, anders uit de titel."""
    return _schone_beschrijving(rij.get("beschrijving"), straatnaam) or onderwerp_uit_titel(
        rij.get("titel"), straatnaam
    )


def normaliseer_zaaknummer(z: str | None) -> str | None:
    """Knip aangeplakte tekst af: 'Z2025-00001636voor' → 'Z2025-00001636'."""
    if not z:
        return None
    m = re.match(r"^[A-Za-z]*\d[\w./-]*\d", z.strip())
    return m.group(0) if m else z.strip()


# ── Bundelen ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FaseUitkomst:
    fase: str  # lopend | besloten | gestopt | onbekend
    fase_label: str
    uitkomst: str | None


def bepaal_fase(rijen: Iterable[dict[str, Any]], vandaag: date) -> FaseUitkomst:
    """Fase van een zaak: het laatste bericht dat iets over de fase zegt."""
    met_soort = [(r, soort_van(r.get("type_besluit"), r.get("titel"))) for r in rijen]
    relevant = [(r, s) for r, s in met_soort if s]
    if not relevant:
        return FaseUitkomst("onbekend", "Afloop onbekend", None)
    laatste, soort = max(relevant, key=lambda rs: (rs[0]["datum_publicatie"], _GEWICHT[rs[1]]))
    fase = _FASE_VAN_SOORT[soort]
    if soort == "ingetrokken":
        return FaseUitkomst(fase, "Ingetrokken", "ingetrokken")
    if soort == "buiten_behandeling":
        return FaseUitkomst(fase, "Buiten behandeling", "buiten_behandeling")
    if soort == "melding":
        return FaseUitkomst(fase, "Melding", "melding")
    if soort == "besluit":
        # Het laatste besluit kan een kaal "besluit op aanvraag" zijn terwijl een
        # eerder bericht de uitkomst wel noemt.
        besluiten = sorted(
            (r for r, s in relevant if s == "besluit"),
            key=lambda r: r["datum_publicatie"],
            reverse=True,
        )
        for r in besluiten:
            if r.get("type_besluit") in _UITKOMST:
                uitkomst, label = _UITKOMST[r["type_besluit"]]
                return FaseUitkomst(fase, label, uitkomst)
        return FaseUitkomst(fase, "Besloten", None)
    if (vandaag - laatste["datum_publicatie"]).days > LOPEND_MAX_DAGEN:
        return FaseUitkomst("onbekend", "Afloop onbekend", None)
    return FaseUitkomst(fase, "In behandeling", None)


def zaak_sleutel(rij: dict[str, Any]) -> str:
    """Zaaknummers zijn per bevoegd gezag; zonder nummer is de kennisgeving de zaak."""
    z = normaliseer_zaaknummer(rij.get("zaaknummer_bg"))
    return f"{rij['bg_naam']}|{z}" if z else rij["koop_id"]


# 5.671 straatnamen in de bron beginnen met titelwoorden ("Aanvraag
# omgevingsvergunning Damrak"): de adres-parser van de loader pakt daar te veel.
_STRAAT_RUIS = re.compile(
    r"^(?:(?:aanvraag|besluit|kennisgeving|ontvangst)\s+)?(?:omgevings)?vergunning(?:\s+|$)"
    r"|^(?:aanvraag|besluit|kennisgeving|ontvangst)\s*$",
    re.IGNORECASE,
)


def _adres_van(rij: dict[str, Any]) -> str | None:
    straat = _STRAAT_RUIS.sub("", (rij.get("straatnaam") or "").strip()).strip()
    if not straat:
        return None
    return f"{straat} {(rij.get('huisnummer') or '').strip()}".strip()


def _zelfde_adres(rij: dict[str, Any], postcode: str | None, huisnummer: str | None) -> bool:
    if not postcode or not huisnummer:
        return False
    pc = (rij.get("postcode") or "").replace(" ", "").upper()
    return (
        pc == postcode.replace(" ", "").upper()
        and (rij.get("huisnummer") or "").strip() == huisnummer.strip()
    )


def bundel_tot_zaken(
    rijen: list[dict[str, Any]],
    vandaag: date,
    postcode: str | None = None,
    huisnummer: str | None = None,
) -> list[dict[str, Any]]:
    """Groepeer kennisgevingen tot zaken, nieuwste bericht eerst.

    Verwacht rijen met `afstand_m` (None voor berichten die alleen via het
    zaaknummer zijn bijgehaald en zelf geen geometrie binnen de straal hebben).
    """
    groepen: dict[str, list[dict[str, Any]]] = {}
    for r in rijen:
        groepen.setdefault(zaak_sleutel(r), []).append(r)

    zaken = []
    for sleutel, groep in groepen.items():
        groep.sort(key=lambda r: (r["datum_publicatie"], r["koop_id"]))
        nieuwste = groep[-1]
        f = bepaal_fase(groep, vandaag)

        stappen = [
            {
                "koop_id": r["koop_id"],
                "datum": r["datum_publicatie"],
                "soort": soort_van(r.get("type_besluit"), r.get("titel")) or "overig",
                "titel": r["titel"],
                "preferred_url": r.get("preferred_url"),
                "pdf_url": r.get("pdf_url"),
            }
            for r in groep
        ]

        adres_rij = next((r for r in reversed(groep) if _adres_van(r)), None)
        straat = _adres_van(adres_rij).rsplit(" ", 1)[0] if adres_rij else None
        # Het besluit is doorgaans het best geformuleerd.
        kandidaten = [
            onderwerp_van(r, straat)
            for r in sorted(
                reversed(groep),
                key=lambda r: soort_van(r.get("type_besluit"), r.get("titel")) == "besluit",
                reverse=True,
            )
        ]
        onderwerp = next((k for k in kandidaten if k), None)

        afstanden = [r["afstand_m"] for r in groep if r.get("afstand_m") is not None]
        geo = next((r for r in reversed(groep) if r.get("x") is not None), None)
        start = next((s["datum"] for s in stappen if s["soort"] == "ontvangst"), None)
        besluit = next((s["datum"] for s in reversed(stappen) if s["soort"] == "besluit"), None)
        activiteiten = [r["activiteit_code"] for r in reversed(groep) if r.get("activiteit_code")]
        zaaknummers = [
            normaliseer_zaaknummer(r.get("zaaknummer_bg"))
            for r in reversed(groep)
            if r.get("zaaknummer_bg")
        ]

        zaken.append(
            {
                "zaak_id": sleutel,
                "zaaknummer": zaaknummers[0] if zaaknummers else None,
                "bg_naam": nieuwste["bg_naam"],
                "publicatieblad": nieuwste.get("publicatieblad"),
                "activiteit_code": activiteiten[0] if activiteiten else None,
                "onderwerp": onderwerp,
                "titel": nieuwste["titel"],
                "adres": _adres_van(adres_rij) if adres_rij else None,
                "woonplaats": (adres_rij or nieuwste).get("woonplaats"),
                "x": geo["x"] if geo else None,
                "y": geo["y"] if geo else None,
                "afstand_m": round(min(afstanden)) if afstanden else None,
                "op_adres": any(_zelfde_adres(r, postcode, huisnummer) for r in groep),
                "fase": f.fase,
                "fase_label": f.fase_label,
                "uitkomst": f.uitkomst,
                "datum": nieuwste["datum_publicatie"],
                "doorlooptijd_dagen": (
                    (besluit - start).days if start and besluit and besluit >= start else None
                ),
                "is_afwijk": any(r.get("afwijk_status") == "buitenplans_expliciet" for r in groep),
                "stappen": stappen,
            }
        )
    zaken.sort(key=lambda z: (z["datum"], z["zaak_id"]), reverse=True)
    return zaken
