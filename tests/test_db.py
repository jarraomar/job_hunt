import psycopg
import pytest
import pytest_asyncio

from pipeline.db import close_pool, connection, transaction
from scripts.migrate import apply_migrations


@pytest_asyncio.fixture
async def pooled(db, migrated_db, monkeypatch):
    """Point the module-level pool at the test database, and tear it down after.

    The pool is a process-wide singleton by design (it must survive across warm
    serverless invocations), so a test that opens it has to close it.
    """
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    await close_pool()
    yield
    await close_pool()


async def test_connection_yields_dict_rows(pooled):
    async with connection() as conn:
        cur = await conn.execute("SELECT 1 AS answer")
        row = await cur.fetchone()
    assert row["answer"] == 1


async def test_transaction_commits_on_success(pooled):
    async with transaction() as conn:
        await conn.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    async with connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM companies")
        assert (await cur.fetchone())["n"] == 1


async def test_transaction_rolls_back_on_error(pooled):
    with pytest.raises(RuntimeError):
        async with transaction() as conn:
            await conn.execute(
                "INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')"
            )
            raise RuntimeError("boom")
    async with connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM companies")
        assert (await cur.fetchone())["n"] == 0


async def test_apply_migrations_is_idempotent(db, migrations_dir):
    # The session fixture already applied 001; a second call must be a no-op.
    assert await apply_migrations(db, migrations_dir) == []


async def test_migration_created_expected_tables(db):
    cur = await db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    names = {r["tablename"] for r in await cur.fetchall()}
    assert {"companies", "jobs", "run_log", "http_cache", "schema_version"} <= names


async def test_jobs_fingerprint_is_unique(db):
    await db.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    sql = (
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at)"
        " VALUES (%s, 1, %s, %s, 'Engineer', 'desc', 'https://x', now(), now())"
    )
    await db.execute(sql, ("fp1", "greenhouse", "1"))
    # Same content from a different source must collide: this is what makes a
    # repost collapse instead of appearing as a new job (spec section 5).
    with pytest.raises(psycopg.errors.UniqueViolation):
        await db.execute(sql, ("fp1", "lever", "2"))


async def test_jobs_source_id_pair_is_unique(db):
    await db.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    sql = (
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at)"
        " VALUES (%s, 1, 'greenhouse', '1', 'Engineer', 'desc', 'https://x', now(), now())"
    )
    await db.execute(sql, ("fp1",))
    with pytest.raises(psycopg.errors.UniqueViolation):
        await db.execute(sql, ("fp2",))


async def test_filter_reason_required_when_filtered(db):
    await db.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    sql = (
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at, filtered_out, filter_reason)"
        " VALUES (%s, 1, 'greenhouse', %s, 'Engineer', 'd', 'https://x', now(), now(), %s, %s)"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(sql, ("fp-bad", "1", True, None))


async def test_filter_reason_rejected_when_not_filtered(db):
    await db.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    sql = (
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at, filtered_out, filter_reason)"
        " VALUES (%s, 1, 'greenhouse', %s, 'Engineer', 'd', 'https://x', now(), now(), %s, %s)"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(sql, ("fp-bad2", "2", False, "salary_below_floor"))


async def test_timestamps_round_trip_as_aware_datetimes(db):
    """A naive datetime anywhere in this system is a bug (Global Constraints)."""
    await db.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    await db.execute(
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at)"
        " VALUES ('fp-tz', 1, 'greenhouse', '1', 'Engineer', 'd', 'https://x', now(), now())"
    )
    cur = await db.execute("SELECT first_seen_at FROM jobs WHERE fingerprint = 'fp-tz'")
    row = await cur.fetchone()
    assert row["first_seen_at"].tzinfo is not None
