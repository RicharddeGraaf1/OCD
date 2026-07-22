"""Fetch te-hertalen unieke teksten en schrijf JSONL-shards voor subagent-fan-out.

Onderdeel van het GOLF-RECEPT (subscription-route: geen API-credits, geen lokale
GPU) — het alternatief voor begrijpelijk-hertaling.py (per-item) en de Batch API:

  1. python begrijpelijk-fetch-shards.py 5000 claude-sonnet-5 150 topshard
       -> C:/tmp/topshard_01.jsonl ... (150 teksten per shard, {"h","t"} per regel)
  2. Spawn per golf ~8 Claude-subagents (Sonnet/Haiku), elk op één shard.
       Prompt = verbatim de v1-prompt uit begrijpelijk-hertaling.py (context-vrij!).
       Output per agent: C:/tmp/son_out_<NN>_{a,b,c}.jsonl met {"h","tekst"} per
       regel (drie deelbestanden tegen truncatie bij één grote write).
  3. python begrijpelijk-insert.py claude-sonnet-5 "C:/tmp/son_out_*.jsonl"
  4. Herhaal tot stap 1 "0 te doen" meldt. Idempotent op hash: gaten rollen
       vanzelf de volgende golf in.

KOP-EERST: de shards zijn gesorteerd op frequentie (count DESC over de hele
actieve DB). De verdeling is extreem scheef (bruidsschat): top-1.000 unieke
teksten dekt 61,7% van alle ~391k elementen, top-5.000 = 74,5%. De staart
(~51k singletons, laatste 13%) NIET bulk-precomputen maar lazy per locatie
vullen (klein restgolfje per nieuw product; zie 2026-07-22 in de vault-log).

Gebruik:  python begrijpelijk-fetch-shards.py <topN> <model> <shard_size> <prefix>
          topN=0 betekent: geen kop-limiet (alles wat nog ontbreekt).
"""
import json
import sys

import psycopg

DB_URL = "postgresql://postgres:postgres@localhost:5434/dso"
TOPN = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
MODEL = sys.argv[2] if len(sys.argv) > 2 else "claude-sonnet-5"
SHARD = int(sys.argv[3]) if len(sys.argv) > 3 else 150
PREFIX = sys.argv[4] if len(sys.argv) > 4 else "topshard"
TRUNC = 3000

SQL = """
WITH u AS (
  SELECT v2a.norm_hash(te.inhoud_plain) h, count(*) n, min(te.inhoud_plain) t
  FROM p2p.tekst_element te
  JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
  WHERE te.element_type IN ('Lid','Divisietekst') AND te.inhoud_plain IS NOT NULL
    AND length(te.inhoud_plain) > 30 AND NOT r.inactief
  GROUP BY 1
), top AS (
  SELECT h, n, t FROM u ORDER BY n DESC, h LIMIT %(topn)s
)
SELECT h, n, t FROM top
WHERE NOT EXISTS (
  SELECT 1 FROM v2a.hertaling x
  WHERE x.bron_hash = top.h AND x.model = %(model)s AND x.prompt_versie = 'v1')
ORDER BY n DESC, h
"""

with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
    cur.execute("SET max_parallel_workers_per_gather = 0")
    cur.execute(SQL, {"topn": TOPN or None, "model": MODEL})
    rows = cur.fetchall()

print(f"top-{TOPN or 'alles'} nog te doen voor {MODEL}: {len(rows)} "
      f"(dekking-gewicht van deze rest: {sum(r[1] for r in rows)} elementen)")
for i in range(0, len(rows), SHARD):
    path = f"C:/tmp/{PREFIX}_{i // SHARD + 1:02d}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for h, n, t in rows[i:i + SHARD]:
            f.write(json.dumps({"h": h, "t": " ".join(t.split())[:TRUNC]},
                               ensure_ascii=False) + "\n")
    print(f"  {path}: {min(SHARD, len(rows) - i)} teksten")
