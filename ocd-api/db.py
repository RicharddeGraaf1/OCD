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

pool = ConnectionPool(
    DATABASE_URL,
    kwargs={"row_factory": dict_row},
    min_size=2,
    max_size=10,
    open=False,
)


@contextmanager
def get_conn():
    with pool.connection() as conn:
        yield conn
