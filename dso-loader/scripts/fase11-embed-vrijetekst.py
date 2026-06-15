"""Fase 11 — embed Divisietekst-chunks van Visies/Programma's/N2000-besluiten/Projectbesluiten
voor regelingen die in de Plan-A-testset-scope vallen maar nog geen chunks hebben in v2a.

Achtergrond: Plan A §2 Fase 0 stelde vast dat vrijetekst-instrumenten alleen
Divisietekst hebben (geen Lid/Artikel). De Fase 5 corpus-uitbreiding heeft die
documenttypes feitelijk niet meegenomen voor de testset-locaties — cluster C
(r34/r37/r39) faalt daardoor structureel ook na de Fase 10 scope-fix.

Selecteert per testpunt de Visies/Programma's met regelingsgebied dat het punt
intersecteert, embedt hun Divisietekst-chunks (kop_pad + ' · ' + inhoud_plain
via Ollama nomic-embed-text), INSERT in v2a.tekst_embedding.

Idempotent: skipt regelingen die al ≥1 chunk in v2a hebben.
"""
import json
import time
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
DB_URL = "postgresql://postgres:postgres@localhost:5434/dso"

FREETEXT_DOCTYPES = (
    "Omgevingsvisie", "Programma",
    "Aanwijzingsbesluit N2000", "Projectbesluit", "Toegangsbeperkingsbesluit",
)


def collect_testset_coords() -> list[tuple[float, float]]:
    """Pluk RD-coorden uit de meest recente OCD-eval-results-trace."""
    import re
    path = Path("c:/GIT/omgevingsbot.nl/backend/tests/evaluation/last_results_test_cases_ocd.json")
    with path.open(encoding="utf-8") as f:
        d = json.load(f)
    coords = []
    for r in d["results"]:
        for t in r.get("trace", []):
            if "RD:" in t:
                m = re.search(r"RD[:\s]+([0-9.]+)[,\s]+([0-9.]+)", t)
                if m:
                    coords.append((float(m.group(1)), float(m.group(2))))
                    break
    return coords


def regelingen_in_scope(conn, coords: list[tuple[float, float]]) -> list[str]:
    """Vrijetekst-regelingen waarvan regelingsgebied minstens 1 testpunt raakt."""
    exprs = set()
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        for x, y in coords:
            cur.execute(
                """SELECT DISTINCT r.frbr_expression
                   FROM p2p.regeling r
                   JOIN p2p.locatie_subdiv ls ON ls.identificatie = r.regelingsgebied_id
                   WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
                     AND NOT r.inactief
                     AND r.documenttype = ANY(%s)""",
                (x, y, list(FREETEXT_DOCTYPES)),
            )
            exprs.update(r["frbr_expression"] for r in cur.fetchall())
    return sorted(exprs)


def reeds_geembed(conn, exprs: list[str]) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT regeling_expression FROM v2a.tekst_embedding WHERE regeling_expression = ANY(%s)",
            (exprs,),
        )
        return {r["regeling_expression"] for r in cur.fetchall()}


def fetch_chunks(conn, expr: str) -> list[dict]:
    """Recursief kop_pad bouwen via parent_id-keten."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE kop_chain AS (
                SELECT id, parent_id, opschrift, 1 AS d, ARRAY[opschrift]::text[] AS path
                FROM p2p.tekst_element
                WHERE regeling_expression = %s
                  AND inhoud_plain IS NOT NULL AND length(inhoud_plain) > 30
                UNION ALL
                SELECT k.id, p.parent_id, k.opschrift,
                       k.d + 1,
                       CASE WHEN p.opschrift IS NOT NULL AND p.opschrift <> ''
                            THEN p.opschrift || k.path ELSE k.path END
                FROM kop_chain k
                JOIN p2p.tekst_element p ON p.id = k.parent_id
                WHERE k.d < 20
            ),
            best AS (
                SELECT DISTINCT ON (id) id, path
                FROM kop_chain ORDER BY id, d DESC
            )
            SELECT te.id, te.inhoud_plain,
                   array_to_string(b.path, ' > ') AS kop_pad
            FROM p2p.tekst_element te
            JOIN best b ON b.id = te.id
            WHERE te.regeling_expression = %s
              AND te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > 30
            ORDER BY te.volgorde
            """,
            (expr, expr),
        )
        return cur.fetchall()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Ollama nomic-embed-text accepteert array van strings — batch-friendly."""
    r = httpx.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["embeddings"]


def to_vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def insert_chunks(conn, expr: str, rows: list[dict], embeds: list[list[float]]):
    # `fts` is een generated column (to_tsvector('dutch', inhoud_plain)) — niet zelf invullen.
    with conn.cursor() as cur:
        for row, vec in zip(rows, embeds):
            cur.execute(
                """INSERT INTO v2a.tekst_embedding
                   (tekst_element_id, regeling_expression, bron_soort, kop_pad,
                    inhoud_plain, embedding)
                   VALUES (%s, %s, 'Divisietekst', %s, %s, %s::vector)""",
                (row["id"], expr, row["kop_pad"], row["inhoud_plain"],
                 to_vector_literal(vec)),
            )
    conn.commit()


def main():
    t0 = time.monotonic()
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        coords = collect_testset_coords()
        print(f"Testset-coorden: {len(coords)}")
        exprs = regelingen_in_scope(conn, coords)
        print(f"Vrijetekst-regelingen in scope: {len(exprs)}")
        al_klaar = reeds_geembed(conn, exprs)
        todo = [e for e in exprs if e not in al_klaar]
        print(f"Al geëmbed: {len(al_klaar)}; te embedden: {len(todo)}")

        totaal_chunks = 0
        for i, expr in enumerate(todo, 1):
            rows = fetch_chunks(conn, expr)
            if not rows:
                print(f"  [{i}/{len(todo)}] {expr} — geen chunks (skip)")
                continue
            texts = [
                (r["kop_pad"] + " · " + r["inhoud_plain"] if r["kop_pad"] else r["inhoud_plain"])
                for r in rows
            ]
            # Batch in stukjes van 50 om Ollama-call-size te begrenzen
            embeds = []
            for j in range(0, len(texts), 50):
                embeds.extend(embed_batch(texts[j:j + 50]))
            insert_chunks(conn, expr, rows, embeds)
            totaal_chunks += len(rows)
            elapsed = time.monotonic() - t0
            print(f"  [{i}/{len(todo)}] +{len(rows):4d} chunks {expr[-60:]} (totaal {totaal_chunks}, {elapsed:.0f}s)")

    elapsed = time.monotonic() - t0
    print(f"\nKlaar: {totaal_chunks} chunks gefild in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
