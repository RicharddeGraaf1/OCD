"""Meet welke routes een artikel of divisietekst aan een onderwerp kunnen helpen.

Read-only. Vergelijkt per corpus de **objectroute** (IMOW-annotatie, activiteit,
gebiedsaanwijzing, IMRO-bestemming) met de **padroute** die vandaag in
`v2a.artikel_indeling` zit, en rapporteert dekking én overeenstemming.

Aanleiding: de vraag of een objectgebaseerde indeling ("variant B") naast of in
plaats van de padgebaseerde indeling moet komen. Onderbouwing en uitkomst van de
eerste run staan in de vault-analyse `Objectroutes naast de padroute in de
categorie-indeling` (OmgevingswetKnowledgeBase) en in
`docs/indeling-objectroutes-plan.md`.

Drie corpora, elk met een eigen eenheid:

  ow          artikelen in artikelstructuur-regelingen (v2a.artikel_indeling)
  wro         Artikel-objecten in IMRO-plannen (wro.wro_tekst_object)
  vrijetekst  divisieteksten in RegelingVrijetekst (p2p.tekst_element)

Gebruik:
    python scripts/meet_indelingsroutes.py                 # alle drie
    python scripts/meet_indelingsroutes.py --corpus ow
    python scripts/meet_indelingsroutes.py --json logs/indelingsroutes.json

De Wro-meting leest ~800k tekstobjecten en ~1,2M planobjectnamen in geheugen
(~1 GB piek) en duurt enkele minuten; de andere twee zijn seconden.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg.rows import tuple_row

from src.db import get_conn

# --- Kruistabel IMOW-waardelijst -> thema ------------------------------------
# Afgeleid uit de waardelijsten IMOW 5.1.0 zoals gegroepeerd in de vault-analyse
# `IMOW Thema-categorisatie waardelijsten`. `functie`, `ruimtelijk gebruik` en
# `beperkingengebied` zijn juridische typen, geen onderwerp: die worden apart
# geteld (ga_jur) in plaats van als thema meegerekend.
TYPE2THEMA = {'bodem': 'bodem',
 'mijnbouw': 'bodem',
 'bouw': 'bouwen',
 'energievoorziening': 'energie',
 'erfgoed': 'erfgoed',
 'geluid': 'geluid',
 'leiding': 'infrastructuur',
 'landschap': 'landschap',
 'lucht': 'lucht',
 'geur': 'milieu',
 'verkeer': 'mobiliteit',
 'natuur': 'natuur',
 'functie': 'planologisch gebruik',
 'ruimtelijk gebruik': 'planologisch gebruik',
 'recreatie': 'recreatie',
 'defensie': 'veiligheid',
 'externe veiligheid': 'veiligheid',
 'water en watersysteem': 'water',
 'beperkingengebied': 'geen thema gevonden'}

ACT2THEMA = {'bodemactiviteit': 'bodem',
 'mijnbouwlocatieactiviteit': 'bodem',
 'ontgrondingsactiviteit': 'bodem',
 'bouwactiviteit ruimtelijk': 'bouwen',
 'bouwactiviteit technisch': 'bouwen',
 'gebruiken-van-bouwwerkenactiviteit': 'bouwen',
 'in-stand-houden-van-bouwwerkenactiviteit': 'bouwen',
 'sloopactiviteit': 'bouwen',
 'cultuuractiviteit': 'economie',
 'dienstverleningsactiviteit': 'economie',
 'evenementactiviteit': 'economie',
 'exploitatieactiviteit bedrijf': 'economie',
 'exploitatieactiviteit detailhandel': 'economie',
 'exploitatieactiviteit horeca': 'economie',
 'exploitatieactiviteit kantoor': 'economie',
 'festiviteitactiviteit': 'economie',
 'maatschappelijke activiteit': 'economie',
 'reclameactiviteit': 'economie',
 'standplaatsactiviteit': 'economie',
 'cultureel-erfgoedactiviteit': 'erfgoed',
 'monumentenactiviteit': 'erfgoed',
 'rijksmonumentenactiviteit': 'erfgoed',
 'werelderfgoedactiviteit': 'erfgoed',
 'aansluitactiviteit': 'infrastructuur',
 'beperkingengebiedactiviteit m.b.t. een leiding': 'infrastructuur',
 'agrarische activiteit': 'landbouw',
 'landinrichtingsactiviteit': 'landbouw',
 'lozingsactiviteit': 'milieu',
 'milieubelastende activiteit': 'milieu',
 'milieubelastende activiteit met beperkt effect': 'milieu',
 'milieubelastende activiteit overig': 'milieu',
 'opslagactiviteit': 'milieu',
 'stortingsactiviteit': 'milieu',
 'beperkingengebiedactiviteit m.b.t. een luchthaven': 'mobiliteit',
 'beperkingengebiedactiviteit m.b.t. een spoorweg': 'mobiliteit',
 'beperkingengebiedactiviteit m.b.t. een weg': 'mobiliteit',
 'parkeeractiviteit': 'mobiliteit',
 'uitritactiviteit': 'mobiliteit',
 'wegactiviteit': 'mobiliteit',
 'dierenactiviteit': 'natuur',
 'flora- en fauna-activiteit': 'natuur',
 'jachtgeweeractiviteit': 'natuur',
 'kapactiviteit': 'natuur',
 'natura 2000-activiteit': 'natuur',
 'natuuractiviteit': 'natuur',
 'toegangsactiviteit': 'natuur',
 'valkeniersactiviteit': 'natuur',
 'herstructureringsactiviteit': 'planologisch gebruik',
 'moderniseringsactiviteit': 'planologisch gebruik',
 'planologische gebruiksactiviteit': 'planologisch gebruik',
 'gelegenheid-tot-zwemmen-en-baden-biedenactiviteit': 'recreatie',
 'kampeeractiviteit': 'recreatie',
 'ontspanningsactiviteit': 'recreatie',
 'recreatieactiviteit': 'recreatie',
 'sportactiviteit': 'recreatie',
 'alarminstallatieactiviteit': 'veiligheid',
 'beperkingengebiedactiviteit m.b.t. een bergingsgebied': 'water',
 'beperkingengebiedactiviteit m.b.t. een installatie in een waterstaatswerk': 'water',
 'beperkingengebiedactiviteit m.b.t. een oppervlaktewaterlichaam': 'water',
 'beperkingengebiedactiviteit m.b.t. een waterkering': 'water',
 'beperkingengebiedactiviteit m.b.t. een waterstaatswerk': 'water',
 'beperkingengebiedactiviteit m.b.t. een zuiveringtechnisch werk': 'water',
 'beperkingengebiedactiviteit m.b.t. grondwater': 'water',
 'beperkingengebiedactiviteit m.b.t. zeegebied': 'water',
 'grondwateractiviteit': 'water',
 'hemelwateractiviteit': 'water',
 'noordzee-activiteit': 'water',
 'wateronttrekkingsactiviteit': 'water',
 'waterstaatswerkenactiviteit': 'water',
 'ligplaatsactiviteit': 'wonen',
 'woonactiviteit': 'wonen',
 'beperkingengebiedactiviteit': 'geen thema gevonden',
 'grafactiviteit': 'geen thema gevonden',
 'overig': 'geen thema gevonden',
 'verrichten-van-werken-en-werkzaamhedenactiviteit': 'geen thema gevonden'}

GA_JURIDISCH = {"functie", "ruimtelijk gebruik", "beperkingengebied"}

# Vervallen IMOW-thema's -> hun actieve opvolger.
DEPRECATED = {
    "bouwwerken": "bouwen",
    "cultureel erfgoed": "erfgoed",
    "water en watersystemen": "water",
    "milieu algemeen": "milieu",
    "landgebruik": "planologisch gebruik",
    "energie en natuurlijke hulpbronnen": "energie",
    "externe veiligheid": "veiligheid",
}


def norm_thema(t: str) -> str:
    """`WaterEnWatersystemen` en `water en watersystemen` zijn hetzelfde thema."""
    t = re.sub(r"(?<!^)(?=[A-Z])", " ", t).lower().strip()
    return DEPRECATED.get(t, t)


def pct(n, d):
    return f"{100 * n / d:.1f}%" if d else "-"


# --- Ow: artikelen in artikelstructuur ---------------------------------------

def meet_ow(conn, out):
    cur = conn.cursor(row_factory=tuple_row)
    cur.execute("""
        CREATE TEMP TABLE art AS
        SELECT ai.tekst_element_id id, ai.categorie pad_cat, r.documenttype dt,
               pb.nb >= 50 AS landelijk
          FROM v2a.artikel_indeling ai
          JOIN p2p.regeling r ON r.frbr_expression = ai.regeling_expression
          JOIN (SELECT ai2.pad_sleutel, count(DISTINCT r2.bronhouder) nb
                  FROM v2a.artikel_indeling ai2
                  JOIN p2p.regeling r2 ON r2.frbr_expression = ai2.regeling_expression
                 GROUP BY 1) pb ON pb.pad_sleutel = ai.pad_sleutel""")
    cur.execute("CREATE INDEX ON art(id)")
    # Een juridische regel hangt aan een artikel of aan een lid daarvan; klim op
    # tot het artikel, want dat is de eenheid waarop de indeling wordt getoond.
    cur.execute("""
        CREATE TEMP TABLE jr_art AS
        SELECT jr.identificatie jr_id, jr.thema,
               CASE WHEN te.element_type = 'Artikel' THEN te.id
                    WHEN p.element_type  = 'Artikel' THEN p.id
                    WHEN g.element_type  = 'Artikel' THEN g.id END art_id
          FROM p2p.juridische_regel jr
          JOIN p2p.tekst_element te ON te.regeling_expression = jr.regeling_expression
                                   AND te.wid = jr.regeltekst_wid
          LEFT JOIN p2p.tekst_element p ON p.id = te.parent_id
          LEFT JOIN p2p.tekst_element g ON g.id = p.parent_id""")
    cur.execute("CREATE INDEX ON jr_art(jr_id)")
    cur.execute("CREATE INDEX ON jr_art(art_id)")
    rows = cur.execute("""
        SELECT a.id, a.pad_cat, a.dt, a.landelijk,
          (SELECT array_agg(DISTINCT t) FROM jr_art j, unnest(j.thema) t WHERE j.art_id = a.id),
          (SELECT count(*) FROM jr_art j WHERE j.art_id = a.id),
          (SELECT array_agg(DISTINCT ga.type) FROM jr_art j
             JOIN p2p.juridische_regel_gebiedsaanwijzing x ON x.juridische_regel_id = j.jr_id
             JOIN p2p.gebiedsaanwijzing ga ON ga.identificatie = x.gebiedsaanwijzing_id
            WHERE j.art_id = a.id),
          (SELECT array_agg(DISTINCT act.groep) FROM jr_art j
             JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = j.jr_id
             JOIN p2p.activiteit act ON act.identificatie = ala.activiteit_id
            WHERE j.art_id = a.id)
          FROM art a""").fetchall()

    per_groep, overeen, zonder_groep = defaultdict(Counter), defaultdict(Counter), Counter()
    for _id, pad, dt, land, themas, n_jr, ga_types, act_groepen in rows:
        th = {norm_thema(x) for x in (themas or [])}
        ga, ga_jur = set(), False
        for t in ga_types or []:
            t = (t or "").lower()
            if t in GA_JURIDISCH:
                ga_jur = True
            elif t in TYPE2THEMA:
                ga.add(TYPE2THEMA[t])
        act = {ACT2THEMA[g.lower()] for g in act_groepen or []
               if g and g.lower() in ACT2THEMA and ACT2THEMA[g.lower()] != "geen thema gevonden"}
        obj = th | ga | act
        groep = dt if dt != "Omgevingsplan" else (
            "Omgevingsplan - landelijk pad" if land else "Omgevingsplan - eigen pad")
        for g in (groep, "TOTAAL"):
            c = per_groep[g]
            c["n"] += 1
            c["jr"] += n_jr > 0
            c["thema-annotatie"] += bool(th)
            c["gebiedsaanwijzing"] += bool(ga)
            c["ga-juridisch type"] += ga_jur
            c["activiteit"] += bool(act)
            c["objectthema"] += bool(obj)
            c["padroute"] += bool(pad)
            c["samen"] += bool(pad or obj)
        if not pad:
            zonder_groep["n"] += 1
            zonder_groep["objectthema"] += bool(obj)
        for route, s in (("thema-annotatie", th), ("gebiedsaanwijzing", ga),
                         ("activiteit", act), ("unie", obj)):
            if s and pad:
                overeen[route]["n"] += 1
                overeen[route]["pad in set"] += pad in s
    out["ow"] = {"per_groep": {k: dict(v) for k, v in per_groep.items()},
                 "overeenstemming": {k: dict(v) for k, v in overeen.items()},
                 "zonder_padcategorie": dict(zonder_groep)}

    kolommen = ["jr", "thema-annotatie", "gebiedsaanwijzing", "ga-juridisch type",
                "activiteit", "objectthema", "padroute", "samen"]
    print("\n=== Ow-artikelen: dekking per route ===")
    print(" | ".join(["groep", "n"] + kolommen))
    for g, c in sorted(per_groep.items(), key=lambda x: -x[1]["n"]):
        print(" | ".join([g, f"{c['n']:,}"] + [pct(c[k], c["n"]) for k in kolommen]))
    print("\n=== Ow: overeenstemming objectroute met padroute (niveau 1) ===")
    for route, c in overeen.items():
        print(f"{route}: n={c['n']:,} pad in objectset {pct(c['pad in set'], c['n'])}")
    print(f"artikelen zonder padcategorie: {zonder_groep['n']:,}, "
          f"daarvan objectthema {pct(zonder_groep['objectthema'], zonder_groep['n'])}")


# --- Wro: artikelen in IMRO-plannen -------------------------------------------

def _naam_sleutel(s: str) -> str:
    """`Gemengd-1`, `Gemengd - 1` en `gemengd 1` moeten dezelfde sleutel geven."""
    return re.sub(r"[\s\-–—_:,.()]+", "", (s or "").lower())


def _strip_artikelnummer(n: str) -> str:
    n = re.sub(r"^(artikel\s+)?[\d.]+[a-z]?\s*[:.-]?\s*", "", (n or "").strip(), flags=re.I)
    n = re.sub(r"^bestemming\s+", "", n, flags=re.I)
    n = re.sub(r"\s*\((artikel\s+)?[\d.]+\.?\)\s*$", "", n, flags=re.I)
    return _naam_sleutel(n)


def meet_wro(conn, out):
    cur = conn.cursor(row_factory=tuple_row)
    t0 = time.time()
    tekst = {}
    for id_, parent, soort, naam, inst in cur.execute(
            "SELECT identificatie, parent_id, object_type, naam, instrument_idn "
            "FROM wro.wro_tekst_object"):
        tekst[id_] = (parent, soort, naam or "", inst)
    print(f"[{time.time() - t0:.0f}s] {len(tekst):,} tekstobjecten")

    # IMRO kent geen verwijzing van planobject naar artikel; de koppeling loopt
    # via de naam die het bestemmingsartikel in zijn opschrift draagt.
    planobject = defaultdict(dict)
    for inst, soort, naam, hg, gahg in cur.execute("""
            SELECT instrument_idn, object_type, naam,
                   max(bestemmingshoofdgroep), max(gebiedsaanduidinghoofdgroep)
              FROM wro.planobject
             WHERE object_type IN ('Enkelbestemming', 'Dubbelbestemming',
                                   'Gebiedsaanduiding', 'Functieaanduiding', 'Bouwaanduiding')
             GROUP BY 1, 2, 3"""):
        planobject[inst].setdefault(_naam_sleutel(naam), (soort, hg or gahg))
    print(f"[{time.time() - t0:.0f}s] planobjectnamen in {len(planobject):,} plannen")

    def keten(i):
        keten_, d = [], 0
        while i in tekst and d < 15:
            keten_.append(i)
            i = tekst[i][0]
            d += 1
        return keten_

    stats, hoofdgroepen, soorten = Counter(), Counter(), Counter()
    for id_, (_parent, soort, _naam, inst) in tekst.items():
        if soort != "Artikel":
            continue
        ch = keten(id_)
        namen = [tekst[x][2] for x in ch]
        if "Toelichting" in [tekst[x][1] for x in ch] or any(
                re.match(r"toelichting", n, re.I) for n in namen):
            stats["toelichting"] += 1
            continue
        po = planobject.get(inst, {})
        treffer = next((po[_strip_artikelnummer(tekst[x][2])] for x in ch
                        if tekst[x][1] == "Artikel"
                        and _strip_artikelnummer(tekst[x][2]) in po), None)
        if treffer:
            stats["gekoppeld aan planobject"] += 1
            soorten[treffer[0]] += 1
            hoofdgroepen[treffer[1]] += 1
            continue
        pad = " > ".join(n for n in reversed(namen[1:]) if n)[:120].lower()
        if not po:
            stats["plan zonder geladen planobjecten"] += 1
        elif re.search(r"bestemmingsregels|bestemmingen", pad):
            stats["onder bestemmingsregels, geen naam-match"] += 1
        else:
            stats["ander hoofdstuk"] += 1
            if re.search(r"(inleidende|algemene|overgangs|slot)", pad):
                stats["  waarvan inleidend/algemeen/overgang/slot"] += 1

    out["wro"] = {"stats": dict(stats), "hoofdgroepen": dict(hoofdgroepen),
                  "objecttypen": dict(soorten)}
    tot = sum(v for k, v in stats.items() if not k.startswith("  "))
    print("\n=== Wro-artikelen ===")
    for k, v in stats.most_common():
        print(f"{k}: {v:,} ({pct(v, tot)})")
    print("gekoppeld naar objecttype:", soorten.most_common())
    print("gekoppeld naar hoofdgroep:", hoofdgroepen.most_common(12))


# --- Vrijetekst: divisieteksten ----------------------------------------------

def meet_vrijetekst(conn, out):
    cur = conn.cursor(row_factory=tuple_row)
    elem = {}
    for id_, parent, soort, wid, rx, dt in cur.execute("""
            SELECT te.id, te.parent_id, te.element_type, te.wid,
                   te.regeling_expression, r.documenttype
              FROM p2p.tekst_element te
              JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
             WHERE NOT r.inactief AND r.regelingmodel = 'RegelingVrijetekst'"""):
        elem[id_] = (parent, soort, wid, rx, dt)
    op_wid = {(v[3], v[2]): k for k, v in elem.items()}

    annotatie = defaultdict(lambda: {"th": set(), "ga": set(), "ga_jur": False,
                                     "hoofdlijn": False, "tekstdeel": False})
    n_td = n_raak = 0
    for _tid, rx_td, thema, d_rx, d_wid, ga_types, hoofdlijn in cur.execute("""
            SELECT t.identificatie, t.regeling_expression, t.thema,
                   d.regeling_expression, d.wid,
                   (SELECT array_agg(ga.type) FROM p2p.tekstdeel_gebiedsaanwijzing x
                      JOIN p2p.gebiedsaanwijzing ga ON ga.identificatie = x.gebiedsaanwijzing_id
                     WHERE x.tekstdeel_id = t.identificatie),
                   EXISTS(SELECT 1 FROM p2p.tekstdeel_hoofdlijn h
                           WHERE h.tekstdeel_id = t.identificatie)
              FROM p2p.tekstdeel t
              LEFT JOIN p2p.divisie d ON d.identificatie = t.divisie_wid"""):
        n_td += 1
        # De brug tekstdeel -> divisie -> tekst_element (G-118, hersteld 2026-09-10).
        k = op_wid.get(((d_rx or rx_td), d_wid)) if d_wid else None
        if k is None:
            continue
        n_raak += 1
        a = annotatie[k]
        a["tekstdeel"] = True
        a["hoofdlijn"] |= hoofdlijn
        a["th"] |= {norm_thema(x) for x in (thema or [])}
        for t in ga_types or []:
            t = (t or "").lower()
            if t in GA_JURIDISCH:
                a["ga_jur"] = True
            elif t in TYPE2THEMA:
                a["ga"].add(TYPE2THEMA[t])
    print(f"tekstdelen: {n_td:,}, bereikt een tekst_element in een actieve "
          f"vrijetekstregeling: {n_raak:,} ({pct(n_raak, n_td)})")

    def effectief(k):
        """Een annotatie op een divisie geldt ook voor de teksten eronder."""
        res = {"th": set(), "ga": set(), "ga_jur": False, "hoofdlijn": False,
               "tekstdeel": False}
        i, d = k, 0
        while i in elem and d < 20:
            if i in annotatie:
                for f in ("th", "ga"):
                    res[f] |= annotatie[i][f]
                for f in ("ga_jur", "hoofdlijn", "tekstdeel"):
                    res[f] |= annotatie[i][f]
            i = elem[i][0]
            d += 1
        return res

    per_type = defaultdict(Counter)
    for k, v in elem.items():
        if v[1] != "Divisietekst":
            continue
        e = effectief(k)
        for g in (v[4], "TOTAAL"):
            c = per_type[g]
            c["n"] += 1
            c["tekstdeel"] += e["tekstdeel"]
            c["thema-annotatie"] += bool(e["th"])
            c["gebiedsaanwijzing"] += bool(e["ga"])
            c["ga-juridisch type"] += e["ga_jur"]
            c["hoofdlijn"] += e["hoofdlijn"]
            c["objectthema"] += bool(e["th"] or e["ga"])
    out["vrijetekst"] = {"tekstdelen": n_td, "tekstdelen_met_tekst": n_raak,
                         "per_type": {k: dict(v) for k, v in per_type.items()}}
    kolommen = ["tekstdeel", "thema-annotatie", "gebiedsaanwijzing",
                "ga-juridisch type", "hoofdlijn", "objectthema"]
    print("\n=== Vrijetekst: divisieteksten ===")
    print(" | ".join(["documenttype", "n"] + kolommen))
    for g, c in sorted(per_type.items(), key=lambda x: -x[1]["n"]):
        print(" | ".join([g, f"{c['n']:,}"] + [pct(c[k], c["n"]) for k in kolommen]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", choices=["ow", "wro", "vrijetekst", "alle"], default="alle")
    ap.add_argument("--json", metavar="PAD", help="schrijf de tellingen ook als JSON weg")
    args = ap.parse_args()

    out = {}
    with get_conn() as conn:
        conn.execute("SET statement_timeout = '1800s'")
        if args.corpus in ("ow", "alle"):
            meet_ow(conn, out)
        if args.corpus in ("wro", "alle"):
            meet_wro(conn, out)
        if args.corpus in ("vrijetekst", "alle"):
            meet_vrijetekst(conn, out)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print(f"\nJSON weggeschreven: {args.json}")


if __name__ == "__main__":
    main()
