"""
Database connection.

One connection POOL per database URL: connections are opened once and reused
(opening a new Postgres connection for every query is slow).
Vectors are sent/received as numpy arrays thanks to pgvector's adapter.
"""
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from ingestion.config import settings

_pools: dict[str, ConnectionPool] = {}


def _configure(conn: psycopg.Connection) -> None:
    """Runs on every new pooled connection: teach it the 'vector' type (if the extension exists yet)."""
    try:
        register_vector(conn)
    except psycopg.ProgrammingError:
        pass                     # extension not created yet (before the first migration)
    conn.commit()


def get_pool(url: str | None = None) -> ConnectionPool:
    url = url or settings.database_url
    if url not in _pools:
        _pools[url] = ConnectionPool(url, min_size=1, max_size=5, configure=_configure,
                                     kwargs={"connect_timeout": 5}, open=True)
    return _pools[url]


@contextmanager
def connect(url: str | None = None):
    """
    with connect() as conn:  ... all statements run in ONE transaction:
    committed if the block finishes, rolled back if anything raises.
    """
    with get_pool(url).connection() as conn:
        yield conn


def close_all() -> None:
    for pool in _pools.values():
        pool.close()
    _pools.clear()


def can_connect(url: str | None = None, timeout: int = 2) -> bool:
    try:
        with psycopg.connect(url or settings.database_url, connect_timeout=timeout):
            return True
    except Exception:
        return False
