"""Postgres connection pool.

Managed databases (Neon, Supabase, RDS) drop idle connections, so the pool is
configured to check a connection before handing it out and to reconnect on its
own. Every caller gets dict rows.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import ConfigError, get_settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    """Runs once per physical connection."""
    timeout = get_settings().db_statement_timeout_ms
    if timeout > 0:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(timeout)}")
    conn.commit()


def get_pool() -> ConnectionPool:
    """Open the pool on first use and reuse it afterwards."""
    global _pool
    if _pool is None:
        settings = get_settings()
        conninfo = settings.require_database_url()
        _pool = ConnectionPool(
            conninfo=conninfo,
            min_size=settings.db_pool_min,
            max_size=max(settings.db_pool_min, settings.db_pool_max),
            kwargs={"row_factory": dict_row, "application_name": "claudejobs"},
            configure=_configure,
            check=ConnectionPool.check_connection,
            open=False,
            name="claudejobs",
        )
        try:
            _pool.open(wait=True, timeout=15)
        except Exception as exc:  # pool failed to reach the database
            _pool = None
            raise ConfigError(
                f"Could not connect to Postgres: {exc}\n"
                "Check DATABASE_URL in .env — for hosted databases the string "
                "usually needs ?sslmode=require. Verify with: claudejobs selfcheck"
            ) from exc
        log.info("database pool ready (min=%s max=%s)", settings.db_pool_min, settings.db_pool_max)
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection wrapped in a transaction.

    The transaction commits when the block exits normally and rolls back if it
    raises, which is what every caller in this codebase wants.
    """
    with get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ping() -> str:
    """Round-trip the database and return its version (used by selfcheck/health)."""
    with connection() as conn:
        row = conn.execute("SELECT version() AS version").fetchone()
    return str(row["version"]) if row else "unknown"
