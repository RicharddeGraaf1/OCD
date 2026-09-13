"""Backfill van het vrijetekst-werk van 2026-09-10 naar prod (runbook §3a).

De gewone replicatie scoopt op de expressies van de laatste run; de herlaad van
602 regelingen op 10-09 hoogde `geladen_op` niet op, dus die data bereikt prod
langs geen enkele route. Twee delen: de tabel p2p.divisie (op prod leeg) en de
twee kolommen op p2p.tekstdeel (op prod NULL).
"""
import sys, io, time; sys.path.insert(0,'.')
import psycopg
from psycopg.rows import dict_row
from src.db import get_conn

prod = open(r'C:\tmp\sync-20260912\.produrl').read().strip()
DROOG = '--ja' not in sys.argv
loc = get_conn(); lc = loc.cursor()

lc.execute("select count(*) c from p2p.divisie"); n_div = lc.fetchone()['c']
lc.execute("""select count(*) c from p2p.tekstdeel
              where idealisatie is not null or divisie_soort is not null""")
n_td = lc.fetchone()['c']
print(f"lokaal: p2p.divisie {n_div:,} rijen · tekstdeel met waarden {n_td:,}")

with psycopg.connect(prod, row_factory=dict_row, connect_timeout=60) as p:
    pc = p.cursor()
    pc.execute("select count(*) c from p2p.divisie"); print(f"prod   : p2p.divisie {pc.fetchone()['c']:,} rijen")
    pc.execute("""select count(*) c from p2p.tekstdeel
                  where idealisatie is not null or divisie_soort is not null""")
    print(f"prod   : tekstdeel met waarden {pc.fetchone()['c']:,}")
    if DROOG:
        print("\nDROOGLOOP — draai met --ja"); sys.exit(0)

    # 1. p2p.divisie via COPY
    t = time.time()
    buf = io.StringIO()
    with lc.copy("COPY (SELECT identificatie, wid, soort, regeling_expression FROM p2p.divisie) TO STDOUT") as cp:
        for blok in cp: buf.write(blok.tobytes().decode('utf-8'))
    buf.seek(0)
    pc.execute("CREATE TEMP TABLE _div (LIKE p2p.divisie) ON COMMIT DROP")
    with pc.copy("COPY _div (identificatie, wid, soort, regeling_expression) FROM STDIN") as cp:
        cp.write(buf.read())
    pc.execute("""INSERT INTO p2p.divisie SELECT * FROM _div
                  ON CONFLICT (identificatie) DO UPDATE
                  SET wid=EXCLUDED.wid, soort=EXCLUDED.soort,
                      regeling_expression=EXCLUDED.regeling_expression""")
    print(f"  p2p.divisie: {pc.rowcount:,} rijen ({time.time()-t:.1f}s)")

    # 2. de twee kolommen op tekstdeel
    t = time.time()
    buf = io.StringIO()
    with lc.copy("""COPY (SELECT identificatie, idealisatie, divisie_soort FROM p2p.tekstdeel
                     WHERE idealisatie IS NOT NULL OR divisie_soort IS NOT NULL) TO STDOUT""") as cp:
        for blok in cp: buf.write(blok.tobytes().decode('utf-8'))
    buf.seek(0)
    pc.execute("CREATE TEMP TABLE _td (identificatie text, idealisatie text, divisie_soort text) ON COMMIT DROP")
    with pc.copy("COPY _td (identificatie, idealisatie, divisie_soort) FROM STDIN") as cp:
        cp.write(buf.read())
    pc.execute("""UPDATE p2p.tekstdeel td SET idealisatie = s.idealisatie, divisie_soort = s.divisie_soort
                  FROM _td s WHERE s.identificatie = td.identificatie
                    AND (td.idealisatie IS DISTINCT FROM s.idealisatie
                      OR td.divisie_soort IS DISTINCT FROM s.divisie_soort)""")
    print(f"  tekstdeel bijgewerkt: {pc.rowcount:,} rijen ({time.time()-t:.1f}s)")
    p.commit()

    pc.execute("select count(*) c from p2p.divisie"); print(f"prod na: p2p.divisie {pc.fetchone()['c']:,}")
    pc.execute("""select count(*) c from p2p.tekstdeel
                  where idealisatie is not null or divisie_soort is not null""")
    print(f"prod na: tekstdeel met waarden {pc.fetchone()['c']:,}")
