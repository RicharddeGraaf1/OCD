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

Gebruik:  python begrijpelijk-fetch-shards.py --top 5000 --prefix topshard
          --top 0 betekent: geen kop-limiet (alles wat nog ontbreekt).
          --types Artikel               alleen Artikel-elementen (default Lid,Divisietekst,Artikel)
          --scope instructieregel       alleen teksten aan een juridische_regel
                                        met regel_type='Instructieregel' (product-restgolfje)
"""
import argparse
import json

import psycopg

DB_URL = "postgresql://postgres:postgres@localhost:5434/dso"
TRUNC = 3000

ap = argparse.ArgumentParser()
ap.add_argument("--top", type=int, default=5000, help="kop-limiet op frequentie; 0 = alles")
ap.add_argument("--model", default="claude-sonnet-5")
ap.add_argument("--shard", type=int, default=150)
ap.add_argument("--prefix", default="topshard")
ap.add_argument("--types", default="Lid,Divisietekst,Artikel",
                help="comma-gescheiden element_types")
ap.add_argument("--scope", choices=["all", "instructieregel"], default="all")
args = ap.parse_args()
TOPN, MODEL, SHARD, PREFIX = args.top, args.model, args.shard, args.prefix
TYPES = [t.strip() for t in args.types.split(",") if t.strip()]

# Scope-filter: bij 'instructieregel' alleen elementen waarvan de wid aan een
# Instructieregel hangt (provinciale verordeningen — staart, geen bruidsschat-
# dedup, dus die haal je nooit via de kop; dit is het product-restgolfje).
SCOPE_FILTER = {
    "all": "",
    "instructieregel": """
      AND EXISTS (
        SELECT 1 FROM p2p.juridische_regel jr
        WHERE jr.regeltekst_wid = te.wid
          AND jr.regeling_expression = te.regeling_expression
          AND jr.regel_type = 'Instructieregel')
    """,
}[args.scope]

SQL = f"""
WITH u AS (
  SELECT v2a.norm_hash(te.inhoud_plain) h, count(*) n, min(te.inhoud_plain) t
  FROM p2p.tekst_element te
  JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
  WHERE te.element_type = ANY(%(types)s) AND te.inhoud_plain IS NOT NULL
    AND length(te.inhoud_plain) > 30 AND NOT r.inactief
    {SCOPE_FILTER}
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
    cur.execute(SQL, {"topn": TOPN or None, "model": MODEL, "types": TYPES})
    rows = cur.fetchall()

print(f"top-{TOPN or 'alles'} ({args.scope}, {'+'.join(TYPES)}) nog te doen voor {MODEL}: "
      f"{len(rows)} (dekking-gewicht van deze rest: {sum(r[1] for r in rows)} elementen)")
for i in range(0, len(rows), SHARD):
    path = f"C:/tmp/{PREFIX}_{i // SHARD + 1:02d}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for h, n, t in rows[i:i + SHARD]:
            f.write(json.dumps({"h": h, "t": " ".join(t.split())[:TRUNC]},
                               ensure_ascii=False) + "\n")
    print(f"  {path}: {min(SHARD, len(rows) - i)} teksten")
