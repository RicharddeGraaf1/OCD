"""Zet het GIO-koppelingsherstel (G-119) over naar productie.

Waarom niet het gewone replicatiescript
---------------------------------------
`repliceer_p2p_naar_prod.py --expressies` werkt, maar kopieert álles van de
betrokken regelingen: 1,4 miljoen rijen voor 121 regelingen, terwijl er maar
drie dingen zijn veranderd. Dat is bandbreedte en tijd voor niets, over een
proxy die we liever kort openzetten.

Dit script verplaatst alleen wat het herstel heeft aangeraakt:

  1. `p2p.geo_informatieobject`  — de GIO's die lokaal zijn bijgeladen
  2. `p2p.gio_basisgeo`          — hun basisgeo-junctions
  3. `p2p.tekst_inline_referentie` — alleen de kolommen `target_soort` en
     `target_gio_expression`, en alleen waar prod nog niets heeft

Waarom een update op `id` mag
-----------------------------
`tekst_inline_referentie.id` is een identity-kolom, dus normaal geen
betrouwbare sleutel tussen twee databases. Hier wel, en dat wordt vooraf
bewezen in plaats van aangenomen: het script vergelijkt count, min, max én een
md5 over alle id's binnen de scope. Wijkt er iets af, dan stopt hij. Gemeten
2026-08-08 over 121 regelingen: n=114.903, min=440404, max=670541, md5 gelijk.

Zonder die controle zou een update op id stilletjes de verkeerde verwijzingen
naar een werkingsgebied laten wijzen — het soort fout dat pas maanden later
opvalt.

Draaien:
    python scripts/herstel_gio_naar_prod.py --expressies C:/tmp/....txt
    python scripts/herstel_gio_naar_prod.py --expressies C:/tmp/....txt --ja
"""

import argparse
import os
import sys
import time

import psycopg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LOKAAL = os.environ.get("OCD_DB_URL", "postgresql://postgres:postgres@localhost:5434/dso")
PROD = os.environ["PROD_DB_URL"]

PARITEIT = """
SELECT count(*), coalesce(min(tir.id),0), coalesce(max(tir.id),0),
       coalesce(md5(string_agg(tir.id::text, ',' ORDER BY tir.id)), '-')
  FROM p2p.tekst_inline_referentie tir
  JOIN p2p.tekst_element te ON te.id = tir.tekst_element_id
 WHERE te.regeling_expression = ANY(%s)
"""


def log(*a):
    print(*a, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expressies", required=True, metavar="BESTAND")
    ap.add_argument("--ja", action="store_true")
    args = ap.parse_args()

    exprs = [r.strip() for r in open(args.expressies, encoding="utf-8") if r.strip()]
    log(f"{len(exprs)} regelingen in scope")

    lconn = psycopg.connect(LOKAAL)
    pconn = psycopg.connect(PROD, connect_timeout=30)
    lc, pc = lconn.cursor(), pconn.cursor()
    pc.execute("SET max_parallel_workers_per_gather = 0")

    # ── pariteit van de id-ruimte, vóór er iets gebeurt ──
    l = lc.execute(PARITEIT, (exprs,)).fetchone()
    p = pc.execute(PARITEIT, (exprs,)).fetchone()
    log(f"  lokaal n={l[0]:,} min={l[1]} max={l[2]} md5={l[3][:16]}")
    log(f"  prod   n={p[0]:,} min={p[1]} max={p[2]} md5={p[3][:16]}")
    if l != p:
        sys.exit("STOP: id-ruimte van tekst_inline_referentie verschilt — een "
                 "update op id zou de verkeerde rijen raken.")
    log("  pariteit bevestigd — id's zijn uitwisselbaar")

    # ── 1. GIO's die prod mist ──
    lc.execute("SELECT frbr_expression FROM p2p.geo_informatieobject "
               "WHERE regeling_expression = ANY(%s)", (exprs,))
    lok_gio = {r[0] for r in lc.fetchall()}
    pc.execute("SELECT frbr_expression FROM p2p.geo_informatieobject "
               "WHERE regeling_expression = ANY(%s)", (exprs,))
    prod_gio = {r[0] for r in pc.fetchall()}
    nieuw_gio = lok_gio - prod_gio

    # ── 2. koppelingen die prod mist ──
    lc.execute("""SELECT count(*) FROM p2p.tekst_inline_referentie tir
                    JOIN p2p.tekst_element te ON te.id = tir.tekst_element_id
                   WHERE te.regeling_expression = ANY(%s)
                     AND tir.target_gio_expression IS NOT NULL""", (exprs,))
    n_lok_kop = lc.fetchone()[0]
    pc.execute("""SELECT count(*) FROM p2p.tekst_inline_referentie tir
                    JOIN p2p.tekst_element te ON te.id = tir.tekst_element_id
                   WHERE te.regeling_expression = ANY(%s)
                     AND tir.target_gio_expression IS NOT NULL""", (exprs,))
    n_prod_kop = pc.fetchone()[0]

    log(f"  GIO's      : lokaal {len(lok_gio):,} / prod {len(prod_gio):,} → {len(nieuw_gio):,} te kopiëren")
    log(f"  koppelingen: lokaal {n_lok_kop:,} / prod {n_prod_kop:,} → {n_lok_kop - n_prod_kop:,} te zetten")

    if not args.ja:
        log("\nDROOGLOOP — draai met --ja.")
        return

    t0 = time.time()

    # 1. geo_informatieobject
    if nieuw_gio:
        kols = [r[0] for r in lc.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='p2p' AND table_name='geo_informatieobject' "
            "AND is_generated <> 'ALWAYS' ORDER BY ordinal_position").fetchall()]
        k = ", ".join(f'"{x}"' for x in kols)
        pc.execute(f"CREATE TEMP TABLE stg_gio ON COMMIT DROP AS "
                   f"SELECT {k} FROM p2p.geo_informatieobject WITH NO DATA")
        with lc.copy(f"COPY (SELECT {k} FROM p2p.geo_informatieobject "
                     f"WHERE frbr_expression = ANY(%s)) TO STDOUT", (list(nieuw_gio),)) as uit:
            with pc.copy(f"COPY stg_gio ({k}) FROM STDIN") as inn:
                for blok in uit:
                    inn.write(blok)
        pc.execute(f"INSERT INTO p2p.geo_informatieobject ({k}) SELECT {k} FROM stg_gio "
                   f"ON CONFLICT (frbr_expression) DO NOTHING")
        log(f"  geo_informatieobject: {pc.rowcount:,} ingevoegd")

    # 2. gio_basisgeo
    pc.execute("CREATE TEMP TABLE stg_gb (gio_frbr text, basisgeo_id text) ON COMMIT DROP")
    with lc.copy("COPY (SELECT gio_frbr, basisgeo_id FROM p2p.gio_basisgeo "
                 "WHERE gio_frbr = ANY(%s)) TO STDOUT", (list(lok_gio),)) as uit:
        with pc.copy("COPY stg_gb (gio_frbr, basisgeo_id) FROM STDIN") as inn:
            for blok in uit:
                inn.write(blok)
    pc.execute("INSERT INTO p2p.gio_basisgeo (gio_frbr, basisgeo_id) "
               "SELECT gio_frbr, basisgeo_id FROM stg_gb ON CONFLICT DO NOTHING")
    log(f"  gio_basisgeo: {pc.rowcount:,} ingevoegd")

    # 3. de koppelingen zelf — alleen id + de twee kolommen
    pc.execute("CREATE TEMP TABLE stg_kop (id bigint, soort text, gio text) ON COMMIT DROP")
    with lc.copy("""COPY (SELECT tir.id, tir.target_soort, tir.target_gio_expression
                           FROM p2p.tekst_inline_referentie tir
                           JOIN p2p.tekst_element te ON te.id = tir.tekst_element_id
                          WHERE te.regeling_expression = ANY(%s)
                            AND tir.target_gio_expression IS NOT NULL) TO STDOUT""",
                 (exprs,)) as uit:
        with pc.copy("COPY stg_kop (id, soort, gio) FROM STDIN") as inn:
            for blok in uit:
                inn.write(blok)
    pc.execute("""UPDATE p2p.tekst_inline_referentie tir
                     SET target_soort = s.soort, target_gio_expression = s.gio
                    FROM stg_kop s
                   WHERE tir.id = s.id
                     AND tir.target_gio_expression IS DISTINCT FROM s.gio""")
    log(f"  koppelingen bijgewerkt: {pc.rowcount:,}")

    pconn.commit()
    log(f"\nKlaar in {time.time() - t0:.0f}s.")


if __name__ == "__main__":
    main()
