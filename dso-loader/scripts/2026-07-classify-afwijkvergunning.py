"""Classificeer afwijkvergunning (BOPA) op vth.vergunningkennisgeving.

Achtergrond: een machine-leesbare binnenplans/buitenplans-typering bestaat
nergens (DSO presenteren/v8 heeft geen getypeerd veld; downloaden-API kent geen
vergunning-besluiten; de gmb-publicatie is proza). Zie de vault-analyse
"Afwijkvergunning (BOPA) vastleggen" + gaps G-84. De best bereikbare bron is de
al-geenrichte bodytekst (inhoud_tekst), met de titel als zwakkere fallback.

Vult vier afgeleide kolommen:
  afwijk_status    buitenplans_expliciet | binnenplans_expliciet | opa_onbepaald | geen_opa (NULL)
  procedure        regulier | uitgebreid | onbekend (NULL)
  afwijk_bron      body-tekst | titel
  afwijk_evidence  de canonieke marker-frase die matchte

Rijen zonder OPA-signaal blijven NULL (= geen_opa) → de migratie raakt alleen
de ~34k relevante rijen i.p.v. de hele 804k tabel te herschrijven.

Gebruik:
  python scripts/2026-07-classify-afwijkvergunning.py --sample     # leest, schrijft niet
  python scripts/2026-07-classify-afwijkvergunning.py --apply      # ALTER + UPDATE + index
Idempotent: --apply mag herhaald worden.
"""
import pathlib
import sys
import argparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from src.config import cfg
import psycopg
from psycopg.rows import dict_row

T = "vth.vergunningkennisgeving"

# Markers (ILIKE-patronen). Volgorde = prioriteit bij status-toekenning.
BUITEN = ["%buitenplanse omgevingsplanactiviteit%", "%(bopa)%"]
BINNEN = ["%binnenplanse omgevingsplanactiviteit%"]
OPA = "%omgevingsplanactiviteit%"
TITEL_BOPA = ["%bopa%", "%buitenplanse omgevingsplanactiviteit%"]
PROC_UITG = "%uitgebreide voorbereidingsprocedure%"
PROC_REG = "%reguliere voorbereidingsprocedure%"

# Superset-filter: alleen rijen die enig OPA-/BOPA-signaal dragen (in body of titel).
STATUS_SCOPE = """(
    inhoud_tekst ILIKE %(opa)s
 OR titel ILIKE %(tbopa1)s
 OR titel ILIKE %(tbopa2)s
)"""

# CASE-expressies, herbruikt in sample (SELECT) en apply (UPDATE).
STATUS_CASE = """CASE
    WHEN inhoud_tekst ILIKE %(bu1)s OR inhoud_tekst ILIKE %(bu2)s THEN 'buitenplans_expliciet'
    WHEN inhoud_tekst ILIKE %(bi1)s                               THEN 'binnenplans_expliciet'
    WHEN inhoud_tekst ILIKE %(opa)s                               THEN 'opa_onbepaald'
    WHEN titel ILIKE %(tbopa1)s OR titel ILIKE %(tbopa2)s         THEN 'buitenplans_expliciet'
    ELSE 'geen_opa'
END"""

BRON_CASE = """CASE
    WHEN inhoud_tekst ILIKE %(opa)s THEN 'body-tekst'
    WHEN titel ILIKE %(tbopa1)s OR titel ILIKE %(tbopa2)s THEN 'titel'
    ELSE NULL
END"""

EVIDENCE_CASE = """CASE
    WHEN inhoud_tekst ILIKE %(bu1)s THEN 'buitenplanse omgevingsplanactiviteit'
    WHEN inhoud_tekst ILIKE %(bu2)s THEN 'BOPA'
    WHEN inhoud_tekst ILIKE %(bi1)s THEN 'binnenplanse omgevingsplanactiviteit'
    WHEN inhoud_tekst ILIKE %(opa)s THEN 'omgevingsplanactiviteit'
    WHEN titel ILIKE %(tbopa1)s OR titel ILIKE %(tbopa2)s THEN 'BOPA (titel)'
    ELSE NULL
END"""

PROC_CASE = f"""CASE
    WHEN inhoud_tekst ILIKE %(puitg)s THEN 'uitgebreid'
    WHEN inhoud_tekst ILIKE %(preg)s  THEN 'regulier'
    ELSE NULL
END"""

PARAMS = {
    "bu1": BUITEN[0], "bu2": BUITEN[1], "bi1": BINNEN[0], "opa": OPA,
    "tbopa1": TITEL_BOPA[0], "tbopa2": TITEL_BOPA[1],
    "puitg": PROC_UITG, "preg": PROC_REG,
}

DDL = f"""
ALTER TABLE {T}
    ADD COLUMN IF NOT EXISTS afwijk_status   TEXT
        CHECK (afwijk_status IS NULL OR afwijk_status IN
               ('buitenplans_expliciet','binnenplans_expliciet','opa_onbepaald','geen_opa')),
    ADD COLUMN IF NOT EXISTS procedure       TEXT
        CHECK (procedure IS NULL OR procedure IN ('regulier','uitgebreid')),
    ADD COLUMN IF NOT EXISTS afwijk_bron     TEXT,
    ADD COLUMN IF NOT EXISTS afwijk_evidence TEXT;

CREATE INDEX IF NOT EXISTS idx_vk_afwijk_status
    ON {T} (afwijk_status) WHERE afwijk_status IS NOT NULL;
"""


def sample(conn):
    cur = conn.cursor()
    print("Verdeling afwijk_status (berekend, niet weggeschreven) — seq-scan, ~1-2 min…\n")
    cur.execute(f"""
        SELECT {STATUS_CASE} AS status, count(*) n
        FROM {T} WHERE {STATUS_SCOPE}
        GROUP BY 1 ORDER BY 2 DESC""", PARAMS)
    for r in cur.fetchall():
        print(f"  {r['status']:24s} {r['n']:>7d}")
    print("  (rijen zonder OPA-signaal → NULL = geen_opa; niet in scope hierboven)\n")

    cur.execute(f"""
        SELECT {PROC_CASE} AS proc, count(*) n
        FROM {T} WHERE inhoud_tekst ILIKE %(puitg)s OR inhoud_tekst ILIKE %(preg)s
        GROUP BY 1 ORDER BY 2 DESC""", PARAMS)
    print("Verdeling procedure:")
    for r in cur.fetchall():
        print(f"  {str(r['proc']):24s} {r['n']:>7d}")

    print("\nSteekproef per status (5 titels + evidence + bron):")
    for st in ("buitenplans_expliciet", "binnenplans_expliciet", "opa_onbepaald"):
        cur.execute(f"""
            SELECT koop_id, titel, {EVIDENCE_CASE} AS ev, {BRON_CASE} AS bron
            FROM (SELECT *, {STATUS_CASE} AS _st FROM {T} WHERE {STATUS_SCOPE}) s
            WHERE _st = %(st)s LIMIT 5""", {**PARAMS, "st": st})
        rows = cur.fetchall()
        print(f"\n  [{st}] ({len(rows)} voorbeelden):")
        for r in rows:
            print(f"    {r['koop_id']} · ev={r['ev']!r} bron={r['bron']}")
            print(f"       {r['titel'][:110]}")


def apply(conn):
    cur = conn.cursor()
    print("1) DDL (kolommen + index, idempotent)…")
    cur.execute(DDL)

    print("2) UPDATE afwijk_status/bron/evidence op OPA-superset…")
    cur.execute(f"""
        UPDATE {T} SET
            afwijk_status   = {STATUS_CASE},
            afwijk_bron     = {BRON_CASE},
            afwijk_evidence = {EVIDENCE_CASE}
        WHERE {STATUS_SCOPE}""", PARAMS)
    print(f"   {cur.rowcount} rijen geclassificeerd (status).")

    print("3) UPDATE procedure op procedure-superset…")
    cur.execute(f"""
        UPDATE {T} SET procedure = {PROC_CASE}
        WHERE inhoud_tekst ILIKE %(puitg)s OR inhoud_tekst ILIKE %(preg)s""", PARAMS)
    print(f"   {cur.rowcount} rijen kregen een procedure.")

    conn.commit()
    print("\nEindverdeling:")
    cur.execute(f"SELECT afwijk_status, count(*) n FROM {T} GROUP BY 1 ORDER BY 2 DESC")
    for r in cur.fetchall():
        print(f"  {str(r['afwijk_status']):24s} {r['n']:>7d}")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sample", action="store_true", help="lees + toon verdeling/voorbeelden, geen writes")
    g.add_argument("--apply", action="store_true", help="ALTER + UPDATE + index (idempotent)")
    args = ap.parse_args()

    conn = psycopg.connect(cfg.db_url, row_factory=dict_row)
    try:
        if args.sample:
            sample(conn)
        else:
            apply(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
