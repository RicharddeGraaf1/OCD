"""ACHTERHAALD sinds 2026-08-10 — gebruik `bouw_indeling.py --alleen-wijzigingen`.

Dit script legt de ontwerp-tekst tegen de centroïden van `v2a.categorie`, en
draagt daarmee de asvermenging die het register op 2026-08-09 kwijtraakte: het
onderwerp waar een bepaling OVER gaat en het soort bepaling dat het IS zaten op
één as. Resultaat op wijzigingen: de grootste "categorie" was "Tanken en
vloeibare brandstoffen" met 6.026 artikelen, gevolgd door drie waarden die in
werkelijkheid typeBepaling zijn.

De tour leest nu `v2a.wijziging_indeling`, gevuld uit de opschriftketen met exact
de regels van het register. `v2a.wijziging_categorie` en
`wijziging_artikel_categorie` worden door niets meer gelezen.

Blijft staan om één reden: de gatenvuller hieronder (`--embed-gaten`) is de enige
plek waar de chunk-opbouw voor ontwerp-tekst staat die gelijk is aan
run_overnight_ontwerp.py. Draai de toewijzing niet meer.

---

Wijs een onderwerp toe aan ontwerp-/besluit-tekst (de onderwerp-as op wijzigingen).

Vult `v2a.wijziging_categorie` — zie scripts/2026-08-add-wijziging-categorie.sql
voor het waarom. Plan: c:/GIT/OCDviewer/docs/plans/wijzigingentour-op-onderwerp.md

Kern in drie zinnen:
  - de embeddings bestaan al (`v2a.tekst_embedding` met source_type='ontwerp',
    weggeschreven door run_overnight_ontwerp.py met dezelfde chunk-opbouw als de
    p2p-laag), dus dit script embed normaal gesproken niets;
  - toewijzing = dichtstbijzijnde BEVESTIGDE centroïde uit `v2a.categorie`,
    cosine-afstand, drempel 0,50 — identiek aan stap 6 van build_categorie.py;
  - de brug tussen chunk en artikel loopt over de wId, niet over source_ref:
    p2pwijziging wordt bij elke load opnieuw gevuld met verse BIGSERIALs, dus
    source_ref veroudert (gm0394: 2.182 van 4.555 resolveert nog).

Gebruik:
    python scripts/classify_wijziging.py --werk /akn/nl/act/gm0394/2020/omgevingsplan
    python scripts/classify_wijziging.py --top 10          # grootste ontwerpen eerst
    python scripts/classify_wijziging.py --alle
    python scripts/classify_wijziging.py --alle --embed-gaten   # ook de 14 besluiten
                                                                # zonder ontwerp-chunks
    python scripts/classify_wijziging.py --alle --her-toewijzen --drempel 0.45

Idempotent: een tweede run over dezelfde scope voegt niets toe (ON CONFLICT DO
NOTHING). Met --her-toewijzen worden bestaande toewijzingen wel overschreven —
gebruik dat na een taxonomie-bump of een andere drempel.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import get_conn  # noqa: E402

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
DDL_PATH = os.path.join(SCRIPTS, "2026-08-add-wijziging-categorie.sql")

OLLAMA = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
BATCH = 64

# Gelijk aan build_categorie.py stap 6: COVER_SIM = 0.50, afstand = 1 - cosine.
DREMPEL = 0.50

t0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[{time.monotonic() - t0:6.1f}s] {msg}", flush=True)


# ── Embed-gatenvuller ───────────────────────────────────────────────────────
#
# Alleen nodig voor besluiten waar run_overnight_ontwerp.py nog niet overheen is
# (14 van de 445 op 2026-08-07). De chunk-opbouw is LETTERLIJK die van dat script
# — kop_pad · inhoud_plain over Lid/Divisietekst/Begrip + Artikel-zonder-Lid,
# length > 30. Wijkt dat af, dan zijn de vectoren niet vergelijkbaar met de
# centroïden en is de toewijzing onzin.

FETCH_ONTBREKEND = """
WITH RECURSIVE kop AS (
    SELECT id, parent_id, 1 AS d, ARRAY[opschrift]::text[] AS path
    FROM p2pwijziging.tekst_element
    WHERE ontwerpbesluit_id = %(o)s AND inhoud_plain IS NOT NULL AND length(inhoud_plain) > 30
    UNION ALL
    SELECT k.id, p.parent_id, k.d + 1,
           CASE WHEN p.opschrift IS NOT NULL AND p.opschrift <> ''
                THEN p.opschrift || k.path ELSE k.path END
    FROM kop k JOIN p2pwijziging.tekst_element p ON p.id = k.parent_id WHERE k.d < 20
),
best AS (SELECT DISTINCT ON (id) id, path FROM kop ORDER BY id, d DESC)
SELECT t.id, t.element_type, t.wid, t.inhoud_plain,
       array_to_string(array_remove(b.path, NULL), ' > ') AS kop_pad
FROM p2pwijziging.tekst_element t JOIN best b ON b.id = t.id
WHERE t.ontwerpbesluit_id = %(o)s
  AND t.inhoud_plain IS NOT NULL AND length(t.inhoud_plain) > 30
  AND (t.element_type IN ('Lid', 'Divisietekst', 'Begrip')
       OR (t.element_type = 'Artikel'
           AND NOT EXISTS (SELECT 1 FROM p2pwijziging.tekst_element kid
                           WHERE kid.parent_id = t.id AND kid.element_type = 'Lid')))
  AND NOT EXISTS (SELECT 1 FROM v2a.tekst_embedding v
                  WHERE v.source_type = 'ontwerp'
                    AND v.regeling_expression = %(w)s
                    AND v.wid = t.wid)
ORDER BY t.volgorde
"""


def embed_batch(texts: list[str], tries: int = 5) -> list[list[float]]:
    for i in range(tries):
        try:
            r = httpx.post(f"{OLLAMA}/api/embed",
                           json={"model": EMBED_MODEL, "input": texts}, timeout=120)
            if r.status_code == 404:
                log("  nomic 404 -> re-pull")
                httpx.post(f"{OLLAMA}/api/pull", json={"model": EMBED_MODEL}, timeout=600)
                continue
            r.raise_for_status()
            return r.json()["embeddings"]
        except Exception as e:  # noqa: BLE001 — retry-lus, laatste poging her-raist
            log(f"  embed retry {i + 1}/{tries}: {e}")
            time.sleep(5 * (i + 1))
    raise RuntimeError("embed faalde na retries")


def vlit(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


WERKEN_MET_GATEN = """
SELECT b.regeling_work,
       count(*) FILTER (
           WHERE NOT EXISTS (SELECT 1 FROM v2a.tekst_embedding v
                             WHERE v.source_type = 'ontwerp'
                               AND v.regeling_expression = b.regeling_work
                               AND v.wid = t.wid)) AS ontbreekt
FROM   p2pwijziging.tekst_element t
JOIN   p2pwijziging.besluit b USING (ontwerpbesluit_id)
WHERE  t.inhoud_plain IS NOT NULL AND length(t.inhoud_plain) > 30
  AND (t.element_type IN ('Lid', 'Divisietekst', 'Begrip')
       OR (t.element_type = 'Artikel'
           AND NOT EXISTS (SELECT 1 FROM p2pwijziging.tekst_element kid
                           WHERE kid.parent_id = t.id AND kid.element_type = 'Lid')))
GROUP  BY 1
HAVING count(*) FILTER (
           WHERE NOT EXISTS (SELECT 1 FROM v2a.tekst_embedding v
                             WHERE v.source_type = 'ontwerp'
                               AND v.regeling_expression = b.regeling_work
                               AND v.wid = t.wid)) > 0
"""


def werken_met_gaten(conn) -> dict[str, int]:
    """Welke werken missen ontwerp-chunks, en hoeveel.

    Één keer vooraf, niet per werk: de gatendetectie is een recursieve
    NOT EXISTS over de hele mirror en kost per werk ~2 minuten. Op 240 van de
    251 werken is het antwoord 'geen gaten', dus dat werk is bijna helemaal
    weggegooid. De eerste --embed-gaten-run liep hierdoor op ~9 uur.
    """
    with conn.cursor() as cur:
        cur.execute(WERKEN_MET_GATEN)
        return {r["regeling_work"]: r["ontbreekt"] for r in cur.fetchall()}


def embed_gaten(conn, werk: str) -> int:
    """Embed ontbrekende chunks van één werk. Retourneert het aantal nieuwe rijen."""
    with conn.cursor() as cur:
        cur.execute("SELECT ontwerpbesluit_id FROM p2pwijziging.besluit WHERE regeling_work = %s",
                    (werk,))
        bronnen = [r["ontwerpbesluit_id"] for r in cur.fetchall()]

    toegevoegd = 0
    for bron in bronnen:
        with conn.cursor() as cur:
            cur.execute(FETCH_ONTBREKEND, {"o": bron, "w": werk})
            rows = cur.fetchall()
        if not rows:
            continue
        texts = [(r["kop_pad"] + " · " + r["inhoud_plain"]) if r["kop_pad"] else r["inhoud_plain"]
                 for r in rows]
        vecs: list[list[float]] = []
        for j in range(0, len(texts), BATCH):
            vecs.extend(embed_batch(texts[j:j + BATCH]))
        with conn.cursor() as cur:
            # executemany i.p.v. een execute-lus: dit zijn tienduizenden rijen
            # met elk een 768-dim vector, en per-rij round-trips maakten de
            # gatenvuller de traagste stap van het hele script.
            cur.executemany(
                """INSERT INTO v2a.tekst_embedding
                   (regeling_expression, bron_soort, kop_pad, inhoud_plain, embedding,
                    source_type, source_ref, wid)
                   VALUES (%s, %s, %s, %s, %s::vector, 'ontwerp', %s, %s)""",
                [(werk, r["element_type"], r["kop_pad"], r["inhoud_plain"],
                  vlit(v), str(r["id"]), r["wid"]) for r, v in zip(rows, vecs)])
        conn.commit()
        toegevoegd += len(rows)
    return toegevoegd


# ── Artikel-map ─────────────────────────────────────────────────────────────
#
# Per chunk-wId de wId van het dichtstbijzijnde Artikel erboven. Klimt via
# parent_id door de p2pwijziging-boom van dít werk. Een element dat zelf een
# Artikel is (Artikel-zonder-Lid-chunk) mapt op zichzelf.
#
# DISTINCT ON (start_wid) ORDER BY d: dezelfde wId kan in meerdere besluiten van
# hetzelfde werk voorkomen; die klimmen naar hetzelfde artikel, dus de eerste
# treffer volstaat.

ARTIKEL_MAP = """
CREATE TEMP TABLE tmp_artikel_map ON COMMIT DROP AS
WITH RECURSIVE elems AS (
    SELECT t.id, t.parent_id, t.wid, t.element_type
    FROM p2pwijziging.tekst_element t
    JOIN p2pwijziging.besluit b USING (ontwerpbesluit_id)
    WHERE b.regeling_work = %(w)s
),
klim AS (
    SELECT e.wid AS start_wid, e.id, e.parent_id, e.element_type, e.wid, 0 AS d
    FROM elems e
    UNION ALL
    SELECT k.start_wid, p.id, p.parent_id, p.element_type, p.wid, k.d + 1
    FROM klim k JOIN elems p ON p.id = k.parent_id
    WHERE k.element_type <> 'Artikel' AND k.d < 20
)
SELECT DISTINCT ON (start_wid) start_wid AS wid, wid AS artikel_wid
FROM klim
WHERE element_type = 'Artikel'
ORDER BY start_wid, d
"""

TOEWIJZEN = """
INSERT INTO v2a.wijziging_categorie
       (chunk_id, regeling_work, wid, artikel_wid, categorie_id, afstand, taxonomie_versie)
SELECT c.id, c.regeling_expression, c.wid, m.artikel_wid, k.categorie_id, k.afstand, %(versie)s
FROM v2a.tekst_embedding c
LEFT JOIN tmp_artikel_map m ON m.wid = c.wid
CROSS JOIN LATERAL (
    SELECT cat.categorie_id, (c.embedding <=> cat.centroide)::real AS afstand
    FROM v2a.categorie cat
    WHERE cat.status = 'bevestigd' AND cat.centroide IS NOT NULL
    ORDER BY c.embedding <=> cat.centroide
    LIMIT 1
) k
WHERE c.source_type = 'ontwerp'
  AND c.regeling_expression = %(w)s
  AND c.embedding IS NOT NULL
  AND k.afstand <= %(drempel)s
"""

CONFLICT_NIETS = " ON CONFLICT (chunk_id) DO NOTHING"
CONFLICT_UPDATE = """ ON CONFLICT (chunk_id) DO UPDATE
    SET categorie_id     = EXCLUDED.categorie_id,
        artikel_wid      = EXCLUDED.artikel_wid,
        afstand          = EXCLUDED.afstand,
        taxonomie_versie = EXCLUDED.taxonomie_versie"""


def taxonomie_versie(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT taxonomie_versie FROM v2a.categorie "
                    "WHERE taxonomie_versie IS NOT NULL LIMIT 1")
        r = cur.fetchone()
    return r["taxonomie_versie"] if r else None


def kies_werken(conn, args) -> list[str]:
    if args.werk:
        return list(args.werk)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT b.regeling_work, count(*) AS n
            FROM p2pwijziging.tekst_element te
            JOIN p2pwijziging.besluit b USING (ontwerpbesluit_id)
            WHERE te.element_type = 'Artikel'
              AND (te.wijzigactie IS NOT NULL OR te.vervallen OR te.bevat_renvooi)
            GROUP BY 1 ORDER BY 2 DESC
        """)
        rijen = cur.fetchall()
    werken = [r["regeling_work"] for r in rijen]
    return werken[: args.top] if args.top else werken


def rapport(conn, werk: str) -> dict:
    """Dekking per fase voor één werk — de maat waar het plan op afrekent."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH art AS (
                SELECT DISTINCT te.wid,
                       CASE WHEN te.vervallen
                                 OR te.wijzigactie IN ('verwijder', 'verwijderContainer')
                            THEN 'vervallen'
                            WHEN te.wijzigactie IN ('voegtoe', 'nieuweContainer')
                            THEN 'toegevoegd'
                            ELSE 'gewijzigd' END AS fase
                FROM p2pwijziging.tekst_element te
                JOIN p2pwijziging.besluit b USING (ontwerpbesluit_id)
                WHERE b.regeling_work = %(w)s AND te.element_type = 'Artikel'
                  AND (te.wijzigactie IS NOT NULL OR te.vervallen OR te.bevat_renvooi)
            )
            SELECT a.fase, count(*) AS n,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM v2a.wijziging_categorie wc
                       WHERE wc.regeling_work = %(w)s AND wc.artikel_wid = a.wid)) AS met_cat
            FROM art a GROUP BY 1
        """, {"w": werk})
        return {r["fase"]: (r["n"], r["met_cat"]) for r in cur.fetchall()}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--werk", action="append", help="FRBR-work; herhaalbaar")
    p.add_argument("--alle", action="store_true", help="alle werken met wijzigingen")
    p.add_argument("--top", type=int, help="alleen de N werken met de meeste gewijzigde artikelen")
    p.add_argument("--drempel", type=float, default=DREMPEL,
                   help=f"maximale cosine-afstand (default {DREMPEL})")
    p.add_argument("--her-toewijzen", action="store_true",
                   help="bestaande toewijzingen overschrijven i.p.v. overslaan")
    p.add_argument("--embed-gaten", action="store_true",
                   help="ontbrekende ontwerp-chunks alsnog embedden via Ollama")
    p.add_argument("--dry-run", action="store_true", help="niets wegschrijven")
    args = p.parse_args()

    if not (args.werk or args.alle or args.top):
        p.error("kies --werk, --top of --alle")

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(open(DDL_PATH, encoding="utf-8").read())
    conn.commit()
    log("schema klaar")

    versie = taxonomie_versie(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM v2a.categorie "
                    "WHERE status = 'bevestigd' AND centroide IS NOT NULL")
        n_cent = cur.fetchone()["n"]
    if n_cent == 0:
        log("GEEN bevestigde centroïden in v2a.categorie — draai eerst build_categorie.py")
        return 1
    log(f"{n_cent} bevestigde centroïden, taxonomie {versie}, drempel {args.drempel}")

    werken = kies_werken(conn, args)
    log(f"{len(werken)} werk(en) in scope")

    gaten: dict[str, int] = {}
    if args.embed_gaten:
        gaten = werken_met_gaten(conn)
        log(f"{len(gaten)} werk(en) met ontbrekende ontwerp-chunks "
            f"({sum(gaten.values())} chunks)")

    sql = TOEWIJZEN + (CONFLICT_UPDATE if args.her_toewijzen else CONFLICT_NIETS)
    totaal_nieuw, totaal_embed = 0, 0

    for i, werk in enumerate(werken, 1):
        try:
            if gaten.get(werk) and not args.dry_run:
                n = embed_gaten(conn, werk)
                totaal_embed += n
                if n:
                    log(f"  [{i}/{len(werken)}] {werk}: +{n} nieuwe chunks geëmbed")

            if args.dry_run:
                continue

            with conn.cursor() as cur:
                if args.her_toewijzen:
                    cur.execute("DELETE FROM v2a.wijziging_categorie WHERE regeling_work = %s",
                                (werk,))
                cur.execute(ARTIKEL_MAP, {"w": werk})
                cur.execute("CREATE INDEX ON tmp_artikel_map (wid)")
                cur.execute("ANALYZE tmp_artikel_map")
                cur.execute(sql, {"w": werk, "drempel": args.drempel, "versie": versie})
                nieuw = cur.rowcount
            conn.commit()          # commit dropt tmp_artikel_map (ON COMMIT DROP)
            totaal_nieuw += nieuw

            if i <= 10 or i % 25 == 0:
                r = rapport(conn, werk)
                deel = " · ".join(f"{f} {m}/{n}" for f, (n, m) in sorted(r.items()))
                log(f"  [{i}/{len(werken)}] {werk}: +{nieuw} toewijzingen — {deel}")
        except Exception as e:  # noqa: BLE001 — één werk mag de rest niet stoppen
            conn.rollback()
            log(f"  FOUT bij {werk}: {e}")

    log(f"klaar: {totaal_nieuw} toewijzingen"
        + (f", {totaal_embed} chunks geëmbed" if totaal_embed else ""))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n, count(DISTINCT regeling_work) AS w, "
                    "count(DISTINCT artikel_wid) AS a FROM v2a.wijziging_categorie")
        s = cur.fetchone()
    log(f"totaal in v2a.wijziging_categorie: {s['n']} chunks · {s['w']} werken · "
        f"{s['a']} artikelen")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
