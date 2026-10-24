# -*- coding: utf-8 -*-
"""Zet Wro-planteksten v2 (+ koppelingen + onderwerp-indeling) over van lokaal naar productie.

Zie docs/wro-teksten-herstel-plan.md. Per plan idempotent: op prod worden de rijen
van dat plan eerst verwijderd en dan opnieuw geladen, in één transactie per plan.
De API (ocd-api, `_wro_v2_geladen`) gebruikt v2 voor een plan zodra het in
`wro.wro_tekst_laadstand` staat; plannen die niet zijn overgezet blijven op de oude
tabel. Een deelset overzetten is dus veilig: elk plan is óf helemaal v2, óf helemaal oud.

Terugdraaien voor een plan: zijn rij in wro.wro_tekst_laadstand op prod verwijderen
(dan valt de API terug op de oude tabel). Alles terug: de vier tabellen droppen.

LET OP — volume: op 2026-10-23 stond de Railway-volume op 82,2 / 100 GB. Een
volledige overzet is ~10 GB (gemeten: ~158 KB per plan). Daarom een harde grens:
--max-mb (standaard 500). Verhoog die pas als de volume groter is gemaakt
(Railway-dashboard; kan niet via de CLI). Stand 2026-10-24: volume 150 GB, volledige
overzet gedaan (48.161 plannen, prod-DB 74 -> 84 GB, 216 min).

    python scripts/wro_v2_naar_productie.py --bronhouder gm0402,gm0014         # droogloop
    python scripts/wro_v2_naar_productie.py --bronhouder gm0402,gm0014 --ja
    python scripts/wro_v2_naar_productie.py --alle --max-mb 12000 --ja         # pas na volume-vergroting
"""
import argparse
import os
import sys
import time

import psycopg
from dotenv import load_dotenv

HIER = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HIER, "..", ".env"))
LOKAAL = os.environ.get("DSO_DB") or "postgresql://postgres:postgres@localhost:5434/dso"
PROD = os.environ["PROD_DB_URL"]

DDL = """
CREATE TABLE IF NOT EXISTS wro.wro_tekst_object_v2 (
    instrument_idn TEXT NOT NULL, identificatie TEXT NOT NULL, volgnummer INT NOT NULL,
    niveau INT NOT NULL, parent_id TEXT NULL, object_type TEXT NOT NULL, artikel_type TEXT NULL,
    label TEXT NULL, nummer TEXT NULL, naam TEXT NULL, inhoud TEXT NULL,
    PRIMARY KEY (instrument_idn, identificatie));
CREATE INDEX IF NOT EXISTS wro_tekst_v2_parent_idx ON wro.wro_tekst_object_v2 (instrument_idn, parent_id);
CREATE TABLE IF NOT EXISTS wro.wro_tekst_link (
    instrument_idn TEXT NOT NULL, tekst_id TEXT NOT NULL, planobject_id TEXT NOT NULL, soort TEXT NOT NULL,
    PRIMARY KEY (instrument_idn, tekst_id, planobject_id));
CREATE INDEX IF NOT EXISTS wro_tekst_link_po_idx ON wro.wro_tekst_link (planobject_id);
CREATE TABLE IF NOT EXISTS wro.wro_tekst_laadstand (
    instrument_idn TEXT PRIMARY KEY, n_teksten INT NULL, n_links INT NULL, n_paginas INT NULL,
    fout TEXT NULL, geladen_op TIMESTAMPTZ NOT NULL DEFAULT now(),
    bron TEXT NOT NULL DEFAULT 'ihr-teksten', bron_url TEXT NULL, html_reden TEXT NULL);
CREATE TABLE IF NOT EXISTS v2a.wro_artikel_indeling (
    instrument_idn TEXT NOT NULL, tekst_id TEXT NOT NULL, categorie TEXT NULL,
    subcategorie TEXT NULL, herkomst TEXT NULL, PRIMARY KEY (instrument_idn, tekst_id));
"""

TABELLEN = [
    ("wro.wro_tekst_object_v2", "instrument_idn, identificatie, volgnummer, niveau, parent_id, object_type, "
                                "artikel_type, label, nummer, naam, inhoud"),
    ("wro.wro_tekst_link", "instrument_idn, tekst_id, planobject_id, soort"),
    ("v2a.wro_artikel_indeling", "instrument_idn, tekst_id, categorie, subcategorie, herkomst"),
]
# laadstand als laatste: pas als die rij er staat, gaat de API het plan uit v2 lezen
LAADSTAND = ("wro.wro_tekst_laadstand", "instrument_idn, n_teksten, n_links, n_paginas, fout, geladen_op, "
                                        "bron, bron_url, html_reden")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bronhouder")
    ap.add_argument("--idn")
    ap.add_argument("--alle", action="store_true")
    ap.add_argument("--max-mb", type=float, default=500)
    ap.add_argument("--ja", action="store_true")
    a = ap.parse_args()
    if not (a.bronhouder or a.idn or a.alle):
        ap.error("geef --bronhouder, --idn of --alle")

    with psycopg.connect(LOKAAL) as lc, psycopg.connect(PROD, connect_timeout=30) as pc:
        l, p = lc.cursor(), pc.cursor()
        waar, arg = "ls.fout IS NULL AND ls.n_teksten > 0", []
        if a.idn:
            waar += " AND ls.instrument_idn = ANY(%s)"
            arg.append(a.idn.split(","))
        elif a.bronhouder:
            waar += " AND ri.bronhouder = ANY(%s)"
            arg.append(a.bronhouder.split(","))
        l.execute(f"""SELECT ls.instrument_idn FROM wro.wro_tekst_laadstand ls
                      JOIN wro.ruimtelijk_instrument ri ON ri.idn = ls.instrument_idn
                      WHERE {waar} ORDER BY 1""", arg)
        plannen = [r[0] for r in l.fetchall()]
        l.execute("""SELECT coalesce(sum(pg_column_size(t.*)),0) FROM wro.wro_tekst_object_v2 t
                     WHERE instrument_idn = ANY(%s)""", (plannen,))
        mb = l.fetchone()[0] * 1.6 / 1e6   # heap + indexen + link/indeling: gemeten factor ~1,6
        print(f"{len(plannen)} plannen, geschat {mb:.0f} MB op prod (grens --max-mb {a.max_mb:.0f})", flush=True)

        # planobject-id's moeten aan beide kanten gelijk zijn, anders hangen de koppelingen aan niets
        l.execute("""SELECT planobject_id FROM wro.wro_tekst_link WHERE instrument_idn = ANY(%s)
                     ORDER BY random() LIMIT 200""", (plannen,))
        steek = [r[0] for r in l.fetchall()]
        p.execute("SELECT count(*) FROM wro.planobject WHERE identificatie = ANY(%s)", (steek,))
        gevonden = p.fetchone()[0]
        print(f"steekproef planobject-id's op prod gevonden: {gevonden}/{len(steek)}", flush=True)
        if steek and gevonden < 0.95 * len(steek):
            sys.exit("STOP: de planobjecten op prod wijken af van lokaal — eerst wro.planobject gelijk trekken.")
        if mb > a.max_mb:
            sys.exit(f"STOP: {mb:.0f} MB > --max-mb {a.max_mb:.0f}. Volume eerst vergroten (Railway-dashboard).")
        if not a.ja:
            print("droogloop — niets geschreven. Draai met --ja.")
            return

        p.execute(DDL)
        pc.commit()
        t0 = time.time()
        for i, idn in enumerate(plannen, 1):
            with pc.transaction():
                for tabel, _k in TABELLEN + [LAADSTAND]:
                    p.execute(f"DELETE FROM {tabel} WHERE instrument_idn = %s", (idn,))
                for tabel, kol in TABELLEN + [LAADSTAND]:
                    l.execute(f"SELECT {kol} FROM {tabel} WHERE instrument_idn = %s", (idn,))
                    rijen = l.fetchall()
                    if rijen:
                        with p.copy(f"COPY {tabel} ({kol}) FROM STDIN") as cp:
                            for r in rijen:
                                cp.write_row(r)
            if i % 100 == 0 or i == len(plannen):
                print(f"  {i}/{len(plannen)} plannen overgezet · {(time.time()-t0)/60:.1f} min", flush=True)
        p.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
        print("prod DB-grootte nu:", p.fetchone()[0])


if __name__ == "__main__":
    main()
