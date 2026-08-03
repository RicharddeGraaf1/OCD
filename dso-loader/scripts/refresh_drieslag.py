"""Refresh alle materialized views + niet-annoteerbaar-markering voor de
drieslag tekst↔object in de juiste volgorde.

Gebruik: draai na een ingest of backfill die p2p.tekst_element,
p2p.tekst_inline_referentie, p2p.gebiedsaanwijzing, p2p.activiteit,
p2p.norm, p2p.activiteit_locatieaanduiding, p2p.juridische_regel,
p2p.locatie_basisgeo of p2p.gio_basisgeo wijzigt.

Draait automatisch mee in fase 6 (post) van scripts/full_sync.py. Alleen
`--skip-post` slaat 'm over; dan zijn de MV's hieronder verouderd.

Refresh-modus (gebruikerskeuze 2026-08-01): standaard een **gewone** REFRESH.
Die zet de view kort op slot (ACCESS EXCLUSIVE), wat 's nachts prima is, en is
veel goedkoper dan CONCURRENTLY — dat bouwt een volledige tweede kopie en
verschilt die. Gemeten op prod: `naammatch_signaal` (2,1 GB / 6,2M rijen) liep
met CONCURRENTLY ruim 2 uur en werd IO-gebonden onder de 2 GB-geheugencap van
de Railway-container. Draai je overdag terwijl viewer/bot doorlezen: gebruik
`--concurrently`.

Volgorde:
  1. Niet-annoteerbaar markeren (recursive UPDATE op tekst_element)
  1b. REFRESH v2a.mv_element_hash (element→content-hash voor hertalingen, ~10s)
  1c. REFRESH p2p.gio_locatie (GIO ↔ locatie-koppeling)
  1d. REFRESH p2p.ala_punt (activiteit-op-locatie voor de punt-endpoints, ~2s)
  2. REFRESH p2p.naammatch_signaal (de duurste — 20-35 min lokaal)
  3. REFRESH p2p.naammatch_signaal_intra (~10s; hangt af van #2)
  4. REFRESH p2p.tekst_object_consistentie_mv (~30s; hangt af van #3)
  5. REFRESH p2p.gio_referentie_consistentie_mv (GIO-tak, ~3 min; onafhankelijk
     van de naammatch-keten, hangt af van geo_informatieobject.naam_informatieobject
     + tekst_inline_referentie)

Run: python scripts/refresh_drieslag.py
"""
import os
import sys
import argparse
import time

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# setdefault raakt alleen kindprocessen; de eigen streams moeten zelf om, anders
# knalt al `--help` op de ↔ in de docstring wanneer de console cp1252 is.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from src.db import get_conn

# (naam, matview-of-sqlbestand, teller)
STAPPEN = [
    ("Niet-annoteerbaar markeren",
     "scripts/2026-05-add-niet-annoteerbaar.sql",
     "SELECT COUNT(*) FILTER (WHERE is_niet_annoteerbaar) AS n FROM p2p.tekst_element"),
    ("v2a.mv_element_hash",  # element→content-hash koppeling (hertalingen), ~10s
     "v2a.mv_element_hash",
     "SELECT COUNT(*) AS n FROM v2a.mv_element_hash"),
    ("p2p.gio_locatie",
     "p2p.gio_locatie",
     "SELECT COUNT(*) AS n FROM p2p.gio_locatie"),
    # ~2s. Voedt de punt-endpoints van de viewer (welke activiteiten gelden
    # hier?). Verouderd deze niet mee, dan blijft de viewer oude activiteiten
    # tonen zonder foutmelding — zie 2026-07-add-ala-punt-mv.sql.
    ("p2p.ala_punt",
     "p2p.ala_punt",
     "SELECT COUNT(*) AS n FROM p2p.ala_punt"),
    ("p2p.naammatch_signaal",
     "p2p.naammatch_signaal",
     "SELECT COUNT(*) AS n FROM p2p.naammatch_signaal"),
    ("p2p.naammatch_signaal_intra",
     "p2p.naammatch_signaal_intra",
     "SELECT COUNT(*) AS n FROM p2p.naammatch_signaal_intra"),
    ("p2p.tekst_object_consistentie_mv",
     "p2p.tekst_object_consistentie_mv",
     "SELECT COUNT(*) AS n FROM p2p.tekst_object_consistentie_mv"),
    ("p2p.gio_referentie_consistentie_mv",
     "p2p.gio_referentie_consistentie_mv",
     "SELECT COUNT(*) AS n FROM p2p.gio_referentie_consistentie_mv"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--concurrently", action="store_true",
                    help="ververs met CONCURRENTLY: lezers blijven doordraaien, "
                         "maar Postgres bouwt een volledige tweede kopie en "
                         "verschilt die — fors duurder. Gebruik dit alleen als "
                         "de viewer/bot tijdens de refresh beschikbaar moet "
                         "blijven (dus overdag), niet in de nachtelijke sync.")
    ap.add_argument("--vanaf", type=int, default=1, metavar="N",
                    help="hervat bij stap N (1-based). Elke stap commit apart, "
                         "dus na een afgebroken run hoef je de al geslaagde "
                         "stappen niet over te doen. Zie de nummering hierboven.")
    ap.add_argument("--registreer", action="store_true",
                    help="leg deze run vast in core.load_run als bron 'drieslag-mv', "
                         "zodat hij in het data-actualiteit-dashboard verschijnt. "
                         "NIET gebruiken vanuit full_sync — die wikkelt de aanroep "
                         "al in een eigen load_run, en dan krijg je twee regels.")
    args = ap.parse_args()

    if not 1 <= args.vanaf <= len(STAPPEN):
        ap.error(f"--vanaf moet tussen 1 en {len(STAPPEN)} liggen")

    if args.registreer:
        from src.run_log import load_run
        with load_run("drieslag-mv", scope=f"losse run vanaf stap {args.vanaf}"
                                           f"{' (concurrently)' if args.concurrently else ''}"):
            return _draai(args)
    return _draai(args)


def _draai(args):
    # Default is een gewone REFRESH: die neemt een ACCESS EXCLUSIVE lock, dus
    # queries op de view wachten tot hij klaar is. Dat is 's nachts akkoord
    # (gebruikerskeuze 2026-08-01) en aanzienlijk goedkoper. Gemeten op prod:
    # naammatch_signaal (2,1 GB / 6,2M rijen) liep met CONCURRENTLY >2 uur en
    # werd IO-gebonden onder de 2 GB-geheugencap van de Railway-container; de
    # dubbele kopie past daar simpelweg niet in.
    modus = "CONCURRENTLY " if args.concurrently else ""
    print(f"Refresh-modus: {'CONCURRENTLY (lezers blijven door)' if args.concurrently else 'exclusief (sneller; view is even op slot)'}\n",
          flush=True)

    conn = get_conn()
    cur = conn.cursor()
    # Parallelisme UIT: zowel de lokale Docker-PostGIS als de Railway-container
    # hebben een kleine /dev/shm; een parallelle REFRESH faalt anders met
    # "could not resize shared memory segment / No space left on device"
    # (tekst_object_consistentie_mv sloopte hier eerder op). SET (zonder LOCAL)
    # blijft de hele sessie staan. get_conn() zet dit al bij een prod-DSN; hier
    # onvoorwaardelijk zodat ook lokaal geen /dev/shm-fout optreedt.
    cur.execute("SET max_parallel_workers_per_gather = 0")
    cur.execute("SET max_parallel_maintenance_workers = 0")
    conn.commit()
    t_total = time.time()

    for i, (naam, sql_or_view, count_query) in enumerate(STAPPEN, 1):
        if i < args.vanaf:
            print(f"[{i}/{len(STAPPEN)}] {naam} — overgeslagen (--vanaf {args.vanaf})",
                  flush=True)
            continue
        print(f"[{i}/{len(STAPPEN)}] {naam}...", flush=True)
        t0 = time.time()

        # Stap 1 is een SQL-bestand; de rest zijn matview-namen.
        if sql_or_view.endswith(".sql"):
            with open(sql_or_view, encoding="utf-8") as f:
                cur.execute(f.read())
        else:
            cur.execute(f"REFRESH MATERIALIZED VIEW {modus}{sql_or_view}")
        conn.commit()

        elapsed = time.time() - t0
        cur.execute(count_query)
        n = cur.fetchone()["n"]
        print(f"  Klaar in {elapsed/60:.1f} min — {n:,} rijen", flush=True)

    print(f"\nTotaal: {(time.time()-t_total)/60:.1f} min", flush=True)
    print("\n=== Verdeling klassen ===", flush=True)
    cur.execute(
        "SELECT consistentie_klasse, COUNT(*) AS n "
        "FROM p2p.tekst_object_consistentie_mv "
        "GROUP BY consistentie_klasse ORDER BY n DESC"
    )
    for r in cur.fetchall():
        print(f"  {r['consistentie_klasse']:35} {r['n']:>9,}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
