"""Transitie-export voor Locus Legis: de voorraad van het tijdelijk deel per gemeente.

Contract: c:/GIT/Locus/docs/plans/transitieplan/plan-1-voorraad-uit-ocd.md (contractversie 1).
Plan aan deze kant: docs/transitie-export-plan.md.

Drie endpoints, alleen met de privésleutel:
    GET /v1/transitie/{gemeentecode}/voorraad      plannen, artikelen, tegels (H), bruidsschat
    GET /v1/transitie/{gemeentecode}/geometrie     plangebied en geldend deel per plan, en de pons
    GET /v1/transitie/plan/{idn}/artikel/{tekst}   één artikel met zijn onderdelen en tekst

Sleutels over de grens zijn natuurlijk: `idn` en `(idn, tekstobjectId)`. Nooit een id-kolom.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import tegels_h
from db import get_conn

router = APIRouter(prefix="/v1/transitie", tags=["transitie"])

CONTRACTVERSIE = 1
VEREENVOUDIGING_M = 2.0
MIN_OVERLAP_M2 = 1000  # een inpassingsplan telt mee als het minstens 0,1 ha in de gemeente ligt
GELDENDE_STATUS = ("vastgesteld", "onherroepelijk", "vigerend", "geconsolideerd")
# Soorten die bij het tijdelijk deel horen (art. 4.6 lid 1 Invoeringswet, volgens IPLO) maar die
# OCD niet laadt. Zie vault G-181.
ONTBREKENDE_SOORTEN = ["beheersverordening", "exploitatieplan", "voorbereidingsbesluit"]
GEEN_REGEL = re.compile(r"\.(toelichting|bijlagenbijtoelichting)[._]", re.I)
RE_GEMEENTE = re.compile(r"^gm\d{4}$")
RE_TAM = re.compile(r"\.TAM", re.I)
RE_TAM_NAAM = re.compile(r"^\s*TAM\b", re.I)
RE_WELSTAND = re.compile(r"welstand", re.I)


def alleen_prive(request: Request) -> None:
    """De export is voor backend-afnemers (Locus), niet voor browsers met de publieke sleutel."""
    if getattr(request.state, "tier", None) not in ("private", "anonymous"):
        raise HTTPException(status_code=403, detail="Alleen met de privésleutel")


def _gemeente(code: str) -> str:
    code = code.lower()
    if not RE_GEMEENTE.match(code):
        raise HTTPException(status_code=400, detail="Gemeentecode in de vorm gm0000")
    return code


def _echte_bronhouder(idn: str, bronhouder: str) -> str:
    """OCD zet bij inpassingsplannen 'gm' voor het IMRO-prefix (gm9921, gm0000). Herstel dat."""
    m = re.match(r"^NL\.IMRO\.(\d{4})", idn or "")
    prefix = m.group(1) if m else ""
    if prefix == "0000":
        return "rijk"
    if prefix.startswith("99"):
        return "pv" + prefix[2:]
    return bronhouder


def _plangrens(herkomst: str | None) -> str:
    return {None: "pdok", "ihr-plangrens": "ihr", "ambtsgebied-imro2006": "ambtsgebied"}.get(herkomst, herkomst or "pdok")


# ── selectie van het tijdelijk deel ──────────────────────────────────────────────────────────────

SELECTIE_SQL = f"""
WITH g AS (
    SELECT geometrie AS geo FROM core.gemeentegrens WHERE overheidscode = %(gm)s
),
pons AS (
    SELECT ST_UnaryUnion(ST_Collect(ST_MakeValid(l.geometrie))) AS geo
      FROM p2p.pons p JOIN p2p.locatie l ON l.identificatie = p.locatie_id
     WHERE p.identificatie LIKE %(pons_like)s
),
kand AS (
    SELECT r.idn, r.dossier, r.naam, r.type_plan, r.planstatus, r.datum, r.bronhouder,
           r.geometrie_herkomst, r.geometrie
      FROM wro.ruimtelijk_instrument r, g
     WHERE r.pons_status = 'actief'
       AND r.type_plan <> 'structuurvisie'
       AND lower(r.planstatus) IN {GELDENDE_STATUS}
       AND (r.bronhouder = %(gm)s
            OR (r.type_plan = 'inpassingsplan' AND r.bronhouder <> %(gm)s
                AND r.geometrie && g.geo
                AND ST_Area(ST_Intersection(r.geometrie, g.geo)) > {MIN_OVERLAP_M2}))
),
nieuwste AS (
    SELECT DISTINCT ON (dossier) * FROM kand
     ORDER BY dossier, datum DESC NULLS LAST, idn DESC
)
SELECT n.idn, n.dossier, n.naam, n.type_plan, n.planstatus, n.datum, n.bronhouder,
       n.geometrie_herkomst,
       ST_Area(n.geometrie) AS oppervlakte,
       CASE WHEN pons.geo IS NULL OR NOT ST_Intersects(n.geometrie, pons.geo)
            THEN ST_Area(n.geometrie)
            ELSE ST_Area(ST_Difference(ST_MakeValid(n.geometrie), pons.geo)) END AS geldend,
       o.dossierstatus, o.is_paraplu, o.is_tam, o.verwijderd_op, o.relaties,
       l.n_teksten, l.fout AS laadfout
  FROM nieuwste n
  CROSS JOIN pons
  LEFT JOIN LATERAL (
        SELECT ob.* FROM wro.wro_plan_observatie ob
          JOIN wro.wro_snapshot s ON s.snapshot_id = ob.snapshot_id
         WHERE ob.identificatie = n.idn
         ORDER BY s.datum DESC LIMIT 1) o ON true
  LEFT JOIN wro.wro_tekst_laadstand l ON l.instrument_idn = n.idn
 ORDER BY n.naam, n.idn
"""


def _selectie(cur, gm: str) -> tuple[list[dict], list[dict]]:
    cur.execute("SET LOCAL statement_timeout = 120000")
    cur.execute(SELECTIE_SQL, {"gm": gm, "pons_like": f"nl.imow-{gm}.%"})
    rijen = cur.fetchall()
    vervangen = set()
    for r in rijen:
        rel = r["relaties"] or {}
        if isinstance(rel, str):
            rel = json.loads(rel)
        for x in rel.get("vervangt") or []:
            if x.get("id"):
                vervangen.add(x["id"])
    plannen, uitgesloten = [], []
    for r in rijen:
        reden = None
        if r["verwijderd_op"]:
            reden = "verwijderd uit de IHR"
        elif r["is_tam"] or RE_TAM.search(r["idn"] or "") or RE_TAM_NAAM.match(r["naam"] or ""):
            # TAM-IMRO is een wijziging van het omgevingsplan via de IMRO-route, geen tijdelijk deel.
            # De IHR zet is_tam niet altijd (Nunspeet TAM0022n), dus ook op identificatie en naam.
            reden = "TAM-omgevingsplan (geen tijdelijk deel)"
        elif RE_WELSTAND.search(r["idn"] or "") or RE_WELSTAND.search(r["naam"] or ""):
            reden = "welstandsnota, geen ruimtelijk plan"
        elif r["idn"] in vervangen:
            reden = "vervangen door een ander plan"
        if reden:
            uitgesloten.append({"idn": r["idn"], "naam": r["naam"], "reden": reden})
        else:
            plannen.append(r)
    return plannen, uitgesloten


def _relaties(rel) -> list[dict]:
    if isinstance(rel, str):
        rel = json.loads(rel)
    uit = []
    for soort, lijst in (rel or {}).items():
        for x in lijst or []:
            if x.get("id"):
                uit.append({"soort": soort, "idn": x["id"]})
    return uit


# ── artikelen ────────────────────────────────────────────────────────────────────────────────────

TEKSTEN_SQL = """
SELECT t.instrument_idn, t.identificatie, t.volgnummer, t.niveau, t.parent_id, t.object_type,
       t.label, t.nummer, t.naam,
       CASE WHEN coalesce(t.naam, t.label) ~* 'bestemmingsomschrijving' THEN t.inhoud END AS inhoud,
       i.categorie, i.subcategorie, i.herkomst
  FROM wro.wro_tekst_object_v2 t
  LEFT JOIN v2a.wro_artikel_indeling i
         ON i.instrument_idn = t.instrument_idn AND i.tekst_id = t.identificatie
 WHERE t.instrument_idn = ANY(%(idns)s)
   AND t.identificatie !~* '[.](toelichting|bijlagenbijtoelichting)[._]'
"""

LINKS_SQL = """
WITH po AS MATERIALIZED (
    SELECT identificatie, naam, object_type, bestemmingshoofdgroep
      FROM wro.planobject
     WHERE instrument_idn = ANY(%(idns)s)
       AND object_type IN ('Enkelbestemming', 'Dubbelbestemming', 'Gebiedsaanduiding', 'Functieaanduiding'))
SELECT DISTINCT l.instrument_idn, l.tekst_id, po.naam, po.object_type, po.bestemmingshoofdgroep
  FROM wro.wro_tekst_link l
  JOIN po ON po.identificatie = l.planobject_id
 WHERE l.instrument_idn = ANY(%(idns)s)
"""
# MATERIALIZED: eerst alle planobjecten van deze plannen in één scan (index op instrument_idn), dan
# hashen. Zonder dat kiest de planner ~70.000 losse opzoekingen op de pkey, wat op een koude cache
# de tijdsgrens haalt (Nunspeet, 2026-11-12).

RE_ARTIKEL_KOP = re.compile(r"^\s*artikel\s+[\w.]+\s*[-–—:.]?\s*", re.I)


def _artikelen(cur, idns: list[str]) -> list[dict]:
    if not idns:
        return []
    cur.execute(TEKSTEN_SQL, {"idns": idns})
    teksten = cur.fetchall()
    cur.execute(LINKS_SQL, {"idns": idns})
    links = defaultdict(list)
    for r in cur.fetchall():
        links[(r["instrument_idn"], r["tekst_id"])].append(r)

    per_plan = defaultdict(dict)
    kinderen = defaultdict(list)
    for t in teksten:
        per_plan[t["instrument_idn"]][t["identificatie"]] = t
    for idn, knopen in per_plan.items():
        for t in knopen.values():
            if t["parent_id"]:
                kinderen[(idn, t["parent_id"])].append(t)

    def subtree(idn, t):
        uit, stapel = [], [t]
        while stapel:
            x = stapel.pop()
            uit.append(x)
            stapel.extend(kinderen.get((idn, x["identificatie"]), []))
        return uit

    def hoofdstuk(knopen, t):
        p = knopen.get(t["parent_id"]) if t["parent_id"] else None
        while p is not None:
            if p["object_type"] not in ("Artikel",) and (p["niveau"] or 0) >= 1:
                return p["label"] or p["naam"]
            p = knopen.get(p["parent_id"]) if p["parent_id"] else None
        return None

    artikelen = []
    for idn, knopen in per_plan.items():
        for t in knopen.values():
            if t["object_type"] != "Artikel":
                continue
            ouder = knopen.get(t["parent_id"]) if t["parent_id"] else None
            if ouder is not None and ouder["object_type"] == "Artikel":
                continue  # sub-artikel of lid: hoort bij het artikel erboven
            boom = subtree(idn, t)
            tegels, subs = [], []
            for x in boom:
                # Van de onderdelen telt alleen een indeling via een bestemming of aanduiding mee. Zonder
                # die grens krijgt "Begrippen" de tegel van elke begripsbepaling eronder (Nunspeet: 5 tegels).
                eigen_pad = x is t or x["herkomst"] in ("bestemming", "aanduiding")
                pad = {"categorie": x["categorie"], "sub": x["subcategorie"]} if x["categorie"] and eigen_pad else None
                tt, _ = tegels_h.tegels_e(pad=pad, opschrift=x["naam"] or x["label"], inhoud=x["inhoud"])
                for tg in tt:
                    if tg not in tegels:
                        tegels.append(tg)
                if pad and x["subcategorie"] and x["subcategorie"] not in subs:
                    subs.append(x["subcategorie"])
            eigen = t["categorie"] and (t["categorie"], t["subcategorie"], t["herkomst"])
            if not eigen:
                teller = Counter((x["categorie"], x["subcategorie"], x["herkomst"]) for x in boom if x["categorie"])
                eigen = teller.most_common(1)[0][0] if teller else (None, None, None)
            bestemmingen = {}
            for x in boom:
                for b in links.get((idn, x["identificatie"]), []):
                    bestemmingen[(b["naam"], b["object_type"])] = {
                        "naam": b["naam"], "soort": b["object_type"], "hoofdgroep": b["bestemmingshoofdgroep"]}
            kop = RE_ARTIKEL_KOP.sub("", t["label"] or t["naam"] or "").strip() or (t["naam"] or "")
            artikelen.append({
                "idn": idn,
                "tekstobjectId": t["identificatie"],
                "volgnummer": t["volgnummer"],
                "nummer": t["nummer"],
                "kop": kop,
                "hoofdstuk": hoofdstuk(knopen, t),
                "categorie": eigen[0],
                "subcategorie": eigen[1],
                "indelingHerkomst": eigen[2],
                "tegels": tegels or ["niet-ingedeeld"],
                "subonderwerpen": subs,
                "bestemmingen": sorted(bestemmingen.values(), key=lambda b: (b["soort"] or "", b["naam"] or "")),
                "aantalLeden": len(kinderen.get((idn, t["identificatie"]), [])),
                "tekstBron": "ihr",
                "pdfPagina": None,
                "gecontroleerd": None,
            })
    artikelen.sort(key=lambda a: (a["idn"], a["volgnummer"]))
    return artikelen


def _signalen(plannen: list[dict], artikelen: list[dict]) -> dict[str, list[dict]]:
    """Per plan wat de afnemer moet weten over de betrouwbaarheid van de artikelen."""
    per_plan = defaultdict(list)
    for a in artikelen:
        per_plan[a["idn"]].append(a)
    uit = defaultdict(list)
    for p in plannen:
        arts = per_plan.get(p["idn"], [])
        if p["laadfout"] is not None or not (p["n_teksten"] or 0):
            uit[p["idn"]].append({"soort": "tekst ontbreekt",
                                  "toelichting": "De IHR levert voor dit plan geen teksten; zie de pdf op ruimtelijkeplannen.nl."})
        elif arts and all(a["nummer"] and "." in str(a["nummer"]) for a in arts):
            # Bijvoorbeeld Nunspeet MOLENBEEK: in de IHR hangen de sub-artikelen (1.1, 1.2) direct onder
            # Regels, zonder artikelknopen. De export verzint die niet.
            uit[p["idn"]].append({"soort": "platte opbouw",
                                  "toelichting": "De IHR kent hier geen artikelen, alleen genummerde onderdelen."})
    return uit


# ── bruidsschat (hoofdstuk 22 van het omgevingsplan) ────────────────────────────────────────────

def _omgevingsplan_expressie(cur, gm: str) -> tuple[str | None, str | None]:
    cur.execute("""
        SELECT frbr_work, frbr_expression FROM p2p.regeling
         WHERE frbr_work ~ %(re)s AND NOT coalesce(inactief, false)
         ORDER BY frbr_expression DESC LIMIT 1""", {"re": f"^/akn/nl/act/{gm}/[0-9]+/omgevingsplan$"})
    r = cur.fetchone()
    return (r["frbr_work"], r["frbr_expression"]) if r else (None, None)


def _bruidsschat(cur, expressie: str | None) -> list[dict]:
    if not expressie:
        return []
    cur.execute("""
        SELECT te.wid, te.opschrift, ai.categorie, ai.subcategorie
          FROM p2p.tekst_element te
          LEFT JOIN v2a.artikel_indeling ai ON ai.tekst_element_id = te.id
         WHERE te.regeling_expression = %(e)s AND te.element_type = 'Artikel' AND te.wid ~ '__chp_22__'
         ORDER BY te.volgorde""", {"e": expressie})
    artikelen = cur.fetchall()
    cur.execute("""
        SELECT regeltekst_wid, thema FROM p2p.juridische_regel
         WHERE regeling_expression = %(e)s AND regeltekst_wid ~ '__chp_22__'""", {"e": expressie})
    themas = defaultdict(list)
    for r in cur.fetchall():
        m = re.match(r"^(.*?__art_[^_]+)", r["regeltekst_wid"] or "")
        if m:
            for th in r["thema"] or []:
                th = tegels_h.norm(th)
                if th not in themas[m.group(1)]:
                    themas[m.group(1)].append(th)
    uit = []
    for a in artikelen:
        pad = {"categorie": a["categorie"], "sub": a["subcategorie"]} if a["categorie"] else None
        tegels, _ = tegels_h.tegels_e(pad=pad, imow=themas.get(a["wid"]))
        uit.append({
            "wId": a["wid"],
            "kop": a["opschrift"],
            "tegels": tegels or ["niet-ingedeeld"],
            "subonderwerpen": [a["subcategorie"]] if a["subcategorie"] else [],
        })
    return uit


# ── endpoints ────────────────────────────────────────────────────────────────────────────────────

@router.get("/{gemeentecode}/voorraad", dependencies=[Depends(alleen_prive)])
def voorraad(gemeentecode: str) -> dict[str, Any]:
    gm = _gemeente(gemeentecode)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM core.gemeentegrens WHERE overheidscode = %s", (gm,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Gemeente {gm} onbekend")
        plannen, uitgesloten = _selectie(cur, gm)
        artikelen = _artikelen(cur, [p["idn"] for p in plannen])
        signalen = _signalen(plannen, artikelen)
        werk, expressie = _omgevingsplan_expressie(cur, gm)
        bruidsschat = _bruidsschat(cur, expressie)
        cur.execute("SELECT max(datum) AS d FROM wro.wro_snapshot")
        wro_stand = cur.fetchone()["d"]
        cur.execute("SELECT max(curatie_versie) AS v FROM v2a.artikel_indeling WHERE regeling_expression = %s",
                    (expressie,))
        curatie = cur.fetchone()["v"]
        cur.execute("SELECT array_agg(identificatie ORDER BY identificatie) AS ids FROM p2p.pons WHERE identificatie LIKE %s",
                    (f"nl.imow-{gm}.%",))
        pons_ids = cur.fetchone()["ids"] or []

    return {
        "contractversie": CONTRACTVERSIE,
        "gemeentecode": gm,
        "gemaaktOp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bron": {
            "ponsUitRegeling": werk,
            "ponsUitExpressie": expressie,
            "ponsIdentificaties": pons_ids,
            "wroStand": wro_stand.isoformat() if wro_stand else None,
            "indeling": tegels_h.INDELING,
            "indelingVersie": tegels_h.INDELING_VERSIE,
            "curatieVersie": str(curatie) if curatie is not None else None,
        },
        "tegels": tegels_h.TEGELS,
        "ontbrekendeSoorten": ONTBREKENDE_SOORTEN,
        "plannen": [{
            "idn": p["idn"],
            "dossier": p["dossier"],
            "naam": p["naam"],
            "typePlan": p["type_plan"],
            "planstatus": p["planstatus"],
            "datum": p["datum"].isoformat() if p["datum"] else None,
            "dossierstatus": p["dossierstatus"],
            "bronhouder": _echte_bronhouder(p["idn"], p["bronhouder"]),
            "isParaplu": bool(p["is_paraplu"]),
            "plangrens": _plangrens(p["geometrie_herkomst"]),
            "oppervlakteM2": round(p["oppervlakte"] or 0),
            "geldendM2": round(p["geldend"] or 0),
            "tekstenVolledig": p["laadfout"] is None and (p["n_teksten"] or 0) > 0,
            "relaties": _relaties(p["relaties"]),
            "signalen": signalen.get(p["idn"], []),
        } for p in plannen],
        "uitgesloten": uitgesloten,
        "artikelen": artikelen,
        "bruidsschat": bruidsschat,
    }


@router.get("/{gemeentecode}/geometrie", dependencies=[Depends(alleen_prive)])
def geometrie(
    gemeentecode: str,
    precisie: str = Query("vereenvoudigd", pattern="^(vereenvoudigd|exact)$"),
    idn: list[str] | None = Query(None, description="Bij precisie=exact: de plannen waarvan de grens nodig is"),
) -> dict[str, Any]:
    gm = _gemeente(gemeentecode)
    features = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 120000")
        if precisie == "exact":
            if not idn:
                raise HTTPException(status_code=400, detail="Bij precisie=exact hoort ten minste één idn")
            cur.execute("""
                SELECT idn, ST_AsGeoJSON(ST_MakeValid(geometrie))::json AS g
                  FROM wro.ruimtelijk_instrument WHERE idn = ANY(%s)""", (idn,))
            for r in cur.fetchall():
                features.append({"type": "Feature", "geometry": r["g"],
                                 "properties": {"idn": r["idn"], "rol": "plangebied", "precisie": "exact"}})
        else:
            plannen, _ = _selectie(cur, gm)
            idns = [p["idn"] for p in plannen]
            cur.execute(f"""
                WITH pons AS (
                    SELECT ST_UnaryUnion(ST_Collect(ST_MakeValid(l.geometrie))) AS geo
                      FROM p2p.pons p JOIN p2p.locatie l ON l.identificatie = p.locatie_id
                     WHERE p.identificatie LIKE %(pl)s)
                SELECT r.idn,
                       ST_AsGeoJSON(ST_MakeValid(ST_SimplifyPreserveTopology(r.geometrie, {VEREENVOUDIGING_M})))::json AS plangebied,
                       CASE WHEN pons.geo IS NOT NULL AND ST_Intersects(r.geometrie, pons.geo)
                            THEN ST_AsGeoJSON(ST_MakeValid(ST_SimplifyPreserveTopology(
                                     ST_Difference(ST_MakeValid(r.geometrie), pons.geo), {VEREENVOUDIGING_M})))::json
                       END AS geldend,
                       pons.geo IS NOT NULL AND ST_Intersects(r.geometrie, pons.geo) AS geraakt
                  FROM wro.ruimtelijk_instrument r CROSS JOIN pons
                 WHERE r.idn = ANY(%(idns)s)""", {"pl": f"nl.imow-{gm}.%", "idns": idns})
            for r in cur.fetchall():
                features.append({"type": "Feature", "geometry": r["plangebied"],
                                 "properties": {"idn": r["idn"], "rol": "plangebied"}})
                features.append({"type": "Feature", "geometry": r["geldend"] if r["geraakt"] else r["plangebied"],
                                 "properties": {"idn": r["idn"], "rol": "geldend"}})
            cur.execute(f"""
                SELECT p.identificatie,
                       ST_AsGeoJSON(ST_MakeValid(ST_SimplifyPreserveTopology(l.geometrie, {VEREENVOUDIGING_M})))::json AS g
                  FROM p2p.pons p JOIN p2p.locatie l ON l.identificatie = p.locatie_id
                 WHERE p.identificatie LIKE %s""", (f"nl.imow-{gm}.%",))
            for r in cur.fetchall():
                features.append({"type": "Feature", "geometry": r["g"],
                                 "properties": {"identificatie": r["identificatie"], "rol": "pons"}})
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::28992"}},
        "contractversie": CONTRACTVERSIE,
        "precisie": precisie,
        "features": features,
    }


@router.get("/plan/{idn}/artikel/{tekstobject_id}", dependencies=[Depends(alleen_prive)])
def artikel(idn: str, tekstobject_id: str) -> dict[str, Any]:
    if GEEN_REGEL.search(tekstobject_id):
        raise HTTPException(status_code=400, detail="Toelichting is geen regel")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH RECURSIVE boom AS (
                SELECT t.*, 0 AS diepte FROM wro.wro_tekst_object_v2 t
                 WHERE t.instrument_idn = %(idn)s AND t.identificatie = %(id)s
                UNION ALL
                SELECT c.*, b.diepte + 1 FROM wro.wro_tekst_object_v2 c
                  JOIN boom b ON c.instrument_idn = b.instrument_idn AND c.parent_id = b.identificatie)
            SELECT identificatie, parent_id, diepte, volgnummer, object_type, label, nummer, naam, inhoud
              FROM boom ORDER BY volgnummer""", {"idn": idn, "id": tekstobject_id})
        rijen = cur.fetchall()
    if not rijen:
        raise HTTPException(status_code=404, detail="Artikel niet gevonden")
    kop = rijen[0]
    return {
        "contractversie": CONTRACTVERSIE,
        "idn": idn,
        "tekstobjectId": tekstobject_id,
        "nummer": kop["nummer"],
        "kop": RE_ARTIKEL_KOP.sub("", kop["label"] or kop["naam"] or "").strip(),
        "inhoud": kop["inhoud"],
        "tekstBron": "ihr",
        "onderdelen": [{
            "tekstobjectId": r["identificatie"],
            "parent": r["parent_id"],
            "diepte": r["diepte"],
            "nummer": r["nummer"],
            "kop": r["label"] or r["naam"],
            "inhoud": r["inhoud"],
        } for r in rijen[1:]],
    }
