"""Bouwt v2a.categorie (gecureerde generieke lijst) + v2a.chunk_categorie.

Ruggengraat = 28 IMOW-thema's (empirische centroide waar thema-geannoteerd, anders
label-embedding). Discovery = gededupliceerde HDBSCAN die alleen kandidaten voorstelt
die de ruggengraat mist (reconcile op cosine). Zie vault-analyse Categorie-destillatie.
"""
import json, os, re, time, hashlib, numpy as np, httpx, psycopg
from sklearn.decomposition import TruncatedSVD
from sklearn.cluster import HDBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer

DB = "postgresql://postgres:postgres@localhost:5434/dso"
OLLAMA = "http://localhost:11434"; MODEL = "nomic-embed-text"
MIN_EMPIRICAL = 15; RECONCILE_SIM = 0.60; COVER_SIM = 0.50; VERSIE = "v1-2026-07-07"
DDL_PATH = r"C:\GIT\OCD\dso-loader\scripts\2026-07-add-categorie.sql"
STOPWORDS = ("de het een en van te dat die in op voor met als aan bij door of om naar dan "
    "ook maar niet zijn wordt worden is was deze dit uit tot per over onder tussen wel geen "
    "artikel lid onderdeel bedoeld eerste tweede derde genoemd volgens indien ten aanzien "
    "mag mogen moet moeten kan kunnen").split()
t0 = time.monotonic()


def norm(s):
    return re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', s or '').lower().strip()

def embed(texts):
    return httpx.post(f"{OLLAMA}/api/embed", json={"model": MODEL, "input": texts}, timeout=90).json()["embeddings"]

def vlit(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

def unit(m):
    return m / (np.linalg.norm(m) + 1e-9)


c = psycopg.connect(DB, autocommit=True); cur = c.cursor()
cur.execute(open(DDL_PATH, encoding="utf-8").read())
print(f"[{time.monotonic()-t0:.0f}s] tabellen aangemaakt")

# 1. chunk-embeddings
ids, txt, vecs = [], [], []
with psycopg.connect(DB) as c2, c2.cursor(name="e") as cc:
    cc.itersize = 2000
    cc.execute("""select v.id, v.inhoud_plain, v.embedding::text from v2a.tekst_embedding v
      where v.bron_soort in ('Lid','Divisietekst') and v.kop_pad not ilike '%%toelicht%%'
        and v.inhoud_plain not ilike '%%/join/id/regdata/%%'""")
    for rid, tx, emb in cc:
        ids.append(rid); txt.append(tx or "")
        vecs.append(np.fromstring(emb.strip("[]"), sep=",", dtype=np.float32))
X = np.vstack(vecs); del vecs
X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
idpos = {rid: i for i, rid in enumerate(ids)}
print(f"[{time.monotonic()-t0:.0f}s] embeddings {X.shape}")

# 2. thema-map: chunk_id -> set(genormaliseerde thema)
themap = {}
cur.execute("""select v.id, jr.thema from v2a.tekst_embedding v
  join p2p.tekst_element te on te.id=v.tekst_element_id
  join p2p.juridische_regel jr on jr.regeltekst_wid=te.wid and jr.regeling_expression=te.regeling_expression
  where jr.thema is not null and array_length(jr.thema,1)>0""")
for rid, th in cur.fetchall():
    if rid in idpos:
        themap.setdefault(rid, set()).update(norm(t) for t in th)

# 3. seed 28 IMOW-thema (empirisch waar mogelijk, anders label-embedding)
cur.execute("select label, term from core.imow_thema where not coalesce(deprecated,false)")
themas = cur.fetchall()
bb, cat_cent, emp, lab = [], [], 0, 0   # bb = [(cid, naam)] van de ruggengraat
low = []
for label, term in themas:
    nl = norm(label)
    members = [idpos[r] for r, s in themap.items() if nl in s and r in idpos]
    cid = nl.replace(" ", "-")
    if len(members) >= MIN_EMPIRICAL:
        cen = unit(X[members].mean(0))
        cur.execute("""insert into v2a.categorie(categorie_id,naam,centroide,n_chunks_seen,status,bron,taxonomie_versie)
          values(%s,%s,%s::vector,%s,'bevestigd','imow-thema',%s)""", (cid, label, vlit(cen), len(members), VERSIE))
        bb.append((cid, label)); cat_cent.append(cen); emp += 1
    else:
        low.append((cid, label, f"{label} {term or ''}".strip(), len(members)))
if low:
    for (cid, label, _, nm), e in zip(low, embed([x[2] for x in low])):
        e = unit(np.array(e, dtype=np.float32))
        cur.execute("""insert into v2a.categorie(categorie_id,naam,centroide,n_chunks_seen,status,bron,taxonomie_versie)
          values(%s,%s,%s::vector,%s,'bevestigd','imow-thema',%s)""", (cid, label, vlit(e), nm, VERSIE))
        bb.append((cid, label)); cat_cent.append(e); lab += 1
# 3b. gecureerde categorieen terugladen
#
# Waarom dit hier moet: de DDL hierboven DROPt v2a.categorie, dus elk menselijk
# oordeel over de taxonomie is bij een herbouw weg. Het DDL-commentaar noemt de
# tabel "first-class, compounding" en de categorie_id "stabiel, overleeft
# re-runs" — dat gold geen van beide zolang er geen bron buiten de tabel was.
# `curatie/categorie-curatie.json` is die bron; zie scripts/export_categorie_curatie.py.
#
# Ze gaan de ruggengraat IN (bb + cat_cent), niet ernaast: stap 6 wijst alleen
# toe aan wat in `C` staat. Een gecureerde categorie die daar niet in zit krijgt
# per definitie nul chunks — precies de reden dat `lucht`, `veiligheid`, `erfgoed`
# en `milieu` op nul stonden terwijl er wel degelijk clusters bij hoorden.
CURATIE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "curatie", "categorie-curatie.json")
n_cur = 0
if os.path.exists(CURATIE):
    thema_cid = {naam: cid for cid, naam in bb}
    for r in json.load(open(CURATIE, encoding="utf-8"))["categorieen"]:
        parent = thema_cid.get(r["thema"])
        cid = "curatie." + re.sub(r"[^a-z0-9]+", "-", r["naam"].lower()).strip("-")
        cen = unit(np.array(r["centroide"], dtype=np.float32))
        cur.execute("""insert into v2a.categorie(categorie_id,naam,parent_id,centroide,n_chunks_seen,status,bron,taxonomie_versie)
          values(%s,%s,%s,%s::vector,%s,'bevestigd','gebruiker',%s)""",
          (cid, r["naam"], parent, vlit(cen), r.get("n_chunks_seen", 0), VERSIE))
        bb.append((cid, r["naam"])); cat_cent.append(cen); n_cur += 1
    print(f"[{time.monotonic()-t0:.0f}s] curatie teruggeladen: {n_cur} categorieen")

C = np.vstack(cat_cent)
print(f"[{time.monotonic()-t0:.0f}s] ruggengraat: {emp} empirisch + {lab} label-embed = {len(bb)} thema's")

# 4. gededupliceerde discovery (evenwichts-hefboom: near-duplicaten tellen 1x)
seen, keep = set(), []
for i, t in enumerate(txt):
    h = hashlib.md5(t[:300].encode("utf-8", "ignore")).hexdigest()
    if h not in seen:
        seen.add(h); keep.append(i)
keep = np.array(keep); Xd = X[keep]
print(f"[{time.monotonic()-t0:.0f}s] dedup: {len(txt)} -> {len(keep)} unieke chunks")
Xr = TruncatedSVD(50, random_state=0).fit_transform(Xd); Xr /= np.linalg.norm(Xr, axis=1, keepdims=True) + 1e-9
dl = HDBSCAN(min_cluster_size=40, min_samples=8, metric="euclidean").fit_predict(Xr)
ncl = len(set(dl)) - (1 if -1 in dl else 0)
print(f"[{time.monotonic()-t0:.0f}s] HDBSCAN(dedup): {ncl} clusters, noise={100*(dl==-1).mean():.0f}%")

# 5. reconcile: elk cluster -> kandidaat-SUBcategorie onder zijn dichtstbijzijnde thema
clusters = sorted(set(dl) - {-1})
cl_cent = {k: unit(Xd[np.where(dl == k)[0]].mean(0)) for k in clusters}
docs = {k: " ".join(txt[keep[i]] for i in np.where(dl == k)[0]) for k in clusters}
vec = TfidfVectorizer(stop_words=STOPWORDS, ngram_range=(1, 2), max_features=6000,
                      token_pattern=r"[a-zA-ZÀ-ſ]{4,}", sublinear_tf=True)
M = vec.fit_transform([docs[k] for k in clusters]); terms = np.array(vec.get_feature_names_out())
csims = np.array([float((C @ cl_cent[k]).max()) for k in clusters])
print(f"[{time.monotonic()-t0:.0f}s] cluster->ruggengraat cosine — p10/p50/p90 = "
      f"{np.percentile(csims,10):.2f}/{np.percentile(csims,50):.2f}/{np.percentile(csims,90):.2f}")
cand_rows = []
for i, k in enumerate(clusters):
    cen = cl_cent[k]; simvec = C @ cen; j = int(simvec.argmax()); sim = float(simvec[j])
    parent_cid, parent_naam = bb[j]
    top = terms[M[i].toarray().ravel().argsort()[::-1][:3]]
    naam = " / ".join(top); cid = f"kandidaat.{k}"
    # ver van álles (< RECONCILE_SIM) -> top-level kandidaat; anders subcategorie
    parent = None if sim < RECONCILE_SIM else parent_cid
    cur.execute("""insert into v2a.categorie(categorie_id,naam,parent_id,centroide,n_chunks_seen,status,bron,bron_run,taxonomie_versie)
      values(%s,%s,%s,%s::vector,%s,'kandidaat','discovery',%s,%s)""",
      (cid, naam, parent, vlit(cen), int((dl == k).sum()), VERSIE, VERSIE))
    cand_rows.append((parent_naam if parent else "(top-level)", naam, int((dl == k).sum()), round(sim, 2)))
print(f"[{time.monotonic()-t0:.0f}s] discovery: {len(cand_rows)} kandidaat-subcategorieen voorgesteld")

# 6. toewijzing alle chunks -> dichtstbijzijnde BEVESTIGDE (ruggengraat) categorie
sims = X @ C.T; best = sims.argmax(1); bestsim = sims.max(1)
print(f"[{time.monotonic()-t0:.0f}s] chunk->ruggengraat cosine — p10/p50/p90 = "
      f"{np.percentile(bestsim,10):.2f}/{np.percentile(bestsim,50):.2f}/{np.percentile(bestsim,90):.2f}")
covered = int((bestsim >= COVER_SIM).sum())
rows = [(int(ids[i]), bb[best[i]][0], float(1 - bestsim[i]), VERSIE) for i in range(len(ids)) if bestsim[i] >= COVER_SIM]
cur.executemany("insert into v2a.chunk_categorie(chunk_id,categorie_id,afstand,taxonomie_versie) values(%s,%s,%s,%s)", rows)
print(f"[{time.monotonic()-t0:.0f}s] toewijzing (naar ruggengraat): {covered}/{len(ids)} = {100*covered/len(ids):.0f}% (sim>={COVER_SIM})")

# 7. rapport
from collections import defaultdict
byparent = defaultdict(list)
for parent, naam, n, sim in cand_rows:
    byparent[parent].append((naam, n, sim))
print("\n=== kandidaat-subcategorieen per thema (top 3 per thema, op omvang) ===")
for parent in sorted(byparent, key=lambda p: -sum(x[1] for x in byparent[p])):
    tops = sorted(byparent[parent], key=lambda x: -x[1])[:3]
    print(f"  {parent}:")
    for naam, n, sim in tops:
        print(f"      n={n:>4} sim={sim}  {naam}")
print("\n=== ruggengraat-support (chunk-toewijzing per thema) ===")
cur.execute("""select k.naam, k.bron, count(cc.chunk_id) n
  from v2a.categorie k left join v2a.chunk_categorie cc on cc.categorie_id=k.categorie_id
  where k.status='bevestigd' group by 1,2 order by 3 desc""")
for naam, bron, n in cur.fetchall():
    print(f"  {n:>5}  {naam:24} [{bron}]")
print(f"\nTotaal: {len(bb)} bevestigde thema's + {len(cand_rows)} kandidaat-subcategorieen")
c.close()
