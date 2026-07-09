"""Overnight-orchestrator Fase 6b — p2pwijziging (ontwerp/delta-laag) in de vectorindex.

Embed p2pwijziging.tekst_element (ONTWERP-tekst, niet-vigerend) als een APARTE
source_type='ontwerp'. Scope-sleutel regeling_expression = besluit.regeling_work
(de gewijzigde regeling -> zelfde regelingsgebied-geometrie). Zelfde chunk-granulariteit
als p2p (Lid/Divisietekst/Begrip + Artikel-zonder-Lid), kop_pad uit parent_id-boom.

NB: dit is delta/ontwerp-content (bevat_renvooi/wijzigactie/vervallen) — bedoeld voor
een "wat gaat hier veranderen"-vindlaag, NOOIT mengen met "wat geldt nu". Daarom een
eigen source_type en een eigen (toekomstige) include_ontwerp-flag in de query.

Resumable (per-chunk NOT EXISTS op source_ref), commit op feat/vector-chunk-lagen (geen push).
"""
import os, re, time, subprocess, traceback
import httpx, psycopg
from psycopg.rows import dict_row

DB = "postgresql://postgres:postgres@localhost:5434/dso"
OLLAMA = "http://localhost:11434"; MODEL = "nomic-embed-text"
REPO = r"C:\GIT\OCD"; SCRIPTS = r"C:\GIT\OCD\dso-loader\scripts"
LOG = os.path.join(SCRIPTS, "overnight_ontwerp.log")
REPORT = os.path.join(SCRIPTS, "MORNING-REPORT-ONTWERP.md")
BRANCH = "feat/vector-chunk-lagen"; BATCH = 64
t0 = time.monotonic(); report = []


def log(m):
    line = f"[{(time.monotonic()-t0)/60:6.1f}m] {m}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def conn():
    return psycopg.connect(DB, row_factory=dict_row)

def embed_batch(texts, tries=5):
    for i in range(tries):
        try:
            r = httpx.post(f"{OLLAMA}/api/embed", json={"model": MODEL, "input": texts}, timeout=120)
            if r.status_code == 404:
                log("  nomic 404 -> re-pull"); httpx.post(f"{OLLAMA}/api/pull", json={"model": MODEL}, timeout=600); continue
            r.raise_for_status(); return r.json()["embeddings"]
        except Exception as e:
            log(f"  embed retry {i+1}: {e}"); time.sleep(5 * (i + 1))
    raise RuntimeError("embed faalde")

def vlit(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

FETCH = """
WITH RECURSIVE kop AS (
    SELECT id, parent_id, 1 AS d, ARRAY[opschrift]::text[] AS path
    FROM p2pwijziging.tekst_element
    WHERE ontwerpbesluit_id = %(o)s AND inhoud_plain IS NOT NULL AND length(inhoud_plain) > 30
    UNION ALL
    SELECT k.id, p.parent_id, k.d+1,
           CASE WHEN p.opschrift IS NOT NULL AND p.opschrift <> '' THEN p.opschrift || k.path ELSE k.path END
    FROM kop k JOIN p2pwijziging.tekst_element p ON p.id = k.parent_id WHERE k.d < 20
),
best AS (SELECT DISTINCT ON (id) id, path FROM kop ORDER BY id, d DESC)
SELECT t.id, t.element_type, t.wid, t.inhoud_plain,
       array_to_string(array_remove(b.path, NULL), ' > ') AS kop_pad
FROM p2pwijziging.tekst_element t JOIN best b ON b.id = t.id
WHERE t.ontwerpbesluit_id = %(o)s AND t.inhoud_plain IS NOT NULL AND length(t.inhoud_plain) > 30
  AND (t.element_type IN ('Lid','Divisietekst','Begrip')
       OR (t.element_type = 'Artikel'
           AND NOT EXISTS (SELECT 1 FROM p2pwijziging.tekst_element kid
                           WHERE kid.parent_id = t.id AND kid.element_type = 'Lid')))
  AND NOT EXISTS (SELECT 1 FROM v2a.tekst_embedding v
                  WHERE v.source_ref = t.id::text AND v.source_type = 'ontwerp')
ORDER BY t.volgorde
"""


def fase6b_embed():
    log("FASE 6b — embed p2pwijziging (ontwerp-tekst)")
    with conn() as c, c.cursor() as cur:
        cur.execute("""select b.ontwerpbesluit_id, b.regeling_work
          from p2pwijziging.besluit b
          where exists (select 1 from p2pwijziging.tekst_element t
                        where t.ontwerpbesluit_id=b.ontwerpbesluit_id
                          and t.inhoud_plain is not null and length(t.inhoud_plain)>30)
          order by b.ontwerpbesluit_id""")
        besluiten = cur.fetchall()
    log(f"  {len(besluiten)} ontwerpbesluiten met tekst")
    start = _count(); added, done = 0, 0
    for i, b in enumerate(besluiten, 1):
        try:
            with conn() as c, c.cursor() as cur:
                cur.execute(FETCH, {"o": b["ontwerpbesluit_id"]}); rows = cur.fetchall()
                if not rows:
                    continue
                texts = [(r["kop_pad"] + " · " + r["inhoud_plain"]) if r["kop_pad"] else r["inhoud_plain"] for r in rows]
                embeds = []
                for j in range(0, len(texts), BATCH):
                    embeds.extend(embed_batch(texts[j:j + BATCH]))
                with cur.connection.transaction():
                    for r, v in zip(rows, embeds):
                        cur.execute("""INSERT INTO v2a.tekst_embedding
                            (regeling_expression, bron_soort, kop_pad, inhoud_plain, embedding,
                             source_type, source_ref, wid)
                            VALUES (%s,%s,%s,%s,%s::vector,'ontwerp',%s,%s)""",
                            (b["regeling_work"], r["element_type"], r["kop_pad"], r["inhoud_plain"],
                             vlit(v), str(r["id"]), r["wid"]))
                added += len(rows); done += 1
            if i % 100 == 0:
                log(f"  [{i}/{len(besluiten)}] +{added} chunks")
        except Exception as e:
            log(f"  FOUT ontwerpbesluit {b['ontwerpbesluit_id']}: {e}")
    tot = _count()
    log(f"  ontwerp-embed klaar: +{tot-start} chunks ({done} besluiten)")
    report.append(f"## Fase 6b — p2pwijziging (ontwerp) embed\n+{tot-start} ontwerp-chunks van {done} ontwerpbesluiten. "
                  f"source_type='ontwerp', scope=regeling_work -> gewijzigde regeling. NIET-vigerend; aparte laag.\n")


def _count():
    with conn() as c, c.cursor() as cur:
        cur.execute("select count(*) n from v2a.tekst_embedding where source_type='ontwerp'")
        return cur.fetchone()["n"]


def report_out():
    with conn() as c, c.cursor() as cur:
        cur.execute("select count(*) n, count(distinct regeling_expression) r from v2a.tekst_embedding where source_type='ontwerp'")
        o = cur.fetchone()
        cur.execute("select pg_size_pretty(pg_total_relation_size('v2a.tekst_embedding')) s, pg_size_pretty(pg_database_size('dso')) db")
        sz = cur.fetchone()
    report.append(f"## Eindstand\n- ontwerp-chunks: **{o['n']}** over **{o['r']}** gewijzigde regelingen\n- v2a.tekst_embedding: {sz['s']} · DB totaal: {sz['db']}")


def git_commit():
    files = ["dso-loader/scripts/run_overnight_ontwerp.py"]
    def g(*a):
        return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True)
    try:
        g("checkout", BRANCH)
        for f in files:
            g("add", f)
        r = g("commit", "-m", "Fase 6b: p2pwijziging (ontwerp) in vectorindex, source_type='ontwerp'\n\n"
              "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>")
        log(f"  git: {r.stdout.strip()[:160]}")
    except Exception as e:
        log(f"  git FOUT: {e}")


def main():
    open(LOG, "w").close()
    log("=== ONTWERP overnight (Fase 6b) gestart ===")
    report.append("# Overnight-run Fase 6b — p2pwijziging (ontwerp) in de vectorindex\n")
    try:
        fase6b_embed()
    except Exception as e:
        log(f"EMBED FAALDE: {e}\n{traceback.format_exc()}")
        report.append(f"## ⚠️ Embed faalde\n```\n{e}\n```\n")
    try:
        report_out()
    except Exception as e:
        log(f"report faalde: {e}")
    git_commit()
    log(f"=== KLAAR in {(time.monotonic()-t0)/60:.0f} min ===")
    report.append(f"\n---\n_Looptijd: {(time.monotonic()-t0)/60:.0f} min._")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(report))


if __name__ == "__main__":
    main()
