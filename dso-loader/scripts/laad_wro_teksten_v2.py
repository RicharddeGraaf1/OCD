# -*- coding: utf-8 -*-
"""Wro-planteksten opnieuw laden — volledig, per plan uniek, mét koppeling naar planobjecten.

Waarom een tweede loader naast `src/loaders/ihr_loader.py` (gemeten 2026-10-23,
zie docs/wro-teksten-herstel-plan.md):

1. De oude loader stopt na 100 teksten: hij leest `page.totalElements`, maar IHR v4
   levert alleen `_links.next`. Hilversum Binnenstad 2013: 770 bij IHR, 22 in OCD.
2. De oude sleutel `identificatie` is niet uniek: IHR-tekst-id's
   (`NL.IMRO.PT.regels.s1266`) gelden per plan. `ON CONFLICT DO NOTHING` gooide zo
   stil de teksten van later geladen plannen weg (Earnewâld 2013: 0).
3. De koppeling tekst -> bestemmingsvlak/aanduiding (`_links`) werd niet bewaard,
   dus de API kon per punt niet het artikel van de bestemming kiezen.

Schrijft naar NIEUWE tabellen; de oude `wro.wro_tekst_object` blijft ongemoeid:

    wro.wro_tekst_object_v2   (instrument_idn, identificatie) als sleutel
    wro.wro_tekst_link        tekst -> planobject, per soort link
    wro.wro_tekst_laadstand   per plan: wanneer, hoeveel, fout — maakt hervatten mogelijk

Draaien (alleen tegen de LOKALE database; productie gaat via een aparte overzet):

    python scripts/laad_wro_teksten_v2.py --idn NL.IMRO.0402.11bp00binnenstad-va01
    python scripts/laad_wro_teksten_v2.py --bronhouder gm0402,gm0737
    python scripts/laad_wro_teksten_v2.py --alle [--workers 8] [--opnieuw]

Zonder --opnieuw slaat hij plannen over die al zonder fout geladen zijn.
"""
import argparse
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
from src.db import is_prod_url  # noqa: E402

DB = os.environ.get("DSO_DB") or "postgresql://postgres:postgres@localhost:5434/dso"

DDL = """
CREATE TABLE IF NOT EXISTS wro.wro_tekst_object_v2 (
    instrument_idn  TEXT NOT NULL,
    identificatie   TEXT NOT NULL,
    volgnummer      INT  NOT NULL,
    niveau          INT  NOT NULL,
    parent_id       TEXT NULL,
    object_type     TEXT NOT NULL,
    artikel_type    TEXT NULL,
    label           TEXT NULL,
    nummer          TEXT NULL,
    naam            TEXT NULL,
    inhoud          TEXT NULL,
    PRIMARY KEY (instrument_idn, identificatie)
);
CREATE INDEX IF NOT EXISTS wro_tekst_v2_parent_idx ON wro.wro_tekst_object_v2 (instrument_idn, parent_id);
CREATE TABLE IF NOT EXISTS wro.wro_tekst_link (
    instrument_idn  TEXT NOT NULL,
    tekst_id        TEXT NOT NULL,
    planobject_id   TEXT NOT NULL,
    soort           TEXT NOT NULL,
    PRIMARY KEY (instrument_idn, tekst_id, planobject_id)
);
CREATE INDEX IF NOT EXISTS wro_tekst_link_po_idx ON wro.wro_tekst_link (planobject_id);
CREATE TABLE IF NOT EXISTS wro.wro_tekst_laadstand (
    instrument_idn  TEXT PRIMARY KEY,
    n_teksten       INT  NULL,
    n_links         INT  NULL,
    n_paginas       INT  NULL,
    fout            TEXT NULL,
    geladen_op      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Soorten links die naar een planobject wijzen (IHR v4 _links van een tekst).
LINK_SOORTEN = ("bestemmingsvlakken", "bouwaanduidingen", "functieaanduidingen",
                "gebiedsaanduidingen", "maatvoeringen", "figuren", "bouwvlakken",
                "lettertekenaanduidingen")

ONTWERP = ("ontwerp", "voorontwerp", "concept")


def object_type(titel: str) -> str:
    """Zelfde heuristiek als ihr_loader._determine_object_type, zodat v1 en v2 vergelijkbaar zijn."""
    t = (titel or "").lower().strip()
    if t.startswith("hoofdstuk"):
        return "Hoofdstuk"
    if t.startswith("afdeling"):
        return "Afdeling"
    if t.startswith("paragraaf") or t.startswith("§"):
        return "Paragraaf"
    if t.startswith("artikel") or re.match(r"^\d+\.\d+", t):
        return "Artikel"
    if t.startswith("bijlage"):
        return "Bijlage"
    if t.startswith("toelichting"):
        return "Toelichting"
    if t.startswith("regels"):
        return "Regels"
    return "Overig"


BLOK = re.compile(r"</(p|li|tr|h\d|div)>|<br\s*/?>", re.I)


def schoon(html: str):
    """XML/HTML -> tekst, met een regelovergang per alinea/lijstitem (v1 plakte alles aan elkaar)."""
    if not html:
        return None
    s = BLOK.sub("\n", html)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip() or None


class Ihr:
    def __init__(self, per_seconde: float):
        self.min_tussen = 1.0 / per_seconde
        self.slot = threading.Lock()
        self.laatste = 0.0
        self.client = httpx.Client(headers={"X-Api-Key": cfg.IHR_API_KEY}, timeout=60)
        self.aanroepen = 0

    def get(self, url, params=None):
        for poging in range(6):
            with self.slot:   # globale snelheidsgrens over alle workers
                wacht = self.laatste + self.min_tussen - time.monotonic()
                if wacht > 0:
                    time.sleep(wacht)
                self.laatste = time.monotonic()
                self.aanroepen += 1
            try:
                r = self.client.get(url, params=params)
            except httpx.HTTPError:
                time.sleep(2 ** poging)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(60, 2 ** poging * 2))
                continue
            return r
        raise RuntimeError(f"IHR bleef falen: {url}")


def haal_plan(ihr: Ihr, idn: str):
    """Alle teksten van één plan, via _links.next. -> (teksten, n_paginas) of (None, fout)."""
    url = f"{cfg.IHR_BASE}/plannen/{idn}/teksten"
    params = {"pageSize": 100, "page": 1}
    teksten, paginas = [], 0
    while url:
        r = ihr.get(url, params)
        if r.status_code in (400, 404):
            return [], paginas, f"HTTP {r.status_code}"
        r.raise_for_status()
        d = r.json()
        paginas += 1
        teksten += d.get("_embedded", {}).get("teksten", [])
        nxt = (d.get("_links") or {}).get("next")
        url, params = (nxt["href"], None) if nxt and nxt.get("href") else (None, None)
        if paginas > 200:   # 20.000 teksten: dan klopt er iets niet
            return teksten, paginas, "afgebroken na 200 pagina's"
    return teksten, paginas, None


def rijen(idn, teksten):
    obj, links = [], []
    gezien = set()
    for t in teksten:
        tid = t.get("id")
        if not tid or tid in gezien:
            continue
        gezien.add(tid)
        titel = t.get("titel") or ""
        kruimel = t.get("kruimelpad") or []
        m = re.match(r"(?:Artikel\s+)?(\d+(?:\.\d+)*)", titel, re.I)
        obj.append((idn, tid, t.get("volgnummer") or 0, len(kruimel),
                    kruimel[-1].get("id") if kruimel else None,
                    object_type(titel), t.get("artikelType"),
                    titel[:200] or None, m.group(1) if m else None, titel[:200] or None,
                    schoon(t.get("inhoud"))))
        for soort in LINK_SOORTEN:
            for l in (t.get("_links") or {}).get(soort) or []:
                href = (l or {}).get("href") or ""
                po = href.rstrip("/").rsplit("/", 1)[-1]
                if po:
                    links.append((idn, tid, po, soort))
    # zelfde tekst kan via twee soorten naar hetzelfde object wijzen: één rij per paar
    uniek = {}
    for l in links:
        uniek.setdefault(l[:3], l)
    return obj, list(uniek.values())


def schrijf(conn, idn, obj, links, paginas, fout):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM wro.wro_tekst_link WHERE instrument_idn = %s", (idn,))
            cur.execute("DELETE FROM wro.wro_tekst_object_v2 WHERE instrument_idn = %s", (idn,))
            if obj:
                with cur.copy("""COPY wro.wro_tekst_object_v2 (instrument_idn, identificatie, volgnummer,
                                 niveau, parent_id, object_type, artikel_type, label, nummer, naam, inhoud)
                                 FROM STDIN""") as cp:
                    for r in obj:
                        cp.write_row(r)
            if links:
                with cur.copy("COPY wro.wro_tekst_link (instrument_idn, tekst_id, planobject_id, soort) FROM STDIN") as cp:
                    for r in links:
                        cp.write_row(r)
            cur.execute("""INSERT INTO wro.wro_tekst_laadstand (instrument_idn, n_teksten, n_links, n_paginas, fout, geladen_op)
                           VALUES (%s,%s,%s,%s,%s, now())
                           ON CONFLICT (instrument_idn) DO UPDATE SET n_teksten=EXCLUDED.n_teksten,
                             n_links=EXCLUDED.n_links, n_paginas=EXCLUDED.n_paginas, fout=EXCLUDED.fout,
                             geladen_op=EXCLUDED.geladen_op""",
                        (idn, len(obj), len(links), paginas, fout))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--idn", help="komma-gescheiden plan-idn's")
    ap.add_argument("--bronhouder", help="komma-gescheiden, bv. gm0402,pv25")
    ap.add_argument("--alle", action="store_true", help="alle niet-ontwerp-plannen")
    ap.add_argument("--opnieuw", action="store_true", help="ook plannen die al geladen zijn")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--per-seconde", type=float, default=12.0, help="IHR-aanroepen per seconde, totaal")
    a = ap.parse_args()
    if not (a.idn or a.bronhouder or a.alle):
        ap.error("geef --idn, --bronhouder of --alle")
    if is_prod_url(DB) or os.environ.get("OCD_DB_URL"):
        sys.exit("STOP: dit script laadt alleen lokaal; productie gaat via een aparte overzet.")
    if not cfg.IHR_API_KEY:
        sys.exit("IHR_API_KEY ontbreekt")

    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute(DDL)
        with conn.cursor() as cur:
            if a.idn:
                cur.execute("SELECT idn FROM wro.ruimtelijk_instrument WHERE idn = ANY(%s)", (a.idn.split(","),))
            else:
                waar, arg = ["coalesce(lower(planstatus),'') <> ALL(%s)"], [list(ONTWERP)]
                if a.bronhouder:
                    waar.append("bronhouder = ANY(%s)")
                    arg.append(a.bronhouder.split(","))
                cur.execute("SELECT idn FROM wro.ruimtelijk_instrument WHERE " + " AND ".join(waar)
                            + " ORDER BY bronhouder, idn", arg)
            plannen = [r[0] for r in cur.fetchall()]
            if not a.opnieuw:
                cur.execute("SELECT instrument_idn FROM wro.wro_tekst_laadstand WHERE fout IS NULL")
                klaar = {r[0] for r in cur.fetchall()}
                plannen = [p for p in plannen if p not in klaar]
    print(f"{len(plannen)} plannen te laden, {a.workers} workers, max {a.per_seconde}/s", flush=True)

    ihr = Ihr(a.per_seconde)
    lokaal = threading.local()
    tel = {"plannen": 0, "teksten": 0, "links": 0, "fout": 0}
    slot = threading.Lock()
    t0 = time.time()

    def werk(idn):
        if not hasattr(lokaal, "conn"):
            lokaal.conn = psycopg.connect(DB, autocommit=True)
        try:
            teksten, paginas, fout = haal_plan(ihr, idn)
        except Exception as e:  # noqa: BLE001 — een plan mag de run niet stoppen
            teksten, paginas, fout = [], 0, str(e)[:300]
        obj, links = rijen(idn, teksten)
        schrijf(lokaal.conn, idn, obj, links, paginas, fout)
        with slot:
            tel["plannen"] += 1
            tel["teksten"] += len(obj)
            tel["links"] += len(links)
            tel["fout"] += bool(fout and not fout.startswith("HTTP 404"))
            if tel["plannen"] % 250 == 0 or tel["plannen"] == len(plannen):
                dt = time.time() - t0
                print(f"  {tel['plannen']}/{len(plannen)} plannen · {tel['teksten']} teksten · "
                      f"{tel['links']} links · {tel['fout']} fouten · {ihr.aanroepen} aanroepen · "
                      f"{dt/60:.1f} min ({tel['plannen']/max(dt,1):.1f} plan/s)", flush=True)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for f in as_completed([ex.submit(werk, p) for p in plannen]):
            f.result()
    print("klaar:", tel, flush=True)


if __name__ == "__main__":
    main()
