"""Vergunningcheck-ingang: werkzaamheid x overheid -> vragenbomen + wetsartikelen.

Het idee: de burger kiest in begrijpelijke taal wat hij wil doen (een
`i2a.werkzaamheid`), de pagina toont de vragenbomen die het bevoegd gezag
daarvoor heeft gepubliceerd, en daarnáást **alle wetsartikelen die die
vragenbomen dragen** — zodat exact juridisch zichtbaar is waar de uitkomst
vandaan komt. Werkt voor de check (Conclusie) én voor de aanvraag
(Indieningsvereisten).

Nul nieuwe API-calls: alles komt uit wat al geladen is.

    i2a.werkzaamheid                (293, met ~21 trefwoorden elk als zoekingang)
      -> i2a.werkzaamheid_activiteit (91.207 koppelingen, per overheid)
        -> i2a.regelbeheerobject     (Conclusie / Indieningsvereisten / Maatregelen)
          -> i2a.toepasbaar_regelbestand.beslisgraaf  (nodes + edges + when/then)
        -> p2p.activiteit_locatieaanduiding -> juridische_regel -> tekst_element
          -> v2a.element_hertaling   (begrijpelijke variant per lid, 87,2%)

Gemeten vorm van de data (2026-09-01/02), bepalend voor het ontwerp:

* **werkzaamheid x overheid -> mediaan 1 activiteit.** Dakkapel plaatsen raakt
  347 overheden; 316 daarvan hebben precies één activiteit. De combinatie is de
  natuurlijke sleutel van de pagina.
* **Vanaf de regel is de fan-out 1:3 tot 1:16** (mediaan 3, p90 16 dragende
  regelteksten). De artikelenlijst is dus het product, geen voetnoot.
* **Dekking is niet vanzelfsprekend**: Tuinmeubilair raakt 360 overheden,
  Vuurwerk afsteken 49. En 12,7% van de regelbeheerobjecten heeft geen gevulde
  vragenboom. De pagina moet dat kunnen zeggen in plaats van leeg te blijven.
* **De annotatie zegt niet wát een regel doet**: 89,4% van de kwalificaties is
  "anders geduid". De wetstekst ernaast leggen is dus de enige manier om te zien
  hoe en wat.
* **De begrijpelijke variant dekt 87,2%** van de TR-gekoppelde regelteksten, niet
  alles. Een lege `begrijpelijk` is normaal en moet als zodanig getoond worden.
* **1,5% van de koppelingen wijst naar een activiteit die niet in `p2p` staat.**
  Die komt mee met `in_p2p: false` en zonder regelteksten — een dekkingsgat dat
  we bewust tonen in plaats van wegfilteren (zie gaps#G-136).

Endpoints:
    GET /v1/vergunningcheck/werkzaamheden           zoeken op naam of trefwoord
    GET /v1/vergunningcheck/{urn}/overheden         waar bestaat dit?
    GET /v1/vergunningcheck/{urn}/{overheid}        de pagina-payload
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from db import get_conn

router = APIRouter(prefix="/v1/vergunningcheck", tags=["vergunningcheck"])

CACHE_HEADER = "public, max-age=3600, s-maxage=86400"

# Heuristiek, GEEN annotatie. Er bestaat geen structurele koppeling tussen een
# artikel en het regelbeheerobject-type; de drie typen hangen alle drie aan
# dezelfde activiteit. Deze cues zijn afgeleid uit twee doorgerekende
# voorbeelden (gm0345 boom kappen, gm0344 dakkapel) waarin de artikelen zich wél
# langs die lijn ordenden. Het veld heet daarom `heuristiek_onderdeel` en draagt
# het bewijs mee, zodat de UI het als hint kan tonen en niet als feit.
_CUES: list[tuple[str, re.Pattern[str]]] = [
    ("indieningsvereisten",
     re.compile(r"bij (?:de |een )?aanvraag|worden de volgende gegevens|"
                r"gegevens en bescheiden|in te dienen", re.I)),
    ("maatregelen",
     re.compile(r"^\s*aan (?:de |een )?omgevingsvergunning|voorschrift(?:en)? "
                r"(?:worden|kunnen)|wordt verbonden", re.I)),
    ("conclusie",
     re.compile(r"het is verboden|is het verboden|vergunningplicht|"
                r"zonder omgevingsvergunning|meldingsplicht|"
                r"in afwijking van artikel", re.I)),
]


def _heuristiek(tekst: str | None) -> tuple[str | None, str | None]:
    if not tekst:
        return None, None
    for naam, pat in _CUES:
        m = pat.search(tekst)
        if m:
            return naam, m.group(0)[:60]
    return None, None


# ----------------------------------------------------------------- modellen
class Werkzaamheid(BaseModel):
    urn: str
    naam: str
    trefwoorden: list[str] = []
    overheden: int | None = None


class OverheidRij(BaseModel):
    overheid_ns: str
    naam: str | None = None
    bestuurslaag: str | None = None
    activiteiten: int
    met_vragenboom: int


# ----------------------------------------------------------------- 1. zoeken
@router.get("/werkzaamheden", response_model=list[Werkzaamheid])
def zoek_werkzaamheden(
    response: Response,
    q: str | None = Query(None, description="Zoekterm; matcht naam én trefwoorden."),
    overheid: str | None = Query(None, description="Beperk tot een overheid, bv. gm0344."),
    limit: int = Query(50, le=200),
):
    """Zoek een werkzaamheid. Zonder `q` de hele lijst, alfabetisch."""
    response.headers["Cache-Control"] = CACHE_HEADER
    where, params = [], {}
    if q:
        where.append("(lower(w.naam) LIKE %(q)s OR EXISTS "
                     "(SELECT 1 FROM unnest(w.trefwoorden) t WHERE lower(t) LIKE %(q)s))")
        params["q"] = f"%{q.lower()}%"
    if overheid:
        where.append("EXISTS (SELECT 1 FROM i2a.werkzaamheid_activiteit k "
                     "WHERE k.werkzaamheid_urn = w.urn AND k.overheid_ns = %(oh)s)")
        params["oh"] = overheid
    params["lim"] = limit
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT w.urn, w.naam, COALESCE(w.trefwoorden, '{{}}') AS trefwoorden,
                   (SELECT count(DISTINCT k.overheid_ns)
                      FROM i2a.werkzaamheid_activiteit k
                     WHERE k.werkzaamheid_urn = w.urn) AS overheden
              FROM i2a.werkzaamheid w
             {"WHERE " + " AND ".join(where) if where else ""}
             ORDER BY w.naam
             LIMIT %(lim)s""", params)
        return [Werkzaamheid(**r) for r in cur.fetchall()]


# ------------------------------------------------------------- 2. dekking
@router.get("/{urn}/overheden", response_model=list[OverheidRij])
def overheden_voor(urn: str, response: Response):
    """Bij welke overheden bestaat deze werkzaamheid, en waar is er echt een
    vragenboom? Zonder dit kan de pagina niet eerlijk zeggen dat iets in jouw
    gemeente niet bestaat."""
    response.headers["Cache-Control"] = CACHE_HEADER
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT k.overheid_ns, b.naam, k.bestuurslaag,
                   count(*) AS activiteiten,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM i2a.regelbeheerobject r
                       JOIN i2a.toepasbaar_regelbestand t
                         ON t.regelbeheerobject = r.functionele_structuur_ref
                        AND t.heeft_logica
                      WHERE r.activiteit_id = k.activiteit_urn)) AS met_vragenboom
              FROM i2a.werkzaamheid_activiteit k
              LEFT JOIN core.bronhouder b ON b.overheidscode = k.overheid_ns
             WHERE k.werkzaamheid_urn = %s
             GROUP BY 1, 2, 3
             ORDER BY k.bestuurslaag, b.naam NULLS LAST""", (urn,))
        rijen = cur.fetchall()
    if not rijen:
        raise HTTPException(404, f"Onbekende werkzaamheid: {urn}")
    return [OverheidRij(**r) for r in rijen]


# --------------------------------------------------------- 3. de payload
@router.get("/{urn}/{overheid}")
def pagina(urn: str, overheid: str, response: Response,
           begrijpelijk: bool = Query(True, description="Begrijpelijke variant meesturen."),
           beslisgraaf: bool = Query(
               False,
               description="Volledige DMN-beslisgraaf meesturen. Standaard uit: "
                           "gemeten kost hij een factor 14 aan payload (112 KB "
                           "tegen 7,7 KB voor dakkapel/Utrecht) en de meeste "
                           "afnemers tonen hem niet.")):
    """De volledige pagina: vragenbomen + de wetsartikelen eronder."""
    response.headers["Cache-Control"] = CACHE_HEADER
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT urn, naam, COALESCE(trefwoorden,'{}') AS trefwoorden "
                    "FROM i2a.werkzaamheid WHERE urn = %s", (urn,))
        wz = cur.fetchone()
        if not wz:
            raise HTTPException(404, f"Onbekende werkzaamheid: {urn}")

        cur.execute("""SELECT k.activiteit_urn, a.naam, k.gezien_in_p2p, k.bestuurslaag
                         FROM i2a.werkzaamheid_activiteit k
                         LEFT JOIN p2p.activiteit a ON a.identificatie = k.activiteit_urn
                        WHERE k.werkzaamheid_urn = %s AND k.overheid_ns = %s
                        -- Activiteiten die we in p2p terugvinden eerst; de rest
                        -- is een dekkingsgat van circa anderhalf procent en
                        -- levert een leeg blok op.
                        ORDER BY k.gezien_in_p2p DESC, a.naam NULLS LAST""",
                    (urn, overheid))
        acts = cur.fetchall()
        if not acts:
            raise HTTPException(
                404, f"'{wz['naam']}' is niet gepubliceerd bij {overheid}.")

        cur.execute("SELECT naam, bestuurslaag FROM core.bronhouder "
                    "WHERE overheidscode = %s", (overheid,))
        bh = cur.fetchone() or {}

        uit = []
        for act in acts:
            aid = act["activiteit_urn"]

            cur.execute("""
                SELECT CASE
                         WHEN r.functionele_structuur_ref ~ '/concept/Conclusie'
                              THEN 'Conclusie'
                         WHEN r.functionele_structuur_ref ~ '/concept/Indieningsvereisten'
                              THEN 'Indieningsvereisten'
                         WHEN r.functionele_structuur_ref ~ '/concept/Maatregelen'
                              THEN 'Maatregelen'
                         ELSE 'Overig' END AS typering,
                       r.functionele_structuur_ref AS fsr,
                       COALESCE(t.heeft_logica, false) AS heeft_logica,
                       t.aantal_decisions, t.aantal_regels,
                       jsonb_array_length(t.beslisgraaf->'nodes') AS knopen,
                       jsonb_array_length(t.beslisgraaf->'edges') AS randen,
                       CASE WHEN %s THEN t.beslisgraaf END AS beslisgraaf
                  FROM i2a.regelbeheerobject r
                  LEFT JOIN i2a.toepasbaar_regelbestand t
                    ON t.regelbeheerobject = r.functionele_structuur_ref
                 WHERE r.activiteit_id = %s
                 ORDER BY 1""", (beslisgraaf, aid))
            rbos = cur.fetchall()

            cur.execute("""
                WITH tek AS (
                    SELECT DISTINCT jr.regeling_expression, jr.regeltekst_wid,
                           ala.kwalificatie
                      FROM p2p.activiteit_locatieaanduiding ala
                      JOIN p2p.juridische_regel jr
                        ON jr.identificatie = ala.juridische_regel_id
                      JOIN p2p.regeling reg
                        ON reg.frbr_expression = jr.regeling_expression
                       AND NOT reg.inactief
                     WHERE ala.activiteit_id = %s)
                SELECT reg.citeertitel AS regeling, reg.frbr_expression AS expression,
                       COALESCE(par.nummer, te.nummer) AS artikel,
                       COALESCE(par.opschrift, te.opschrift) AS opschrift,
                       te.element_type AS niveau, te.nummer AS lid,
                       te.wid, te.eid, te.inhoud_plain AS tekst,
                       tek.kwalificatie, h.begrijpelijk
                  FROM tek
                  JOIN p2p.tekst_element te
                    ON te.regeling_expression = tek.regeling_expression
                   AND te.wid = tek.regeltekst_wid
                  LEFT JOIN p2p.tekst_element par ON par.id = te.parent_id
                  JOIN p2p.regeling reg
                    ON reg.frbr_expression = tek.regeling_expression
                  -- LATERAL met LIMIT 1, GEEN gewone LEFT JOIN: de view
                  -- v2a.element_hertaling telt 719.682 rijen voor 405.353
                  -- unieke (wid, expression)-paren. Een gewone join zou de
                  -- regeltekst dus voor ruwweg driekwart dubbel opleveren.
                  LEFT JOIN LATERAL (
                        SELECT h.begrijpelijk
                          FROM v2a.element_hertaling h
                         WHERE %s
                           AND h.wid = te.wid
                           AND h.regeling_expression = te.regeling_expression
                         ORDER BY h.gegenereerd_op DESC NULLS LAST
                         LIMIT 1) h ON true
                 ORDER BY 1, 3, 6""", (aid, begrijpelijk))
            teksten = []
            for t in cur.fetchall():
                onderdeel, bewijs = _heuristiek(t["tekst"])
                teksten.append({**t,
                                "heuristiek_onderdeel": onderdeel,
                                "heuristiek_bewijs": bewijs})

            uit.append({
                "activiteit_urn": aid,
                "activiteit_naam": act["naam"],
                "in_p2p": act["gezien_in_p2p"],
                "regelbeheerobjecten": rbos,
                "regelteksten": teksten,
            })

    return {
        "werkzaamheid": wz,
        "overheid": {"code": overheid, "naam": bh.get("naam"),
                     "bestuurslaag": bh.get("bestuurslaag")},
        "activiteiten": uit,
        "let_op": {
            "heuristiek_onderdeel":
                "Lexicale hint, geen annotatie. Er bestaat geen structurele "
                "koppeling tussen een artikel en het regelbeheerobject-type.",
            "kwalificatie":
                "89,4% van alle activiteit-kwalificaties is 'anders geduid'; "
                "lees dit veld niet als betekenis.",
            "beslisgraaf":
                "Standaard weggelaten (scheelt een factor 14 aan payload); "
                "vraag hem op met ?beslisgraaf=true.",
        },
    }
