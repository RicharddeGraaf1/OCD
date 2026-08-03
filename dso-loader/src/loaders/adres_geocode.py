"""Adres-geocodering via de PDOK Locatieserver, met cache in de database.

WAAROM DIT BESTAAT
------------------
KOOP levert per bekendmaking vaak meerdere `<ow:gebiedsmarkering>`-blokken —
bij blok-zaken één per pand. Zie bijvoorbeeld gmb-2024-503409: 16 punten voor
één besluit. `select_geometry_candidate` in koop_vergunning.py moet daar één
uit kiezen, maar had tot 2026-08 geen enkel aanknopingspunt om te bepalen
wélke bij het huisnummer van de publicatie hoort: het greep pas in bij >1 km
onderlinge afstand (de Woerden-pin, gaps G-87) en pakte daaronder `cands[0]`.

Gemeten in postcodegebied 1097 (Amsterdam), 80 records met meerdere kandidaten
die >10 m uiteen liggen: bij slechts 6 was de eerste kandidaat ook de dichtste
bij het eigen adres; bij 22 lag een andere kandidaat >25 m dichterbij, meestal
op 0 m. Het juiste punt zit dus wél in de bron — het werd alleen niet gekozen.

Om te weten wélke kandidaat bij "Veeteeltstraat 20" hoort, moet je weten waar
dat adres ligt. Daar is deze module voor.

ONTWERPKEUZE — de geocode kiest, maar levert niet
-------------------------------------------------
We slaan het BAG-punt **niet** op als geometrie. Het register toont wat het
bevoegd gezag publiceerde; zou het onze geocode tonen, dan staat er een
positie die in geen enkele bron voorkomt. De geocode wordt alleen gebruikt om
uit de door de bronhouder aangeleverde kandidaten de dichtstbijzijnde te
kiezen. Ligt geen kandidaat binnen `MAX_ADRES_AFSTAND_M`, dan grijpen we niet
in en houdt de bestaande selectie het laatste woord.

CACHE
-----
`vth.adres_geocode` is de cache, met de genormaliseerde vraag als sleutel.
Ook niet-gevonden adressen worden gecached (`gevonden = false`), anders vraagt
elke herhaalde run ze opnieuw op. De cache is het geheugen van de PDOK-
aanroepen: een tweede backfill over dezelfde wijk doet nul netwerkverkeer.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Optional

PDOK_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

# Boven deze afstand geloven we de koppeling adres <-> kandidaat niet meer en
# laten we de bestaande keuze staan. 250 m is ruim genoeg voor een lang
# bouwblok, en krap genoeg om een pin in een andere buurt niet te "bevestigen".
MAX_ADRES_AFSTAND_M = 250.0

# Alleen ingrijpen als het echt uitmaakt. Onder deze winst is de verschuiving
# ruis (punt versus omhullend vlak van hetzelfde pand).
MIN_WINST_M = 15.0

_PAUZE_S = 0.1  # PDOK is een gratis publieke dienst; niet rammen.
_POSTCODE_RE = re.compile(r"^\s*(\d{4})\s*([A-Za-z]{2})\s*$")
_TOEVOEGING_RE = re.compile(r"^[A-Za-z]{1,2}$|^\d{1,4}$")

DDL = """
CREATE TABLE IF NOT EXISTS vth.adres_geocode (
    vraag         text PRIMARY KEY,
    gevonden      boolean     NOT NULL,
    lon           double precision,
    lat           double precision,
    rd_x          double precision,
    rd_y          double precision,
    weergavenaam  text,
    opgehaald_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE vth.adres_geocode IS
  'Cache van PDOK-Locatieserver-antwoorden. Gebruikt om bij meerdere '
  'gebiedsmarkeringen de kandidaat te kiezen die bij het huisnummer hoort; '
  'de gecachete coordinaten worden zelf nooit als geometrie opgeslagen.';
"""


def zorg_voor_cache(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def bouw_vraag(
    straatnaam: Optional[str],
    huisnummer: Optional[str],
    huisletter: Optional[str],
    huisnummertoevoeging: Optional[str],
    postcode: Optional[str],
    woonplaats: Optional[str],
    gemeente: Optional[str] = None,
) -> Optional[str]:
    """Bouw een genormaliseerde zoekvraag, of None als het adres te mager is.

    Vangt onderweg een parsefout in de bron af: bij een deel van de records
    staat de postcode in `huisnummertoevoeging` en is `postcode` leeg (zie
    gmb-2024-377279, toevoeging "1097WV"). Die verhuist hier naar de postcode
    in plaats van als onzin aan het huisnummer geplakt te worden.
    """
    if not straatnaam or not huisnummer:
        return None

    toev = (huisnummertoevoeging or "").strip()
    pc = (postcode or "").strip()
    m = _POSTCODE_RE.match(toev)
    if m:
        if not pc:
            pc = f"{m.group(1)}{m.group(2).upper()}"
        toev = ""
    if toev and not _TOEVOEGING_RE.match(toev):
        toev = ""

    letter = (huisletter or "").strip()
    if letter and not _TOEVOEGING_RE.match(letter):
        letter = ""

    plaats = (woonplaats or gemeente or "").strip()
    if not plaats and not pc:
        return None

    nummer = f"{str(huisnummer).strip()}{letter}{toev}"
    pcm = _POSTCODE_RE.match(pc)
    pc_norm = f"{pcm.group(1)}{pcm.group(2).upper()}" if pcm else ""
    delen = [f"{straatnaam.strip()} {nummer}"]
    if pc_norm:
        delen.append(pc_norm)
    if plaats:
        delen.append(plaats)
    return ", ".join(delen)


def _bevraag_pdok(vraag: str) -> Optional[dict]:
    url = PDOK_URL + "?" + urllib.parse.urlencode({
        "q": vraag, "fq": "type:adres", "rows": 1,
        "fl": "weergavenaam,centroide_ll,centroide_rd",
    })
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
    except Exception:
        return None
    docs = data.get("response", {}).get("docs") or []
    if not docs:
        return None
    d = docs[0]
    ll = d.get("centroide_ll", "").replace("POINT(", "").replace(")", "").split()
    rd = d.get("centroide_rd", "").replace("POINT(", "").replace(")", "").split()
    if len(ll) != 2:
        return None
    return {
        "lon": float(ll[0]), "lat": float(ll[1]),
        "rd_x": float(rd[0]) if len(rd) == 2 else None,
        "rd_y": float(rd[1]) if len(rd) == 2 else None,
        "weergavenaam": d.get("weergavenaam"),
    }


class Geocoder:
    """Geocodeert adressen, met de databasecache ertussen.

    Houdt de tellers bij zodat een backfill kan rapporteren hoeveel er
    daadwerkelijk over het netwerk ging — handig om te zien of een tweede run
    inderdaad volledig uit de cache komt.
    """

    def __init__(self, conn, sta_netwerk_toe: bool = True):
        self.conn = conn
        self.sta_netwerk_toe = sta_netwerk_toe
        self.uit_cache = 0
        self.opgehaald = 0
        self.niet_gevonden = 0

    def __call__(self, vraag: Optional[str]) -> Optional[dict]:
        if not vraag:
            return None
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT gevonden, lon, lat, rd_x, rd_y, weergavenaam "
                "FROM vth.adres_geocode WHERE vraag = %s", (vraag,))
            rij = cur.fetchone()
        if rij is not None:
            self.uit_cache += 1
            return None if not rij["gevonden"] else dict(rij)

        if not self.sta_netwerk_toe:
            return None

        res = _bevraag_pdok(vraag)
        time.sleep(_PAUZE_S)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO vth.adres_geocode "
                "  (vraag, gevonden, lon, lat, rd_x, rd_y, weergavenaam) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (vraag) DO NOTHING",
                (vraag, res is not None,
                 res and res["lon"], res and res["lat"],
                 res and res["rd_x"], res and res["rd_y"],
                 res and res["weergavenaam"]))
        self.conn.commit()
        if res is None:
            self.niet_gevonden += 1
        else:
            self.opgehaald += 1
        return res


def kies_op_adres(
    cands: list[dict],
    geocode: Optional[dict],
    huidig_rd: Optional[tuple[float, float]] = None,
) -> tuple[Optional[dict], Optional[float], Optional[float]]:
    """Kies de kandidaat die het dichtst bij het geocodeerde adres ligt.

    Geeft (kandidaat, afstand_huidig_m, afstand_nieuw_m). Kandidaat is None
    wanneer er niets te kiezen valt, de geocode ontbreekt, of geen enkele
    kandidaat binnen MAX_ADRES_AFSTAND_M ligt — in dat laatste geval vertrouwen
    we onze eigen koppeling niet en blijft de bestaande selectie staan.
    """
    if not geocode or geocode.get("rd_x") is None:
        return None, None, None
    doel = (geocode["rd_x"], geocode["rd_y"])

    bruikbaar = [c for c in cands
                 if c.get("rd_x") is not None and c.get("rd_y") is not None]
    if len(bruikbaar) < 2:
        return None, None, None

    def afstand(p: tuple[float, float]) -> float:
        return ((p[0] - doel[0]) ** 2 + (p[1] - doel[1]) ** 2) ** 0.5

    beste = min(bruikbaar, key=lambda c: afstand((c["rd_x"], c["rd_y"])))
    d_nieuw = afstand((beste["rd_x"], beste["rd_y"]))
    if d_nieuw > MAX_ADRES_AFSTAND_M:
        return None, None, d_nieuw

    d_huidig = afstand(huidig_rd) if huidig_rd else None
    return beste, d_huidig, d_nieuw
