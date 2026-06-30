"""Endpoints voor de RP-planvoorraad-view op ponsenkaart.nl.

Meet de leegloop van de bestemmingsplan-voorraad (IMRO-kant) als tegenhanger van
de pons-aangroei (Ow-kant). Leest uit:
- `wro.wro_snapshot` + `wro.wro_plan_observatie` — temporele snapshots van het
  IMRO-manifest (bron: RP-Opvragen v4; loader: dso-loader wro_planvoorraad.py).
- `core.gemeentegrens` — 342 gemeenten (voor de noemer + lege-manifest-telling).

Drie endpoints:
    GET /v1/planvoorraad/national     → KPI's + de snapshot-tijdas (SNAPSHOTS)
    GET /v1/planvoorraad/gemeenten    → per-gemeente aggregaten (incl. lege)
    GET /v1/planvoorraad/{code}       → plannen van één gemeente + presence-tijdlijn

Classificatie v1 (uit één snapshot + verwijderdOp):
    WEG      = verwijderd_op IS NOT NULL  (gewenste eindtoestand)
    AANWEZIG = verwijderd_op IS NULL
TERUG/HERLEEFD (via relaties) volgt als verrijking zodra meerdere echte snapshots
zijn opgebouwd. Scope = legacy bestemmingsplannen (is_tam = false).

De presence-tijdlijn wordt gereconstrueerd uit `verwijderd_op` + `planstatus_datum`
over tweemaandelijkse virtuele snapshots vanaf 1-1-2024 (Ow-ingang). De RP-API
bewaart verwijderde plannen tot 2024-01-02, dus de volle transitieperiode is
backfillbaar. Vanaf nu groeien echte maandelijkse snapshots eraan vast.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from db import get_conn

router = APIRouter(prefix="/v1/planvoorraad", tags=["planvoorraad"])

CACHE_HEADER = "public, max-age=3600, s-maxage=86400"

# Tijdas: virtuele snapshots vanaf de Omgevingswet-ingang. De RP-API blijkt
# verwijderde plannen te bewaren tot 2024-01-02 (volle transitieperiode), dus de
# presence-tijdlijn is met terugwerkende kracht reconstrueerbaar vanaf 1-1-2024.
# Stap = 2 maanden → ~16 cellen, past bij de ponskaart-celgrootte.
AS_START = date(2024, 1, 1)
AS_STAP_MND = 2

# Scope van de leegloop-meting:
#   * geen TAM (TAM = nieuwe OW-inhoud via de IMRO-route, geen oude voorraad)
#   * confound-filter op planstatus: alleen vastgestelde/onherroepelijke/
#     geconsolideerde plannen. Verdwijnende ontwerp/voorontwerp-versies zijn
#     housekeeping bij onherroepelijk worden — geen pons — en zouden de
#     "weggehaald"-teller vervuilen.
SCOPE_FILTER = ("is_tam = false AND lower(planstatus) IN "
                "('onherroepelijk', 'vastgesteld', 'geconsolideerd')")


# ─────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────

class NationaalResponse(BaseModel):
    peildatum: date | None
    snapshots: list[str]          # ISO-datums, oplopend
    in_voorraad: int
    sinds_start_verdwenen: int
    weg_deze_snapshot: int
    gemeenten_leeg: int
    gemeenten_totaal: int


class GemeenteStat(BaseModel):
    naam: str
    code: str                     # overheidscode (gm....)
    provincie: str | None
    plans_nu: int
    verdwenen: int
    pct_af: float
    leeg: bool
    stip: str                     # green | yellow | gray


class PlanItem(BaseModel):
    id: str
    titel: str | None
    dossier: str | None
    planstatus: str | None
    dossierstatus: str | None
    classificatie: str            # WEG | AANWEZIG
    presence: list[bool]
    weg_idx: int
    weg_datum: str | None
    start_datum: str | None       # planstatus_datum (sinds wanneer in voorraad)


class GemeenteDetail(BaseModel):
    naam: str
    code: str
    bronhouder: str | None
    plans_nu: int
    verdwenen: int
    pct_af: float
    leeg: bool
    plans: list[PlanItem]


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _maandelijkse_as(tot: date) -> list[date]:
    """Virtuele-snapshot-reeks van AS_START t/m de maand van `tot`, met stap
    AS_STAP_MND maanden."""
    out = []
    y, m = AS_START.year, AS_START.month
    while (y, m) <= (tot.year, tot.month):
        out.append(date(y, m, 1))
        m += AS_STAP_MND
        while m > 12:
            m -= 12
            y += 1
    # Zorg dat de huidige peilmaand altijd de laatste cel is.
    if out[-1] < date(tot.year, tot.month, 1):
        out.append(date(tot.year, tot.month, 1))
    return out


def _laatste_snapshot(cur) -> dict | None:
    cur.execute("""SELECT snapshot_id, datum FROM wro.wro_snapshot
                   ORDER BY datum DESC, snapshot_id DESC LIMIT 1""")
    return cur.fetchone()


def _reconstrueer_presence(verwijderd_op, planstatus_datum, as_dates: list[date]) -> list[bool]:
    """Een plan is aanwezig op virtuele datum T als het toen al bestond en nog
    niet verwijderd was."""
    weg = verwijderd_op.date() if verwijderd_op else None
    start = planstatus_datum if (planstatus_datum and planstatus_datum > AS_START) else None
    res = []
    for t in as_dates:
        aanwezig = (start is None or start <= t) and (weg is None or weg > t)
        res.append(aanwezig)
    return res


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────

@router.get("/national", response_model=NationaalResponse)
def national(response: Response) -> NationaalResponse:
    response.headers["Cache-Control"] = CACHE_HEADER
    with get_conn() as conn:
        with conn.cursor() as cur:
            snap = _laatste_snapshot(cur)
            if not snap:
                raise HTTPException(503, "Nog geen planvoorraad-snapshot geladen")
            sid, peildatum = snap["snapshot_id"], snap["datum"]
            as_dates = _maandelijkse_as(peildatum)

            cur.execute(f"""
                SELECT
                    COUNT(*) FILTER (WHERE verwijderd_op IS NULL)     AS in_voorraad,
                    COUNT(*) FILTER (WHERE verwijderd_op IS NOT NULL) AS verdwenen,
                    COUNT(*) FILTER (WHERE verwijderd_op IS NOT NULL
                        AND verwijderd_op >= (DATE_TRUNC('month', %s::date)
                                              - INTERVAL '1 month')) AS weg_recent
                  FROM wro.wro_plan_observatie
                 WHERE snapshot_id = %s AND {SCOPE_FILTER}
            """, (peildatum, sid))
            r = cur.fetchone()

            # Lege manifesten: gemeenten zonder enig in-voorraad legacy-plan.
            cur.execute(f"""
                SELECT COUNT(*) AS leeg FROM core.gemeentegrens g
                 WHERE NOT EXISTS (
                    SELECT 1 FROM wro.wro_plan_observatie o
                     WHERE o.snapshot_id = %s AND {SCOPE_FILTER}
                       AND o.verwijderd_op IS NULL
                       AND o.bronhouder_code = g.overheidscode)
            """, (sid,))
            leeg = cur.fetchone()["leeg"]

    return NationaalResponse(
        peildatum=peildatum,
        snapshots=[d.isoformat() for d in as_dates],
        in_voorraad=int(r["in_voorraad"] or 0),
        sinds_start_verdwenen=int(r["verdwenen"] or 0),
        weg_deze_snapshot=int(r["weg_recent"] or 0),
        gemeenten_leeg=int(leeg or 0),
        gemeenten_totaal=342,
    )


@router.get("/gemeenten", response_model=list[GemeenteStat])
def gemeenten(response: Response) -> list[GemeenteStat]:
    response.headers["Cache-Control"] = CACHE_HEADER
    with get_conn() as conn:
        with conn.cursor() as cur:
            snap = _laatste_snapshot(cur)
            if not snap:
                raise HTTPException(503, "Nog geen planvoorraad-snapshot geladen")
            sid = snap["snapshot_id"]

            cur.execute(f"""
                SELECT g.naam, g.overheidscode AS code, g.provincie,
                       COUNT(o.*) FILTER (WHERE o.verwijderd_op IS NULL)     AS plans_nu,
                       COUNT(o.*) FILTER (WHERE o.verwijderd_op IS NOT NULL) AS verdwenen
                  FROM core.gemeentegrens g
                  LEFT JOIN wro.wro_plan_observatie o
                         ON o.snapshot_id = %s AND {SCOPE_FILTER}
                        AND o.bronhouder_code = g.overheidscode
                 GROUP BY g.naam, g.overheidscode, g.provincie
                 ORDER BY g.naam
            """, (sid,))
            rows = cur.fetchall()

    out = []
    for r in rows:
        nu = int(r["plans_nu"] or 0)
        weg = int(r["verdwenen"] or 0)
        start = nu + weg
        pct = (weg / start) if start else 0.0
        leeg = (nu == 0 and start > 0)  # leeg manifest na ooit plannen gehad te hebben
        stip = "green" if leeg else ("yellow" if weg > 0 else "gray")
        out.append(GemeenteStat(
            naam=r["naam"], code=r["code"], provincie=r["provincie"],
            plans_nu=nu, verdwenen=weg, pct_af=round(pct, 4),
            leeg=leeg, stip=stip,
        ))
    return out


@router.get("/{code}", response_model=GemeenteDetail)
def gemeente_detail(code: str, response: Response) -> GemeenteDetail:
    response.headers["Cache-Control"] = CACHE_HEADER
    with get_conn() as conn:
        with conn.cursor() as cur:
            snap = _laatste_snapshot(cur)
            if not snap:
                raise HTTPException(503, "Nog geen planvoorraad-snapshot geladen")
            sid, peildatum = snap["snapshot_id"], snap["datum"]
            as_dates = _maandelijkse_as(peildatum)

            cur.execute("SELECT naam, overheidscode FROM core.gemeentegrens WHERE overheidscode = %s", (code,))
            g = cur.fetchone()
            if not g:
                raise HTTPException(404, f"Onbekende gemeentecode: {code}")

            cur.execute(f"""
                SELECT identificatie, titel, dossier, planstatus, dossierstatus,
                       planstatus_datum, verwijderd_op, bronhouder_naam
                  FROM wro.wro_plan_observatie
                 WHERE snapshot_id = %s AND {SCOPE_FILTER}
                   AND bronhouder_code = %s
                 ORDER BY (verwijderd_op IS NOT NULL), titel
            """, (sid, code))
            rows = cur.fetchall()

    plans = []
    bronhouder = None
    nu = weg = 0
    for r in rows:
        bronhouder = bronhouder or r["bronhouder_naam"]
        presence = _reconstrueer_presence(r["verwijderd_op"], r["planstatus_datum"], as_dates)
        is_weg = r["verwijderd_op"] is not None
        if is_weg:
            weg += 1
        else:
            nu += 1
        # weg_idx = eerste virtuele snapshot waarin het plan ontbrak
        weg_idx = -1
        if is_weg:
            for i, aanwezig in enumerate(presence):
                if not aanwezig:
                    weg_idx = i
                    break
        plans.append(PlanItem(
            id=r["identificatie"], titel=r["titel"], dossier=r["dossier"],
            planstatus=r["planstatus"], dossierstatus=r["dossierstatus"],
            classificatie="WEG" if is_weg else "AANWEZIG",
            presence=presence,
            weg_idx=weg_idx,
            weg_datum=r["verwijderd_op"].date().isoformat() if r["verwijderd_op"] else None,
            start_datum=r["planstatus_datum"].isoformat() if r["planstatus_datum"] else None,
        ))

    start = nu + weg
    return GemeenteDetail(
        naam=g["naam"], code=g["overheidscode"], bronhouder=bronhouder,
        plans_nu=nu, verdwenen=weg,
        pct_af=round(weg / start, 4) if start else 0.0,
        leeg=(nu == 0 and start > 0),
        plans=plans,
    )
