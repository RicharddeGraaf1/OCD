"""Genereer begrijpelijke varianten (hertalingen) — CONTENT-ADRESSEERBAAR.

De hertaling hoort bij de TEKST, niet bij het element. We hashen de
genormaliseerde inhoud_plain (v2a.norm_hash) en cachen per UNIEKE tekst in
v2a.hertaling. De bruidsschat heeft dezelfde regeltekst in ~342 gemeenten
gekopieerd → gemeten dedup 3,87× (391k elementen → 101k unieke teksten). Zo
kost een standaardzin één LLM-call i.p.v. honderden, en is het compounding:
hertaal een zin één keer → geldt voor alle gemeenten (ook toekomstige).

De prompt is CONTEXT-VRIJ (geen regelingnaam) — dat is de VOORWAARDE voor
dedup: met een regeling-titel erin zou dezelfde tekst per gemeente een andere
prompt → andere output → geen dedup geven.

Scope:
  --scope broekhem33  (default)  de 3 RoM-regelingen op RD 185904,320095
  --scope all                    alle actieve regelingen (hele DB, ~101k uniek)

Provider:
  --provider ollama  (default)   lokaal Qwen (qwen2.5:14b) — jouw GPU
  --provider anthropic           Anthropic-servers (Sonnet 5) — geen lokale last
  --model <naam>                 override modelnaam

Idempotent: skipt teksten die al een hertaling hebben voor (model, prompt_versie).
De hash wordt door de DB berekend (v2a.norm_hash) — één bron van waarheid.

NB: voor de volle DB (~101k uniek) per-item draaien is traag; de Batch API
(50% korting, klaar in ~1u) is dan de aangewezen route — aparte stap.

Run:  python scripts/begrijpelijk-hertaling.py --scope broekhem33
      python scripts/begrijpelijk-hertaling.py --scope broekhem33 --limit 20
      python scripts/begrijpelijk-hertaling.py --scope all --provider anthropic --model claude-sonnet-5
"""
from __future__ import annotations

import argparse
import os
import time

import httpx
import psycopg
from psycopg.rows import dict_row

OLLAMA_URL = "http://localhost:11434"
DB_URL = "postgresql://postgres:postgres@localhost:5434/dso"
X, Y = 185904, 320095

PROMPT_VERSIE = "v1"
ELEMENT_TYPES = ("Lid", "Divisietekst")
MIN_LEN = 30
TRUNC = 3000
COMMIT_EVERY = 25

DEFAULT_MODEL = {"ollama": "qwen2.5:14b", "anthropic": "claude-sonnet-5"}

SYSTEM_PROMPT = "Je bent een helper die juridische teksten uitlegt in eenvoudig Nederlands."


def user_prompt(inhoud_plain: str) -> str:
    """Context-VRIJ (geen regelingnaam) — voorwaarde voor content-dedup."""
    return (
        "Leg de volgende tekst uit de Nederlandse omgevingsregelgeving uit in "
        "begrijpelijke taal voor een gewone burger. Schrijf maximaal 3-4 korte "
        "zinnen. Gebruik geen juridisch jargon. Geef alleen de uitleg, geen inleiding.\n\n"
        f"Tekst:\n{inhoud_plain[:TRUNC]}"
    )


def todo_texts(conn, scope: str, model: str, limit: int | None,
               sinds: str | None = None) -> list[dict]:
    """Unieke teksten (per genormaliseerde hash) die nog geen hertaling hebben."""
    if scope == "broekhem33":
        scope_cte = """
            WITH scope AS (
                SELECT DISTINCT r.frbr_expression AS expr
                FROM p2p.regeling r
                JOIN p2p.locatie_subdiv ls ON ls.identificatie = r.regelingsgebied_id
                WHERE ST_Intersects(ls.geometrie, ST_SetSRID(ST_MakePoint(%(x)s,%(y)s),28992))
                  AND NOT r.inactief
                  AND r.documenttype IN ('Omgevingsplan','Omgevingsverordening','Waterschapsverordening')
            ),
            elems AS (
                SELECT te.inhoud_plain, v2a.norm_hash(te.inhoud_plain) AS bh
                FROM scope s JOIN p2p.tekst_element te ON te.regeling_expression = s.expr
                WHERE te.element_type = ANY(%(types)s)
                  AND te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > %(minlen)s
            )
        """
    elif scope == "sync":
        # De regelingen die een sync zojuist heeft geladen. Toegevoegd 2026-08-13:
        # na een sync wil je de nieuwe tekst hertalen, niet het hele land. Van de
        # acht regelingen van 12-08 waren 3.707 teksten uniek en had 45% al een
        # hertaling (content-adressering: de bruidsschat staat landelijk gekopieerd),
        # dus 2.021 te doen tegen ~85.000 bij `--scope all`.
        scope_cte = """
            WITH scope AS (
                SELECT frbr_expression AS expr FROM p2p.regeling_load
                 WHERE geladen_op >= %(sinds)s
            ),
            elems AS (
                SELECT te.inhoud_plain, v2a.norm_hash(te.inhoud_plain) AS bh
                FROM scope s JOIN p2p.tekst_element te ON te.regeling_expression = s.expr
                WHERE te.element_type = ANY(%(types)s)
                  AND te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > %(minlen)s
            )
        """
    else:  # all
        scope_cte = """
            WITH elems AS (
                SELECT te.inhoud_plain, v2a.norm_hash(te.inhoud_plain) AS bh
                FROM p2p.tekst_element te
                JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
                WHERE te.element_type = ANY(%(types)s)
                  AND te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > %(minlen)s
                  AND NOT r.inactief
            )
        """
    sql = scope_cte + """
        SELECT DISTINCT ON (e.bh) e.bh, e.inhoud_plain
        FROM elems e
        WHERE NOT EXISTS (
            SELECT 1 FROM v2a.hertaling h
            WHERE h.bron_hash = e.bh AND h.model = %(model)s AND h.prompt_versie = %(pv)s
        )
        ORDER BY e.bh, length(e.inhoud_plain)
    """
    params = {"x": X, "y": Y, "types": list(ELEMENT_TYPES), "minlen": MIN_LEN,
              "model": model, "pv": PROMPT_VERSIE, "sinds": sinds}
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows[:limit] if limit else rows


def chat_ollama(model: str, system: str, user: str) -> str:
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json={
        "model": model, "stream": False, "options": {"temperature": 0.3},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }, timeout=180)
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content", "").strip()


def chat_anthropic(client, model: str, system: str, user: str) -> str:
    # Sonnet 5: geen temperature (wordt geweigerd); thinking uit voor een platte hertaling.
    msg = client.messages.create(
        model=model, max_tokens=300,
        thinking={"type": "disabled"},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["broekhem33", "sync", "all"], default="broekhem33")
    ap.add_argument("--sinds", default=None,
                    help="alleen bij --scope sync: ISO-tijdstip; default = start van "
                         "de laatste geslaagde sync-run")
    ap.add_argument("--provider", choices=["ollama", "anthropic"], default="ollama")
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    model = args.model or DEFAULT_MODEL[args.provider]
    anthropic_client = None
    if args.provider == "anthropic":
        import anthropic  # vereist ANTHROPIC_API_KEY in .env / env
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY ontbreekt (zet 'm in dso-loader/.env).")
        anthropic_client = anthropic.Anthropic()

    t0 = time.monotonic()
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        sinds = args.sinds
        if args.scope == "sync" and not sinds:
            with conn.cursor() as cur:
                cur.execute("SELECT max(gestart_op) AS t FROM audit.sync_run "
                            "WHERE klaar_op IS NOT NULL "
                            "  AND coalesce(opmerking,'') NOT ILIKE '%%afgebroken%%'")
                sinds = cur.fetchone()["t"]
            print(f"Scope sync: regelingen geladen sinds {sinds}")
        todo = todo_texts(conn, args.scope, model, args.limit, sinds)
        print(f"Unieke teksten te hertalen: {len(todo)} "
              f"(scope={args.scope}, model={model}, prompt={PROMPT_VERSIE})\n")
        done = fails = 0
        for i, row in enumerate(todo, 1):
            try:
                up = user_prompt(row["inhoud_plain"])
                if args.provider == "anthropic":
                    tekst = chat_anthropic(anthropic_client, model, SYSTEM_PROMPT, up)
                else:
                    tekst = chat_ollama(model, SYSTEM_PROMPT, up)
                if not tekst:
                    raise ValueError("lege LLM-output")
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO v2a.hertaling (bron_hash, model, prompt_versie, tekst)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (bron_hash, model, prompt_versie)
                           DO UPDATE SET tekst = EXCLUDED.tekst, gegenereerd_op = now()""",
                        (row["bh"], model, PROMPT_VERSIE, tekst),
                    )
                done += 1
                if done % COMMIT_EVERY == 0:
                    conn.commit()
                print(f"  [{i}/{len(todo)}] {row['bh'][:8]} +{len(tekst):4d}ch  "
                      f"{tekst.replace(chr(10), ' ')[:80]}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  [{i}/{len(todo)}] {row['bh'][:8]} FOUT: {e}")
        conn.commit()
    dt = time.monotonic() - t0
    print(f"\nKlaar: {done} unieke hertalingen, {fails} fouten in {dt:.0f}s "
          f"({dt / max(done, 1):.1f}s/tekst)")


if __name__ == "__main__":
    main()
