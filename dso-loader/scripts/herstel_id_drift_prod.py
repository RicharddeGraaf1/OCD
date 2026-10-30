"""Trek de tekstelement-id's op prod gelijk met lokaal voor regelingen met id-drift (vault G-175).

Achtergrond: docs/id-drift-tekst-element.md. Bij 13 regelingen uit de sync van 01-08-2026
(prod laadde toen zelf) zijn 11.766 tekstelement-id's tussen lokaal en prod verwisseld.
Wat per id naar prod gaat (chunks, verwijzingen in de tekst), wijst daar naar andere tekst.

Wat dit script doet, per regeling en alles in één transactie:
1. de lokale `p2p.tekst_element`-rijen, `tekst_inline_referentie` en `v2a.artikel_indeling`
   van die regelingen naar staging-tabellen op prod;
2. controles: dezelfde natuurlijke sleutels aan beide kanten, en de lokale id's zijn op prod
   alleen in gebruik bij deze regelingen (anders botst het invoegen);
3. op prod de tekstelementen van die regelingen weg. Dat cascadeert naar de verwijzingen en
   naar `artikel_indeling` (FK ON DELETE CASCADE), en binnen de boom via `parent_id`;
4. de lokale rijen erin, mét hun id (`OVERRIDING SYSTEM VALUE`), en daarna verwijzingen en
   indeling;
5. natellen op natuurlijke sleutel. Pas dan COMMIT.

De chunks (`v2a.tekst_embedding.tekst_element_id`) dragen al de lokale id's en kloppen daarna
vanzelf. Daarna op prod de MV's verversen: `refresh_drieslag.py`, `refresh_health_mvs.py
--target prod`, en `REFRESH MATERIALIZED VIEW v2a.mv_element_hash` (de hertalingskoppeling).

Tweede modus, `--rijtabel`: voor tabellen met een eigen id waar niets naar verwijst
(`activiteit_locatieaanduiding`, `normwaarde`). Daar staat dezelfde inhoud soms onder een
ander id: 67 en 11 rijen op 2026-10-30. Voor gebruikers is dat onschadelijk, maar een
replicatie die op id upsert, botst erop (`uq_ala_natural`) of overschrijft stil een andere rij
(normwaarde heeft geen natuurlijke unieke sleutel). De rijen waarvan id en inhoud niet aan
beide kanten overeenkomen, gaan op prod weg; de lokale versie komt terug met het lokale id.

Gebruik:
    python scripts/herstel_id_drift_prod.py --bestand regelingen.txt        # droogloop
    python scripts/herstel_id_drift_prod.py --bestand regelingen.txt --ja
    python scripts/herstel_id_drift_prod.py --rijtabel p2p.activiteit_locatieaanduiding [--ja]
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LOKAAL = "postgresql://postgres:postgres@localhost:5434/dso"
PROD = os.environ["PROD_DB_URL"]


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def kolommen(cur, schema: str, tabel: str) -> list[str]:
    """Gewone kolommen (geen generated), in tabelvolgorde."""
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema = %s AND table_name = %s AND is_generated = 'NEVER'
                   ORDER BY ordinal_position""", (schema, tabel))
    return [r[0] for r in cur.fetchall()]


def naar_staging(lc, pc, tabel: str, kols: list[str], waar: str, params) -> int:
    stg = "_stg_" + tabel.split(".")[1]
    kl = ", ".join(kols)
    pc.execute(f"CREATE TEMP TABLE {stg} AS SELECT {kl} FROM {tabel} WITH NO DATA")
    lc.execute(f"SELECT {kl} FROM {tabel} {waar}", params)
    n = 0
    with pc.copy(f"COPY {stg} ({kl}) FROM STDIN") as cp:
        for r in lc:
            cp.write_row(r)
            n += 1
    return n


SLEUTELS = {
    "tekst_element": "SELECT te.id, te.regeling_expression||'|'||te.eid FROM p2p.tekst_element te "
                     "WHERE te.regeling_expression = ANY(%s)",
    "parent": "SELECT c.regeling_expression||'|'||c.eid, p.regeling_expression||'|'||p.eid "
              "FROM p2p.tekst_element c JOIN p2p.tekst_element p ON p.id = c.parent_id "
              "WHERE c.regeling_expression = ANY(%s)",
    "verwijzing": "SELECT te.regeling_expression||'|'||te.eid||'|'||r.soort||'|'||r.target_ref||'|'||r.positie, "
                  "coalesce(r.target_soort,'')||'|'||coalesce(r.target_gio_expression,'') "
                  "FROM p2p.tekst_inline_referentie r JOIN p2p.tekst_element te ON te.id = r.tekst_element_id "
                  "WHERE te.regeling_expression = ANY(%s)",
    "indeling": "SELECT a.regeling_expression||'|'||a.wid, te.regeling_expression||'|'||te.eid||'|'||coalesce(a.categorie,'') "
                "FROM v2a.artikel_indeling a JOIN p2p.tekst_element te ON te.id = a.tekst_element_id "
                "WHERE a.regeling_expression = ANY(%s)",
    "chunk": "SELECT e.id::text, coalesce(te.regeling_expression||'|'||te.eid,'WEES') FROM v2a.tekst_embedding e "
             "LEFT JOIN p2p.tekst_element te ON te.id = e.tekst_element_id WHERE e.regeling_expression = ANY(%s)",
}


def stand(cur, expr) -> dict[str, dict]:
    uit = {}
    for naam, q in SLEUTELS.items():
        cur.execute(q, (expr,))
        uit[naam] = dict(cur.fetchall())
    return uit


def vergelijk(lok: dict, prod: dict) -> dict[str, int]:
    """Per onderdeel: aantal sleutels waarvan prod iets anders zegt dan lokaal."""
    uit = {}
    for naam in SLEUTELS:
        l, p = lok[naam], prod[naam]
        uit[naam] = sum(1 for k in set(l) | set(p) if l.get(k) != p.get(k))
    return uit


RIJTABELLEN = ("p2p.activiteit_locatieaanduiding", "p2p.normwaarde")


def herstel_rijtabel(lc, pc, tabel: str, ja: bool) -> None:
    """Zelfde inhoud onder een ander id: op prod de afwijkende rijen vervangen door de lokale."""
    if tabel not in RIJTABELLEN:
        sys.exit(f"STOP: {tabel} staat niet in {RIJTABELLEN}; controleer eerst dat er niets naar verwijst")
    schema, naam = tabel.split(".")
    kols = kolommen(lc, schema, naam)
    if kols != kolommen(pc, schema, naam):
        sys.exit("STOP: kolommen verschillen tussen lokaal en prod")
    inhoud = ", ".join(k for k in kols if k != "id")

    def hashes(cur):
        cur.execute(f"SELECT id, md5(row({inhoud})::text) FROM {tabel}")
        return dict(cur.fetchall())

    lh, ph = hashes(lc), hashes(pc)
    lok_ids = [i for i, h in lh.items() if ph.get(i) != h]
    prod_ids = [i for i, h in ph.items() if lh.get(i) != h]
    inhoud_gelijk = sorted(lh.values()) == sorted(ph.values())
    log(f"  {tabel}: {len(lok_ids)} lokale rijen niet op prod onder hun id, {len(prod_ids)} prod-rijen "
        f"niet lokaal onder hun id · inhoud als geheel gelijk: {inhoud_gelijk}")
    if not lok_ids and not prod_ids:
        return
    if not inhoud_gelijk:
        sys.exit("STOP: de inhoud verschilt ook, niet alleen de id's; dit is geen id-drift")
    if not ja:
        log("  DROOGLOOP — niets geschreven.")
        return
    naar_staging(lc, pc, tabel, kols, "WHERE id = ANY(%s)", (lok_ids,))
    pc.execute(f"DELETE FROM {tabel} WHERE id = ANY(%s)", (prod_ids,))
    weg = pc.rowcount
    kl = ", ".join(kols)
    pc.execute(f"INSERT INTO {tabel} ({kl}) OVERRIDING SYSTEM VALUE SELECT {kl} FROM _stg_{naam}")
    erin = pc.rowcount
    na = hashes(pc)
    rest = sum(1 for i, h in lh.items() if na.get(i) != h) + sum(1 for i, h in na.items() if lh.get(i) != h)
    if rest:
        pc.connection.rollback()
        sys.exit(f"STOP: na het herstel nog {rest} afwijkingen; teruggedraaid")
    pc.connection.commit()
    log(f"  {tabel}: {weg} weg, {erin} terug met lokaal id · gecommit, id + inhoud nu gelijk")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bestand", help="één regeling_expression per regel")
    ap.add_argument("--rijtabel", help=f"herstel één tabel zonder verwijzingen: {', '.join(RIJTABELLEN)}")
    ap.add_argument("--ja", action="store_true", help="echt schrijven (anders droogloop)")
    args = ap.parse_args()
    if not (args.bestand or args.rijtabel):
        ap.error("geef --bestand of --rijtabel")

    lconn = psycopg.connect(LOKAAL)
    pconn = psycopg.connect(PROD, connect_timeout=30)
    lc, pc = lconn.cursor(), pconn.cursor()
    pc.execute("SELECT current_database()")
    if pc.fetchone()[0] != "railway":
        sys.exit("STOP: PROD_DB_URL wijst niet naar de prod-database")
    pc.execute("SET max_parallel_workers_per_gather = 0")
    if args.rijtabel:
        herstel_rijtabel(lc, pc, args.rijtabel, args.ja)
        return
    with open(args.bestand, encoding="utf-8") as f:
        expr = [r.strip() for r in f if r.strip()]
    log(f"{len(expr)} regelingen · {'ECHT' if args.ja else 'droogloop'}")

    lok = stand(lc, expr)
    voor = vergelijk(lok, stand(pc, expr))
    log(f"  vóór: afwijkingen per onderdeel {voor}")

    # staging
    te_kols = kolommen(lc, "p2p", "tekst_element")
    tir_kols = kolommen(lc, "p2p", "tekst_inline_referentie")
    ai_kols = kolommen(lc, "v2a", "artikel_indeling")
    if te_kols != kolommen(pc, "p2p", "tekst_element") or tir_kols != kolommen(pc, "p2p", "tekst_inline_referentie") \
            or ai_kols != kolommen(pc, "v2a", "artikel_indeling"):
        sys.exit("STOP: kolommen verschillen tussen lokaal en prod")
    n_te = naar_staging(lc, pc, "p2p.tekst_element", te_kols, "WHERE regeling_expression = ANY(%s)", (expr,))
    n_tir = naar_staging(lc, pc, "p2p.tekst_inline_referentie", tir_kols,
                         "WHERE tekst_element_id IN (SELECT id FROM p2p.tekst_element WHERE regeling_expression = ANY(%s))",
                         (expr,))
    n_ai = naar_staging(lc, pc, "v2a.artikel_indeling", ai_kols, "WHERE regeling_expression = ANY(%s)", (expr,))
    log(f"  lokaal: {n_te:,} tekstelementen, {n_tir:,} verwijzingen, {n_ai:,} indelingsrijen")

    # controles vóór er iets weg gaat
    pc.execute("SELECT count(*) FROM p2p.tekst_element WHERE regeling_expression = ANY(%s)", (expr,))
    n_te_prod = pc.fetchone()[0]
    pc.execute("""SELECT count(*) FROM p2p.tekst_element t JOIN _stg_tekst_element s ON s.id = t.id
                  WHERE t.regeling_expression <> ALL(%s)""", (expr,))
    botst_te = pc.fetchone()[0]
    pc.execute("""SELECT count(*) FROM p2p.tekst_inline_referentie r JOIN _stg_tekst_inline_referentie s ON s.id = r.id
                  JOIN p2p.tekst_element t ON t.id = r.tekst_element_id WHERE t.regeling_expression <> ALL(%s)""", (expr,))
    botst_tir = pc.fetchone()[0]
    log(f"  prod: {n_te_prod:,} tekstelementen · lokale id's elders in gebruik: tekstelement {botst_te}, verwijzing {botst_tir}")
    if n_te_prod != n_te or botst_te or botst_tir:
        pconn.rollback()
        sys.exit("STOP: aantallen of id's sluiten niet aan; niets gedaan")

    if not args.ja:
        pconn.rollback()
        log("DROOGLOOP — controles in orde, niets geschreven. Draai opnieuw met --ja.")
        return

    t0 = time.time()
    pc.execute("DELETE FROM p2p.tekst_element WHERE regeling_expression = ANY(%s)", (expr,))
    log(f"  prod: {pc.rowcount:,} tekstelementen weg (cascade naar verwijzingen en indeling)")
    kl = ", ".join(te_kols)
    pc.execute(f"INSERT INTO p2p.tekst_element ({kl}) OVERRIDING SYSTEM VALUE SELECT {kl} FROM _stg_tekst_element")
    log(f"  prod: {pc.rowcount:,} tekstelementen terug, met lokale id's")
    kl = ", ".join(tir_kols)
    pc.execute(f"INSERT INTO p2p.tekst_inline_referentie ({kl}) OVERRIDING SYSTEM VALUE "
               f"SELECT {kl} FROM _stg_tekst_inline_referentie")
    log(f"  prod: {pc.rowcount:,} verwijzingen terug")
    kl = ", ".join(ai_kols)
    pc.execute(f"INSERT INTO v2a.artikel_indeling ({kl}) SELECT {kl} FROM _stg_artikel_indeling")
    log(f"  prod: {pc.rowcount:,} indelingsrijen terug")

    na = vergelijk(lok, stand(pc, expr))
    log(f"  na (nog niet gecommit): afwijkingen per onderdeel {na}")
    if any(na.values()):
        pconn.rollback()
        sys.exit("STOP: na het herstel wijkt prod nog af; teruggedraaid")
    pconn.commit()
    log(f"Gecommit in {time.time() - t0:.0f}s. Nu op prod: refresh_drieslag.py, refresh_health_mvs.py "
        "--target prod en REFRESH MATERIALIZED VIEW v2a.mv_element_hash.")


if __name__ == "__main__":
    main()
