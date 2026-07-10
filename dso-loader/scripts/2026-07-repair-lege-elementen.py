"""Herstel lege tekst-elementen na onvolledige documentstructuur-loads.

Bevinding (2026-07-10, score-traject omgevingsbot): 36 actieve regelingen
hebben elementen met lege inhoud waar de vorige expressie wél tekst had —
bij de ergste gevallen (OV Fryslân, OV Zeeland, ZH OV) ontbreekt de complete
Artikel/Lid-laag. De Presenteren-API levert de inhoud nu wél volledig
(geverifieerd: art. 3.9 Fryslân = 2129 chars via verse documentstructuur-
call), dus de oorspronkelijke load kreeg een onvolledige respons.

Werkwijze per geraakte regeling:
  1. versie-check: DSO-actueel expressionId == onze actieve expressie,
     anders skip (dan is een reguliere reload-flow nodig);
  2. verse documentstructuur ophalen; sanity: minstens evenveel gevulde
     elementen als nu in de DB, anders skip;
  3. v2a-chunks (cascade: chunk_annotatie), inline-referenties en
     tekst-elementen van de expressie verwijderen; herladen via
     load_documentstructuur;
  4. na alle regelingen: nieuwe elementen embedden (zelfde selectie als
     run_overnight fase 4a+5, idempotent) en chunk_annotatie rebuilden
     (scripts/2026-07-add-chunk-annotatie.sql, idempotent).

Run:  python scripts/2026-07-repair-lege-elementen.py [--dry-run]
"""
import sys
import time

sys.path.insert(0, ".")

import httpx

from src.config import cfg
from src.db import get_conn
from src.loaders.api_loader import (
    _encode_regeling_uri,
    _flatten_components,
    _get,
    load_documentstructuur,
)

OLLAMA = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
BATCH = 64

# Zelfde meetcriteria als de audit: actieve expressie met lege
# Artikel/Lid/Divisietekst-elementen waarvan een inactieve sibling-expressie
# (zelfde wid) wél inhoud heeft.
AFFECTED_SQL = """
SELECT ra.frbr_work AS work, act.regeling_expression AS expr,
       ra.opschrift, count(*) AS lege
FROM p2p.tekst_element act
JOIN p2p.regeling ra ON ra.frbr_expression = act.regeling_expression AND NOT ra.inactief
WHERE length(coalesce(act.inhoud_plain,'')) <= 30
  AND act.element_type IN ('Artikel','Lid','Divisietekst')
  AND EXISTS (SELECT 1 FROM p2p.tekst_element oud
              JOIN p2p.regeling ro ON ro.frbr_expression = oud.regeling_expression AND ro.inactief
              WHERE oud.wid = act.wid AND length(oud.inhoud_plain) > 30)
GROUP BY 1, 2, 3
ORDER BY lege DESC
"""

# Embed-selectie: letterlijke kopie van run_overnight.FETCH zodat de
# herstelde elementen exact zoals de reguliere build worden opgenomen.
FETCH = """
WITH RECURSIVE kop_chain AS (
    SELECT id, parent_id, opschrift, 1 AS d, ARRAY[opschrift]::text[] AS path
    FROM p2p.tekst_element WHERE regeling_expression = %(e)s
      AND inhoud_plain IS NOT NULL AND length(inhoud_plain) > 30
    UNION ALL
    SELECT k.id, p.parent_id, k.opschrift, k.d+1,
           CASE WHEN p.opschrift IS NOT NULL AND p.opschrift <> '' THEN p.opschrift || k.path ELSE k.path END
    FROM kop_chain k JOIN p2p.tekst_element p ON p.id = k.parent_id WHERE k.d < 20
),
best AS (SELECT DISTINCT ON (id) id, path FROM kop_chain ORDER BY id, d DESC)
SELECT te.id, te.element_type, te.wid, te.inhoud_plain,
       array_to_string(b.path, ' > ') AS kop_pad
FROM p2p.tekst_element te JOIN best b ON b.id = te.id
WHERE te.regeling_expression = %(e)s
  AND te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > 30
  AND (te.element_type IN ('Lid','Divisietekst','Begrip')
       OR (te.element_type = 'Artikel'
           AND NOT EXISTS (SELECT 1 FROM p2p.tekst_element kid
                           WHERE kid.parent_id = te.id AND kid.element_type = 'Lid')))
  AND NOT EXISTS (SELECT 1 FROM v2a.tekst_embedding v WHERE v.tekst_element_id = te.id)
ORDER BY te.volgorde
"""


def vlit(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def embed_batch(texts):
    r = httpx.post(f"{OLLAMA}/api/embed",
                   json={"model": EMBED_MODEL, "input": texts}, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]


def dso_actuele_expressie(work: str) -> str | None:
    """expressionId van de DSO-actuele versie van dit work (None bij fout)."""
    try:
        data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{_encode_regeling_uri(work)}")
        return data.get("expressionId")
    except Exception as e:  # noqa: BLE001
        print(f"    versie-check faalde: {e}")
        return None


def main(dry_run: bool):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(AFFECTED_SQL)
        affected = cur.fetchall()
    print(f"{len(affected)} geraakte regelingen")

    repaired = []
    for reg in affected:
        work, expr, titel = reg["work"], reg["expr"], (reg["opschrift"] or "")[:55]
        print(f"\n== {titel} ({reg['lege']} lege) ==")

        actueel = dso_actuele_expressie(work)
        if actueel != expr:
            print(f"    SKIP: DSO-actueel is {actueel!r}, onze actieve is {expr!r} — reguliere reload nodig")
            continue

        # Verse structuur + sanity-check.
        try:
            data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{_encode_regeling_uri(work)}/documentstructuur")
        except Exception as e:  # noqa: BLE001
            print(f"    SKIP: documentstructuur-call faalde: {e}")
            continue
        els = _flatten_components(data.get("_embedded", {}).get("documentComponenten", []))
        vers_gevuld = sum(1 for e in els if e["inhoud"] and len(e["inhoud"]) > 30)
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) AS n FROM p2p.tekst_element
                           WHERE regeling_expression = %s AND length(coalesce(inhoud_plain,'')) > 30""",
                        (expr,))
            db_gevuld = cur.fetchone()["n"]
        print(f"    vers: {len(els)} elementen, {vers_gevuld} gevuld | DB nu: {db_gevuld} gevuld")
        if vers_gevuld <= db_gevuld:
            print("    SKIP: verse respons niet beter dan huidige DB-inhoud")
            continue
        if dry_run:
            print("    DRY-RUN: zou herladen")
            continue

        # Verwijderen + herladen in één transactie.
        with conn.cursor() as cur:
            cur.execute("""DELETE FROM v2a.tekst_embedding
                           WHERE regeling_expression = %s
                             AND tekst_element_id IS NOT NULL""", (expr,))
            n_chunks = cur.rowcount
            cur.execute("""DELETE FROM p2p.tekst_inline_referentie
                           WHERE tekst_element_id IN
                                 (SELECT id FROM p2p.tekst_element WHERE regeling_expression = %s)""",
                        (expr,))
            cur.execute("DELETE FROM p2p.tekst_element WHERE regeling_expression = %s", (expr,))
            n_el = cur.rowcount
        n_new = load_documentstructuur(conn, work, expr)
        conn.commit()
        print(f"    herladen: {n_el} elementen + {n_chunks} chunks weg → {n_new} nieuwe elementen")
        repaired.append(expr)

    # Embed nieuwe elementen van de herstelde expressies.
    if repaired and not dry_run:
        print(f"\n== embed-fase voor {len(repaired)} expressies ==")
        for expr in repaired:
            with conn.cursor() as cur:
                cur.execute(FETCH, {"e": expr})
                rows = cur.fetchall()
            if not rows:
                print(f"    {expr}: niets te embedden?")
                continue
            texts = [(r["kop_pad"] + " · " + r["inhoud_plain"]) if r["kop_pad"] else r["inhoud_plain"]
                     for r in rows]
            embeds = []
            for j in range(0, len(texts), BATCH):
                embeds.extend(embed_batch(texts[j:j + BATCH]))
                print(f"    {expr[-40:]}: {min(j + BATCH, len(texts))}/{len(texts)} embedded", end="\r")
            with conn.cursor() as cur:
                for r, v in zip(rows, embeds):
                    cur.execute("""INSERT INTO v2a.tekst_embedding
                        (tekst_element_id, regeling_expression, bron_soort, kop_pad, inhoud_plain,
                         embedding, wid, source_type, source_id)
                        VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,%s)""",
                        (r["id"], expr, r["element_type"], r["kop_pad"],
                         r["inhoud_plain"], vlit(v), r["wid"], r["element_type"], r["id"]))
            conn.commit()
            print(f"\n    {expr}: {len(rows)} chunks toegevoegd")

    conn.close()
    print("\nKlaar. Vergeet niet: python scripts/run_sql.py scripts/2026-07-add-chunk-annotatie.sql")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
