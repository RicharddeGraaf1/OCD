"""Fast-path: gestructureerde, deterministische antwoorden voor de retrieval-kernel.

Convergentie bot ↔ viewer (viewer-first): de kernel kan voor norm-vragen een exact
antwoord teruggeven zónder LLM, mits het *ondubbelzinnig* is. Bewust conservatief:
liever `None` (val terug op regelteksten + LLM) dan een fout gezaghebbend getal.

De rijke interpretatie (max-selectie, Wro-`maatvoering`-fallback, drempelwaarde-
afweging) zit in de bot-Path-A en hoort bij de bot-migratie (R2/R3) — niet hier
opnieuw opgebouwd. Deze module doet alleen de veilige, ondubbelzinnige gevallen.

Afgeleid van de `/v1/normwaarde` detector-bucket in main.py (zelfde join/scope).
"""
from __future__ import annotations


def norm_fast_path(cur, x: float, y: float, naam: str) -> dict | None:
    """Geef een deterministisch norm-antwoord als er op (x,y) precies één distinct
    kwantitatieve waarde geldt voor de via `naam` gematchte norm. Anders None.

    Conservatief: meerdere verschillende waarden (bv. deelgebieden met andere
    bouwhoogtes) → ambigu → None → de wrapper laat de LLM het afhandelen.
    """
    cur.execute(
        """
        SELECT  n.naam                         AS norm_naam,
                n.eenheid,
                nw.kwantitatieve_waarde,
                r.opschrift                     AS regeling,
                ocd_artikel_label(te.opschrift, te.wid) AS artikel
        FROM    p2p.normwaarde              nw
        JOIN    p2p.norm                    n   ON n.identificatie  = nw.norm_id
        JOIN    p2p.locatie                 l   ON l.identificatie  = nw.locatie_id
        LEFT JOIN p2p.juridische_regel_norm jrn ON jrn.norm_id      = n.identificatie
        LEFT JOIN p2p.juridische_regel      jr  ON jr.identificatie = jrn.juridische_regel_id
        LEFT JOIN p2p.tekst_element         te  ON te.wid           = jr.regeltekst_wid
        LEFT JOIN p2p.regeling              r   ON r.frbr_expression = te.regeling_expression
        WHERE   ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
          AND   n.naam ILIKE %s
          AND   nw.kwantitatieve_waarde IS NOT NULL
        ORDER BY nw.kwantitatieve_waarde DESC
        """,
        (x, y, f"%{naam}%"),
    )
    rows = cur.fetchall()
    if not rows:
        return None

    distinct_waarden = {r["kwantitatieve_waarde"] for r in rows}
    if len(distinct_waarden) != 1:
        return None  # ambigu — laat de LLM/regelteksten het afhandelen

    top = rows[0]
    return {
        "type": "norm",
        "naam": top["norm_naam"],
        "waarde": top["kwantitatieve_waarde"],
        "eenheid": top["eenheid"],
        "regeling": top["regeling"],
        "artikel": top["artikel"],
        "confidence": "HOOG",
    }
