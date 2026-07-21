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
# pool leegtrekt. Default 20s (was 10s) — de exacte-count-endpoints (o.a.
# v_bron_totalen voor het dashboard) zitten normaal ~2s maar tikken bij een
# koude buffercache tegen 10s aan; 20s geeft marge. Overrideable per request
# via `SET LOCAL statement_timeout`.
STATEMENT_TIMEOUT_MS = int(os.environ.get("OCD_STATEMENT_TIMEOUT_MS", "20000"))


def _configure_connection(conn):
    """Per-connection setup: timeout afdwingen zodra een conn uit de pool komt."""
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        # Parallelle query-workers uit: onder eval-load (veel gelijktijdige
        # spatiale/FTS-queries) putten de dynamic-shared-memory-segmenten de
        # kleine Docker-/dev/shm (64MB) uit -> psycopg DiskFull "could not
        # resize shared memory segment ... No space left on device" -> 500.
        # Non-parallel plannen vermijden dsm volledig en zijn deterministischer
        # (stabielere eval-meting); de subdiv-spatiale index maakt de queries
        # ook non-parallel snel genoeg.
        cur.execute("SET max_parallel_workers_per_gather = 0")
    conn.commit()


pool = ConnectionPool(
    DATABASE_URL,
    kwargs={"row_factory": dict_row},
    min_size=2,
    max_size=20,
    open=False,
    configure=_configure_connection,
)


@contextmanager
def get_conn():
    with pool.connection() as conn:
        yield conn
