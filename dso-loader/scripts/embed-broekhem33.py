"""Embed alle ontbrekende LOKALE regelingen die gelden op Broekhem 33 Valkenburg
(RD 185904, 320095) in v2a.tekst_embedding — voor de SKOS-vs-vector PoC.

Generalisatie van fase11-embed-vrijetekst.py:
- één coördinaat i.p.v. testset-coorden;
- ALLE documenttypes behalve landelijke AMvB / Ministeriële Regeling
  (die zitten al in v2a en worden door de tiering ×0.55 gedempt);
- correcte bron_soort per element_type (Lid→Lid, Divisietekst→Divisietekst,
  Begrip→Begrip) i.p.v. hardcoded 'Divisietekst';
- idempotent: skipt regelingen die al ≥1 chunk in v2a hebben.

Model: nomic-embed-text (768-dim) — moet matchen met de bestaande v2a-index.
"""
import time

import httpx
import psycopg
from psycopg.rows import dict_row

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
DB_URL = "postgresql://postgres:postgres@localhost:5434/dso"
X, Y = 185904, 320095

# element_type -> bron_soort, afgeleid uit de bestaande v2a-conventie
EMBED_TYPES = ("Lid", "Divisietekst", "Begrip")


def scope_regelingen(conn) -> list[dict]:
    """Lokale regelingen op het punt die nog geen chunks in v2a hebben."""
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute(
            """
            WITH scope AS (
                SELECT DISTINCT r.frbr_expression AS expr, r.opschrift AS titel,
                       r.documenttype AS dt
                FROM p2p.regeling r
                JOIN p2p.locatie_subdiv ls ON ls.identificatie = r.regelingsgebied_id
                WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                  AND NOT r.inactief
                  AND r.documenttype <> 'AMvB'
                  AND r.documenttype NOT ILIKE 'Minister%%'
            )
            SELECT s.* FROM scope s
            WHERE NOT EXISTS (
                SELECT 1 FROM v2a.tekst_embedding v WHERE v.regeling_expression = s.expr
            )
            ORDER BY s.dt, s.titel
            """,
            (X, Y),
        )
        return cur.fetchall()


def fetch_chunks(conn, expr: str) -> list[dict]:
    """Content-dragende elementen (Lid/Divisietekst/Begrip) met recursief kop_pad."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE kop_chain AS (
                SELECT id, parent_id, opschrift, 1 AS d, ARRAY[opschrift]::text[] AS path
                FROM p2p.tekst_element
                WHERE regeling_expression = %(e)s
                  AND inhoud_plain IS NOT NULL AND length(inhoud_plain) > 30
                UNION ALL
                SELECT k.id, p.parent_id, k.opschrift, k.d + 1,
                       CASE WHEN p.opschrift IS NOT NULL AND p.opschrift <> ''
                            THEN p.opschrift || k.path ELSE k.path END
                FROM kop_chain k
                JOIN p2p.tekst_element p ON p.id = k.parent_id
                WHERE k.d < 20
            ),
            best AS (
                SELECT DISTINCT ON (id) id, path FROM kop_chain ORDER BY id, d DESC
            )
            SELECT te.id, te.element_type, te.inhoud_plain,
                   array_to_string(b.path, ' > ') AS kop_pad
            FROM p2p.tekst_element te
            JOIN best b ON b.id = te.id
            WHERE te.regeling_expression = %(e)s
              AND te.element_type = ANY(%(types)s)
              AND te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > 30
            ORDER BY te.volgorde
            """,
            {"e": expr, "types": list(EMBED_TYPES)},
        )
        return cur.fetchall()


def embed_batch(texts: list[str]) -> list[list[float]]:
    r = httpx.post(f"{OLLAMA_URL}/api/embed",
                   json={"model": EMBED_MODEL, "input": texts}, timeout=90)
    r.raise_for_status()
    return r.json()["embeddings"]


def to_vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def insert_chunks(conn, expr: str, rows: list[dict], embeds: list[list[float]]):
    with conn.cursor() as cur:
        for row, vec in zip(rows, embeds):
            cur.execute(
                """INSERT INTO v2a.tekst_embedding
                   (tekst_element_id, regeling_expression, bron_soort, kop_pad,
                    inhoud_plain, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s::vector)""",
                (row["id"], expr, row["element_type"], row["kop_pad"],
                 row["inhoud_plain"], to_vec(vec)),
            )
    conn.commit()


def main():
    t0 = time.monotonic()
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        todo = scope_regelingen(conn)
        print(f"Lokale regelingen op Broekhem 33 zonder v2a-chunks: {len(todo)}\n")
        totaal = 0
        for i, reg in enumerate(todo, 1):
            rows = fetch_chunks(conn, reg["expr"])
            if not rows:
                print(f"  [{i}/{len(todo)}] {(reg['titel'] or '')[:45]:45} — 0 chunks (skip)")
                continue
            texts = [(r["kop_pad"] + " · " + r["inhoud_plain"]) if r["kop_pad"]
                     else r["inhoud_plain"] for r in rows]
            embeds = []
            for j in range(0, len(texts), 50):
                embeds.extend(embed_batch(texts[j:j + 50]))
            insert_chunks(conn, reg["expr"], rows, embeds)
            totaal += len(rows)
            print(f"  [{i}/{len(todo)}] {(reg['titel'] or '')[:45]:45} +{len(rows):4d} "
                  f"[{reg['dt']}] (tot {totaal}, {time.monotonic()-t0:.0f}s)")
    print(f"\nKlaar: {totaal} chunks toegevoegd in {time.monotonic()-t0:.0f}s")


if __name__ == "__main__":
    main()
