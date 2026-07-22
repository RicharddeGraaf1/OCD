"""Bundel subagent-output-JSONL in v2a.hertaling. Eén schrijver, geparametriseerd.

Stap 3 van het golf-recept in begrijpelijk-fetch-shards.py: de subagents
schrijven elk {"h": "<hash>", "tekst": "<hertaling>"}-regels; dit script leest
alle output-bestanden en upsert ze in één transactie (ON CONFLICT DO UPDATE,
dus her-draaien is veilig).

Gebruik:  python begrijpelijk-insert.py <model> <glob>
bijv.:    python begrijpelijk-insert.py claude-sonnet-5 "C:/tmp/son_out_*.jsonl"
"""
import glob as globmod
import json
import sys

import psycopg

DB_URL = "postgresql://postgres:postgres@localhost:5434/dso"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-sonnet-5"
GLOB = sys.argv[2] if len(sys.argv) > 2 else "C:/tmp/son_out_*.jsonl"
PROMPT_VERSIE = "v1"

rows, bad = [], 0
for path in sorted(globmod.glob(GLOB)):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
                h, tekst = o["h"], (o["tekst"] or "").strip()
                if h and tekst:
                    rows.append((h, tekst))
                else:
                    bad += 1
            except Exception:  # noqa: BLE001
                bad += 1

print(f"gelezen: {len(rows)} geldig, {bad} ongeldig/leeg  (model={MODEL})")
if not rows:
    sys.exit("niets te doen")

with psycopg.connect(DB_URL) as conn:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO v2a.hertaling (bron_hash, model, prompt_versie, tekst)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (bron_hash, model, prompt_versie)
               DO UPDATE SET tekst = EXCLUDED.tekst, gegenereerd_op = now()""",
            [(h, MODEL, PROMPT_VERSIE, t) for h, t in rows],
        )
    conn.commit()
print(f"ingevoerd/bijgewerkt: {len(rows)} in v2a.hertaling")
