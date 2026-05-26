import os
from contextlib import contextmanager
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5434/dso",
)

# Statement timeout op DB-niveau: voorkomt dat één slechte ST_Intersects de
# pool leegtrekt. Default 10s, overrideable per request via
# `SET LOCAL statement_timeout`.
STATEMENT_TIMEOUT_MS = int(os.environ.get("OCD_STATEMENT_TIMEOUT_MS", "10000"))


def _configure_connection(conn):
    """Per-connection setup: timeout afdwingen zodra een conn uit de pool komt."""
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    conn.commit()


pool = ConnectionPool(
    DATABASE_URL,
    kwargs={"row_factory": dict_row},
    min_size=2,
    max_size=10,
    open=False,
    configure=_configure_connection,
)


@contextmanager
def get_conn():
    with pool.connection() as conn:
        yield conn
