"""Fase-regressiecheck: vergelijkt wat een sync deed met wat hij eerder deed.

Waarom
------
Het sync-rapport meldde tot 2026-08-08 per fase "ok" of een exceptietelling. Een
fase die 342 keer een lege respons verwerkt is dan "ok". Zo bleef de i2a-loader
vier maanden lang leeg draaien (vault gaps.md G-117), stond
`regeling_load.n_locatie` voor 2.000 regelingen op nul (G-118) en groeide de
GIO-laag sinds juli niet meer (G-119) — drie keer een nul die als succes werd
gerapporteerd.

Deze module kijkt daarom niet naar exceptions maar naar **aangroei**. De
boekhouding daarvoor lag er al: `load_run` schrijft `details.totaal_voor` en
`details.totaal_na` per fase, gevoed door `core.bron_totaal()`. Wat ontbrak was
de vergelijking.

De drie signalen
----------------
1. **Daling** — `totaal_na < totaal_voor`. Er is data verdwenen tijdens een
   fase die zichzelf "ok" noemt. Altijd melden.
2. **Structurele stilstand** — geen aangroei, én de vorige `STIL_DREMPEL`
   geslaagde runs van die bron groeiden evenmin, terwijl de bron ooit wél
   groeide. Dit is het i2a-geval: elke afzonderlijke run kon "niets nieuws"
   betekenen, de reeks niet.
3. **Verwachting niet gehaald** — de preview zei dat er N bij zou komen en het
   werden er minder. Dit is de preview-vs-uitkomst-check die het runbook §5 als
   belangrijkste openstaande verbetering noemde; de verwachting wordt aan het
   begin van de run in `audit.sync_run.metrics->'verwacht'` gezet.

Bewust ruw gehouden: alleen deze drie, met grove drempels. Een check die
iedereen wegklikt is slechter dan geen check.

Losse run (leest alleen):
    python -m src.sync_regressie            # laatste sync
    python -m src.sync_regressie --run-id 6
"""

from __future__ import annotations

import argparse

from src.db import get_conn

# Aantal voorafgaande geslaagde runs zonder aangroei voordat stilstand
# "structureel" heet. 2 betekent: deze run plus twee eerdere, dus drie op rij.
STIL_DREMPEL = 2

# Bronnen waarvoor nul-aangroei normaal is: volledige herbouw of trage bronnen.
# Voor deze telt signaal 2 niet; signaal 1 en 3 wél.
GEEN_AANGROEI_VERWACHT = {
    "pdok-gemeentegrenzen",   # 1x per jaar, herindelingen
    "drieslag-mv",            # MV-refresh, geen eigen totaal
    "health-mv",
    "post-processing",
}


def _fasen_van_run(cur, sync_start, sync_eind) -> list[dict]:
    """De load_run-rijen die binnen deze sync vallen.

    Bovengrens is de start van de vólgende sync-run, niet `klaar_op`: losse
    herstelruns ná de sync (zoals de i2a-herstelrun van 2026-08-03) horen bij
    het venster van die sync en niet bij de vorige. Zonder die grens telde
    dezelfde bron dubbel.

    Bij meerdere runs van één bron binnen het venster telt de laatste — dat is
    de uitkomst waar je mee verder gaat.
    """
    cur.execute("""
        SELECT DISTINCT ON (bron)
               bron, status, n_verwerkt, n_fout, started_at,
               (details->>'totaal_voor')::bigint AS voor,
               (details->>'totaal_na')::bigint   AS na
          FROM core.load_run
         WHERE started_at >= %s
           AND (%s::timestamptz IS NULL OR started_at < %s::timestamptz)
         ORDER BY bron, started_at DESC
    """, (sync_start, sync_eind, sync_eind))
    return cur.fetchall()


def _eerdere_aangroei(cur, bron: str, voor_tijdstip, limiet: int) -> list[int | None]:
    """Aangroei van de voorgaande geslaagde runs van deze bron, nieuwste eerst."""
    cur.execute("""
        SELECT (details->>'totaal_na')::bigint - (details->>'totaal_voor')::bigint AS delta
          FROM core.load_run
         WHERE bron = %s AND started_at < %s AND status IN ('ok','deels')
               AND details ? 'totaal_na' AND details ? 'totaal_voor'
         ORDER BY started_at DESC
         LIMIT %s
    """, (bron, voor_tijdstip, limiet))
    return [r["delta"] for r in cur.fetchall()]


def _ooit_gegroeid(cur, bron: str) -> bool:
    """Heeft deze bron ooit aangroei laten zien? Zo niet, dan is stilstand geen
    regressie maar de normale toestand — dan meldt signaal 2 niets."""
    cur.execute("""
        SELECT 1 FROM core.load_run
         WHERE bron = %s AND (details->>'totaal_na')::bigint
                            > (details->>'totaal_voor')::bigint
         LIMIT 1
    """, (bron,))
    return cur.fetchone() is not None


def _verwachting(cur, run_id: int) -> dict:
    cur.execute("SELECT metrics->'verwacht' AS v FROM audit.sync_run WHERE run_id = %s",
                (run_id,))
    r = cur.fetchone()
    return (r["v"] if r and r["v"] else {}) or {}


def regressie_regels(run_id: int | None = None) -> list[str]:
    """Geeft de rapportregels. Lege lijst = niets bijzonders gevonden."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        if run_id is None:
            cur.execute("SELECT run_id FROM audit.sync_run ORDER BY run_id DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                return ["- geen sync-run gevonden"]
            run_id = row["run_id"]

        cur.execute("SELECT gestart_op FROM audit.sync_run WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
        if not row:
            return [f"- sync-run {run_id} niet gevonden"]
        sync_start = row["gestart_op"]

        cur.execute("SELECT min(gestart_op) AS g FROM audit.sync_run WHERE gestart_op > %s",
                    (sync_start,))
        sync_eind = (cur.fetchone() or {}).get("g")

        verwacht = _verwachting(cur, run_id)
        regels: list[str] = []

        for fase in _fasen_van_run(cur, sync_start, sync_eind):
            bron, voor, na = fase["bron"], fase["voor"], fase["na"]
            if voor is None or na is None:
                continue                       # bron zonder totaal-telling
            delta = na - voor

            # 1. daling
            if delta < 0:
                regels.append(
                    f"- ⚠️ **{bron}**: totaal daalde van {voor:,} naar {na:,} "
                    f"({delta:+,}) terwijl de fase '{fase['status']}' meldt")
                continue

            # 3. verwachting niet gehaald (gaat vóór 2: concreter signaal)
            if bron in verwacht:
                n_verwacht = verwacht[bron]
                if isinstance(n_verwacht, int) and delta < n_verwacht:
                    regels.append(
                        f"- ⚠️ **{bron}**: preview verwachtte +{n_verwacht:,}, "
                        f"geladen +{delta:,}")
                    continue

            # 2. structurele stilstand
            if delta == 0 and bron not in GEEN_AANGROEI_VERWACHT:
                eerder = _eerdere_aangroei(cur, bron, fase["started_at"], STIL_DREMPEL)
                stil = [d for d in eerder if d is not None and d == 0]
                if len(stil) >= STIL_DREMPEL and _ooit_gegroeid(cur, bron):
                    regels.append(
                        f"- ⚠️ **{bron}**: {len(stil) + 1} runs op rij zonder aangroei "
                        f"(staat op {na:,}) — fase meldt '{fase['status']}'")

        return regels
    finally:
        conn.close()


def rapport_sectie(run_id: int | None = None) -> list[str]:
    """Regels voor het sync-rapport, altijd met een uitkomst."""
    regels = regressie_regels(run_id)
    if not regels:
        return ["- geen afwijkingen ten opzichte van eerdere runs"]
    return regels


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", type=int, default=None,
                    help="sync-run om te controleren (default: de laatste)")
    args = ap.parse_args()
    for r in rapport_sectie(args.run_id):
        print(r)


if __name__ == "__main__":
    main()
