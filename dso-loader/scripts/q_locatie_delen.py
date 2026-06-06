"""Hoe vaak delen meerdere normen / gebiedsaanwijzingen exact dezelfde
basisgeo-set binnen één regeling? Dat is het patroon dat de drieslag-
IntIoRef-keten niet kan onderscheiden."""
import os, sys
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from src.db import get_conn

conn = get_conn(); cur = conn.cursor()

# Norm: hoeveel normen delen hun basisgeo-set met andere normen in dezelfde regeling?
cur.execute("""
WITH norm_bg AS (
    SELECT n.identificatie AS norm_id, n.naam,
           split_part(n.identificatie, '.', 2) AS bronhouder_imow,
           array_agg(lb.basisgeo_id ORDER BY lb.basisgeo_id) AS bg_set
    FROM p2p.norm n
    JOIN p2p.normwaarde nw ON nw.norm_id = n.identificatie
    JOIN p2p.locatie_basisgeo lb ON lb.locatie_id = nw.locatie_id
    GROUP BY n.identificatie, n.naam
)
SELECT
    COUNT(DISTINCT norm_id) AS n_normen_totaal,
    COUNT(DISTINCT norm_id) FILTER (WHERE deelgenoten > 0) AS n_normen_met_deelgenoot,
    COUNT(DISTINCT norm_id) FILTER (WHERE deelgenoten = 0) AS n_normen_uniek
FROM (
    SELECT nb1.norm_id,
           COUNT(*) FILTER (WHERE nb2.norm_id IS NOT NULL AND nb2.norm_id <> nb1.norm_id) AS deelgenoten
    FROM norm_bg nb1
    LEFT JOIN norm_bg nb2
      ON nb2.bg_set = nb1.bg_set
     AND nb2.bronhouder_imow = nb1.bronhouder_imow
     AND nb2.norm_id <> nb1.norm_id
    GROUP BY nb1.norm_id
) x;
""")
print("# Normen: uniciteit van basisgeo-set binnen dezelfde bronhouder")
for r in cur.fetchall():
    print(f"  Totaal normen:                {r['n_normen_totaal']:>8,}")
    print(f"  Normen met deelgenoot:        {r['n_normen_met_deelgenoot']:>8,}")
    print(f"  Normen uniek:                 {r['n_normen_uniek']:>8,}")

# Hetzelfde voor Gebiedsaanwijzing
cur.execute("""
WITH ga_bg AS (
    SELECT ga.identificatie AS ga_id, ga.naam,
           split_part(ga.identificatie, '.', 2) AS bronhouder_imow,
           array_agg(lb.basisgeo_id ORDER BY lb.basisgeo_id) AS bg_set
    FROM p2p.gebiedsaanwijzing ga
    JOIN p2p.locatie_basisgeo lb ON lb.locatie_id = ga.locatie_id
    GROUP BY ga.identificatie, ga.naam
)
SELECT
    COUNT(DISTINCT ga_id) AS n_totaal,
    COUNT(DISTINCT ga_id) FILTER (WHERE deelgenoten > 0) AS n_met_deelgenoot,
    COUNT(DISTINCT ga_id) FILTER (WHERE deelgenoten = 0) AS n_uniek
FROM (
    SELECT g1.ga_id,
           COUNT(*) FILTER (WHERE g2.ga_id IS NOT NULL AND g2.ga_id <> g1.ga_id) AS deelgenoten
    FROM ga_bg g1
    LEFT JOIN ga_bg g2
      ON g2.bg_set = g1.bg_set
     AND g2.bronhouder_imow = g1.bronhouder_imow
     AND g2.ga_id <> g1.ga_id
    GROUP BY g1.ga_id
) x;
""")
print("\n# Gebiedsaanwijzingen: uniciteit binnen dezelfde bronhouder")
for r in cur.fetchall():
    print(f"  Totaal GA's:                  {r['n_totaal']:>8,}")
    print(f"  GA's met deelgenoot:          {r['n_met_deelgenoot']:>8,}")
    print(f"  GA's uniek:                   {r['n_uniek']:>8,}")

# Drieslag-impact: van de vermoedelijk_vergeten-rijen, hoeveel zou wegvallen
# als we eisen dat has_naammatch OOK true is?
cur.execute("""
WITH vergeten AS (
    SELECT mv.tekst_element_id, mv.object_id, mv.object_type,
           toc.has_intioref, toc.has_naammatch
    FROM p2p.tekst_object_consistentie_mv mv
    JOIN p2p.tekst_object_consistentie toc
      ON toc.tekst_element_id = mv.tekst_element_id
     AND toc.object_id = mv.object_id
     AND toc.object_type = mv.object_type
    WHERE mv.consistentie_klasse = 'vermoedelijk_vergeten_annotatie'
)
SELECT object_type,
       COUNT(*) AS totaal,
       COUNT(*) FILTER (WHERE has_naammatch) AS met_naammatch,
       COUNT(*) FILTER (WHERE has_intioref AND NOT has_naammatch) AS alleen_intioref,
       COUNT(*) FILTER (WHERE has_naammatch AND NOT COALESCE(has_intioref, FALSE)) AS alleen_naammatch,
       COUNT(*) FILTER (WHERE has_naammatch AND has_intioref) AS beide
FROM vergeten
GROUP BY object_type
ORDER BY object_type;
""")
print("\n# Drieslag: hoeveel 'vermoedelijk_vergeten_annotatie' per signaal-bron?")
print(f"{'object_type':>17}  {'totaal':>8}  {'naammatch':>9}  {'a-iore':>8}  {'a-naam':>8}  {'beide':>8}")
for r in cur.fetchall():
    print(f"{r['object_type']:>17}  "
          f"{r['totaal']:>8,}  "
          f"{r['met_naammatch']:>9,}  "
          f"{r['alleen_intioref']:>8,}  "
          f"{r['alleen_naammatch']:>8,}  "
          f"{r['beide']:>8,}")

conn.close()
