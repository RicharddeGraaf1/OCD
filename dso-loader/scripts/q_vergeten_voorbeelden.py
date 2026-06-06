"""Drie voorbeelden van vermoedelijk-vergeten-annotaties uit de drieslag.

Selectie: random over de hele matview, gebalanceerd over object_type
(activiteit / gebiedsaanwijzing / norm). Per voorbeeld: regeling,
tekst-snippet, object-naam, en welke andere doelen die tekst wel heeft.

Run: python scripts/q_vergeten_voorbeelden.py
"""
import os
import sys
import textwrap

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from src.db import get_conn


def fetch_voorbeelden(cur, object_type: str, limit: int = 1):
    """Eén voorbeeld voor een gegeven object_type."""
    cur.execute(
        """
        SELECT
            mv.tekst_element_id,
            mv.object_type,
            mv.object_id,
            mv.regeling_expression,
            te.eid,
            te.element_type,
            substring(te.inhoud_plain for 400) AS snippet,
            CASE mv.object_type
                WHEN 'Activiteit'        THEN (SELECT naam FROM p2p.activiteit        WHERE identificatie = mv.object_id)
                WHEN 'Gebiedsaanwijzing' THEN (SELECT naam FROM p2p.gebiedsaanwijzing WHERE identificatie = mv.object_id)
                WHEN 'Omgevingsnorm'     THEN (SELECT naam FROM p2p.norm              WHERE identificatie = mv.object_id)
                WHEN 'Omgevingswaarde'   THEN (SELECT naam FROM p2p.norm              WHERE identificatie = mv.object_id)
            END AS object_naam,
            (SELECT count(*) FROM p2p.tekstdeel td WHERE td.divisie_wid = te.wid) AS n_tekstdelen_op_deze_tekst,
            (SELECT count(*) FROM p2p.tekst_object_consistentie_mv mv2
              WHERE mv2.tekst_element_id = mv.tekst_element_id
                AND mv2.consistentie_klasse = 'consistent_aanwezig') AS n_consistent_op_deze_tekst
        FROM p2p.tekst_object_consistentie_mv mv
        JOIN p2p.tekst_element te ON te.id = mv.tekst_element_id
        WHERE mv.consistentie_klasse = 'vermoedelijk_vergeten_annotatie'
          AND mv.object_type = %s
          AND length(te.inhoud_plain) BETWEEN 80 AND 1000
        ORDER BY random()
        LIMIT %s
        """,
        (object_type, limit),
    )
    return cur.fetchall()


def main():
    conn = get_conn()
    cur = conn.cursor()

    print("Drie vermoedelijk-vergeten-annotaties (random selectie)\n")
    print("=" * 78)

    for obj_type in ("Activiteit", "Gebiedsaanwijzing", "Omgevingsnorm"):
        rows = fetch_voorbeelden(cur, obj_type)
        for r in rows:
            print(f"\n## {obj_type.upper()}  —  {r['object_naam']}")
            print(f"   Regeling:       {r['regeling_expression']}")
            print(f"   Tekst-eId:      {r['eid']}  ({r['element_type']})")
            print(f"   Object-id:      {r['object_id']}")
            print(f"   Tekstdelen op deze wId:          {r['n_tekstdelen_op_deze_tekst']}")
            print(f"   Andere consistent_aanwezig hier: {r['n_consistent_op_deze_tekst']}")
            print(f"   Snippet:")
            for line in textwrap.wrap(r["snippet"], width=72,
                                       initial_indent="     > ",
                                       subsequent_indent="     > "):
                print(line)
            print("-" * 78)

    conn.close()


if __name__ == "__main__":
    main()
