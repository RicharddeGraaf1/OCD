"""Genereer een begrijpelijke variant (hertaling) voor alle content-elementen
(Lid/Divisietekst) van de RoM-prototype-regelingen op Broekhem 33 Valkenburg
(RD 185904, 320095) en cache ze in v2a.tekst_begrijpelijk.

Spiegelt de omgevingsbot simplify-call (omgevingsbot.nl/backend/services/
llm_service.py::simplify_text): systeem- + user-prompt verbatim, Ollama
qwen2.5:14b, temperature 0.3. Het verschil: batch + gecached i.p.v. per-view.

Scope = de 3 decentrale regelingen die RoM toont (Omgevingsplan /
Omgevingsverordening / Waterschapsverordening) op het punt, NOT inactief.

Idempotent: de scope-query skipt elementen die al een variant hebben voor
(model, prompt_versie) MET matchende bron_hash. Bump PROMPT_VERSIE of een
re-sync (nieuwe regeling_expression) → alleen het nieuwe/gewijzigde wordt gedaan.

Run:  python scripts/begrijpelijk-broekhem33.py             # volle batch
      python scripts/begrijpelijk-broekhem33.py --limit 5   # rooktest
"""
from __future__ import annotations

import argparse
import time

import httpx
import psycopg
from psycopg.rows import dict_row

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5:14b"
PROMPT_VERSIE = "v1"
DB_URL = "postgresql://postgres:postgres@localhost:5434/dso"
X, Y = 185904, 320095

ELEMENT_TYPES = ("Lid", "Divisietekst")  # wat RoM als lid-tekst rendert
MIN_LEN = 30                              # skip lege/triviale elementen
TRUNC = 3000                             # zelfde truncatie als de bot
TEMPERATURE = 0.3                        # zelfde temp als de bot
COMMIT_EVERY = 20

SYSTEM_PROMPT = "Je bent een helper die juridische teksten uitlegt in eenvoudig Nederlands."


def user_prompt(inhoud_plain: str, regeling_titel: str | None) -> str:
    """Verbatim gespiegeld op omgevingsbot simplify_text."""
    bron_label = f" (uit: {regeling_titel})" if regeling_titel else ""
    return (
        f"Leg de volgende tekst uit de Nederlandse omgevingsregelgeving{bron_label} "
        f"uit in begrijpelijke taal voor een gewone burger. "
        f"Schrijf maximaal 3–4 korte zinnen. Gebruik geen juridisch jargon. "
        f"Geef alleen de uitleg, geen inleiding.\n\n"
        f"Tekst:\n{inhoud_plain[:TRUNC]}"
    )


def scope_elements(conn, limit: int | None) -> list[dict]:
    """Content-elementen van de 3 RoM-regelingen die nog GEEN actuele variant
    hebben (idempotent: LEFT JOIN op v2a.tekst_begrijpelijk met bron_hash-match)."""
    sql = """
        WITH scope AS (
            SELECT DISTINCT r.frbr_expression AS expr
            FROM p2p.regeling r
            JOIN p2p.locatie_subdiv ls ON ls.identificatie = r.regelingsgebied_id
            WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 28992))
              AND NOT r.inactief
              AND r.documenttype IN ('Omgevingsplan','Omgevingsverordening','Waterschapsverordening')
        )
        SELECT te.id, te.wid, te.regeling_expression, te.inhoud_plain,
               r.opschrift AS regeling_titel,
               md5(te.inhoud_plain) AS bron_hash
        FROM scope s
        JOIN p2p.tekst_element te ON te.regeling_expression = s.expr
        JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        LEFT JOIN v2a.tekst_begrijpelijk tb
               ON tb.tekst_element_id = te.id
              AND tb.model = %(model)s
              AND tb.prompt_versie = %(pv)s
              AND tb.bron_hash = md5(te.inhoud_plain)
        WHERE te.element_type = ANY(%(types)s)
          AND te.inhoud_plain IS NOT NULL
          AND length(te.inhoud_plain) > %(minlen)s
          AND tb.id IS NULL
        ORDER BY te.regeling_expression, te.volgorde
    """
    params = {
        "x": X, "y": Y, "model": MODEL, "pv": PROMPT_VERSIE,
        "types": list(ELEMENT_TYPES), "minlen": MIN_LEN,
    }
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows[:limit] if limit else rows


def chat(system: str, user: str) -> str:
    resp = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": TEMPERATURE},
        },
        timeout=180,
    )
    resp.raise_for_status()
    return (resp.json().get("message") or {}).get("content", "").strip()


def upsert(conn, row: dict, tekst: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO v2a.tekst_begrijpelijk
                (tekst_element_id, wid, regeling_expression, tekst, model, prompt_versie, bron_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tekst_element_id, model, prompt_versie)
            DO UPDATE SET tekst = EXCLUDED.tekst,
                          bron_hash = EXCLUDED.bron_hash,
                          gegenereerd_op = now()
            """,
            (row["id"], row["wid"], row["regeling_expression"], tekst,
             MODEL, PROMPT_VERSIE, row["bron_hash"]),
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="max aantal elementen (rooktest)")
    args = ap.parse_args()

    t0 = time.monotonic()
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        todo = scope_elements(conn, args.limit)
        print(f"Te genereren: {len(todo)} elementen "
              f"(model={MODEL}, prompt={PROMPT_VERSIE}, types={ELEMENT_TYPES})\n")
        done = fails = 0
        for i, row in enumerate(todo, 1):
            try:
                tekst = chat(SYSTEM_PROMPT, user_prompt(row["inhoud_plain"], row["regeling_titel"]))
                if not tekst:
                    raise ValueError("lege LLM-output")
                upsert(conn, row, tekst)
                done += 1
                if done % COMMIT_EVERY == 0:
                    conn.commit()
                preview = tekst.replace("\n", " ")[:90]
                print(f"  [{i}/{len(todo)}] te={row['id']:>8} +{len(tekst):4d}ch  {preview}")
            except Exception as e:  # noqa: BLE001 — batch mag niet klappen op één element
                fails += 1
                print(f"  [{i}/{len(todo)}] te={row['id']:>8} FOUT: {e}")
        conn.commit()
    dt = time.monotonic() - t0
    print(f"\nKlaar: {done} gegenereerd, {fails} fouten in {dt:.0f}s "
          f"({dt / max(done, 1):.1f}s/element)")


if __name__ == "__main__":
    main()
