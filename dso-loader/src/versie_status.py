"""Versie-status van regeling-expressies.

Eén regeling-`frbr_work` kan meerdere expressies (versies) hebben. Alleen de
vigerende versie hoort standaard getoond te worden; oudere expressies markeren
we `inactief` met reden 'verouderde-versie'.

OW-objecten/annotaties (`juridische_regel`, `activiteit_locatieaanduiding`,
`tekst_element`) krijgen GEEN eigen vlag — ze erven de status via hun
`regeling_expression`. Retrieval-joins filteren met `AND NOT r.inactief`.

De reden-guard beschermt de 'ingetrokken'-markering (hele regeling vervallen):
die mag niet overschreven of geactiveerd worden door dit versie-mechanisme.
"""
import re


def _versie_sleutel(frbr_expression: str):
    """Numerieke sorteersleutel uit de @-suffix van een FRBR-expressie.

    Pakt alle cijfergroepen uit de suffix en vergelijkt numeriek i.p.v.
    lexicaal. Dekt alle waargenomen formaten correct:
      @2026-05-20;1  → [2026, 5, 20, 1]   (datum;seq)
      @10-0 vs @9-0  → [10, 0] > [9, 0]    (lost lexicale sorteerbug op)
      @2067 vs @1050 → [2067] > [1050]     (numerieke teller)
    Binnen één frbr_work is het suffix-formaat consistent, dus de sleutels
    zijn onderling vergelijkbaar.
    """
    suffix = frbr_expression.rsplit("@", 1)[-1] if "@" in frbr_expression else frbr_expression
    return [int(n) for n in re.findall(r"\d+", suffix)] or [0]


def nieuwste_expression(expressions):
    """Kies de vigerende (nieuwste) expressie uit een lijst van hetzelfde work.

    Fallback-heuristiek voor works die DSO Presenteren niet als losse regeling
    teruggeeft (programma's, tijdelijkdelen). Autoritatief blijft de DSO
    `expressionId`; gebruik deze alleen als die niet beschikbaar is.
    """
    return max(expressions, key=_versie_sleutel)


def markeer_siblings_inactief(cur, frbr_work: str, vigerende_expression: str) -> int:
    """Zet alle andere expressies van dit work op inactief; houd de vigerende actief.

    Aan te roepen direct na het laden van een expressie. Invariant: wat zojuist
    uit DSO geladen is, IS de vigerende versie. Retourneert het aantal
    gemarkeerde siblings.
    """
    cur.execute(
        """
        UPDATE p2p.regeling
           SET inactief       = TRUE,
               datum_inactief = COALESCE(datum_inactief, now()),
               reden_inactief = 'verouderde-versie'
         WHERE frbr_work = %s
           AND frbr_expression <> %s
           AND reden_inactief IS DISTINCT FROM 'ingetrokken'
           AND NOT inactief
        """,
        (frbr_work, vigerende_expression),
    )
    n = cur.rowcount
    # De vigerende expressie mag niet per ongeluk inactief blijven staan
    # (bv. na een eerdere onterechte markering) — tenzij ze is ingetrokken.
    cur.execute(
        """
        UPDATE p2p.regeling
           SET inactief = FALSE, datum_inactief = NULL, reden_inactief = NULL
         WHERE frbr_expression = %s
           AND reden_inactief IS DISTINCT FROM 'ingetrokken'
           AND inactief
        """,
        (vigerende_expression,),
    )
    return n
