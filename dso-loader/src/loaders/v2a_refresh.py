"""Incrementele refresh van de vectorlaag — de executor uit G-97.

Wat er mis was
--------------
`run_overnight.py` embedt "incrementeel": per chunk wordt met `NOT EXISTS`
gecontroleerd of hij al bestaat. Maar om dát te weten haalt hij álle actieve
regelingen op en draait per stuk een recursieve `kop_chain`-CTE over
`p2p.tekst_element` (3,1 GB) — ook voor de duizenden regelingen waar niets is
veranderd. Gemeten 2026-08-08: **574 van 1.979 regelingen in 139 minuten**,
effectief 199 embeddings/min, terwijl Ollama er 25 ms over doet (≈2.400/min).
Het embedden was nooit de bottleneck; de detectie was het.

Dezelfde vorm als de subdiv-storm (28 s × 381 bronhouders, onvoorwaardelijk) en
`naammatch_signaal` (6,3M vergelijkingen om er 43k over te houden): niet "niet
incrementeel", maar veel te veel doen.

Wat dit wél doet
----------------
De **detectie** scopen via `v2a.embed_state`, precies waar die tabel in juli
voor is aangelegd (fase 1 van het G-97-ontwerp; de executor ontbrak). Eén query
levert de dirty-set: nieuw of gewijzigde content-hash. Gemeten na de sync van
2026-08-07: **26 dirty van 1.978** — de rest wordt niet aangeraakt.

Wat het bewust NIET doet
------------------------
`chunk_annotatie` en `chunk_categorie` incrementeel maken. Die herbouwen
volledig in 4,8 en 4,9 minuten (gemeten), en een volledige herbouw is
gegarandeerd consistent. Incrementeel maken zou dirty-state-risico toevoegen
voor winst die er niet is. Dat is dezelfde afweging als bij de intra-scoping van
`naammatch_signaal`: eerst het werk goed scopen, niet de berekening slim maken.

De content-hash
---------------
`md5(string_agg(id || ':' || inhoud_plain, '|' ORDER BY id))` over de
embeddable elementen (Lid/Divisietekst/Begrip + bladeren-Artikel, >30 tekens).
Die vorm is niet verzonnen maar afgeleid uit de bestaande 1.964 rijen en
geverifieerd: hij reproduceert de opgeslagen hashes exact. Content-hash en geen
timestamp, want p2p-herlaad is UPSERT-DO-NOTHING — gewijzigde inhoud kan een
ongewijzigd id en tijdstip houden.

Drop-by-scope
-------------
Een dirty regeling verliest eerst zijn chunks (op `regeling_expression`, nooit
op serial id) en wordt daarna opnieuw geëmbed. Zo verdwijnen ook de chunks van
verdrongen expressies, wat de tweede helft van G-97 is: bij `gm0796` bestond 45%
van de geclassificeerde wId's niet meer in de getoonde versie.

Draaien:
    python -m src.loaders.v2a_refresh            # droogloop: toon de dirty-set
    python -m src.loaders.v2a_refresh --ja
    python -m src.loaders.v2a_refresh --ja --opruimen   # ook inactieve expressies
"""

from __future__ import annotations

import argparse
import os
import time

import httpx
import psycopg
from psycopg.rows import dict_row

DB = os.environ.get("OCD_DB_URL", "postgresql://postgres:postgres@localhost:5434/dso")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = "nomic-embed-text"
BATCH = 64

# De embeddable-predicaten. Identiek aan run_overnight.FETCH — als die wijzigen
# verandert de hash-definitie mee, dus ze horen op één plek te staan.
EMBEDDABLE = """
    te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > 30
    AND (te.element_type IN ('Lid','Divisietekst','Begrip')
         OR (te.element_type = 'Artikel'
             AND NOT EXISTS (SELECT 1 FROM p2p.tekst_element k
                              WHERE k.parent_id = te.id AND k.element_type = 'Lid')))
"""

DIRTY_SQL = f"""
WITH huidig AS (
  SELECT te.regeling_expression AS scope_key,
         md5(string_agg(te.id::text || ':' || te.inhoud_plain, '|' ORDER BY te.id)) AS h,
         count(*) AS n
    FROM p2p.tekst_element te
    JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression AND NOT r.inactief
   WHERE {EMBEDDABLE}
   GROUP BY 1
)
SELECT h.scope_key, h.h AS content_hash, h.n,
       CASE WHEN es.scope_key IS NULL THEN 'nieuw' ELSE 'gewijzigd' END AS reden
  FROM huidig h
  LEFT JOIN v2a.embed_state es
         ON es.scope_key = h.scope_key AND es.source_type = 'p2p'
 WHERE es.scope_key IS NULL OR es.content_hash <> h.h
 ORDER BY h.n DESC
"""

# Per regeling: de chunks mét hun kop-pad, zoals run_overnight ze opbouwt.
FETCH = f"""
WITH RECURSIVE kop_chain AS (
    SELECT id, parent_id, opschrift, 1 AS d, ARRAY[opschrift]::text[] AS path
      FROM p2p.tekst_element
     WHERE regeling_expression = %(e)s
       AND inhoud_plain IS NOT NULL AND length(inhoud_plain) > 30
    UNION ALL
    SELECT k.id, p.parent_id, k.opschrift, k.d + 1,
           CASE WHEN p.opschrift IS NOT NULL AND p.opschrift <> ''
                THEN p.opschrift || k.path ELSE k.path END
      FROM kop_chain k JOIN p2p.tekst_element p ON p.id = k.parent_id
     WHERE k.d < 20
),
best AS (SELECT DISTINCT ON (id) id, path FROM kop_chain ORDER BY id, d DESC)
SELECT te.id, te.element_type, te.wid, te.inhoud_plain,
       array_to_string(b.path, ' > ') AS kop_pad
  FROM p2p.tekst_element te JOIN best b ON b.id = te.id
 WHERE te.regeling_expression = %(e)s AND {EMBEDDABLE}
 ORDER BY te.volgorde
"""


def log(*a):
    print(*a, flush=True)


def _vec(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def embed_batch(texts: list[str], tries: int = 5) -> list:
    for i in range(tries):
        try:
            r = httpx.post(f"{OLLAMA}/api/embed",
                           json={"model": MODEL, "input": texts}, timeout=120)
            if r.status_code == 404:      # nomic is al eens uit Ollama verdwenen
                log("  nomic 404 → re-pull")
                httpx.post(f"{OLLAMA}/api/pull", json={"model": MODEL}, timeout=600)
                continue
            r.raise_for_status()
            return r.json()["embeddings"]
        except Exception as e:
            if i == tries - 1:
                raise
            log(f"  embed-poging {i + 1} faalde ({str(e)[:60]}), opnieuw…")
            time.sleep(2 * (i + 1))
    raise RuntimeError("embed faalde na retries")


def dirty_set(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(DIRTY_SQL)
        return cur.fetchall()


def adopteerbaar(conn, scope_key: str) -> bool:
    """Heeft deze scope al precies de chunks die hij hoort te hebben?

    Bedoeld voor de ingebruikname van `embed_state`: de eerste run ziet élke
    regeling als 'nieuw', ook de duizenden die `run_overnight.py` al netjes
    heeft geëmbed. Die opnieuw embedden kost per chunk een HNSW-insert in een
    index van 3,8 GB die niet in geheugen past — gemeten 2026-08-08: één
    regeling van 2.844 chunks was na 58 minuten nog niet klaar. Voor 26
    regelingen is dat een nacht, voor niets.

    De aanname die je hier maakt, expliciet: als het aantal geëmbedde
    `tekst_element_id`'s exact gelijk is aan het aantal embeddable elementen,
    dan zijn die chunks uit dezelfde tekst gemaakt. Dat is niet bewijsbaar —
    p2p-herlaad is UPSERT-DO-NOTHING, dus inhoud kán zijn gewijzigd onder een
    ongewijzigd id. Precies daarvoor bestaat de content-hash, en vanaf de
    tweede run dékt die het ook. Alleen bij de allereerste vulling is er nog
    geen hash om tegen te vergelijken.

    Daarom staat dit achter een aparte vlag en niet aan by default: het is een
    eenmalige inhaalslag, geen normale werkwijze.
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT (SELECT count(*) FROM p2p.tekst_element te
                     WHERE te.regeling_expression = %s AND {EMBEDDABLE}) AS moet,
                   (SELECT count(*) FROM v2a.tekst_embedding v
                     WHERE v.regeling_expression = %s AND v.tekst_element_id IS NOT NULL) AS heeft
        """, (scope_key, scope_key))
        r = cur.fetchone()
        return r["moet"] > 0 and r["moet"] == r["heeft"]


def adopteer(conn, scope_key: str, content_hash: str, n: int) -> None:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO v2a.embed_state (scope_key, source_type, content_hash, n_chunks)
                       VALUES (%s,'p2p',%s,%s)
                       ON CONFLICT (scope_key, source_type)
                       DO UPDATE SET content_hash=EXCLUDED.content_hash,
                                     n_chunks=EXCLUDED.n_chunks, refreshed_at=now()""",
                    (scope_key, content_hash, n))


def verweesde_scopes(conn) -> list[str]:
    """p2p-expressies met chunks die niet meer vigerend zijn.

    Dit is de tweede helft van G-97: chunks worden alleen toegevoegd, nooit
    opgeruimd, dus na een verdringing blijven de chunks van de oude versie
    staan. De onderwerp-as zeeft ze bij het lezen weg (45% bij gm0796), maar
    de vectorindex zoekt er wel in.

    LET OP de scope. `v2a.tekst_embedding` bevat meer dan p2p: `source_type`
    'wro' (39.358 expressies, 628k chunks) en 'ontwerp' (~240 expressies, 470k
    chunks) horen per definitie niet bij een vigerende `p2p.regeling`. Een
    naïeve "bestaat niet in p2p.regeling"-query markeert die allemaal als wees
    en zou bij `--opruimen` de halve index wissen — de droogloop van
    2026-08-08 gaf 39.846 "wezen" waar er 12 echt waren.

    De scherpe definitie: de expressie komt wél voor in `p2p.regeling` (dus
    het is een p2p-expressie) maar staat op inactief. Verdrongen versies dus,
    niet andere lagen.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT v.regeling_expression
              FROM v2a.tekst_embedding v
              JOIN p2p.regeling r ON r.frbr_expression = v.regeling_expression
             WHERE r.inactief
        """)
        return [r["regeling_expression"] for r in cur.fetchall()]


def refresh_scope(conn, scope_key: str, content_hash: str) -> int:
    """Drop-by-scope + opnieuw embedden. Geeft het aantal nieuwe chunks.

    Niet optimaliseren zonder nieuwe meting — de voor de hand liggende
    verdachten zijn allemaal doorgemeten en geen ervan is het.

    De eerste run (2026-08-08) deed 1.442 nieuwe chunks, 20 adopties en het
    opruimen van 19.506 chunks in **102,9 minuten**. Dat leek te wijzen op de
    per-rij-INSERT of op de HNSW-index van 3,8 GB. Op een rustige database
    gemeten valt dat volledig uit elkaar:

        embedden (Ollama)      1,48 s / 100 =  4.058/min
        insert MET indices     5,82 s / 100 =  1.031/min
        insert ZONDER indices  1,47 s / 100 =  4.095/min   (75% is index-werk)
        DELETE                 1,55 s / 2.984 = 115.430/min
        FETCH (recursieve CTE) 0,74 s per regeling (grootste: 6.911 elementen)
        adopteerbaar()         0,59 s per regeling
        DIRTY_SQL              42,3 s, eenmalig over het hele corpus

    Opgeteld voor die run: 26× FETCH 0,3 min + 26× adopteerbaar 0,3 min +
    DIRTY_SQL 0,7 min + DELETE 0,2 min + INSERT 1,4 min ≈ **2,9 minuten**.

    De overige 100 minuten waren **concurrentie**, geen werk: de refresh liep
    gelijktijdig met de volledige i2a-run (5,6 uur, continu inserts) en een
    parallelle artikel-keten-query. Drie processen op dezelfde disk.

    Conclusie: de HNSW-index is per rij inderdaad duur (75% van de insert-tijd),
    maar bij realistische dirty-sets — na de eerste run was dirty 0 — telt dat
    niet op tot iets dat de moeite waard is. `COPY` zou hooguit de resterende
    25% raken. Wat wél helpt is deze refresh niet naast een andere zware run
    zetten.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM v2a.tekst_embedding WHERE regeling_expression = %s",
                    (scope_key,))
        cur.execute(FETCH, {"e": scope_key})
        rows = cur.fetchall()
        if not rows:
            cur.execute("""INSERT INTO v2a.embed_state (scope_key, source_type, content_hash, n_chunks)
                           VALUES (%s,'p2p',%s,0)
                           ON CONFLICT (scope_key, source_type)
                           DO UPDATE SET content_hash=EXCLUDED.content_hash,
                                         n_chunks=0, refreshed_at=now()""",
                        (scope_key, content_hash))
            return 0

        teksten = [(r["kop_pad"] + " · " + r["inhoud_plain"]) if r["kop_pad"]
                   else r["inhoud_plain"] for r in rows]
        vectors = []
        for i in range(0, len(teksten), BATCH):
            vectors.extend(embed_batch(teksten[i:i + BATCH]))

        for r, v in zip(rows, vectors):
            cur.execute("""INSERT INTO v2a.tekst_embedding
                (tekst_element_id, regeling_expression, bron_soort, kop_pad,
                 inhoud_plain, embedding, wid, source_type, source_id)
                VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,%s)""",
                (r["id"], scope_key, r["element_type"], r["kop_pad"],
                 r["inhoud_plain"], _vec(v), r["wid"], r["element_type"], r["id"]))

        cur.execute("""INSERT INTO v2a.embed_state (scope_key, source_type, content_hash, n_chunks)
                       VALUES (%s,'p2p',%s,%s)
                       ON CONFLICT (scope_key, source_type)
                       DO UPDATE SET content_hash=EXCLUDED.content_hash,
                                     n_chunks=EXCLUDED.n_chunks, refreshed_at=now()""",
                    (scope_key, content_hash, len(rows)))
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ja", action="store_true", help="echt uitvoeren")
    ap.add_argument("--opruimen", action="store_true",
                    help="ook chunks van niet-vigerende expressies verwijderen")
    ap.add_argument("--adopteer", action="store_true",
                    help="scopes waarvan de chunks al compleet zijn niet opnieuw embedden "
                         "maar alleen in embed_state opnemen (eenmalige inhaalslag)")
    ap.add_argument("--limiet", type=int, default=None)
    args = ap.parse_args()

    conn = psycopg.connect(DB, row_factory=dict_row)
    t0 = time.time()

    vuil = dirty_set(conn)
    if args.limiet:
        vuil = vuil[:args.limiet]
    nieuw = sum(1 for v in vuil if v["reden"] == "nieuw")
    log(f"dirty: {len(vuil)} regelingen ({nieuw} nieuw, {len(vuil) - nieuw} gewijzigd)")
    for v in vuil[:10]:
        log(f"  {v['n']:>6} chunks  {v['reden']:<10} {v['scope_key'][-58:]}")
    if len(vuil) > 10:
        log(f"  … en {len(vuil) - 10} meer")

    wees = verweesde_scopes(conn) if args.opruimen else []
    if args.opruimen:
        log(f"verweesd: {len(wees)} niet-vigerende expressies met chunks")

    if not args.ja:
        log("\nDROOGLOOP — draai met --ja.")
        conn.close()
        return

    totaal = n_geadopteerd = 0
    for i, v in enumerate(vuil, 1):
        if args.adopteer and adopteerbaar(conn, v["scope_key"]):
            adopteer(conn, v["scope_key"], v["content_hash"], v["n"])
            conn.commit()
            n_geadopteerd += 1
            log(f"[{i}/{len(vuil)}] geadopteerd ({v['n']} chunks stonden er al)  "
                f"{v['scope_key'][-46:]}")
            continue
        n = refresh_scope(conn, v["scope_key"], v["content_hash"])
        conn.commit()
        totaal += n
        log(f"[{i}/{len(vuil)}] +{n} chunks  {v['scope_key'][-52:]}")

    if wees:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM v2a.tekst_embedding WHERE regeling_expression = ANY(%s)",
                        (wees,))
            weg = cur.rowcount
            cur.execute("DELETE FROM v2a.embed_state WHERE scope_key = ANY(%s) AND source_type='p2p'",
                        (wees,))
        conn.commit()
        log(f"opgeruimd: {weg:,} chunks van {len(wees)} niet-vigerende expressies")

    log(f"\nKlaar in {(time.time() - t0) / 60:.1f} min — {totaal:,} chunks geëmbed"
        + (f", {n_geadopteerd} scopes geadopteerd." if n_geadopteerd else "."))
    log("Draai hierna chunk_annotatie + chunk_categorie (samen ~10 min, "
        "volledige herbouw is hier goedkoper dan incrementeel).")
    conn.close()


if __name__ == "__main__":
    main()
