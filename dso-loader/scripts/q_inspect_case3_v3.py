"""Voor één tekst_element: welke IntIoRefs, welke GIO's, en hoe komt de
keten naar 'Bouwlaag Wonen transformatiegebied' tot stand?"""
import os, sys
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from src.db import get_conn

OBJ = "nl.imow-gm0202.omgevingsnorm.9e203073e25f40bc899876255511f16a"
TE_ID = 19358   # art 10.49 — "Bouwlaag Dienstverlening transformatiegebied"

conn = get_conn(); cur = conn.cursor()

print(f"# Tekst-element {TE_ID}")
cur.execute("SELECT eid, inhoud_plain FROM p2p.tekst_element WHERE id = %s", (TE_ID,))
r = cur.fetchone()
print(f"eid: {r['eid']}")
print(f"inhoud: {r['inhoud_plain'].strip()}\n")

print(f"# IntIoRefs in deze tekst")
cur.execute("""
    SELECT soort, target_ref, target_soort, target_gio_expression
    FROM p2p.tekst_inline_referentie
    WHERE tekst_element_id = %s
""", (TE_ID,))
intiorefs = cur.fetchall()
for r in intiorefs:
    print(f"  - {r['soort']}: target_ref={r['target_ref']} target_soort={r['target_soort']} target_gio={r['target_gio_expression']}")

print(f"\n# Norm {OBJ}")
cur.execute("SELECT naam FROM p2p.norm WHERE identificatie = %s", (OBJ,))
print(f"Naam: {cur.fetchone()['naam']}")

cur.execute("""
    SELECT lb.locatie_id, lb.basisgeo_id
    FROM p2p.normwaarde nw
    JOIN p2p.locatie_basisgeo lb ON lb.locatie_id = nw.locatie_id
    WHERE nw.norm_id = %s
""", (OBJ,))
print("Norm-locatie → basisgeo_ids (eerste 5):")
norm_loc_basisgeos = cur.fetchall()
for r in norm_loc_basisgeos[:5]:
    print(f"  loc={r['locatie_id']} bg={r['basisgeo_id']}")
print(f"  ... totaal {len(norm_loc_basisgeos)} basisgeo:ids voor norm")

print(f"\n# Voor elke IntIoRef-GIO: hoeveel basisgeo:ids, en hoeveel overlap met norm?")
for tir in intiorefs:
    if not tir["target_gio_expression"]:
        continue
    cur.execute("""
        SELECT basisgeo_id FROM p2p.gio_basisgeo WHERE gio_frbr = %s
    """, (tir["target_gio_expression"],))
    gio_bg = {r["basisgeo_id"] for r in cur.fetchall()}
    norm_bg = {r["basisgeo_id"] for r in norm_loc_basisgeos}
    overlap = gio_bg & norm_bg
    norm_subset = norm_bg <= gio_bg if norm_bg else False
    print(f"  GIO {tir['target_gio_expression']}")
    print(f"    |GIO|={len(gio_bg)}  |Norm|={len(norm_bg)}  |overlap|={len(overlap)}")
    print(f"    norm ⊆ GIO? {norm_subset}  ;  set-equality? {gio_bg == norm_bg}")

conn.close()
