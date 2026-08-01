"""Async Postgres access.

One pool per process, reused across warm serverless invocations. Opening a pool
per request would exhaust Neon's pooler under any concurrency at all.

Neon's pooled endpoint runs PgBouncer in transaction mode. Two consequences that
callers must respect: session-level advisory locks are unavailable (claim work
with FOR UPDATE SKIP LOCKED instead), and SQL-level PREPARE is unavailable
(psycopg's protocol-level prepared statements are fine).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from pipeline.config import load_settings

_pool: AsyncConnectionPool | None = None


def get_pool() -> AsyncConnectionPool:
    """The process-wide pool, created on first use."""
    global _pool
    if _pool is None:
        settings = load_settings()
        _pool = AsyncConnectionPool(
            settings.database_url,
            # Deliberately small. Each function instance serves one request at a
            # time and Neon's pooler is the real multiplexer, so a large local
            # pool would only hold server-side connections open for nothing.
            min_size=0,
            max_size=4,
            # Opened lazily inside an event loop; constructing a pool with
            # open=True outside one is an error.
            open=False,
            kwargs={"row_factory": dict_row},
        )
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[AsyncConnection]:
    """A pooled connection. Statements autocommit unless wrapped in a transaction."""
    pool = get_pool()
    await pool.open()
    async with pool.connection() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    """A pooled connection inside a transaction; rolls back if the block raises."""
    async with connection() as conn:
        async with conn.transaction():
            yield conn


async def close_pool() -> None:
    """Close and discard the pool.

    Used by tests and by orderly shutdown. Production code does not call this:
    the pool is meant to outlive individual invocations.
    """
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
