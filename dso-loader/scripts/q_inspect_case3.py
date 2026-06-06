"""Inspecteer geval 3 — Bouwlaag Wonen transformatiegebied — full text."""
import os, sys
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from src.db import get_conn

OBJ = "nl.imow-gm0202.omgevingsnorm.9e203073e25f40bc899876255511f16a"

conn = get_conn(); cur = conn.cursor()

cur.execute("SELECT naam FROM p2p.norm WHERE identificatie = %s", (OBJ,))
naam = cur.fetchone()["naam"]
print(f"Object-naam: {naam!r}")

cur.execute("""
    SELECT mv.tekst_element_id, te.inhoud_plain, length(te.inhoud_plain) AS len
    FROM p2p.tekst_object_consistentie_mv mv
    JOIN p2p.tekst_element te ON te.id = mv.tekst_element_id
    WHERE mv.object_id = %s
      AND mv.consistentie_klasse = 'vermoedelijk_vergeten_annotatie'
""", (OBJ,))
for r in cur.fetchall():
    print(f"\n--- tekst_element_id={r['tekst_element_id']} (len={r['len']}) ---")
    print(r["inhoud_plain"])
    naam_lc = naam.lower()
    text_lc = r["inhoud_plain"].lower()
    n_hits = text_lc.count(naam_lc)
    print(f"\n>> aantal occurrences van naam (case-insensitive substring): {n_hits}")

print("\n=== Andere drieslag-rijen op deze tekst_element(s) ===")
cur.execute("""
    SELECT mv.object_type, mv.object_id, mv.consistentie_klasse,
           CASE mv.object_type
             WHEN 'Activiteit'        THEN (SELECT naam FROM p2p.activiteit        WHERE identificatie = mv.object_id)
             WHEN 'Gebiedsaanwijzing' THEN (SELECT naam FROM p2p.gebiedsaanwijzing WHERE identificatie = mv.object_id)
             WHEN 'Omgevingsnorm'     THEN (SELECT naam FROM p2p.norm              WHERE identificatie = mv.object_id)
             WHEN 'Omgevingswaarde'   THEN (SELECT naam FROM p2p.norm              WHERE identificatie = mv.object_id)
           END AS objnaam
    FROM p2p.tekst_object_consistentie_mv mv
    WHERE mv.tekst_element_id IN (
        SELECT tekst_element_id FROM p2p.tekst_object_consistentie_mv
        WHERE object_id = %s AND consistentie_klasse = 'vermoedelijk_vergeten_annotatie'
    )
    ORDER BY mv.consistentie_klasse, mv.object_type
""", (OBJ,))
for r in cur.fetchall():
    print(f"  [{r['consistentie_klasse']:32}] {r['object_type']:18} {r['objnaam']!r}")

conn.close()
