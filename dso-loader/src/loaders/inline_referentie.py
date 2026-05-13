"""Extract inline tekst-referenties (IntIoRef / ExtIoRef / IntRef / ExtRef)
from tekst_element.inhoud and persist them to p2p.tekst_inline_referentie.

This is type 2 in the drieslag tekst↔object — see
  vault_v1/analysis/Drie verwijsmechanismen tekst-object.md
  vault_v1/analysis/Plan implementatie drieslag tekst-object.md

Until now these references lived inside the XHTML blob and were only
accessible via runtime XPath. After this loader runs they're queryable
as a flat table.
"""

import re
from typing import Iterable

# STOP-elementen voor inline verwijzingen — keten regeltekst → IntIoRef →
# ExtIoRef → GIO. Zie vault_v1/concepts/IntIoRef en ExtIoRef.md voor de
# autoritaire uitleg.
#
#   IntIoRef    in regeltekst; @ref = wId van een ExtIoRef in zelfde document
#   ExtIoRef    declareert FRBR-expression van GIO; @ref + inhoud = FRBR; @wId is doel voor IntIoRef
#   IntRef      verwijzing naar eId van een tekstcomponent (artikel/lid)
#   ExtRef      externe URL
#
# Voor ExtIoRef vangen we ook @wId apart op (eigen_wid in de tabel) —
# die is nodig om de keten op te lossen.
_INLINE_REF_RE = re.compile(
    r"""<\s*
        (?P<soort>IntIoRef|ExtIoRef|IntRef|ExtRef)
        \b(?P<attrs>[^>]*?)
        /?>
    """,
    re.VERBOSE | re.IGNORECASE | re.DOTALL,
)

_REF_ATTR_RE = re.compile(
    r"""\bref\s*=\s*(?:"(?P<d>[^"]*)"|'(?P<s>[^']*)')""",
    re.IGNORECASE,
)
_WID_ATTR_RE = re.compile(
    r"""\bwId\s*=\s*(?:"(?P<d>[^"]*)"|'(?P<s>[^']*)')""",
    re.IGNORECASE,
)


def extract_inline_referenties(inhoud: str | None) -> list[dict]:
    """Parse `inhoud` (XHTML blob) and return a list of inline references.

    Each dict has: soort, target_ref, eigen_wid (only for ExtIoRef), positie.
    target_soort and target_gio_expression are filled later, after the
    rows are inserted (resolution requires DB access).
    """
    if not inhoud:
        return []

    out: list[dict] = []
    for m in _INLINE_REF_RE.finditer(inhoud):
        attrs = m.group("attrs") or ""
        ref_m = _REF_ATTR_RE.search(attrs)
        if not ref_m:
            continue
        ref = ref_m.group("d") if ref_m.group("d") is not None else ref_m.group("s")
        if not ref:
            continue
        soort = m.group("soort")
        # ExtIoRef: vang ook @wId apart op zodat IntIoRef'en hier later naartoe
        # kunnen lookuppen via de twee-traps keten.
        eigen_wid: str | None = None
        if soort.lower() == "extioref":
            wid_m = _WID_ATTR_RE.search(attrs)
            if wid_m:
                eigen_wid = wid_m.group("d") if wid_m.group("d") is not None else wid_m.group("s")
        out.append(
            {
                "soort": soort,
                "target_ref": ref,
                "eigen_wid": eigen_wid,
                "positie": m.start(),
            }
        )
    return out


def insert_inline_referenties(conn, tekst_element_id: int, refs: Iterable[dict]) -> int:
    """Insert one batch of inline references for a single tekst_element.

    Returns the number of rows inserted (excluding ON CONFLICT skips).
    """
    rows = list(refs)
    if not rows:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """INSERT INTO p2p.tekst_inline_referentie
                   (tekst_element_id, soort, target_ref, eigen_wid, positie)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (tekst_element_id, soort, target_ref, positie)
                   DO NOTHING
                """,
                (
                    tekst_element_id,
                    r["soort"],
                    r["target_ref"],
                    r.get("eigen_wid"),
                    r["positie"],
                ),
            )
            inserted += cur.rowcount
    return inserted


def resolve_target_soort(conn, regeling_expression: str | None = None) -> dict[str, int]:
    """Resolve target_soort + target_gio_expression in three passes.

    Pass A — ExtIoRef → GIO via FRBR-match:
      ExtIoRef.@ref is een FRBR-expression; match direct op
      p2p.geo_informatieobject.frbr_expression. Bij hit: target_soort='GIO',
      target_gio_expression=match.

    Pass B — IntIoRef → ExtIoRef → GIO (twee-traps lookup):
      IntIoRef.@ref = wId van een ExtIoRef in dezelfde regeling. Pak
      diens target_gio_expression over (als die in pass A is opgelost).

    Pass C — fallback target_soort:
      - ExtIoRef/ExtRef zonder match → 'Extern'
      - IntRef → 'Tekstcomponent'
      - IntIoRef zonder match → blijft NULL (eId-pad onbekend)

    Args:
      regeling_expression: optioneel — beperk tot één regeling tijdens
        incrementele ingest. None = hele dataset.

    Returns: dict met counts per resolutie-stap.
    """
    where_extra = ""
    where_te_join = ""
    params: tuple = ()
    if regeling_expression is not None:
        where_extra = """ AND tekst_element_id IN (
            SELECT id FROM p2p.tekst_element WHERE regeling_expression = %s
        )"""
        where_te_join = " AND te.regeling_expression = %s"
        params = (regeling_expression,)

    counts = {
        "extioref_to_gio": 0,
        "intioref_via_chain": 0,
        "extern_fallback": 0,
        "tekstcomponent": 0,
    }

    with conn.cursor() as cur:
        # ─── Pass A: ExtIoRef → GIO via FRBR ───
        cur.execute(
            f"""UPDATE p2p.tekst_inline_referentie tir
                SET target_soort = 'GIO',
                    target_gio_expression = gio.frbr_expression
                FROM p2p.geo_informatieobject gio
                WHERE tir.soort = 'ExtIoRef'
                  AND tir.target_soort IS NULL
                  AND tir.target_ref = gio.frbr_expression
                  {where_extra}
            """,
            params,
        )
        counts["extioref_to_gio"] = cur.rowcount

        # ─── Pass B: IntIoRef → ExtIoRef.eigen_wid → GIO ───
        # Beperk tot zelfde regeling: een IntIoRef wijst alleen naar ExtIoRef
        # in HETZELFDE document. Sub-query trick omdat Postgres UPDATE...FROM
        # de target-tabel (tir_int) niet via JOIN herbeschikbaar maakt.
        cur.execute(
            f"""UPDATE p2p.tekst_inline_referentie tir_int
                SET target_soort = 'GIO',
                    target_gio_expression = sub.gio_expr
                FROM (
                    SELECT tir_int.id AS int_id,
                           tir_ext.target_gio_expression AS gio_expr
                    FROM p2p.tekst_inline_referentie tir_int
                    JOIN p2p.tekst_element te_int ON te_int.id = tir_int.tekst_element_id
                    JOIN p2p.tekst_inline_referentie tir_ext
                         ON tir_ext.eigen_wid = tir_int.target_ref
                    JOIN p2p.tekst_element te_ext ON te_ext.id = tir_ext.tekst_element_id
                    WHERE tir_int.soort = 'IntIoRef'
                      AND tir_int.target_soort IS NULL
                      AND tir_ext.soort = 'ExtIoRef'
                      AND tir_ext.eigen_wid IS NOT NULL
                      AND te_int.regeling_expression = te_ext.regeling_expression
                      AND tir_ext.target_gio_expression IS NOT NULL
                      {(' AND te_int.regeling_expression = %s' if regeling_expression else '')}
                ) sub
                WHERE tir_int.id = sub.int_id
            """,
            (regeling_expression,) if regeling_expression else (),
        )
        counts["intioref_via_chain"] = cur.rowcount

        # ─── Pass C1: ExtIoRef + ExtRef zonder match → Extern ───
        cur.execute(
            f"""UPDATE p2p.tekst_inline_referentie
                SET target_soort = 'Extern'
                WHERE soort IN ('ExtIoRef', 'ExtRef')
                  AND target_soort IS NULL
                  {where_extra}
            """,
            params,
        )
        counts["extern_fallback"] = cur.rowcount

        # ─── Pass C2: IntRef → Tekstcomponent ───
        cur.execute(
            f"""UPDATE p2p.tekst_inline_referentie
                SET target_soort = 'Tekstcomponent'
                WHERE soort = 'IntRef'
                  AND target_soort IS NULL
                  {where_extra}
            """,
            params,
        )
        counts["tekstcomponent"] = cur.rowcount

    return counts
