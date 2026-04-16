"""Backfill inhoud_plain column on p2p.tekst_element."""
import os
import psycopg
from dotenv import load_dotenv

load_dotenv()
dsn = os.environ.get("DATABASE_URL", "")
print(f"Connecting to DB...")

with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE p2p.tekst_element
            ADD COLUMN IF NOT EXISTS inhoud_plain TEXT
        """)
    conn.commit()
    print("Column added/verified.")

    with conn.cursor() as cur:
        cur.execute(r"""
            UPDATE p2p.tekst_element
            SET inhoud_plain = trim(regexp_replace(
                regexp_replace(inhoud, E'<[^>]+>', ' ', 'g'),
                E'\s+', ' ', 'g'
            ))
            WHERE inhoud IS NOT NULL AND inhoud_plain IS NULL
        """)
        print(f"Backfilled {cur.rowcount} rows.")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_tekst_element_inhoud_fts
            ON p2p.tekst_element
            USING gin (to_tsvector('dutch', coalesce(inhoud_plain, '')))
        """)
    conn.commit()
    print("FTS index created.")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM p2p.tekst_element WHERE inhoud_plain IS NOT NULL")
        row = cur.fetchone()
        print(f"Rows with inhoud_plain: {row[0]}")
