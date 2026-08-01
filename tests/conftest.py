import os
from pathlib import Path

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from scripts.migrate import apply_migrations

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
TEST_DSN = os.environ.get(
    "JOBHUNT_TEST_DATABASE_URL",
    "postgresql://jobhunt@localhost:5433/jobhunt_test",
)

# Every table 001_initial.sql creates. schema_version is deliberately excluded:
# truncating it would make the session-scoped migration re-run.
_TABLES = "companies, jobs, run_log, http_cache, source_state"


@pytest_asyncio.fixture(scope="session")
async def migrated_db():
    """Apply migrations once for the whole session.

    Migrating per test would dominate the suite's runtime for no added coverage.
    """
    conn = await AsyncConnection.connect(TEST_DSN, autocommit=True)
    try:
        await apply_migrations(conn, MIGRATIONS)
    finally:
        await conn.close()
    return TEST_DSN


@pytest_asyncio.fixture
async def db(migrated_db):
    """A clean connection per test. RESTART IDENTITY keeps generated IDs predictable."""
    conn = await AsyncConnection.connect(migrated_db, autocommit=True, row_factory=dict_row)
    await conn.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrations_dir() -> Path:
    return MIGRATIONS
