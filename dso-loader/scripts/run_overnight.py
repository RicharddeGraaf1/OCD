"""Overnight-orchestrator: werkingsgebied-roadmap Fase 3+4+5 over het VOLLE corpus.

Resumable (per-chunk idempotent: al-geembedde tekst_elementen worden overgeslagen),
fout-tolerant per fase (kern eerst, randfases laatst), en schrijft een
MORNING-REPORT.md + logfile. **Raakt git niet** — het toont alleen de stand van
de werkboom; committen doe je zelf, op `main`. (Tot 2026-08-01 deed dit script
`git checkout -b feat/vector-chunk-lagen` + commit, wat van branch wisselde
onder ongecommit werk door en die branch maanden naast main hield.)
Zie vault: analysis/Per-chunk werkingsgebied- en objectkoppeling...

Draai:  python scripts/run_overnight.py   (bedoeld voor run_in_background)
"""
import os, re, sys, time, subprocess, traceback
import httpx, psycopg
from psycopg.rows import dict_row

DB = os.environ.get("OCD_DB_URL", "postgresql://postgres:postgres@localhost:5434/dso")
OLLAMA = "http://localhost:11434"; MODEL = "nomic-embed-text"
REPO = r"C:\GIT\OCD"
SCRIPTS = r"C:\GIT\OCD\dso-loader\scripts"
LOG = os.path.join(SCRIPTS, "overnight.log")
REPORT = os.path.join(SCRIPTS, "MORNING-REPORT.md")
BATCH = 64
t0 = time.monotonic()
report_lines = []


def log(msg):
    line = f"[{(time.monotonic()-t0)/60:6.1f}m] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def rep(msg):
    report_lines.append(msg)

def conn():
    return psycopg.connect(DB, row_factory=dict_row)

def run_sql_file(path):
    raw = open(path, encoding="utf-8").read()
    clean = "\n".join(re.sub(r"--.*$", "", ln) for ln in raw.splitlines())
    stmts = [s.strip() for s in clean.split(";") if s.strip()]
    with psycopg.connect(DB, autocommit=True) as c:
        for s in stmts:
            with c.cursor() as cur:
                cur.execute(s)

def embed_batch(texts, tries=5):
    for i in range(tries):
        try:
            r = httpx.post(f"{OLLAMA}/api/embed", json={"model": MODEL, "input": texts}, timeout=120)
            if r.status_code == 404:  # model weg -> re-pull (nomic verdween al eens)
                log("  nomic 404 -> re-pull"); httpx.post(f"{OLLAMA}/api/pull", json={"model": MODEL}, timeout=600)
                continue
            r.raise_for_status()
            return r.json()["embeddings"]
        except Exception as e:
            log(f"  embed retry {i+1}/{tries}: {e}"); time.sleep(5 * (i + 1))
    raise RuntimeError("embed faalde na retries")

def vlit(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

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


def fase3():
    log("FASE 3 — canonieke v2a.chunk-laag")
    run_sql_file(os.path.join(SCRIPTS, "2026-07-add-chunk-canoniek.sql"))
    with conn() as c, c.cursor() as cur:
        cur.execute("select count(*) n from information_schema.columns where table_schema='v2a' and table_name='tekst_embedding' and column_name in ('wid','source_type','source_id','source_ref')")
        log(f"  canonieke kolommen: {cur.fetchone()['n']}/4; view v2a.chunk aangemaakt")
    rep("## Fase 3 — canonieke v2a.chunk-laag\nKolommen wid/source_type/source_id/source_ref + backfill + view `v2a.chunk`. Dicht G-88.\n")


def fase45_embed():
    log("FASE 4a+5 — embed alle regelingen (Lid/Divisietekst/Begrip/Artikel-zonder-Lid)")
    with conn() as c, c.cursor() as cur:
        cur.execute("select frbr_expression, opschrift from p2p.regeling where not inactief order by frbr_expression")
        regs = cur.fetchall()
    start_total = _count_chunks()
    done_reg, added = 0, 0
    for i, reg in enumerate(regs, 1):
        try:
            with conn() as c, c.cursor() as cur:
                cur.execute(FETCH, {"e": reg["frbr_expression"]})
                rows = cur.fetchall()
                if not rows:
                    continue
                texts = [(r["kop_pad"] + " · " + r["inhoud_plain"]) if r["kop_pad"] else r["inhoud_plain"] for r in rows]
                embeds = []
                for j in range(0, len(texts), BATCH):
                    embeds.extend(embed_batch(texts[j:j + BATCH]))
                with cur.connection.transaction():
                    for r, v in zip(rows, embeds):
                        cur.execute("""INSERT INTO v2a.tekst_embedding
                            (tekst_element_id, regeling_expression, bron_soort, kop_pad, inhoud_plain,
                             embedding, wid, source_type, source_id)
                            VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,%s)""",
                            (r["id"], reg["frbr_expression"], r["element_type"], r["kop_pad"],
                             r["inhoud_plain"], vlit(v), r["wid"], r["element_type"], r["id"]))
                added += len(rows); done_reg += 1
            if i % 50 == 0 or done_reg % 25 == 0:
                log(f"  [{i}/{len(regs)}] +{added} chunks (regeling {reg['opschrift'][:40] if reg['opschrift'] else ''})")
        except Exception as e:
            log(f"  FOUT regeling {reg['frbr_expression']}: {e}")
    total = _count_chunks()
    log(f"  embed klaar: {total} chunks (+{total-start_total} deze run, {done_reg} regelingen geraakt)")
    rep(f"## Fase 4a+5 — embed volledig corpus\nVan {start_total} naar **{total} chunks** (+{total-start_total}). {done_reg} regelingen aangevuld. Idempotent/resumable per tekst_element.\n")
    return total


def _count_chunks():
    with conn() as c, c.cursor() as cur:
        cur.execute("select count(*) n from v2a.tekst_embedding"); return cur.fetchone()["n"]


def rebuild_annotatie():
    log("REBUILD — v2a.chunk_annotatie over het volle corpus")
    run_sql_file(os.path.join(SCRIPTS, "2026-07-add-chunk-annotatie.sql"))
    with conn() as c, c.cursor() as cur:
        cur.execute("select count(*) n, count(distinct chunk_id) ch from v2a.chunk_annotatie")
        r = cur.fetchone()
        cur.execute("select count(distinct chunk_id) n from v2a.chunk_annotatie where locatie_id is not null")
        wg = cur.fetchone()["n"]
        cur.execute("select count(*) n from v2a.tekst_embedding")
        tot = cur.fetchone()["n"]
    log(f"  chunk_annotatie: {r['n']} rijen, {r['ch']} chunks, {wg} met werkingsgebied ({100*wg/tot:.0f}%)")
    rep(f"## chunk_annotatie herbouwd\n{r['n']} rijen over {r['ch']} chunks; {wg}/{tot} = {100*wg/tot:.0f}% met werkingsgebied.\n")


def extend_categorie():
    """Wijs ALLE Lid/Divisietekst-chunks toe aan de bestaande v2a.categorie-centroiden
    (nearest-centroide, geen her-clustering). Houdt de v1-taxonomie; breidt de dekking uit."""
    log("EXTEND — chunk_categorie over het volle corpus (nearest bevestigde centroide)")
    import numpy as np
    with conn() as c, c.cursor() as cur:
        cur.execute("select categorie_id, centroide::text from v2a.categorie where status='bevestigd'")
        cats = cur.fetchall()
        if not cats:
            log("  geen bevestigde categorieen -> skip"); return
        Cid = [r["categorie_id"] for r in cats]
        C = np.vstack([np.fromstring(r["centroide"].strip("[]"), sep=",", dtype=np.float32) for r in cats])
        C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
        cur.execute("truncate v2a.chunk_categorie")
    ins = 0
    # TWEE aparte connecties: een commit op de write-conn mag de server-side
    # named cursor op de read-conn niet ongeldig maken (dat was de bug).
    read = psycopg.connect(DB); write = psycopg.connect(DB)
    rc = read.cursor(name="catread"); rc.itersize = 10000
    rc.execute("select id, embedding::text from v2a.tekst_embedding where bron_soort in ('Lid','Divisietekst')")
    wc = write.cursor(); buf = []
    for rid, emb in rc:
        v = np.fromstring(emb.strip("[]"), sep=",", dtype=np.float32); v /= np.linalg.norm(v) + 1e-9
        sims = C @ v; j = int(sims.argmax()); s = float(sims[j])
        if s >= 0.50:
            buf.append((rid, Cid[j], float(1 - s), "v1-2026-07-07"))
        if len(buf) >= 10000:
            wc.executemany("insert into v2a.chunk_categorie(chunk_id,categorie_id,afstand,taxonomie_versie) values(%s,%s,%s,%s)", buf)
            write.commit(); ins += len(buf); buf = []
    if buf:
        wc.executemany("insert into v2a.chunk_categorie(chunk_id,categorie_id,afstand,taxonomie_versie) values(%s,%s,%s,%s)", buf)
        write.commit(); ins += len(buf)
    read.close(); write.close()
    log(f"  chunk_categorie: {ins} toewijzingen over het volle corpus")
    rep(f"## chunk_categorie uitgebreid\n{ins} toewijzingen (nearest bevestigde centroide) over alle Lid/Divisietekst-chunks. Taxonomie ongewijzigd (v1).\n")


def fase4b_objectnamen():
    log("FASE 4b — object-namen als chunks (activiteit/gebiedsaanwijzing/norm)")
    sources = {
        "objectnaam-activiteit": """select distinct a.identificatie id, a.naam, jr.regeling_expression expr
            from p2p.activiteit a join p2p.activiteit_locatieaanduiding ala on ala.activiteit_id=a.identificatie
            join p2p.juridische_regel jr on jr.identificatie=ala.juridische_regel_id
            where a.naam is not null and length(a.naam)>3 and jr.regeling_expression is not null""",
        "objectnaam-gebiedsaanwijzing": """select distinct g.identificatie id, g.naam, jr.regeling_expression expr
            from p2p.gebiedsaanwijzing g join p2p.juridische_regel_gebiedsaanwijzing jg on jg.gebiedsaanwijzing_id=g.identificatie
            join p2p.juridische_regel jr on jr.identificatie=jg.juridische_regel_id
            where g.naam is not null and length(g.naam)>3 and jr.regeling_expression is not null""",
        "objectnaam-norm": """select distinct n.identificatie id, n.naam, jr.regeling_expression expr
            from p2p.norm n join p2p.juridische_regel_norm jn on jn.norm_id=n.identificatie
            join p2p.juridische_regel jr on jr.identificatie=jn.juridische_regel_id
            where n.naam is not null and length(n.naam)>3 and jr.regeling_expression is not null""",
    }
    total = 0
    for st, sql in sources.items():
        try:
            with conn() as c, c.cursor() as cur:
                cur.execute(sql + f""" and not exists (select 1 from v2a.tekst_embedding v
                    where v.source_ref=id and v.regeling_expression=jr.regeling_expression and v.source_type='{st}')""" if False else sql)
                rows = cur.fetchall()
            # idempotent-filter in Python (vermijdt alias-issues)
            with conn() as c, c.cursor() as cur:
                cur.execute("select source_ref||'|'||regeling_expression k from v2a.tekst_embedding where source_type=%s", (st,))
                have = {r["k"] for r in cur.fetchall()}
            rows = [r for r in rows if f"{r['id']}|{r['expr']}" not in have]
            log(f"  {st}: {len(rows)} nieuwe (object,regeling)-paren")
            for j in range(0, len(rows), BATCH):
                chunk = rows[j:j + BATCH]
                embeds = embed_batch([r["naam"] for r in chunk])
                with conn() as c, c.cursor() as cur, cur.connection.transaction():
                    for r, v in zip(chunk, embeds):
                        cur.execute("""INSERT INTO v2a.tekst_embedding
                            (regeling_expression, bron_soort, kop_pad, inhoud_plain, embedding, source_type, source_ref)
                            VALUES (%s,'objectnaam',%s,%s,%s::vector,%s,%s)""",
                            (r["expr"], st.split("-")[1], r["naam"], vlit(v), st, r["id"]))
                total += len(chunk)
        except Exception as e:
            log(f"  FOUT {st}: {e}\n{traceback.format_exc()}")
    log(f"  object-namen klaar: +{total} chunks")
    rep(f"## Fase 4b — object-namen\n+{total} objectnaam-chunks (activiteit/gebiedsaanwijzing/norm), gescoped per (object, regeling) via annotatie. Voor de activiteit-as-retrieval; geen werkingsgebied-links (tekst_element_id NULL).\n")


def coverage_report():
    with conn() as c, c.cursor() as cur:
        cur.execute("select count(*) n, count(distinct regeling_expression) r from v2a.tekst_embedding")
        base = cur.fetchone()
        cur.execute("select bron_soort, count(*) n from v2a.tekst_embedding group by 1 order by 2 desc")
        soort = cur.fetchall()
    rep("## Corpus-eindstand")
    rep(f"- **{base['n']} chunks** over **{base['r']} regelingen**")
    rep("- per bron_soort: " + ", ".join(f"{r['bron_soort']}={r['n']}" for r in soort))


def toon_werkboom_status():
    """Meld welke bestanden gewijzigd zijn — commit NIET zelf.

    Dit script deed hier eerder `git checkout -b feat/vector-chunk-lagen` +
    een commit van zes vaste bestanden. Dat is er bewust uitgehaald
    (2026-08-01): een datapijplijn hoort niet aan release-beheer te doen. Het
    wisselde bovendien van branch onder ongecommit werk door, en het voedde
    telkens opnieuw een branch die daardoor maanden naast `main` bleef leven.

    Committen doe je zelf, op `main`.
    """
    try:
        r = subprocess.run(["git", "-C", REPO, "status", "--short"],
                           capture_output=True, text=True)
        gewijzigd = [ln for ln in r.stdout.splitlines() if ln.strip()]
        log(f"GIT — werkboom heeft {len(gewijzigd)} gewijzigde/nieuwe bestanden (niet gecommit)")
        rep("\n## Git\nDit script commit niets meer — het toont alleen de stand. "
            f"Werkboom: {len(gewijzigd)} gewijzigde/nieuwe bestanden. "
            "Committen doe je zelf, op `main`.\n")
    except Exception as e:
        log(f"  git-status FOUT: {e}")


def main():
    open(LOG, "w").close()
    log("=== OVERNIGHT-RUN gestart — Fase 3+4+5 ===")
    rep(f"# Overnight-run vector-chunk-lagen\nGestart. Fase 3+4+5 over het volle corpus.\n")
    for naam, fn in [("Fase3", fase3), ("Embed4a5", fase45_embed), ("Annotatie", rebuild_annotatie),
                     ("Categorie", extend_categorie), ("Objectnamen", fase4b_objectnamen)]:
        try:
            fn()
        except Exception as e:
            log(f"FASE {naam} FAALDE: {e}\n{traceback.format_exc()}")
            rep(f"## ⚠️ {naam} faalde\n```\n{e}\n```\n")
    try:
        coverage_report()
    except Exception as e:
        log(f"coverage faalde: {e}")
    toon_werkboom_status()
    total_min = (time.monotonic() - t0) / 60
    log(f"=== KLAAR in {total_min:.0f} min ===")
    rep(f"\n---\n_Totale looptijd: {total_min:.0f} min. Zie overnight.log voor details._")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))


if __name__ == "__main__":
    main()
