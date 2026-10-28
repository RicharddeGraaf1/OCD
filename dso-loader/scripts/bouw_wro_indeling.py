# -*- coding: utf-8 -*-
"""Onderwerp-indeling voor Wro-planteksten: v2a.wro_artikel_indeling.

De Wro-tegenhanger van v2a.artikel_indeling (bouw_indeling.py), op de teksten uit
wro.wro_tekst_object_v2 (zie docs/wro-teksten-herstel-plan.md). Drie routes, in
deze volgorde, per tekst:

  bestemming  de tekst (of een voorouder) is gekoppeld aan bestemmingsvlakken;
              hun bestemmingshoofdgroep beslist via WRO_HOOFDGROEP. Bij "waarde",
              "overig" of leeg beslist de naam van de bestemming via REGELS.
  aanduiding  gekoppeld aan een aanduiding (gebieds-, functie-, bouwaanduiding,
              maatvoering, figuur): de naam via REGELS ("geluidzone - industrie").
  regels      niet gekoppeld (begrippen, wijze van meten, algemene regels,
              overgangsrecht): de kopjesketen via ruw_label/terugval, zoals bij Ow.

Leden erven van hun artikel. Dezelfde curatie (curatie/subcategorie_regels.py) als
de Ow-indeling, inclusief de verfijning van de subonderwerpen.

    python scripts/bouw_wro_indeling.py            # alle plannen in v2
    python scripts/bouw_wro_indeling.py --idn NL.IMRO.0402.11bp00binnenstad-va01
"""
import argparse
import importlib.util
import os
import sys
import time
from collections import Counter

import psycopg

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, ".."))
from src.db import is_prod_url  # noqa: E402

DB = os.environ.get("DSO_DB") or "postgresql://postgres:postgres@localhost:5434/dso"

_spec = importlib.util.spec_from_file_location("bouw_indeling", os.path.join(HIER, "bouw_indeling.py"))
bi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bi)
_spec2 = importlib.util.spec_from_file_location("regels", os.path.join(HIER, "..", "curatie", "subcategorie_regels.py"))
regels = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(regels)
HOOFDGROEP = regels.WRO_HOOFDGROEP

DDL = """
CREATE TABLE IF NOT EXISTS v2a.wro_artikel_indeling (
    instrument_idn  TEXT NOT NULL,
    tekst_id        TEXT NOT NULL,
    categorie       TEXT NULL,
    subcategorie    TEXT NULL,
    herkomst        TEXT NULL,
    PRIMARY KEY (instrument_idn, tekst_id)
);
"""

BESTEMMING = ("Enkelbestemming", "Dubbelbestemming")


def op_naam(naam):
    """Naam van een bestemming/aanduiding -> (cat, sub) via REGELS, met verfijning."""
    k = bi.kern(naam or "")
    if not k:
        return None
    uit = bi.cureer(k)
    if uit and uit not in bi.VANGNET and uit[0] not in bi.VANGNET_CATEGORIE:
        return uit[0], bi.verfijn(uit[0], uit[1], [], naam)
    return None


def van_planobjecten(objs):
    """Meerderheid over de gekoppelde planobjecten -> (cat, sub, herkomst) of None."""
    best = [o for o in objs if o[0] in BESTEMMING]
    stemmen = Counter()
    for _type, groep, naam in best:
        uit = HOOFDGROEP.get((groep or "").lower()) or op_naam(naam)
        if uit:
            stemmen[uit] += 1
    if stemmen:
        return stemmen.most_common(1)[0][0] + ("bestemming",)
    for _type, _groep, naam in objs:
        uit = op_naam(naam)
        if uit:
            stemmen[uit] += 1
    if stemmen:
        return stemmen.most_common(1)[0][0] + ("aanduiding",)
    return None


def deel_in(cur, idn):
    cur.execute("""SELECT identificatie, parent_id, label FROM wro.wro_tekst_object_v2
                   WHERE instrument_idn = %s ORDER BY volgnummer""", (idn,))
    teksten = cur.fetchall()
    cur.execute("""SELECT l.tekst_id, po.object_type, po.bestemmingshoofdgroep, po.naam
                   FROM wro.wro_tekst_link l
                   JOIN wro.planobject po ON po.identificatie = l.planobject_id
                   WHERE l.instrument_idn = %s""", (idn,))
    per_tekst = {}
    for tid, typ, groep, naam in cur.fetchall():
        per_tekst.setdefault(tid, []).append((typ, groep, naam))
    ouder = {t[0]: t[1] for t in teksten}
    label = {t[0]: t[2] or "" for t in teksten}
    eigen = {tid: van_planobjecten(objs) for tid, objs in per_tekst.items()}

    rijen = []
    for tid, _p, lb in teksten:
        uit, herkomst, x, keten = None, None, tid, []
        while x is not None:
            if eigen.get(x):
                c, s, h = eigen[x]
                uit, herkomst = (c, s), (h if x == tid else "erfelijk")
                break
            keten.append(label.get(x, ""))
            x = ouder.get(x)
        if not uit:
            segs = list(reversed(keten[1:]))   # voorouders, ondiep -> diep
            u = bi.cureer(bi.ruw_label(segs)) if segs else None
            u = u or bi.terugval(segs, lb)
            if u:
                uit, herkomst = (u[0], bi.verfijn(u[0], u[1], segs, lb)), "regels"
        rijen.append((idn, tid, uit[0] if uit else None, uit[1] if uit else None, herkomst))
    return rijen


def _werk(idns):
    """Eén proces: een reeks plannen indelen en schrijven. -> Counter van herkomst."""
    tel = Counter()
    with psycopg.connect(DB, autocommit=True) as conn, conn.cursor() as cur:
        for idn in idns:
            rijen = deel_in(cur, idn)
            with conn.transaction():
                cur.execute("DELETE FROM v2a.wro_artikel_indeling WHERE instrument_idn = %s", (idn,))
                with cur.copy("""COPY v2a.wro_artikel_indeling (instrument_idn, tekst_id, categorie,
                                 subcategorie, herkomst) FROM STDIN""") as cp:
                    for r in rijen:
                        cp.write_row(r)
            for r in rijen:
                tel[r[4] or "niet ingedeeld"] += 1
    return tel


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--idn")
    ap.add_argument("--bronhouder", help="komma-gescheiden, bv. gm0402,pv25")
    ap.add_argument("--sinds", help="alleen plannen die sinds dit tijdstip (opnieuw) geladen zijn, bv. '2026-10-24 02:00'")
    ap.add_argument("--processen", type=int, default=1,
                    help="parallel over plannen; de regex-regels per tekst zijn het knelpunt (~380 teksten/s per proces)")
    a = ap.parse_args()
    if is_prod_url(DB) or os.environ.get("OCD_DB_URL"):
        sys.exit("STOP: alleen lokaal; productie gaat via een overzet.")
    t0 = time.time()
    with psycopg.connect(DB, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        if a.idn:
            plannen = a.idn.split(",")
        elif a.bronhouder:
            cur.execute("""SELECT ls.instrument_idn FROM wro.wro_tekst_laadstand ls
                           JOIN wro.ruimtelijk_instrument ri ON ri.idn = ls.instrument_idn
                           WHERE ls.fout IS NULL AND ls.n_teksten > 0 AND ri.bronhouder = ANY(%s)""",
                        (a.bronhouder.split(","),))
            plannen = [r[0] for r in cur.fetchall()]
        elif a.sinds:
            cur.execute("""SELECT instrument_idn FROM wro.wro_tekst_laadstand
                           WHERE fout IS NULL AND n_teksten > 0 AND geladen_op >= %s::timestamptz""", (a.sinds,))
            plannen = [r[0] for r in cur.fetchall()]
        else:
            cur.execute("SELECT instrument_idn FROM wro.wro_tekst_laadstand WHERE fout IS NULL AND n_teksten > 0")
            plannen = [r[0] for r in cur.fetchall()]
        if a.processen > 1:
            from multiprocessing import Pool
            brokken = [plannen[i:i + 200] for i in range(0, len(plannen), 200)]
            tel = Counter()
            with Pool(a.processen) as pool:
                for j, t in enumerate(pool.imap_unordered(_werk, brokken), 1):
                    tel.update(t)
                    if j % 10 == 0 or j == len(brokken):
                        tot = sum(tel.values()) or 1
                        print(f"  {min(j*200, len(plannen))}/{len(plannen)} plannen · {tot} teksten · "
                              + " · ".join(f"{k} {100*v/tot:.1f}%" for k, v in tel.most_common())
                              + f" · {(time.time()-t0)/60:.1f} min", flush=True)
            return
        tel = Counter()
        for i, idn in enumerate(plannen, 1):
            rijen = deel_in(cur, idn)
            with conn.transaction():
                cur.execute("DELETE FROM v2a.wro_artikel_indeling WHERE instrument_idn = %s", (idn,))
                with cur.copy("""COPY v2a.wro_artikel_indeling (instrument_idn, tekst_id, categorie,
                                 subcategorie, herkomst) FROM STDIN""") as cp:
                    for r in rijen:
                        cp.write_row(r)
            for r in rijen:
                tel[r[4] or "niet ingedeeld"] += 1
            if i % 2000 == 0 or i == len(plannen):
                tot = sum(tel.values()) or 1
                print(f"  {i}/{len(plannen)} plannen · {tot} teksten · "
                      + " · ".join(f"{k} {100*v/tot:.1f}%" for k, v in tel.most_common())
                      + f" · {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
