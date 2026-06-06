#!/usr/bin/env python3
"""
PoC: ingest omgevingsvergunning-kennisgevingen uit KOOP SRU.

Bron: KOOP SRU 2.0 op https://repository.overheid.nl/sru
Filter: dcterms:type scheme="OVERHEIDop.Rubriek" == "omgevingsvergunning"

Persisteer-targets:
- **Postgres** (default): naar het losstaande schema `koop` in de OCD-DB
  (via src.db.get_conn() — zelfde DB als andere dso-loader-bronnen).
  Geometrie als PostGIS POINT/POLYGON.
- **SQLite**: alleen voor lokaal debuggen — data/koop.db.

Eigenschappen:
- Idempotent op koop_id (INSERT … ON CONFLICT DO UPDATE)
- Restartable per dag via etl_run-tabel
- Per-record TYPE_BESLUIT-classifier op basis van titel
- Geometrie:
    * Adres   -> POINT + adresvelden
    * Punt    -> POINT
    * Vlak    -> POLYGON + centroid (RD en WGS84)
    * (geen)  -> label/beschrijving in tekstvelden
- httpx Session met retry/backoff (5/15/45/90/180s, max 5 pogingen)

Achtergrond: dit is een PoC bij analyse-pagina
[[Ingest omgevingsvergunningen uit officielebekendmakingen]] in
vault_v1. Het `koop`-schema staat **losstaand** in OCD (geen FK's naar
dso.* tabellen, geen waardelijst-koppelingen) zoals besloten 2026-05-19.

NB: sinds 2026-05-31 is de canonical loader naar
`src/loaders/koop_vergunning.py` verhuisd. Dit script wrapper-importeert
daaruit en voegt nog twee dingen toe die alleen lokaal nuttig zijn:
- SQLite-backend (--db sqlite) voor debugging zonder Postgres-stack
- backfill-fields-command voor de eenmalige 2026-05-20-velden-backfill

Usage:
    python ingest.py setup                                     # create vth.* tables (Postgres)
    python ingest.py run --from 2026-05-13 --to 2026-05-13     # default: Postgres
    python ingest.py run --from 2026-05-13 --to 2026-05-13 --db sqlite
    python ingest.py run --from 2024-01-01 --to 2026-05-18     # full backfill
    python ingest.py status                                    # show DB summary
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import logging
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "koop.db"
SCHEMA_FILE = ROOT / "schema.sql"

# Zorg dat `src.*` importeerbaar is wanneer dit script vanuit deze map start.
_DSO_LOADER_ROOT = ROOT.parent.parent  # c:/GIT/OCD/dso-loader/
if str(_DSO_LOADER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DSO_LOADER_ROOT))

# Canonical loader-functies + dataclass + constants — alles import.
from src.loaders.koop_vergunning import (  # noqa: E402
    NS,
    PAGE_SIZE,
    REQUEST_INTERVAL,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
    RETRY_BACKOFF_SEQ,
    SRU_BASE,
    USER_AGENT,
    Record,
    _PG_SKIP_COLS,
    _enrich_one_batch,
    classify_type_besluit,
    daterange,
    extract_adres_uit_titel,
    extract_datum_ontvangst,
    extract_tekst_uit_publicatie_xml,
    extract_zaaknummer_bg,
    fetch_page,
    get_client,
    iter_day_records,
    parse_gebiedsmarkering,
    parse_geometrielabel,
    parse_latlon,
    parse_point,
    parse_record,
    pg_finish_run,
    pg_get_conn,
    pg_mark_run,
    pg_run_status,
    pg_upsert,
    polygon_centroid,
    process_day as _pg_process_day,
    text_of,
)
from src.loaders.koop_deeplinks import (  # noqa: E402
    extract_deeplinks,
    upsert_deeplink,
)

PG_AVAILABLE = False
try:
    import psycopg  # noqa: F401
    PG_AVAILABLE = True
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("koop")


# ---------- SQLite schema (lokaal debuggen) --------------------------------

# Houdt de oude PoC-SQLite-DDL hier; src/ddl.py:KOOP_DDL is Postgres-only.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vergunningkennisgeving (
    koop_id           TEXT PRIMARY KEY,
    publicatieblad    TEXT NOT NULL,
    bg_naam           TEXT NOT NULL,
    bg_scheme         TEXT,
    organisatietype   TEXT,
    titel             TEXT NOT NULL,
    datum_publicatie  TEXT NOT NULL,
    jaargang          INTEGER,
    publicatienummer  TEXT,
    rubriek           TEXT,
    activiteit_code   TEXT,
    type_besluit      TEXT,
    geometrie_type    TEXT,
    geometrie_rd_x    REAL,
    geometrie_rd_y    REAL,
    geometrie_lat     REAL,
    geometrie_lon     REAL,
    geometrie_rd      TEXT,
    geometrie_rd_pt   TEXT,
    geometrie_wgs_pt  TEXT,
    geometrielabel    TEXT,
    postcode          TEXT,
    huisnummer        TEXT,
    huisletter        TEXT,
    huisnummertoevoeging TEXT,
    straatnaam        TEXT,
    woonplaats        TEXT,
    ligt_in_gemeente  TEXT,
    beschrijving      TEXT,
    preferred_url     TEXT,
    xml_url           TEXT,
    pdf_url           TEXT,
    raw_xml           TEXT NOT NULL,
    datum_ontvangst       TEXT,
    datum_publicatie_ts   TEXT,
    subject_taxonomie     TEXT,
    ingest_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    ingest_run_id     TEXT
);

CREATE INDEX IF NOT EXISTS idx_vk_bg_datum
    ON vergunningkennisgeving (bg_naam, datum_publicatie DESC);
CREATE INDEX IF NOT EXISTS idx_vk_datum
    ON vergunningkennisgeving (datum_publicatie);
CREATE INDEX IF NOT EXISTS idx_vk_activiteit
    ON vergunningkennisgeving (activiteit_code);
CREATE INDEX IF NOT EXISTS idx_vk_type_besluit
    ON vergunningkennisgeving (type_besluit);

CREATE TABLE IF NOT EXISTS etl_run (
    run_id            TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    processed_date    TEXT NOT NULL,
    record_count      INTEGER,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL,
    error             TEXT,
    UNIQUE (source, processed_date)
);
"""

_SQLITE_EXPECTED_COLS = {
    "pdf_url", "datum_ontvangst", "datum_publicatie_ts", "subject_taxonomie",
}


def sqlite_init() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_SQL)
    existing = {
        r[1] for r in conn.execute("PRAGMA table_info(vergunningkennisgeving)")
    }
    for col in _SQLITE_EXPECTED_COLS - existing:
        conn.execute(f"ALTER TABLE vergunningkennisgeving ADD COLUMN {col} TEXT")
    conn.commit()
    return conn


def sqlite_upsert(conn: sqlite3.Connection, rec: Record, run_id: str) -> None:
    cols = [f.name for f in dataclasses.fields(Record)] + ["ingest_run_id"]
    values = [getattr(rec, c) if c != "ingest_run_id" else run_id for c in cols]
    placeholders = ",".join(["?"] * len(cols))
    sql = (
        f"INSERT OR REPLACE INTO vergunningkennisgeving ({','.join(cols)}) "
        f"VALUES ({placeholders})"
    )
    conn.execute(sql, values)


def sqlite_mark_run(conn: sqlite3.Connection, run_id: str, day: dt.date,
                    started: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO etl_run "
        "(run_id, source, processed_date, started_at, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (run_id, "koop_omgevingsvergunning", day.isoformat(), started),
    )
    conn.commit()


def sqlite_finish_run(conn: sqlite3.Connection, run_id: str, count: int,
                      finished: str, ok: bool, error: Optional[str]) -> None:
    if ok:
        conn.execute(
            "UPDATE etl_run SET record_count=?, finished_at=?, status='ok' "
            "WHERE run_id=?",
            (count, finished, run_id),
        )
    else:
        conn.execute(
            "UPDATE etl_run SET record_count=?, finished_at=?, status='error', error=? "
            "WHERE run_id=?",
            (count, finished, (error or "")[:500], run_id),
        )
    conn.commit()


def sqlite_run_status(conn: sqlite3.Connection, day: dt.date) -> Optional[str]:
    row = conn.execute(
        "SELECT status FROM etl_run WHERE source=? AND processed_date=?",
        ("koop_omgevingsvergunning", day.isoformat()),
    ).fetchone()
    return row[0] if row else None


# ---------- Postgres setup (gebruikt lokale schema.sql voor PoC-compat) ----

def pg_setup() -> None:
    """Execute schema.sql against Postgres.

    Houdt PoC-compat: leest de wrapper-schema.sql in deze map. De canonical
    KOOP-DDL leeft in src/ddl.py:KOOP_DDL en kan via
    `python -m src.cli setup-koop` worden toegepast.
    """
    if not SCHEMA_FILE.exists():
        raise FileNotFoundError(f"schema.sql not found at {SCHEMA_FILE}")
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn = pg_get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        log.info("Postgres schema applied from %s", SCHEMA_FILE.name)
    finally:
        conn.close()


# ---------- Backend dispatch ------------------------------------------------

class Backend:
    """Tiny dispatcher to keep process_day backend-agnostic."""
    def __init__(self, kind: str):
        if kind not in ("postgres", "sqlite"):
            raise ValueError(f"unknown backend: {kind}")
        self.kind = kind
        self.conn = pg_get_conn() if kind == "postgres" else sqlite_init()

    def upsert(self, rec: Record, run_id: str) -> None:
        if self.kind == "postgres":
            pg_upsert(self.conn, rec, run_id)
        else:
            sqlite_upsert(self.conn, rec, run_id)

    def mark_run(self, run_id: str, day: dt.date, started: str) -> None:
        if self.kind == "postgres":
            pg_mark_run(self.conn, run_id, day, started)
        else:
            sqlite_mark_run(self.conn, run_id, day, started)

    def finish_run(self, run_id: str, count: int, finished: str,
                   ok: bool, error: Optional[str]) -> None:
        if self.kind == "postgres":
            pg_finish_run(self.conn, run_id, count, finished, ok, error)
        else:
            sqlite_finish_run(self.conn, run_id, count, finished, ok, error)

    def run_status(self, day: dt.date) -> Optional[str]:
        return (pg_run_status if self.kind == "postgres" else sqlite_run_status)(
            self.conn, day
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def process_day(backend: Backend, day: dt.date) -> int:
    """Backend-agnostische dag-verwerking (Postgres of SQLite).

    Voor Postgres-only paden kun je direct
    `src.loaders.koop_vergunning.process_day(conn, day)` gebruiken.
    """
    run_id = str(uuid.uuid4())
    started = dt.datetime.now().isoformat(timespec="seconds")
    backend.mark_run(run_id, day, started)

    count = 0
    try:
        for rec_elem in iter_day_records(day):
            rec = parse_record(rec_elem)
            if rec is None:
                continue
            backend.upsert(rec, run_id)
            count += 1
        backend.commit()
        finished = dt.datetime.now().isoformat(timespec="seconds")
        backend.finish_run(run_id, count, finished, ok=True, error=None)
        log.info("%s: %d records ingested", day.isoformat(), count)
        return count
    except Exception as e:
        finished = dt.datetime.now().isoformat(timespec="seconds")
        backend.finish_run(run_id, count, finished, ok=False, error=str(e))
        raise


# ---------- CLI --------------------------------------------------------------

def cmd_setup(_: argparse.Namespace) -> int:
    """Create the vth.* schema in Postgres from schema.sql."""
    log.info("Applying schema.sql to Postgres ...")
    pg_setup()
    return 0


def _fetch_publication_xml(url: str) -> bytes:
    """GET the full publication XML, with the same retry-backoff as SRU."""
    client = get_client()
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as e:
            last_exc = e
            wait = RETRY_BACKOFF_SEQ[min(attempt, len(RETRY_BACKOFF_SEQ) - 1)]
            log.warning("  fetch %s failed (%d/%d): %s — retry in %ds",
                        url[-60:], attempt + 1, MAX_RETRIES, type(e).__name__, wait)
            time.sleep(wait)
    raise RuntimeError(f"giving up after {MAX_RETRIES} retries: {last_exc}")


def cmd_enrich(args: argparse.Namespace) -> int:
    """
    Voor records zonder inhoud_geladen_at: haal de volledige publicatie-XML
    op via xml_url, parse de body-tekst, en update inhoud_xml /
    inhoud_tekst / zaaknummer_bg. Probeer ook nog adresvelden te vullen
    uit de titel als ze leeg waren.

    Met --loop blijft hij in cycles draaien zolang er pending records
    bijkomen (handig parallel naast `run`); stopt pas na een
    configureerbaar aantal lege cycles op rij.
    """
    if args.db != "postgres":
        log.error("enrich only supports --db postgres for now")
        return 1

    # Parse comma-separated --type-besluit into a tuple, or None for "all".
    type_filter: Optional[tuple[str, ...]] = None
    if args.type_besluit:
        type_filter = tuple(
            t.strip() for t in args.type_besluit.split(",") if t.strip()
        )
        log.info("Filtering on type_besluit IN %s", type_filter)

    empty_cycles = 0
    total_enriched = 0
    while True:
        n = _enrich_one_batch(args.limit, type_filter)
        total_enriched += n
        if n == 0:
            empty_cycles += 1
            if not args.loop or empty_cycles >= args.stop_after_empty:
                log.info("enrich done. total this run: %d", total_enriched)
                return 0
            log.info("no pending records, sleeping %ds (empty cycle %d/%d)",
                     args.sleep, empty_cycles, args.stop_after_empty)
            time.sleep(args.sleep)
        else:
            empty_cycles = 0
            if not args.loop:
                log.info("enrich done. total this run: %d", total_enriched)
                return 0


def cmd_run(args: argparse.Namespace) -> int:
    start = dt.date.fromisoformat(args.from_date)
    end = dt.date.fromisoformat(args.to_date)
    backend = Backend(args.db)
    log.info("Backend: %s", backend.kind)
    try:
        total = 0
        for day in daterange(start, end):
            status = backend.run_status(day)
            if status == "ok" and not args.force:
                log.info("%s: already ingested (use --force to redo)", day.isoformat())
                continue
            total += process_day(backend, day)
        log.info("Total upserts across range: %d", total)
        return 0
    finally:
        backend.close()


# Query helpers that work on both backends. Postgres returns dict_row;
# SQLite returns tuples — we normalize to tuples for printing.
def _rows(cur_or_conn, sql: str, kind: str) -> list[tuple]:
    if kind == "postgres":
        with cur_or_conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [tuple(r.values()) if isinstance(r, dict) else r for r in rows]
    else:
        return list(cur_or_conn.execute(sql))


def _one(cur_or_conn, sql: str, kind: str) -> Optional[tuple]:
    rows = _rows(cur_or_conn, sql, kind)
    return rows[0] if rows else None


def cmd_status(args: argparse.Namespace) -> int:
    kind = args.db
    if kind == "sqlite" and not DB_PATH.exists():
        print(f"No SQLite DB yet at {DB_PATH}")
        return 0
    if kind == "postgres":
        conn = pg_get_conn()
        prefix = "vth."
        print(f"DB: Postgres (vth schema)\n")
    else:
        conn = sqlite3.connect(DB_PATH)
        prefix = ""
        print(f"DB: {DB_PATH}\n")
    try:
        tbl = f"{prefix}vergunningkennisgeving"
        n = _one(conn, f"SELECT COUNT(*) FROM {tbl}", kind)[0]
        print(f"Total records in DB: {n:,}")
        if n == 0:
            return 0
        minmax = _one(conn,
            f"SELECT MIN(datum_publicatie), MAX(datum_publicatie) FROM {tbl}", kind)
        print(f"Date range:           {minmax[0]} .. {minmax[1]}")
        print("\nPer publicatieblad:")
        for row in _rows(conn,
            f"SELECT publicatieblad, COUNT(*) FROM {tbl} "
            f"GROUP BY publicatieblad ORDER BY 2 DESC", kind):
            print(f"  {row[0]:6s}  {row[1]:>8,}")
        print("\nPer activiteit (top 10):")
        for row in _rows(conn,
            f"SELECT COALESCE(activiteit_code,'(none)'), COUNT(*) "
            f"FROM {tbl} GROUP BY 1 ORDER BY 2 DESC LIMIT 10", kind):
            print(f"  {row[0]:30s}  {row[1]:>8,}")
        print("\nPer type_besluit:")
        for row in _rows(conn,
            f"SELECT COALESCE(type_besluit,'(none)'), COUNT(*) "
            f"FROM {tbl} GROUP BY 1 ORDER BY 2 DESC", kind):
            print(f"  {row[0]:30s}  {row[1]:>8,}")
        print("\nPer organisatietype:")
        for row in _rows(conn,
            f"SELECT COALESCE(organisatietype,'(none)'), COUNT(*) "
            f"FROM {tbl} GROUP BY 1 ORDER BY 2 DESC", kind):
            print(f"  {row[0]:20s}  {row[1]:>8,}")
        # Geometry stats — different column names per backend.
        if kind == "postgres":
            geom_sql = (
                f"SELECT COALESCE(geometrie_type,'(NULL)') gt, COUNT(*) c, "
                f" SUM(CASE WHEN geometrie_rd IS NOT NULL THEN 1 ELSE 0 END) rd, "
                f" SUM(CASE WHEN geometrie_wgs_pt IS NOT NULL THEN 1 ELSE 0 END) wgs "
                f"FROM {tbl} GROUP BY 1 ORDER BY 2 DESC"
            )
        else:
            geom_sql = (
                f"SELECT COALESCE(geometrie_type,'(NULL)') gt, COUNT(*) c, "
                f" SUM(CASE WHEN geometrie_rd_x IS NOT NULL THEN 1 ELSE 0 END) rd, "
                f" SUM(CASE WHEN geometrie_lat IS NOT NULL THEN 1 ELSE 0 END) wgs "
                f"FROM {tbl} GROUP BY 1 ORDER BY 2 DESC"
            )
        print("\nPer geometrie_type (with coords?):")
        for row in _rows(conn, geom_sql, kind):
            print(f"  {row[0]:18s}  total={row[1]:>5,}  RD={row[2]:>5,}  WGS={row[3]:>5,}")
        print("\nAdres-fields aanwezig:")
        ar = _one(conn,
            f"SELECT "
            f" SUM(CASE WHEN postcode IS NOT NULL THEN 1 ELSE 0 END) AS pc, "
            f" SUM(CASE WHEN straatnaam IS NOT NULL THEN 1 ELSE 0 END) AS st, "
            f" SUM(CASE WHEN woonplaats IS NOT NULL THEN 1 ELSE 0 END) AS wp, "
            f" COUNT(*) AS tot "
            f"FROM {tbl}", kind)
        total_ar = ar[3] or 0
        pct = lambda x: f"{x:,} ({x/total_ar*100:.1f}%)" if total_ar else "0"
        print(f"  with postcode:   {pct(ar[0] or 0)}")
        print(f"  with straatnaam: {pct(ar[1] or 0)}")
        print(f"  with woonplaats: {pct(ar[2] or 0)}")
        print(f"  total:           {total_ar:,}")
        print("\nLaatste 5 etl_run entries:")
        for row in _rows(conn,
            f"SELECT processed_date, record_count, status, error "
            f"FROM {prefix}etl_run ORDER BY processed_date DESC LIMIT 5", kind):
            print(f"  {row[0]}  count={(row[1] or 0):>5}  status={row[2]}  err={row[3] or ''}")
        return 0
    finally:
        conn.close()


def cmd_backfill_fields(args: argparse.Namespace) -> int:
    """Backfill van vier velden op bestaande records, **zonder her-crawl**:

    - Drie SRU-velden (`pdf_url`, `datum_publicatie_ts`, `subject_taxonomie`)
      worden uit `raw_xml` opnieuw geparsed.
    - `datum_ontvangst` wordt uit `inhoud_tekst` van enriched records
      geëxtraheerd (alleen records met `inhoud_geladen_at IS NOT NULL`).

    Idempotent: records die de velden al gevuld hebben worden overgeslagen
    (via WHERE-clauses). Batched commits per `--batch` records.
    """
    if args.db != "postgres":
        log.error("backfill-fields only supports --db postgres for now")
        return 1

    conn = pg_get_conn()
    try:
        sru_updated = _backfill_sru_fields(conn, args.batch)
        log.info("SRU-velden bijgewerkt op %d records", sru_updated)
        ontv_updated = _backfill_datum_ontvangst(conn, args.batch)
        log.info("datum_ontvangst bijgewerkt op %d records", ontv_updated)
        return 0
    finally:
        conn.close()


def _backfill_sru_fields(conn, batch_size: int) -> int:
    """Re-parse raw_xml voor records met ontbrekende SRU-velden."""
    total = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT koop_id, raw_xml FROM vth.vergunningkennisgeving "
                "WHERE pdf_url IS NULL "
                "   OR datum_publicatie_ts IS NULL "
                "   OR subject_taxonomie IS NULL "
                "LIMIT %s",
                (batch_size,),
            )
            todo = cur.fetchall()
        if not todo:
            break

        rows: list[tuple[Optional[str], Optional[str], Optional[str], str]] = []
        for r in todo:
            koop_id = r["koop_id"]
            try:
                root = ET.fromstring(r["raw_xml"])
            except ET.ParseError as e:
                log.warning("  %s: ParseError, skipping: %s", koop_id, e)
                continue
            pdf_url = None
            for item in root.findall(".//gzd:itemUrl", NS):
                if item.get("manifestation") == "pdf":
                    pdf_url = text_of(item)
                    break
            ts_work = text_of(
                root.find(".//cup:datumTijdstipWijzigingWork", NS)
            )
            subject = None
            for s in root.findall(".//dcterms:subject", NS):
                if s.get("scheme") == "OVERHEID.TaxonomieBeleidsagendaDecentraal":
                    subject = text_of(s)
                    break
            rows.append((pdf_url, ts_work, subject, koop_id))

        if not rows:
            break
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE vth.vergunningkennisgeving SET "
                "  pdf_url = COALESCE(pdf_url, %s), "
                "  datum_publicatie_ts = COALESCE(datum_publicatie_ts, %s::timestamptz), "
                "  subject_taxonomie = COALESCE(subject_taxonomie, %s) "
                "WHERE koop_id = %s",
                rows,
            )
        conn.commit()
        total += len(rows)
        log.info("  SRU-backfill: %d records (running total: %d)", len(rows), total)
        if len(todo) < batch_size:
            break
    return total


def _backfill_datum_ontvangst(conn, batch_size: int) -> int:
    """Extract datum_ontvangst uit inhoud_tekst voor enriched records."""
    total = 0
    last_id = ""
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT koop_id, inhoud_tekst FROM vth.vergunningkennisgeving "
                "WHERE inhoud_tekst IS NOT NULL "
                "  AND datum_ontvangst IS NULL "
                "  AND koop_id > %s "
                "ORDER BY koop_id "
                "LIMIT %s",
                (last_id, batch_size),
            )
            todo = cur.fetchall()
        if not todo:
            break

        rows: list[tuple[Optional[str], str]] = []
        for r in todo:
            d = extract_datum_ontvangst(r["inhoud_tekst"])
            if d:
                rows.append((d, r["koop_id"]))
            last_id = r["koop_id"]

        if rows:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE vth.vergunningkennisgeving "
                    "SET datum_ontvangst = %s::date WHERE koop_id = %s",
                    rows,
                )
            conn.commit()
            total += len(rows)
        log.info("  datum_ontvangst-backfill: %d hits in batch (running total: %d)",
                 len(rows), total)
        if len(todo) < batch_size:
            break
    return total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    p_setup = sub.add_parser("setup", help="create vth schema in Postgres (runs schema.sql)")
    p_setup.set_defaults(func=cmd_setup)

    p_run = sub.add_parser("run", help="Ingest one or more days")
    p_run.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD inclusive")
    p_run.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD inclusive")
    p_run.add_argument("--db", choices=["postgres", "sqlite"], default="postgres",
                       help="Persistence backend (default: postgres)")
    p_run.add_argument("--force", action="store_true", help="Re-ingest days already marked ok")
    p_run.set_defaults(func=cmd_run)

    p_st = sub.add_parser("status", help="Show DB and run summary")
    p_st.add_argument("--db", choices=["postgres", "sqlite"], default="postgres",
                      help="Persistence backend (default: postgres)")
    p_st.set_defaults(func=cmd_status)

    p_en = sub.add_parser(
        "enrich",
        help="Fetch full publication XML and fill inhoud_tekst/zaaknummer_bg",
    )
    p_en.add_argument("--db", choices=["postgres", "sqlite"], default="postgres",
                      help="Persistence backend (only postgres supported)")
    p_en.add_argument("--limit", type=int, default=100,
                      help="Max records per batch (default 100)")
    p_en.add_argument("--loop", action="store_true",
                      help="Keep running until no pending records remain "
                           "(useful when run in parallel with `run`)")
    p_en.add_argument("--sleep", type=int, default=60,
                      help="Sleep seconds between empty cycles in --loop mode")
    p_en.add_argument("--stop-after-empty", type=int, default=5,
                      help="Stop after N consecutive empty cycles in --loop mode")
    p_en.add_argument("--type-besluit", default=None,
                      help="Filter op type_besluit (comma-separated, bv. "
                           "'verleend,geweigerd,ontwerp,van_rechtswege'). "
                           "Records buiten de filter blijven NULL en kunnen "
                           "later met een nieuwe run zonder filter worden "
                           "opgepakt — true incremental.")
    p_en.set_defaults(func=cmd_enrich)

    p_bf = sub.add_parser(
        "backfill-fields",
        help="Vul de vier 2026-05-20-velden op bestaande records (geen her-crawl)",
    )
    p_bf.add_argument("--db", choices=["postgres", "sqlite"], default="postgres",
                      help="Persistence backend (only postgres supported)")
    p_bf.add_argument("--batch", type=int, default=2000,
                      help="Records per batch / commit (default 2000)")
    p_bf.set_defaults(func=cmd_backfill_fields)

    args = p.parse_args()
    if args.cmd is None:
        p.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
