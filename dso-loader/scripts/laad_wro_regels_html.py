# -*- coding: utf-8 -*-
"""Wro-planregels uit het HTML-regelsdocument, voor plannen zonder objectgerichte teksten.

Aanvulling op `laad_wro_teksten_v2.py` (zie docs/wro-teksten-herstel-plan.md). Voor
een deel van de plannen levert IHR v4 0 teksten: het plan heeft geen objectgerichte
teksten (`heeftOnderdelen[].heeftObjectgerichteTeksten = []`), alleen een regels-
document als externe referentie, meestal
`https://www.ruimtelijkeplannen.nl/documents/<idn>/r_<idn>.htm`. Voorbeeld:
Bestemmingsplan Earnewâld 2013 (NL.IMRO.0737.04BPI-vg01).

Dit script haalt dat document op, deelt het op in hoofdstuk / artikel / lid / sublid
(de opmaakklassen en nummering van de SVBP-planregels), en schrijft het in dezelfde
v2-tabellen. Identificaties krijgen het voorvoegsel `html:`.

De koppeling tekst -> planobject bestaat in HTML niet. Hij wordt afgeleid uit de
naam: een artikel of lid waarvan de naam EXACT (na normaliseren) gelijk is aan de
naam van een bestemming of aanduiding in hetzelfde plan, krijgt een link met soort
`naam`. Geen gelijkenis, geen gok: liever ongekoppeld (dan geldt de tekst planbreed)
dan aan de verkeerde bestemming.

Alleen tegen de LOKALE database.

    python scripts/laad_wro_regels_html.py --idn NL.IMRO.0737.04BPI-vg01
    python scripts/laad_wro_regels_html.py --alle [--workers 6]
"""
import argparse
import html as htmlmod
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import psycopg
from dotenv import load_dotenv

HIER = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HIER, "..", ".env"))
sys.path.insert(0, os.path.join(HIER, ".."))
from src.config import cfg  # noqa: E402

DB = os.environ.get("DSO_DB") or "postgresql://postgres:postgres@localhost:5434/dso"
MAX_BYTES = 15_000_000

P_RE = re.compile(r"<p\b([^>]*)>(.*?)</p>", re.S | re.I)
CLASS_RE = re.compile(r"""class\s*=\s*["']?([\w-]+)""", re.I)
HOOFDSTUK_RE = re.compile(r"^(hoofdstuk|deel)\s+(\d+[a-z]?)\b\s*(.*)$", re.I)
ARTIKEL_RE = re.compile(r"^artikel\s+(\d+[a-z]?)\.?\s+(.+)$", re.I)
LID_RE = re.compile(r"^(\d+[a-z]?\.\d+[a-z]?)\.?\s+(\S.*)$")
SUBLID_RE = re.compile(r"^(\d+[a-z]?\.\d+[a-z]?\.\d+[a-z]?)\.?\s+(\S.*)$")


def tekst_van(fragment: str) -> str:
    # Een regelovergang in de HTML-bron is witruimte, geen tekstregel: Word breekt
    # koppen af ("Artikel 3 Agrarisch - Bedrijf\ngrondgebonden"). Alleen <br> telt.
    s = re.sub(r"[\r\n]+", " ", fragment)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmlmod.unescape(s).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def naam_sleutel(s: str) -> str:
    s = (s or "").lower().replace("–", "-").replace("—", "-")
    s = re.sub(r"[\"'‘’“”]", "", s)
    # streepjes, schuine strepen en spaties zijn schrijfwijze, geen naam:
    # "Maatschappelijk - sociaal-/cultureel" == "Maatschappelijk - Sociaal cultureel"
    s = re.sub(r"[-/]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" .:;")


def ontleed(html: str):
    """-> lijst van knopen {seq, soort, nummer, label, inhoud[], ouder}."""
    knopen, stapel = [], {}   # stapel: niveau -> knoop
    seq = 0

    def nieuw(niveau, soort, nummer, label):
        nonlocal seq
        seq += 1
        ouder = None
        for n in range(niveau - 1, 0, -1):
            if n in stapel:
                ouder = stapel[n]["id"]
                break
        k = {"id": f"html:{seq}", "seq": seq, "niveau": niveau, "soort": soort,
             "nummer": nummer, "label": label[:200], "inhoud": [], "ouder": ouder}
        for n in list(stapel):
            if n >= niveau:
                del stapel[n]
        stapel[niveau] = k
        knopen.append(k)
        return k

    huidig = None
    # Opknippen bij elke blokcode (p, div, h1-6, li, tr, td, br), niet zoeken naar
    # de sluitcode: Word-exports laten alinea's open (dan slokte een niet-gesloten
    # <p> de volgende koppen op — Earnewâld 2013 art. 3, 4, 13, 40), en andere
    # planprogramma's schrijven <div>/<h2> in plaats van <p> (Borgerswold).
    # Elk stuk tekst tussen twee blokcodes is één regel; zo komt geneste tekst
    # nooit dubbel mee.
    alineas = []
    delen = re.split(r"(<(?:p|div|h[1-6]|li|tr|td|br)\b[^>]*>)", html, flags=re.I)
    attrs = ""
    for d in delen:
        if re.match(r"<(?:p|div|h[1-6]|li|tr|td|br)\b", d, re.I):
            attrs = d
        else:
            alineas.append((attrs, d))
    for attrs, inner in alineas:
        t = tekst_van(inner)
        if not t:
            continue
        cls = (CLASS_RE.search(attrs).group(1).lower() if CLASS_RE.search(attrs) else "")
        kort = len(t) <= 140
        m = HOOFDSTUK_RE.match(t)
        # een kop gaat verder met een hoofdletter (of niets): "Hoofdstuk 2 van deze
        # regels" en "Artikel 12 is niet van toepassing" zijn lopende tekst
        if m and kort and (not m.group(3) or m.group(3)[:1].isupper() or t.isupper()):
            huidig = nieuw(1, "Hoofdstuk", m.group(2), t)
            continue
        m = ARTIKEL_RE.match(t)
        if m and kort and (m.group(2)[:1].isupper() or "artikel" in cls or re.match(r"<h[1-6]", attrs, re.I)):
            huidig = nieuw(2, "Artikel", m.group(1), t)
            continue
        m = SUBLID_RE.match(t)
        if m and kort and huidig is not None:
            huidig = nieuw(4, "Artikel", m.group(1), t)
            continue
        m = LID_RE.match(t)
        if m and kort and huidig is not None and ("lid" in cls or "artikel" in cls or len(t) <= 90):
            huidig = nieuw(3, "Artikel", m.group(1), t)
            continue
        if huidig is not None:
            huidig["inhoud"].append(t)
    # Inhoudsopgaven en navigatie (elke pagina van een _Index-plan herhaalt ze) zijn
    # koppen zonder tekst en zonder kinderen. Snoeien, van onder naar boven, tot het
    # niet meer verandert: een hoofdstuk uit de navigatie verliest zo eerst zijn
    # navigatie-artikelen en valt dan zelf weg.
    while True:
        heeft_kind = {k["ouder"] for k in knopen if k["ouder"]}
        weg = {k["id"] for k in knopen if not k["inhoud"] and k["id"] not in heeft_kind}
        if not weg:
            break
        knopen = [k for k in knopen if k["id"] not in weg]
    # Een onderdeel dat een tweede keer voorkomt (zelfde niveau, nummer en kop)
    # valt met al zijn kinderen weg: overlappende pagina's zonder wortel.
    gezien, weg = set(), set()
    for k in knopen:
        if k["ouder"] in weg:
            weg.add(k["id"])
            continue
        sleutel = (k["niveau"], k["nummer"], k["label"])
        if sleutel in gezien:
            weg.add(k["id"])
        else:
            gezien.add(sleutel)
    return [k for k in knopen if k["id"] not in weg]


class Net:
    def __init__(self, per_seconde):
        self.min_tussen = 1.0 / per_seconde
        self.slot = threading.Lock()
        self.laatste = 0.0
        self.ihr = httpx.Client(headers={"X-Api-Key": cfg.IHR_API_KEY}, timeout=60)
        self.web = httpx.Client(timeout=120, follow_redirects=True,
                                headers={"User-Agent": "OCD-dso-loader (wro-regels-html)"})

    def wacht(self):
        with self.slot:
            w = self.laatste + self.min_tussen - time.monotonic()
            if w > 0:
                time.sleep(w)
            self.laatste = time.monotonic()

    def get(self, client, url):
        for poging in range(5):
            self.wacht()
            try:
                r = client.get(url)
            except httpx.HTTPError:
                time.sleep(2 ** poging)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(60, 2 ** poging * 2))
                continue
            return r
        raise RuntimeError(f"bleef falen: {url}")


def decodeer(inhoud: bytes) -> str:
    m = re.search(rb"charset=([\w-]+)", inhoud[:4000])
    enc = m.group(1).decode() if m else "windows-1252"
    try:
        return inhoud.decode(enc, errors="replace")
    except LookupError:
        return inhoud.decode("windows-1252", errors="replace")


def regels_url(net, idn):
    r = net.get(net.ihr, f"{cfg.IHR_BASE}/plannen/{idn}")
    if r.status_code != 200:
        return None, f"IHR plan HTTP {r.status_code}"
    urls = []
    for o in r.json().get("heeftOnderdelen") or []:
        if (o.get("type") or "").lower() == "regels":
            urls += o.get("externeReferenties") or []
    htm = [u for u in urls if re.search(r"\.html?$", u, re.I)]
    if htm:
        return htm[0], None
    return None, ("alleen pdf" if urls else "geen regelsdocument")


def verwerk(net, conn, idn):
    url, reden = regels_url(net, idn)
    if not url:
        return None, reden
    r = net.get(net.web, url)
    if r.status_code != 200:
        return None, f"document HTTP {r.status_code}"
    if len(r.content) > MAX_BYTES:
        return None, f"document te groot ({len(r.content)} bytes)"
    html = decodeer(r.content)
    # Een _Index.html is een inhoudsopgave: elk onderdeel staat op een eigen pagina
    # (r_<idn>_0005Tuin.html) in dezelfde map. Haal die in volgorde op en plak ze aan elkaar.
    if re.search(r"_index\.html?$", url, re.I):
        basis = url.rsplit("/", 1)[0] + "/"
        paginas = []
        for href in re.findall(r"""href\s*=\s*["']([^"'#]+\.html?)""", html, re.I):
            if re.match(r"r_.*_\d{3,4}", href, re.I) and href not in paginas:
                paginas.append(href)
        if not paginas:
            return None, "index zonder onderdelen"
        # De pagina's zijn hiërarchisch: _0000Regels bevat alles, _0001Inleidenderegels
        # een deel daarvan, enz. Is de wortel er, lees dan alleen die (anders
        # alles drie keer — Midsbuorren 36a: 45 artikelen voor 15).
        # Maar soms is de wortel een voorblad (BPDA2011INBR1: 1 artikel): dan telt hij
        # alleen als hij minstens 80% van de artikelen uit de inhoudsopgave bevat.
        in_index = len(set(re.findall(r"Artikel\s+\d+[a-z]?\b", tekst_van(html), re.I)))
        wortel = [h for h in paginas if re.search(r"_0000", h)]
        html = None
        if wortel:
            rw = net.get(net.web, basis + wortel[0])
            if rw.status_code == 200:
                kandidaat = decodeer(rw.content)
                n_art = sum(1 for k in ontleed(kandidaat) if k["niveau"] == 2)
                if n_art >= 0.8 * max(in_index, 1):
                    html = kandidaat
        if html is None:
            stukken = []
            for href in paginas[:400]:
                rp = net.get(net.web, basis + href)
                if rp.status_code == 200:
                    stukken.append(decodeer(rp.content))
            html = "\n".join(stukken)
    knopen = ontleed(html)
    if not knopen:
        return None, "geen structuur herkend"

    with conn.cursor() as cur:
        cur.execute("""SELECT identificatie, naam FROM wro.planobject
                       WHERE instrument_idn = %s AND naam IS NOT NULL
                         AND object_type IN ('Enkelbestemming','Dubbelbestemming','Gebiedsaanduiding',
                                             'Functieaanduiding','Bouwaanduiding')""", (idn,))
        per_naam = {}
        for po, naam in cur.fetchall():
            per_naam.setdefault(naam_sleutel(naam), []).append(po)

    obj, links = [], []
    for k in knopen:
        inhoud = "\n".join(k["inhoud"]) or None
        obj.append((idn, k["id"], k["seq"], k["niveau"], k["ouder"], k["soort"], None,
                    k["label"], k["nummer"], k["label"], inhoud))
        if k["soort"] == "Artikel" and k["niveau"] in (2, 3):
            naam = re.sub(r"^(artikel\s+)?\d+[a-z]?(\.\d+[a-z]?)*\.?\s+", "", k["label"], flags=re.I)
            for po in per_naam.get(naam_sleutel(naam), []):
                links.append((idn, k["id"], po, "naam"))
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM wro.wro_tekst_link WHERE instrument_idn = %s", (idn,))
            cur.execute("DELETE FROM wro.wro_tekst_object_v2 WHERE instrument_idn = %s", (idn,))
            with cur.copy("""COPY wro.wro_tekst_object_v2 (instrument_idn, identificatie, volgnummer,
                             niveau, parent_id, object_type, artikel_type, label, nummer, naam, inhoud)
                             FROM STDIN""") as cp:
                for row in obj:
                    cp.write_row(row)
            if links:
                with cur.copy("COPY wro.wro_tekst_link (instrument_idn, tekst_id, planobject_id, soort) FROM STDIN") as cp:
                    for row in {l[:3]: l for l in links}.values():
                        cp.write_row(row)
            cur.execute("""UPDATE wro.wro_tekst_laadstand SET n_teksten = %s, n_links = %s, bron = 'html',
                                  bron_url = %s, fout = NULL, geladen_op = now()
                           WHERE instrument_idn = %s""", (len(obj), len(links), url, idn))
    return (len(obj), len(links), len(per_naam)), None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--idn")
    ap.add_argument("--alle", action="store_true", help="alle plannen met 0 objectgerichte teksten")
    ap.add_argument("--herkansen", action="store_true",
                    help="ook plannen die eerder 'geen structuur herkend' of een fout gaven")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--per-seconde", type=float, default=6.0)
    a = ap.parse_args()
    if "rlwy.net" in DB or os.environ.get("OCD_DB_URL"):
        sys.exit("STOP: dit script laadt alleen lokaal.")
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute("ALTER TABLE wro.wro_tekst_laadstand ADD COLUMN IF NOT EXISTS bron TEXT NOT NULL DEFAULT 'ihr-teksten'")
        conn.execute("ALTER TABLE wro.wro_tekst_laadstand ADD COLUMN IF NOT EXISTS bron_url TEXT NULL")
        conn.execute("ALTER TABLE wro.wro_tekst_laadstand ADD COLUMN IF NOT EXISTS html_reden TEXT NULL")
        with conn.cursor() as cur:
            if a.idn:
                plannen = a.idn.split(",")
            else:
                cur.execute("""SELECT instrument_idn FROM wro.wro_tekst_laadstand
                               WHERE n_teksten = 0 AND fout IS NULL
                                 AND (html_reden IS NULL OR (%s AND (html_reden = 'geen structuur herkend'
                                                                     OR html_reden LIKE 'fout:%%')))
                               ORDER BY instrument_idn""", (a.herkansen,))
                plannen = [r[0] for r in cur.fetchall()]
    print(f"{len(plannen)} plannen zonder objectgerichte teksten", flush=True)
    net = Net(a.per_seconde)
    lokaal = threading.local()
    tel = {"plannen": 0, "html": 0, "teksten": 0, "links": 0}
    redenen = {}
    slot = threading.Lock()
    t0 = time.time()

    def werk(idn):
        if not hasattr(lokaal, "conn"):
            lokaal.conn = psycopg.connect(DB, autocommit=True)
        try:
            uit, reden = verwerk(net, lokaal.conn, idn)
        except Exception as e:  # noqa: BLE001
            uit, reden = None, ("fout: " + str(e))[:200]
        if reden:
            lokaal.conn.execute("UPDATE wro.wro_tekst_laadstand SET html_reden = %s WHERE instrument_idn = %s",
                                (reden[:200], idn))
        with slot:
            tel["plannen"] += 1
            if uit:
                tel["html"] += 1
                tel["teksten"] += uit[0]
                tel["links"] += uit[1]
            else:
                k = reden.split(" (")[0].split(":")[0]
                redenen[k] = redenen.get(k, 0) + 1
            if tel["plannen"] % 200 == 0 or tel["plannen"] == len(plannen):
                print(f"  {tel['plannen']}/{len(plannen)} · html {tel['html']} · {tel['teksten']} teksten · "
                      f"{tel['links']} links · overgeslagen {redenen} · {(time.time()-t0)/60:.1f} min", flush=True)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for f in as_completed([ex.submit(werk, p) for p in plannen]):
            f.result()
    print("klaar:", tel, redenen, flush=True)


if __name__ == "__main__":
    main()
