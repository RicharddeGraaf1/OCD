"""Herstel de dode GIO-verwijzingen: laad de GIO's en leg de koppeling alsnog.

Het probleem (vault gaps.md G-119)
----------------------------------
Een regeltekst verwijst via ExtIoRef/IntIoRef naar een geometrie-informatie-
object. Die verwijzing wordt bij het laden geresolved door
`inline_referentie.resolve_target_soort()` — maar `api_loader` roept die aan
direct na het laden van de tekst, en op dat moment bestaan de GIO's nog niet:
`gio_zip.process_zip` hangt niet aan de sync maar aan losse backfill-scripts.

Gevolg, gemeten 2026-08-08: 17.582 verwijzingen over 125 regelingen wijzen
nergens naar. In de viewer is dat een regeltekst met een werkingsgebied dat niet
te tonen is.

Wat dit script doet, per regeling
---------------------------------
1. ZIP ophalen via de Ozon Download API (gecached in data/downloads/ow/).
2. `process_zip` — vult `p2p.geo_informatieobject` + basisgeo-junctions.
3. `resolve_target_soort` — legt de koppeling.

Stap 3 werkt pas sinds de conditie in pass A/B is verruimd van
`target_soort IS NULL` naar `target_gio_expression IS NULL`: een rij die al
'GIO' heet zonder koppeling werd anders als afgehandeld beschouwd. Op
Groningen ging het daarmee van 0 naar 483 van de 483 gekoppeld.

Wat het NIET oplost: verwijzingen naar GIO's die de DSO niet meer levert. Bij
Groningen bestond 122 van de 244 unieke targets in de ZIP; de rest hoort bij
eerdere versies die niet opnieuw worden meegeleverd. Die blijven dood, en dat
telt het rapport apart.

Draaien:
    python scripts/herstel_gio_koppeling.py                 # droogloop
    python scripts/herstel_gio_koppeling.py --ja            # echt
    python scripts/herstel_gio_koppeling.py --ja --limiet 10
"""

import argparse
import os
import sys
import time

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ".")
sys.path.insert(0, "src")

from src.db import get_conn                                     # noqa: E402
from src.loaders.gio_zip import process_zip                     # noqa: E402
from src.loaders.inline_referentie import resolve_target_soort  # noqa: E402
from src.loaders.ow_loader import _download_regeling            # noqa: E402

KANDIDATEN = """
SELECT r.frbr_expression, r.frbr_work, r.opschrift,
       count(*) AS dode_refs
  FROM p2p.tekst_inline_referentie tir
  JOIN p2p.tekst_element te ON te.id = tir.tekst_element_id
  JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
 WHERE tir.target_soort = 'GIO' AND tir.target_gio_expression IS NULL
   AND NOT r.inactief
 GROUP BY 1, 2, 3
 ORDER BY count(*) DESC
"""


def log(*a):
    print(*a, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ja", action="store_true", help="echt uitvoeren")
    ap.add_argument("--limiet", type=int, default=None, help="maximaal N regelingen")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(KANDIDATEN)
    kandidaten = cur.fetchall()
    if args.limiet:
        kandidaten = kandidaten[:args.limiet]

    totaal_dood = sum(k["dode_refs"] for k in kandidaten)
    log(f"{len(kandidaten)} regelingen met samen {totaal_dood:,} dode GIO-verwijzingen")
    if not args.ja:
        for k in kandidaten[:15]:
            log(f"  {k['dode_refs']:>6,}  {(k['opschrift'] or '')[:58]}")
        if len(kandidaten) > 15:
            log(f"  … en {len(kandidaten) - 15} meer")
        log("\nDROOGLOOP — draai met --ja om te herstellen.")
        conn.close()
        return

    hersteld = mislukt = onherstelbaar = 0
    for i, k in enumerate(kandidaten, 1):
        expr, work = k["frbr_expression"], k["frbr_work"]
        naam = (k["opschrift"] or "")[:44]
        t0 = time.time()
        try:
            zp = _download_regeling(work)
            if not zp:
                mislukt += 1
                log(f"[{i}/{len(kandidaten)}] {naam}: geen ZIP")
                continue
            process_zip(zp, conn, regeling_expression=expr)
            res = resolve_target_soort(conn, regeling_expression=expr)
            conn.commit()

            cur.execute("""SELECT count(*) FILTER (WHERE target_gio_expression IS NULL) AS rest
                             FROM p2p.tekst_inline_referentie tir
                             JOIN p2p.tekst_element te ON te.id = tir.tekst_element_id
                            WHERE te.regeling_expression = %s AND tir.target_soort='GIO'""",
                        (expr,))
            rest = cur.fetchone()["rest"]
            gekoppeld = res["extioref_to_gio"] + res["intioref_via_chain"]
            hersteld += gekoppeld
            onherstelbaar += rest
            log(f"[{i}/{len(kandidaten)}] {naam}: +{gekoppeld} gekoppeld, "
                f"{rest} blijft dood ({time.time() - t0:.0f}s)")
        except Exception as e:
            conn.rollback()
            mislukt += 1
            log(f"[{i}/{len(kandidaten)}] {naam}: FOUT {str(e)[:110]}")

    log(f"\nKlaar — {hersteld:,} verwijzingen gekoppeld, {onherstelbaar:,} bleven dood "
        f"(GIO niet meer geleverd), {mislukt} regelingen mislukt")
    conn.close()


if __name__ == "__main__":
    main()
