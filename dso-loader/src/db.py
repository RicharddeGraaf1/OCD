"""Database connection and helpers."""

import psycopg
from psycopg.rows import dict_row
from src.config import cfg


def get_conn() -> psycopg.Connection:
    """Get a new database connection."""
    return psycopg.connect(cfg.db_url, row_factory=dict_row, autocommit=False)


def execute_sql_file(conn: psycopg.Connection, sql: str) -> None:
    """Execute a multi-statement SQL string."""
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def table_count(conn: psycopg.Connection, table: str) -> int:
    """Quick row count for a table."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM dso.{table}")  # noqa: S608
        row = cur.fetchone()
        return row["n"] if row else 0
