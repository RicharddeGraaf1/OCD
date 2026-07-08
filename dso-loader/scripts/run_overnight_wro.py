"""Overnight-orchestrator Fase 6a — wro (oude bestemmingsplannen) in de vectorindex.

Embed alle wro.wro_tekst_object (planregels) met kop_pad uit de parent_id-boom;
scope-sleutel regeling_expression=instrument_idn (-> plangebied-geometrie). Fijne
bestemmingsvlak-koppeling (nummer=artikelnummer) best-effort in v2a.chunk_wro_object.
Resumable (per-chunk NOT EXISTS), commit op branch feat/vector-chunk-lagen (geen push).
"""
import os, re, time, subprocess, traceback
import httpx, psycopg
from psycopg.rows import dict_row

DB = "postgresql://postgres:postgres@localhost:5434/dso"
OLLAMA = "http://localhost:11434"; MODEL = "nomic-embed-text"
REPO = r"C:\GIT\OCD"; SCRIPTS = r"C:\GIT\OCD\dso-loader\scripts"
LOG = os.path.join(SCRIPTS, "overnight_wro.log")
REPORT = os.path.join(SCRIPTS, "MORNING-REPORT-WRO.md")
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
    SELECT identificatie AS id, parent_id, 1 AS d,
           ARRAY[COALESCE(naam,label,nummer)]::text[] AS path
    FROM wro.wro_tekst_object
    WHERE instrument_idn = %(i)s AND inhoud IS NOT NULL AND length(inhoud) > 30
    UNION ALL
    SELECT k.id, p.parent_id, k.d+1,
           CASE WHEN COALESCE(p.naam,p.label,p.nummer) IS NOT NULL AND COALESCE(p.naam,p.label,p.nummer) <> ''
                THEN COALESCE(p.naam,p.label,p.nummer) || k.path ELSE k.path END
    FROM kop k JOIN wro.wro_tekst_object p ON p.identificatie = k.parent_id WHERE k.d < 20
),
best AS (SELECT DISTINCT ON (id) id, path FROM kop ORDER BY id, d DESC)
SELECT t.identificatie, t.object_type, t.nummer, t.inhoud,
       array_to_string(array_remove(b.path, NULL), ' > ') AS kop_pad
FROM wro.wro_tekst_object t JOIN best b ON b.id = t.identificatie
WHERE t.instrument_idn = %(i)s AND t.inhoud IS NOT NULL AND length(t.inhoud) > 30
  AND NOT EXISTS (SELECT 1 FROM v2a.tekst_embedding v
                  WHERE v.source_ref = t.identificatie AND v.source_type = 'wro')
ORDER BY t.volgnummer
"""


def fase6a_embed():
    log("FASE 6a — embed wro_tekst_object (planregels)")
    with conn() as c, c.cursor() as cur:
        cur.execute("""select r.idn from wro.ruimtelijk_instrument r where r.geometrie is not null
          and exists (select 1 from wro.wro_tekst_object t where t.instrument_idn=r.idn
                      and t.inhoud is not null and length(t.inhoud)>30) order by r.idn""")
        instr = [row["idn"] for row in cur.fetchall()]
    log(f"  {len(instr)} instrumenten met tekst + geometrie")
    start = _count()
    added, done = 0, 0
    for i, idn in enumerate(instr, 1):
        try:
            with conn() as c, c.cursor() as cur:
                cur.execute(FETCH, {"i": idn}); rows = cur.fetchall()
                if not rows:
                    continue
                texts = [(r["kop_pad"] + " · " + r["inhoud"]) if r["kop_pad"] else r["inhoud"] for r in rows]
                embeds = []
                for j in range(0, len(texts), BATCH):
                    embeds.extend(embed_batch(texts[j:j + BATCH]))
                with cur.connection.transaction():
                    for r, v in zip(rows, embeds):
                        cur.execute("""INSERT INTO v2a.tekst_embedding
                            (regeling_expression, bron_soort, kop_pad, inhoud_plain, embedding, source_type, source_ref)
                            VALUES (%s,%s,%s,%s,%s::vector,'wro',%s)""",
                            (idn, r["object_type"], r["kop_pad"], r["inhoud"], vlit(v), r["identificatie"]))
                added += len(rows); done += 1
            if i % 500 == 0:
                log(f"  [{i}/{len(instr)}] +{added} chunks")
        except Exception as e:
            log(f"  FOUT instrument {idn}: {e}")
    tot = _count()
    log(f"  wro-embed klaar: +{tot-start} chunks ({done} instrumenten)")
    report.append(f"## Fase 6a — wro embed\n+{tot-start} wro-chunks (van {done} plannen). source_type='wro', scope=instrument_idn -> plangebied-geometrie.\n")


def _count():
    with conn() as c, c.cursor() as cur:
        cur.execute("select count(*) n from v2a.tekst_embedding where source_type='wro'")
        return cur.fetchone()["n"]


def fijne_link():
    log("FIJNE LINK — chunk_wro_object via nummer=artikelnummer (best-effort)")
    from_sql = open(os.path.join(SCRIPTS, "2026-07-add-chunk-wro.sql"), encoding="utf-8").read()
    with psycopg.connect(DB, autocommit=True) as c:
        for s in [x.strip() for x in re.sub(r"--.*$", "", from_sql, flags=re.M).split(";") if x.strip()]:
            with c.cursor() as cur:
                cur.execute(s)
    with psycopg.connect(DB, autocommit=True) as c, c.cursor() as cur:
        cur.execute("truncate v2a.chunk_wro_object")
        cur.execute("""INSERT INTO v2a.chunk_wro_object (chunk_id, planobject_id, instrument_idn)
            SELECT DISTINCT v.id, p.identificatie, p.instrument_idn
            FROM v2a.tekst_embedding v
            JOIN wro.wro_tekst_object t ON t.identificatie = v.source_ref AND v.source_type = 'wro'
            JOIN wro.planobject p ON p.instrument_idn = t.instrument_idn
                 AND p.artikelnummer = t.nummer AND p.geometrie IS NOT NULL
            WHERE t.nummer IS NOT NULL AND t.nummer <> ''""")
        cur.execute("select count(*) n, count(distinct chunk_id) c from v2a.chunk_wro_object")
        r = cur.fetchone()
    log(f"  chunk_wro_object: {r['n']} rijen, {r['c']} chunks met bestemmingsvlak-geometrie")
    report.append(f"## Fijne link (bestemmingsvlak)\n{r['n']} rijen, {r['c']} chunks. Rest leunt op de plan-geometrie (coarse, 100%).\n")


def report_out():
    with conn() as c, c.cursor() as cur:
        cur.execute("select count(*) n, count(distinct regeling_expression) r from v2a.tekst_embedding where source_type='wro'")
        w = cur.fetchone()
        cur.execute("select pg_size_pretty(pg_total_relation_size('v2a.tekst_embedding')) s, pg_size_pretty(pg_database_size('dso')) db")
        sz = cur.fetchone()
    report.append(f"## Eindstand\n- wro-chunks: **{w['n']}** over **{w['r']}** plannen\n- v2a.tekst_embedding: {sz['s']} · DB totaal: {sz['db']}")


def git_commit():
    files = ["dso-loader/scripts/2026-07-add-chunk-wro.sql", "dso-loader/scripts/run_overnight_wro.py",
             "dso-loader/scripts/run_overnight.py"]
    def g(*a):
        return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True)
    try:
        g("checkout", BRANCH)
        for f in files:
            g("add", f)
        r = g("commit", "-m", "Fase 6a: wro-chunks in vectorindex + chunk_wro_object fijne link\n\n"
              "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>")
        log(f"  git: {r.stdout.strip()[:160]}")
        report.append(f"\n## Git\nCommit op `{BRANCH}` (niet gepusht).")
    except Exception as e:
        log(f"  git FOUT: {e}")


def main():
    open(LOG, "w").close()
    log("=== WRO overnight (Fase 6a) gestart ===")
    report.append("# Overnight-run Fase 6a — wro in de vectorindex\n")
    for naam, fn in [("Embed", fase6a_embed), ("FijneLink", fijne_link)]:
        try:
            fn()
        except Exception as e:
            log(f"FASE {naam} FAALDE: {e}\n{traceback.format_exc()}")
            report.append(f"## ⚠️ {naam} faalde\n```\n{e}\n```\n")
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
