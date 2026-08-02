import json
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
_TABLES = (
    "companies, jobs, run_log, http_cache, source_state, scores, llm_spend,"
    " applications, answer_bank, unmapped_questions"
)


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


# A profile that exists only in the test process. Without it the web tests fall
# through to ~/.local/share/jobhunt/profile and pass or fail depending on whose
# laptop they run on -- and on CI, where that directory does not exist, the
# application-prep panel would silently never render.
TEST_PROFILE = json.dumps(
    {
        "resume": {
            "summary": "Full stack engineer.",
            "experience": [{"company": "CloudBase", "title": "Engineer", "start": "2021-01"}],
        },
        "identity": {
            "name": "Test Person",
            "email": "test@example.com",
            "phone": "(555) 555-5555",
            "city": "San Leandro",
            "state": "California",
            "work_authorization": "US-born citizen",
        },
    }
)


@pytest.fixture(autouse=True)
def isolated_profile(monkeypatch, tmp_path):
    """Never read the developer's real profile directory.

    PROFILE_JSON wins over disk in pipeline/profile.py, and pointing the
    directory at an empty tmp_path closes the fallback as well.
    """
    monkeypatch.setenv("PROFILE_JSON", TEST_PROFILE)
    monkeypatch.setenv("JOBHUNT_PROFILE_DIR", str(tmp_path / "no-profile-here"))


@pytest_asyncio.fixture
async def web_db(db, monkeypatch):
    """A `db` connection plus the app pointed at the same database.

    The route handlers call pipeline.db.connection() directly rather than
    through a FastAPI dependency, so there is nothing to override -- the pool
    has to be aimed at the test database and then disposed, or it would leak
    across tests still holding the previous DSN.
    """
    import pipeline.db as db_module

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    await db_module.close_pool()
    try:
        yield db
    finally:
        await db_module.close_pool()


@pytest_asyncio.fixture
async def client(web_db):
    """An HTTP client that shares the test's event loop.

    Deliberately not starlette's TestClient: that runs the app in its own loop
    via a portal thread, and the async connection pool cannot be used across
    loops ("The future belongs to a different loop"). ASGITransport calls the
    app in-process on this loop instead.
    """
    import httpx

    from api.index import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def seed_job(db):
    """Insert a company + job + score, returning the job_id.

    Shared by every web test; lives here because `tests` is not an importable
    package, so cross-file imports of a helper do not work.
    """

    async def _seed(
        *,
        title="Backend Engineer",
        company="Acme",
        score=0.8,
        sharia="allowed",
        status=None,
        judged=True,
    ) -> int:
        cur = await db.execute(
            "INSERT INTO companies (name, normalized_name, sharia_verdict, sharia_source,"
            " sharia_reason) VALUES (%s, %s, %s, 'llm', 'stated reason')"
            " ON CONFLICT (normalized_name) DO UPDATE SET"
            " sharia_verdict = EXCLUDED.sharia_verdict RETURNING company_id",
            (company, company.lower().replace(" ", "-"), sharia),
        )
        company_id = (await cur.fetchone())["company_id"]

        cur = await db.execute(
            "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title, location,"
            " remote_type, salary_min, salary_max, salary_source, description, apply_url,"
            " posted_at, first_seen_at, last_seen_at)"
            " VALUES (%s, %s, 'greenhouse', %s, %s, 'Remote', 'remote', 150000, 180000,"
            " 'structured', 'Build things with Python.', 'https://example.com/1', now(),"
            " now(), now()) RETURNING job_id",
            (f"{company}-{title}".ljust(64, "x")[:64], company_id, f"{company}-{title}", title),
        )
        job_id = (await cur.fetchone())["job_id"]

        await db.execute(
            "INSERT INTO scores (job_id, total_score, embed_similarity, rule_score,"
            " freshness_score, relevance_verdict, rationale, model, judged_at)"
            " VALUES (%s, %s, 0.7, 0.6, 1.0,"
            " CASE WHEN %s THEN 'strong' END,"
            " CASE WHEN %s THEN 'Matches your Python and AWS work.' END,"
            " CASE WHEN %s THEN 'claude-haiku-4-5' END,"
            " CASE WHEN %s THEN now() END)",
            (job_id, score, judged, judged, judged, judged),
        )
        if status:
            await db.execute(
                "INSERT INTO applications (job_id, status, applied_at) VALUES (%s, %s,"
                " CASE WHEN %s IN ('queued','dismissed') THEN NULL ELSE now() END)",
                (job_id, status, status),
            )
        return job_id

    return _seed
