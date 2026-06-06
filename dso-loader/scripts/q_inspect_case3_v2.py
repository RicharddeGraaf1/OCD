"""Welk signaal (has_intioref / has_naammatch) drijft de classificatie?"""
import os, sys
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from src.db import get_conn

OBJ = "nl.imow-gm0202.omgevingsnorm.9e203073e25f40bc899876255511f16a"

conn = get_conn(); cur = conn.cursor()

cur.execute("""
    SELECT mv.tekst_element_id, te.eid,
           toc.has_annotatie, toc.has_intioref, toc.has_naammatch, toc.doelen_match
    FROM p2p.tekst_object_consistentie_mv mv
    JOIN p2p.tekst_object_consistentie toc
      ON toc.tekst_element_id = mv.tekst_element_id
     AND toc.object_id = mv.object_id
     AND toc.object_type = mv.object_type
    JOIN p2p.tekst_element te ON te.id = mv.tekst_element_id
    WHERE mv.object_id = %s
      AND mv.consistentie_klasse = 'vermoedelijk_vergeten_annotatie'
    ORDER BY te.eid
""", (OBJ,))
print(f"{'tekst_element_id':>16}  {'has_ann':>7}  {'has_io':>7}  {'has_nm':>7}  eid")
for r in cur.fetchall():
    print(f"{r['tekst_element_id']:>16}  "
          f"{str(r['has_annotatie']):>7}  "
          f"{str(r['has_intioref']):>7}  "
          f"{str(r['has_naammatch']):>7}  "
          f"{r['eid']}")
conn.close()
