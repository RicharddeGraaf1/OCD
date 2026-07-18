"""MER-loader — kopieert de mer-register.nl harvest-store (SQLite) naar OCD-
Postgres, schema `mer`.

De harvest zelf (KOOP SRU MER-events + Commissie m.e.r.-projecten) leeft in de
mer-register.nl-repo en schrijft naar een lokale SQLite. Deze loader is de brug
naar OCD: hij past MER_DDL toe en upsert idempotent event/project/document/link.
Twee logische bronnen voor het data-actualiteit-dashboard:

  - `mer-events`     → mer.event    (Kanaal A, KOOP SRU)
  - `mer-commissie`  → mer.project  (Kanaal B, Commissie m.e.r.) + documenten/links

Provincie/lat/lon komen uit de SQLite-geocache (join op bevoegd_gezag).
`start_advisering`/`lastmod` blijven NULL (vrije tekst in de bron).
"""
import sqlite3
from collections import Counter
from pathlib import Path

from rich.console import Console

from src.db import get_conn
from src.ddl import MER_DDL

console = Console()

# Default-pad naar de harvest-store; overschrijfbaar via de CLI.
DEFAULT_SQLITE = Path(r"c:/GIT/MER-register.nl/harvest/data/mer.db")


def setup_mer() -> None:
    """Pas MER_DDL toe (idempotent)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(MER_DDL)
        conn.commit()
        console.print("[green]MER-schema toegepast (schema mer).[/green]")
    finally:
        conn.close()


def _sqlite_rows(sq, q, params=()):
    cur = sq.execute(q, params)
    return cur.fetchall()


def _project_instrument(sq) -> dict[str, str]:
    """Instrument per project = meerderheid van de gekoppelde event-instrumenten."""
    per: dict[str, Counter] = {}
    for slug, instr in sq.execute(
        """SELECT l.project_slug, e.instrument
           FROM project_event_link l JOIN event e ON e.koop_id = l.koop_id
           WHERE e.instrument IS NOT NULL""").fetchall():
        per.setdefault(slug, Counter())[instr] += 1
    return {slug: c.most_common(1)[0][0] for slug, c in per.items()}


def load_mer_events(sqlite_path: Path | None = None) -> int:
    """Kanaal A: kopieer mer.event uit de SQLite-store. Retourneert het aantal."""
    sqlite_path = Path(sqlite_path or DEFAULT_SQLITE)
    if not sqlite_path.exists():
        raise FileNotFoundError(f"MER SQLite niet gevonden: {sqlite_path}")
    sq = sqlite3.connect(sqlite_path)
    conn = get_conn()
    try:
        ev = _sqlite_rows(sq, """SELECT koop_id, titel, datum_publicatie, publicatieblad,
                                        bevoegd_gezag_naam, event_type, instrument,
                                        subject_taxonomie, url FROM event""")
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO mer.event
                     (koop_id, titel, datum_publicatie, publicatieblad, bevoegd_gezag_naam,
                      event_type, instrument, subject_taxonomie, url)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (koop_id) DO UPDATE SET
                     titel=EXCLUDED.titel, event_type=EXCLUDED.event_type,
                     instrument=EXCLUDED.instrument, datum_publicatie=EXCLUDED.datum_publicatie""",
                ev)
        conn.commit()
        console.print(f"  mer.event: {len(ev)} upserts")
        return len(ev)
    finally:
        conn.close()
        sq.close()


def load_mer_commissie(sqlite_path: Path | None = None) -> int:
    """Kanaal B: kopieer mer.project + document + link. Retourneert het aantal projecten."""
    sqlite_path = Path(sqlite_path or DEFAULT_SQLITE)
    if not sqlite_path.exists():
        raise FileNotFoundError(f"MER SQLite niet gevonden: {sqlite_path}")
    sq = sqlite3.connect(sqlite_path)
    conn = get_conn()
    try:
        instr = _project_instrument(sq)

        pr = _sqlite_rows(sq, """SELECT p.slug, p.project_nr, p.titel, p.bevoegd_gezag,
                                        p.initiatiefnemer, g.provincie, g.lat, g.lon, p.url
                                 FROM project p
                                 LEFT JOIN geocache g ON g.naam = trim(p.bevoegd_gezag)""")
        proj_rows = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], instr.get(r[0]))
                     for r in pr]
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO mer.project
                     (slug, project_nr, titel, bevoegd_gezag, initiatiefnemer,
                      provincie, lat, lon, url, instrument)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (slug) DO UPDATE SET
                     titel=EXCLUDED.titel, bevoegd_gezag=EXCLUDED.bevoegd_gezag,
                     initiatiefnemer=EXCLUDED.initiatiefnemer, provincie=EXCLUDED.provincie,
                     lat=EXCLUDED.lat, lon=EXCLUDED.lon, instrument=EXCLUDED.instrument""",
                proj_rows)

            dc = _sqlite_rows(sq, "SELECT project_slug, soort, bestandsnaam, url FROM document")
            cur.executemany(
                """INSERT INTO mer.document (project_slug, soort, bestandsnaam, url)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (project_slug, bestandsnaam) DO NOTHING""", dc)

            lk = _sqlite_rows(sq, """SELECT project_slug, koop_id, match_methode, zekerheid
                                     FROM project_event_link""")
            cur.executemany(
                """INSERT INTO mer.project_event_link
                     (project_slug, koop_id, match_methode, zekerheid)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (project_slug, koop_id) DO UPDATE SET zekerheid=EXCLUDED.zekerheid""",
                lk)
        conn.commit()
        console.print(f"  mer.project: {len(proj_rows)} · document: {len(dc)} · link: {len(lk)}")
        return len(proj_rows)
    finally:
        conn.close()
        sq.close()
