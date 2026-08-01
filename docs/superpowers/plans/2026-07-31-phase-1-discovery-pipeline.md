# Phase 1: Discovery Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Revised 2026-08-01** — deployment target changed from EC2/SQLite to Vercel/Neon Postgres. See the Revision Log below for exactly which tasks changed.

**Goal:** A fully-tested async Python package that ingests jobs from public ATS and aggregator APIs, deduplicates them, applies the deterministic pre-filter, and persists results to Postgres with a per-run log. Runnable both as a CLI locally and from a bounded serverless invocation.

**Architecture:** One Python package, `pipeline/`. Every source is a module implementing a common async `Source` protocol and returning `RawJob` objects; `normalize.py` converts those to canonical `Job` objects with a content-derived fingerprint; `store.py` upserts into Postgres; `filters/prefilter.py` applies free deterministic rules. All outbound HTTP goes through a single `PoliteSession` that enforces per-host rate limiting, conditional requests, and backoff. No LLM calls, no document generation, no web UI in this phase.

**Tech Stack:** Python 3.14, httpx (async), psycopg 3 (async, binary), PyYAML, pytest, pytest-asyncio, pytest-httpx, PostgreSQL 16.

## Revision Log

The architecture change (spec §4, §5, §13) landed after this plan was first written. Impact by task:

| Task | Status |
|---|---|
| 1 — Scaffolding and DB layer | **Rewritten.** psycopg/Postgres instead of `sqlite3`; migrations run from a script, not at startup; Docker Postgres for tests. |
| 2 — Salary parsing | Unchanged. Pure functions over strings. |
| 3 — Models, normalize, fingerprint | **Minor.** Timestamps become `datetime` (tz-aware) instead of ISO strings. |
| 4 — PoliteSession | **Rewritten as async.** `httpx.AsyncClient`; the sleep becomes `await asyncio.sleep`. Behavior identical. |
| 5–7 — Source adapters | **Minor.** `fetch()` becomes `async def`; parsing logic untouched. |
| 8 — Pre-filter | Unchanged. Pure functions over `Job`. |
| 9 — Persistence | **Rewritten.** Postgres dialect, `ON CONFLICT … RETURNING`, async connections. |
| 10 — Orchestration | **Rewritten.** Adds the wall-clock budget and `budget_hit`; exposes `run()` for both CLI and the cron route. |

Task ordering and test intent are unchanged. Where a task below still says SQLite, the Revision Log governs.

## Global Constraints

These apply to every task. Copied from `docs/superpowers/specs/2026-07-31-job-hunt-system-design.md`.

- **No browser dependency may enter the tree, ever.** Playwright, Selenium, Puppeteer, and pyppeteer are prohibited. This is the structural enforcement of spec §3.
- **No authenticated job-platform requests.** Public unauthenticated endpoints only. LinkedIn, Indeed, Glassdoor, ZipRecruiter, and Wellfound are out of scope entirely.
- **All outbound HTTP goes through `PoliteSession`.** A bare `httpx.get` in a source module is a defect.
- **No native-library dependencies.** Anything requiring `apt-get` cannot be installed in the Vercel Python runtime. This is why WeasyPrint is out (spec §10.2) and why `psycopg[binary]` is specified rather than source-built psycopg.
- **Every write must be idempotent.** Vercel delivers cron duplicates; running a pass twice must converge, never accumulate (spec §4.2).
- **Connect via Neon's pooled endpoint** — so no session-level advisory locks and no SQL-level `PREPARE`. Claim work with `FOR UPDATE SKIP LOCKED`.
- **Workday gets a 2.0s/host delay; everything else 1.0s** (spec §7).
- Salary floor: **125000**. Target: 150000.
- Home location for proximity ranking: **San Leandro, CA**.
- `profile/` is gitignored and must never be committed. `profile.example/` holds committed templates with no personal data.
- Python **3.14**, pinned via `requires-python` in `pyproject.toml`. Vercel supports 3.12–3.14 and silently defaults to 3.12 when unpinned, so the pin is what keeps local and deployed interpreters identical. Type hints on all public functions.
- Timestamps are **tz-aware `datetime`** in Python and `TIMESTAMPTZ` in Postgres. Never a naive datetime, never a string.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, pytest config, `[tool.vercel]` entrypoint |
| `.gitignore` | Excludes `profile/`, `seed/`, `.env*`, `.vercel/` |
| `scripts/dev_db.sh` | Local Postgres 16 cluster for tests (up/down/reset/status) |
| `migrations/001_initial.sql` | Phase-1 subset of the spec §5 schema |
| `scripts/migrate.py` | Applies pending migrations; run by CI, never at request time |
| `pipeline/config.py` | `Settings` dataclass, env loading |
| `pipeline/db.py` | Async connection pool, transaction helper |
| `pipeline/models.py` | `RawJob`, `Job` dataclasses |
| `pipeline/salary.py` | Free-text salary parsing |
| `pipeline/normalize.py` | Normalization, fingerprinting, `RawJob`→`Job` |
| `pipeline/http.py` | `PoliteSession`: rate limit, ETag, backoff, 403 stop |
| `pipeline/sources/base.py` | `Source` protocol, `SourceConfig` |
| `pipeline/sources/registry.py` | Name→source mapping |
| `pipeline/sources/greenhouse.py` | Greenhouse board adapter |
| `pipeline/sources/lever.py` | Lever postings adapter |
| `pipeline/sources/ashby.py` | Ashby job-board adapter |
| `pipeline/sources/remotive.py` | Remotive aggregator adapter |
| `pipeline/sources/hn_algolia.py` | HN "Who is Hiring" adapter |
| `pipeline/store.py` | Upserts, run_log |
| `pipeline/filters/prefilter.py` | Deterministic pre-filter |
| `pipeline/run_discover.py` | Orchestration, wall-clock budget, CLI |
| `scripts/capture_fixture.py` | Records live API responses as test fixtures |
| `tests/` | Mirrors the package layout |

**Fixtures are captured from live endpoints, never hand-written.** Task 5 builds `scripts/capture_fixture.py` for this. Hand-writing a fixture from memory bakes in a guess about an API's shape; capturing it makes the tests reflect reality.

**Deployment wiring (`api/index.py`, `vercel.json`, `.github/workflows/`) is Phase 0, not Phase 1.** Phase 1 is a package with a CLI and a test suite; it must run green locally against Docker Postgres before any of it is deployed.

---

### Task 1: Project scaffolding and database layer

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `docker-compose.yml`, `migrations/001_initial.sql`, `pipeline/__init__.py`, `pipeline/db.py`, `pipeline/config.py`, `scripts/__init__.py`, `scripts/migrate.py`
- Test: `tests/conftest.py`, `tests/test_db.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `pipeline.db.get_pool() -> AsyncConnectionPool` — process-wide singleton, opened lazily
  - `pipeline.db.connection() -> AsyncContextManager[AsyncConnection]` — a pooled connection with `dict_row` rows
  - `pipeline.db.transaction() -> AsyncContextManager[AsyncConnection]` — connection inside an explicit transaction
  - `pipeline.db.close_pool() -> None`
  - `scripts.migrate.apply_migrations(conn: AsyncConnection, migrations_dir: Path) -> list[str]` — returns names applied this call
  - `pipeline.config.Settings` frozen dataclass: `database_url: str`, `migrations_dir: Path`, `profile_dir: Path`, `salary_floor: int`, `home_city: str`, `home_state: str`, `user_agent: str`, `run_budget_seconds: float`
  - `pipeline.config.load_settings(env: Mapping[str, str] | None = None) -> Settings`

**Why the pool is a module singleton.** A serverless instance handles many invocations over its life. Opening a pool per request would exhaust Neon's pooler; opening one per process and reusing it across warm invocations is the shape the platform expects.

- [ ] **Step 1: Create the project skeleton**

`pyproject.toml`:

```toml
[project]
name = "jobhunt"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.27",
    "psycopg[binary,pool]>=3.2",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-httpx>=0.30",
    "ruff>=0.6",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["pipeline*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
asyncio_mode = "auto"
```

`.gitignore`:

```
profile/
seed/
.env
.env.*
.vercel/
venv/
__pycache__/
*.egg-info/
.pytest_cache/
.ruff_cache/
```

`scripts/dev_db.sh` — the test database. Tests run against a real engine because `SKIP LOCKED`, partial unique indexes, `ON CONFLICT`, and `CHECK` constraints have no equivalent in a stub (spec §17).

The dev Mac has neither Docker nor a Postgres server (only libpq's client tools), so this manages a dedicated cluster directly: `brew install postgresql@16`, then `initdb` into a gitignored `.pgdata/` on **port 5433** so it cannot collide with any other Postgres. CI uses a `postgres:16` service container instead and never runs this script. Subcommands: `up`, `down`, `reset`, `status`.

Create empty `pipeline/__init__.py` and `scripts/__init__.py`. **Re-run `pip install -e ".[dev]"` after they exist** — an editable install performed before the packages are created finds nothing to map, and every later import fails.

- [ ] **Step 2: Write the migration**

`migrations/001_initial.sql` — the Phase-1 subset of spec §5. Later phases add `scores`, `applications`, `work_queue`, `answer_bank`, `unmapped_questions`, and `llm_spend` in their own migrations.

```sql
CREATE TABLE companies (
  company_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name              TEXT NOT NULL,
  normalized_name   TEXT NOT NULL UNIQUE,
  domain            TEXT,
  ats_type          TEXT,
  board_token       TEXT,
  sharia_verdict    TEXT NOT NULL DEFAULT 'unknown',
  sharia_sector     TEXT,
  sharia_reason     TEXT,
  sharia_source     TEXT,
  sharia_decided_at TIMESTAMPTZ
);

CREATE TABLE jobs (
  job_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fingerprint   TEXT NOT NULL UNIQUE,
  company_id    BIGINT NOT NULL REFERENCES companies(company_id),
  source        TEXT NOT NULL,
  source_job_id TEXT NOT NULL,
  title         TEXT NOT NULL,
  location      TEXT,
  remote_type   TEXT,
  salary_min    INTEGER,
  salary_max    INTEGER,
  salary_source TEXT NOT NULL DEFAULT 'none',
  description   TEXT NOT NULL,
  apply_url     TEXT NOT NULL,
  posted_at     TIMESTAMPTZ,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL,
  closed_at     TIMESTAMPTZ,
  filtered_out  BOOLEAN NOT NULL DEFAULT FALSE,
  filter_reason TEXT,
  UNIQUE(source, source_job_id),
  -- A reason is required exactly when the job was filtered. Without this,
  -- "why was this dropped?" silently becomes unanswerable.
  CONSTRAINT filter_reason_iff_filtered
    CHECK ((filtered_out AND filter_reason IS NOT NULL)
        OR (NOT filtered_out AND filter_reason IS NULL))
);
CREATE INDEX idx_jobs_company ON jobs(company_id);
CREATE INDEX idx_jobs_posted ON jobs(posted_at DESC NULLS LAST);
CREATE INDEX idx_jobs_live ON jobs(last_seen_at DESC) WHERE filtered_out = FALSE;

CREATE TABLE run_log (
  run_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at     TIMESTAMPTZ NOT NULL,
  finished_at    TIMESTAMPTZ,
  jobs_seen      INTEGER NOT NULL DEFAULT 0,
  jobs_new       INTEGER NOT NULL DEFAULT 0,
  jobs_filtered  INTEGER NOT NULL DEFAULT 0,
  errors         INTEGER NOT NULL DEFAULT 0,
  duration_ms    INTEGER,
  budget_hit     BOOLEAN NOT NULL DEFAULT FALSE,
  notes          TEXT
);

CREATE TABLE http_cache (
  url           TEXT PRIMARY KEY,
  etag          TEXT,
  last_modified TEXT,
  fetched_at    TIMESTAMPTZ NOT NULL
);
```

- [ ] **Step 3: Write the failing tests**

`tests/conftest.py` — one migrated database per session, truncated between tests. Migrating once per test would dominate the suite's runtime.

```python
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
    "postgresql://jobhunt:jobhunt@localhost:5433/jobhunt_test",
)

# Every table 001_initial.sql creates, minus schema_version, which must survive.
_TABLES = "companies, jobs, run_log, http_cache"


@pytest_asyncio.fixture(scope="session")
async def migrated_db():
    """Apply migrations once for the whole session."""
    conn = await AsyncConnection.connect(TEST_DSN, autocommit=True)
    await apply_migrations(conn, MIGRATIONS)
    yield TEST_DSN
    await conn.close()


@pytest_asyncio.fixture
async def db(migrated_db):
    """A clean connection per test. RESTART IDENTITY keeps IDs predictable."""
    conn = await AsyncConnection.connect(migrated_db, autocommit=True, row_factory=dict_row)
    await conn.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE")
    yield conn
    await conn.close()


@pytest.fixture(scope="session")
def migrations_dir() -> Path:
    return MIGRATIONS
```

`tests/test_db.py`:

```python
import psycopg
import pytest

from pipeline.db import close_pool, connection, transaction
from scripts.migrate import apply_migrations


async def test_connection_yields_dict_rows(db, migrated_db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    await close_pool()
    async with connection() as conn:
        cur = await conn.execute("SELECT 1 AS answer")
        row = await cur.fetchone()
    assert row["answer"] == 1
    await close_pool()


async def test_transaction_rolls_back_on_error(db, migrated_db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    await close_pool()
    with pytest.raises(RuntimeError):
        async with transaction() as conn:
            await conn.execute(
                "INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')"
            )
            raise RuntimeError("boom")
    async with connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM companies")
        assert (await cur.fetchone())["n"] == 0
    await close_pool()


async def test_apply_migrations_is_idempotent(db, migrations_dir):
    # The session fixture already applied 001; a second call must be a no-op.
    applied = await apply_migrations(db, migrations_dir)
    assert applied == []


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
    with pytest.raises(psycopg.errors.UniqueViolation):
        await db.execute(sql, ("fp1", "lever", "2"))


async def test_filter_reason_required_when_filtered(db):
    await db.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    sql = (
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at, filtered_out, filter_reason)"
        " VALUES (%s, 1, 'greenhouse', %s, 'Engineer', 'd', 'https://x', now(), now(), %s, %s)"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(sql, ("fp-bad", "1", True, None))
```

`tests/test_config.py`:

```python
import pytest

from pipeline.config import load_settings


def test_defaults_match_spec():
    s = load_settings(env={"DATABASE_URL": "postgresql://x/y"})
    assert s.salary_floor == 125_000
    assert s.home_city == "San Leandro"
    assert s.home_state == "CA"
    assert "jobhunt" in s.user_agent.lower()


def test_database_url_is_required():
    # Defaulting to localhost would let a misconfigured deploy quietly write nowhere.
    with pytest.raises(ValueError, match="DATABASE_URL"):
        load_settings(env={})


def test_env_overrides_defaults():
    s = load_settings(
        env={
            "DATABASE_URL": "postgresql://x/y",
            "JOBHUNT_SALARY_FLOOR": "140000",
            "JOBHUNT_RUN_BUDGET_SECONDS": "60",
        }
    )
    assert s.salary_floor == 140_000
    assert s.run_budget_seconds == 60.0


def test_run_budget_defaults_below_the_invocation_ceiling():
    # Vercel Pro hard-kills at 800s. The budget must leave room to finish
    # bookkeeping and return a response, or run_log never gets its finished_at.
    s = load_settings(env={"DATABASE_URL": "postgresql://x/y"})
    assert s.run_budget_seconds <= 700


def test_settings_is_frozen():
    s = load_settings(env={"DATABASE_URL": "postgresql://x/y"})
    with pytest.raises(Exception) as exc:
        s.salary_floor = 1  # type: ignore[misc]
    assert "frozen" in str(exc.value).lower() or exc.typename == "FrozenInstanceError"
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
brew install postgresql@16
chmod +x scripts/dev_db.sh && ./scripts/dev_db.sh up
python3 -m venv venv && ./venv/bin/pip install -e ".[dev]"
./venv/bin/pytest tests/ -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.migrate'`

- [ ] **Step 5: Implement `scripts/migrate.py`**

Migrations are a deploy step, not a startup step: concurrent cold starts would otherwise race each other (spec §5).

```python
"""Apply pending SQL migrations. Run by CI before a deploy; never at request time."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import scalar_row

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  name       TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def apply_migrations(conn: AsyncConnection, migrations_dir: Path) -> list[str]:
    """Apply every *.sql not yet recorded, in filename order. Returns those applied."""
    await conn.execute(_SCHEMA_VERSION_DDL)
    # Pin the row factory rather than inheriting the caller's: this is called
    # both with tuple-row connections (the CLI) and dict-row ones (the app).
    async with conn.cursor(row_factory=scalar_row) as cur:
        await cur.execute("SELECT name FROM schema_version")
        done = set(await cur.fetchall())

    applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in done:
            continue
        # Postgres DDL is transactional: a failed migration leaves no partial schema.
        async with conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute("INSERT INTO schema_version (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    return applied


async def _main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    conn = await AsyncConnection.connect(dsn, autocommit=True)
    try:
        applied = await apply_migrations(conn, migrations)
    finally:
        await conn.close()
    print(f"applied: {applied}" if applied else "already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
```

Note the connection uses `autocommit=True` so that `conn.transaction()` controls the boundaries explicitly rather than nesting inside psycopg's implicit transaction.

- [ ] **Step 6: Implement `pipeline/db.py`**

```python
"""Async Postgres access.

One pool per process, reused across warm serverless invocations. Opening a pool
per request would exhaust Neon's pooler under concurrency.
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
            # Small: each function instance serves one request at a time, and
            # Neon's pooler is the real multiplexer. A large local pool just
            # holds server-side connections open for nothing.
            min_size=0,
            max_size=4,
            open=False,
            kwargs={"row_factory": dict_row},
        )
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[AsyncConnection]:
    """A pooled connection in autocommit mode."""
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
    """Close and discard the pool. Used by tests; not called in production."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
```

- [ ] **Step 7: Implement `pipeline/config.py`**

```python
"""Runtime settings, loaded from the environment with spec-derived defaults."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

USER_AGENT = "jobhunt/0.1 (personal job search; contact: developer@cloudbaseservices.com)"

# Vercel Pro terminates at 800s. Stopping at 600 leaves room to write run_log
# and return a response — a killed invocation records nothing at all.
DEFAULT_RUN_BUDGET_SECONDS = 600.0


@dataclass(frozen=True)
class Settings:
    database_url: str
    migrations_dir: Path
    profile_dir: Path
    salary_floor: int
    home_city: str
    home_state: str
    user_agent: str
    run_budget_seconds: float


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env

    database_url = e.get("DATABASE_URL", "")
    if not database_url:
        # No localhost fallback on purpose: a misconfigured deploy should fail
        # loudly at startup, not write to a database nobody is reading.
        raise ValueError("DATABASE_URL is required but not set")

    return Settings(
        database_url=database_url,
        migrations_dir=Path(e.get("JOBHUNT_MIGRATIONS_DIR", _ROOT / "migrations")),
        profile_dir=Path(e.get("JOBHUNT_PROFILE_DIR", _ROOT / "profile")),
        salary_floor=int(e.get("JOBHUNT_SALARY_FLOOR", "125000")),
        home_city=e.get("JOBHUNT_HOME_CITY", "San Leandro"),
        home_state=e.get("JOBHUNT_HOME_STATE", "CA"),
        user_agent=e.get("JOBHUNT_USER_AGENT", USER_AGENT),
        run_budget_seconds=float(e.get("JOBHUNT_RUN_BUDGET_SECONDS", DEFAULT_RUN_BUDGET_SECONDS)),
    )
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_db.py tests/test_config.py -v`
Expected: PASS (16 tests)

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .gitignore migrations/ pipeline/ scripts/ tests/
git commit -m "feat: project scaffolding, async Postgres layer, settings"
```

---

### Task 2: Salary parsing

**Files:**
- Create: `pipeline/salary.py`
- Test: `tests/test_salary.py`

**Interfaces:**
- Consumes: nothing
- Produces: `pipeline.salary.parse_salary(text: str) -> tuple[int | None, int | None]` — returns `(min, max)` annualized USD, or `(None, None)`. A single figure returns `(n, n)`.

Spec §6 notes Greenhouse and Workday almost never expose structured salary, so this parses free text. It must not guess: a wrong salary silently drops good jobs at the §8 gate, whereas an *unknown* salary passes it. A false number is therefore strictly worse than no number.

**Three traps this task exists to avoid**, each of which produced a wrong answer during implementation:

1. **Alternation order in the number pattern.** If the bare-digit form is tried before the `k` form, `$130k` matches as `130`, fails the plausibility floor, and reports "no salary found" — a silent truncation, not an error.
2. **Assuming every figure carries its own currency marker.** `USD 145,000 — 185,000` marks only the first, and `150k-200k USD` marks neither individually. Requiring a marker on both loses the range and falls back to a single figure.
3. **Decimals in comma-grouped numbers.** `$120,000.00 - $180,000.00` truncates to a single figure unless the comma form accepts a trailing `.00`.

- [ ] **Step 1: Write the failing test**

`tests/test_salary.py`:

```python
import pytest

from pipeline.salary import parse_salary


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$150,000 - $200,000", (150_000, 200_000)),
        ("$150,000-$200,000 per year", (150_000, 200_000)),
        ("Salary range: $130k to $170k", (130_000, 170_000)),
        # Currency marker on the first figure only — the common ATS phrasing.
        ("USD 145,000 — 185,000 annually", (145_000, 185_000)),
        ("The base pay range is $128,000—$164,000.", (128_000, 164_000)),
        ("Compensation: $180,000", (180_000, 180_000)),
        ("$95/hour", (197_600, 197_600)),  # 95 * 40 * 52
        ("$75 - $95 per hour", (156_000, 197_600)),
        # Trailing currency marker.
        ("Base salary of 210000 USD", (210_000, 210_000)),
        ("We offer competitive compensation.", (None, None)),
        ("", (None, None)),
        # No currency anchor: a customer count must never read as pay.
        ("Founded in 2011, we serve 150,000 customers", (None, None)),
        ("401k matching up to 5%", (None, None)),
        ("€120,000 - €150,000", (None, None)),  # non-USD: refuse
    ],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_min_never_exceeds_max():
    lo, hi = parse_salary("$200,000 - $150,000")
    assert lo is not None and hi is not None and lo <= hi


def test_implausible_values_rejected():
    assert parse_salary("$12 - $19") == (None, None)  # too low even hourly
    assert parse_salary("$5,000,000 - $9,000,000") == (None, None)


def test_equity_and_bonus_language_does_not_become_salary():
    assert parse_salary("Equity: 0.05% - 0.15%") == (None, None)
    assert parse_salary("10,000 stock options") == (None, None)


def test_refuses_rather_than_half_parsing_a_range():
    # If either end of a range is implausible, the whole range is untrustworthy.
    # Returning the good half would silently misrepresent the posting.
    assert parse_salary("$150,000 - $9,000,000") == (None, None)


@pytest.mark.parametrize(
    "text,expected",
    [
        # Trailing ".00" must not truncate the range to its first figure.
        ("Pay Range: $120,000.00 - $180,000.00", (120_000, 180_000)),
        # Currency marker trailing the whole range rather than leading it.
        ("Salary: 150k-200k USD", (150_000, 200_000)),
        # "between X and Y" is a range, not a single figure.
        ("This role pays between $140,000 and $185,000 annually", (140_000, 185_000)),
        ("The base salary range for this position is $145,000 — $190,000 USD.", (145_000, 190_000)),
        ("$130K – $175K + equity", (130_000, 175_000)),
        ("Compensation ranges from USD 135,000 to USD 165,000", (135_000, 165_000)),
        ("Estimated pay: $58.65 - $88.00 per hour", (121_992, 183_040)),
    ],
)
def test_real_world_phrasings(text, expected):
    """Cases drawn from how ATS postings actually word compensation."""
    assert parse_salary(text) == expected


def test_and_requires_currency_on_both_sides():
    # "and" is weak evidence of a range. Without a marker on the second figure
    # this is a salary followed by a bonus percentage, and must read as the
    # single salary rather than a bogus $140,000-$15 range.
    assert parse_salary("$140,000 and 15% bonus") == (140_000, 140_000)


def test_company_metrics_are_never_salary():
    assert parse_salary("Series B, raised $50,000,000") == (None, None)
    assert parse_salary("We have 250,000 users and $10M ARR") == (None, None)
    assert parse_salary("$1,200 signing bonus") == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_salary.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.salary'`

- [ ] **Step 3: Implement `pipeline/salary.py`**

```python
"""Parse annualized USD salary ranges out of free-text job descriptions.

Spec section 6: Greenhouse and Workday almost never expose structured salary, so
most figures have to come from prose.

Biased toward refusing rather than guessing. A wrong number silently drops a good
job at the section 8 salary gate, whereas an unknown salary passes that gate --
so a false number is strictly worse than no number.
"""

from __future__ import annotations

import re

_HOURS_PER_YEAR = 40 * 52

# Plausibility bounds. Anything outside these is a misread, not a salary.
_MIN_ANNUAL = 30_000
_MAX_ANNUAL = 1_000_000
_MIN_HOURLY = 20
_MAX_HOURLY = 500

# Every figure must be anchored to a currency marker. Without this, "we serve
# 150,000 customers" parses as a salary.
_CUR = r"(?:USD|US\$|\$)"

# Ordered alternation, longest form first: a comma-grouped number must win over
# the bare-digit form, and "130k" must win over the "130" inside it. Getting
# this order wrong silently truncates 130k to 130, which then fails the
# plausibility check and reads as "no salary found".
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s?[kK]\b|\d+(?:\.\d+)?"

_SEP = r"\s*(?:-|–|—|\bto\b)\s*"

# "and" is handled separately from the dash forms because it is far weaker
# evidence: "$140,000 and 15% bonus" is not a range. Requiring a currency marker
# on the second figure is what separates "between $X and $Y" from that.
_SEP_AND = r"\s+and\s+"

# Three range shapes, tried in order. Each needs at least one currency anchor.
_RANGE_PATTERNS = [
    # "$150,000 - $200,000" and "USD 145,000 - 185,000" (marker on the first only)
    re.compile(rf"{_CUR}\s?({_NUM}){_SEP}(?:{_CUR}\s?)?({_NUM})", re.IGNORECASE),
    # "between $140,000 and $185,000"
    re.compile(rf"{_CUR}\s?({_NUM}){_SEP_AND}{_CUR}\s?({_NUM})", re.IGNORECASE),
    # "150k-200k USD" — marker trails the whole range
    re.compile(rf"({_NUM}){_SEP}({_NUM})\s?(?:USD|US\$)", re.IGNORECASE),
]

# Leading marker ("$180,000") or trailing marker ("210000 USD").
_SINGLE_RE = re.compile(
    rf"{_CUR}\s?({_NUM})|({_NUM})\s?(?:USD|US\$)",
    re.IGNORECASE,
)

_HOURLY_RE = re.compile(r"(?:per\s+hour|/\s?h(?:ou)?r\b|hourly)", re.IGNORECASE)
_NON_USD_RE = re.compile(r"[€£¥]")


def _to_number(token: str) -> float:
    token = token.replace(",", "").replace(" ", "").lower()
    if token.endswith("k"):
        return float(token[:-1]) * 1_000
    return float(token)


def _annualize(value: float, hourly: bool) -> int | None:
    """Convert to an annual figure, or None if implausible at either scale."""
    if hourly:
        if not (_MIN_HOURLY <= value <= _MAX_HOURLY):
            return None
        value *= _HOURS_PER_YEAR
    if not (_MIN_ANNUAL <= value <= _MAX_ANNUAL):
        return None
    return int(round(value))


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """Return (min, max) annualized USD, or (None, None) if nothing trustworthy.

    A single figure returns (n, n). If either end of a range is implausible the
    whole range is refused: returning the good half would misrepresent the post.
    """
    if not text or _NON_USD_RE.search(text):
        return (None, None)

    hourly = bool(_HOURLY_RE.search(text))

    for pattern in _RANGE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        lo = _annualize(_to_number(match.group(1)), hourly)
        hi = _annualize(_to_number(match.group(2)), hourly)
        if lo is None or hi is None:
            return (None, None)
        return (min(lo, hi), max(lo, hi))

    match = _SINGLE_RE.search(text)
    if match:
        # Exactly one of the two groups participates, depending on whether the
        # currency marker led or trailed.
        token = match.group(1) or match.group(2)
        value = _annualize(_to_number(token), hourly)
        if value is None:
            return (None, None)
        return (value, value)

    return (None, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_salary.py -v`
Expected: PASS (27 tests)

- [ ] **Step 5: Commit**

```bash
git add pipeline/salary.py tests/test_salary.py
git commit -m "feat: free-text salary parsing"
```

### Task 3: Models, normalization, and fingerprinting

**Files:**
- Create: `pipeline/models.py`, `pipeline/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: `pipeline.salary.parse_salary`
- Produces:
  - `pipeline.models.RawJob` — frozen dataclass: `source: str`, `source_job_id: str`, `company_name: str`, `title: str`, `location: str | None`, `description: str`, `apply_url: str`, `posted_at: datetime | None`, `remote_hint: bool | None = None`, `salary_min: int | None = None`, `salary_max: int | None = None`, `salary_source: str = "none"`
  - `pipeline.models.Job` — frozen dataclass: all of the above plus `fingerprint: str`, `normalized_company: str`, `remote_type: str`
  - `pipeline.normalize.normalize_company(name: str) -> str`
  - `pipeline.normalize.normalize_title(title: str) -> str`
  - `pipeline.normalize.normalize_city(location: str | None) -> str`
  - `pipeline.normalize.compute_fingerprint(company: str, title: str, location: str | None) -> str`
  - `pipeline.normalize.classify_remote(title: str, location: str | None, description: str, remote_hint: bool | None) -> str` — returns `"remote" | "hybrid" | "onsite"`
  - `pipeline.normalize.to_job(raw: RawJob) -> Job`

Fingerprinting is what makes repost-dedup work (spec §5). It must collapse trivial variants without collapsing genuinely different roles.

**`classify_remote` takes the title, not just location and description.** "(Remote)" in the title is one of the most common ways a posting signals this, and the location field frequently still names an office. Omitting it makes `Senior Software Engineer (Remote)` in `San Francisco, CA` classify as onsite.

**Four traps, each of which produced a wrong answer during implementation:**

1. **Negation runs in both directions.** "this role is not remote" *and* "remote work is not available" both occur. Guarding only the first leaves the second reading as remote. The guard must not over-trigger either: "this is a remote role, travel is not required" is still remote.
2. **Code-prefixed locations invert the city.** Workday-style `US-CA-San Francisco` yields "us" under a naive first-component split, so every US posting shares one city and same-titled jobs in different cities collide onto a single fingerprint. Detect a leading component of ≤3 characters and take the last instead — **the threshold must stay at 3**, since 4 rewrites "Mesa, AZ" to "az".
3. **A company named entirely of legal suffixes normalizes to `""`.** "Inc" → "" would key every such employer together. Fall back to the unstripped form.
4. **The fingerprint needs an unambiguous field separator.** Without one, `("acme x", "engineer")` and `("acme", "x engineer")` hash identically.

- [ ] **Step 1: Write the failing test**

`tests/test_normalize.py`:

```python
from datetime import UTC, datetime

from pipeline.models import RawJob
from pipeline.normalize import (
    classify_remote,
    compute_fingerprint,
    normalize_city,
    normalize_company,
    normalize_title,
    to_job,
)


def test_normalize_company_strips_suffixes_and_case():
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_company("ACME Inc") == "acme"
    assert normalize_company("  Acme   Corporation ") == "acme"
    assert normalize_company("Acme LLC") == "acme"


def test_normalize_company_keeps_distinct_names_distinct():
    assert normalize_company("Acme Health") != normalize_company("Acme")


def test_normalize_company_never_returns_empty():
    # A name made entirely of legal suffixes must not normalize away to "",
    # which would collide with every other such company under one key.
    assert normalize_company("Inc.") == "inc"
    assert normalize_company("Co") == "co"


def test_normalize_company_does_not_strip_suffixes_inside_words():
    assert normalize_company("Coinbase") == "coinbase"
    assert normalize_company("Incredible Machines") == "incredible machines"


def test_normalize_title_strips_noise():
    assert normalize_title("Senior Software Engineer (Remote)") == "senior software engineer"
    assert normalize_title("Software Engineer II - Backend") == "software engineer ii backend"
    assert normalize_title("  Full-Stack   Engineer  ") == "full stack engineer"


def test_normalize_city_extracts_first_component():
    assert normalize_city("San Francisco, CA, USA") == "san francisco"
    assert normalize_city("Remote - US") == "remote"
    assert normalize_city(None) == ""


def test_fingerprint_collapses_trivial_variants():
    a = compute_fingerprint(
        "Acme, Inc.", "Senior Software Engineer (Remote)", "San Francisco, CA, USA"
    )
    b = compute_fingerprint("ACME Inc", "Senior Software Engineer", "San Francisco, CA")
    assert a == b


def test_fingerprint_separates_real_differences():
    base = compute_fingerprint("Acme", "Software Engineer", "San Francisco, CA")
    assert base != compute_fingerprint("Acme", "Staff Software Engineer", "San Francisco, CA")
    assert base != compute_fingerprint("Acme", "Software Engineer", "Austin, TX")
    assert base != compute_fingerprint("Globex", "Software Engineer", "San Francisco, CA")


def test_fingerprint_is_stable_and_hex():
    fp = compute_fingerprint("Acme", "Engineer", "SF")
    assert fp == compute_fingerprint("Acme", "Engineer", "SF")
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_field_separator_cannot_be_forged():
    # Concatenating fields without an unambiguous separator lets one job's
    # company bleed into another's title and collide by accident.
    assert compute_fingerprint("acme x", "engineer", "sf") != compute_fingerprint(
        "acme", "x engineer", "sf"
    )


def test_classify_remote():
    assert classify_remote("Engineer", "Remote - US", "", None) == "remote"
    assert classify_remote("Engineer", "San Francisco, CA", "", True) == "remote"
    assert (
        classify_remote(
            "Engineer", "San Francisco, CA", "This is a hybrid role, 3 days in office", None
        )
        == "hybrid"
    )
    assert classify_remote("Engineer", "San Francisco, CA", "Onsite position", None) == "onsite"
    assert classify_remote("Engineer", None, "", None) == "onsite"


def test_classify_remote_reads_the_title():
    # "(Remote)" in the title is one of the most common ways a posting signals
    # this, and the location field often still names an office.
    assert classify_remote("Senior Engineer (Remote)", "San Francisco, CA", "", None) == "remote"


def test_classify_remote_respects_negation():
    # "This role is not remote" must not classify as remote.
    assert classify_remote("Engineer", "Austin, TX", "This position is not remote.", None) == (
        "onsite"
    )
    assert classify_remote("Engineer", "Austin, TX", "No remote work available.", None) == "onsite"


def test_to_job_parses_salary_from_description_when_absent():
    raw = RawJob(
        source="greenhouse",
        source_job_id="1",
        company_name="Acme, Inc.",
        title="Senior Software Engineer (Remote)",
        location="San Francisco, CA",
        description="The base pay range is $150,000 - $200,000.",
        apply_url="https://example.com/1",
        posted_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    job = to_job(raw)
    assert job.salary_min == 150_000
    assert job.salary_max == 200_000
    assert job.salary_source == "parsed"
    assert job.normalized_company == "acme"
    assert job.remote_type == "remote"
    assert len(job.fingerprint) == 64


def test_to_job_preserves_structured_salary():
    raw = RawJob(
        source="ashby",
        source_job_id="2",
        company_name="Globex",
        title="Engineer",
        location="Remote",
        description="No numbers here.",
        apply_url="https://example.com/2",
        posted_at=None,
        salary_min=160_000,
        salary_max=190_000,
        salary_source="structured",
    )
    job = to_job(raw)
    assert (job.salary_min, job.salary_max) == (160_000, 190_000)
    assert job.salary_source == "structured"


def test_normalize_company_strips_international_and_pbc_suffixes():
    assert normalize_company("Anthropic PBC") == "anthropic"
    assert normalize_company("Acme Pte Ltd") == "acme"
    assert normalize_company("The Walt Disney Company") == "the walt disney"


def test_normalize_company_keeps_group_and_holdings():
    # These routinely distinguish separate legal entities, so collapsing them
    # would merge genuinely different employers.
    assert normalize_company("Acme Group") != normalize_company("Acme")
    assert normalize_company("Acme Holdings") != normalize_company("Acme")


def test_normalize_city_handles_code_prefixed_locations():
    # Workday-style. Taking the first component yields "us" for every posting,
    # which collapses same-titled jobs in different cities onto one fingerprint.
    assert normalize_city("US-CA-San Francisco") == "san francisco"
    assert normalize_city("USA-TX-Austin") == "austin"
    # A long first component is a real city and must still win.
    assert normalize_city("San Francisco, CA") == "san francisco"


def test_code_prefixed_locations_do_not_collide_across_cities():
    a = compute_fingerprint("Acme", "Software Engineer", "US-CA-San Francisco")
    b = compute_fingerprint("Acme", "Software Engineer", "US-TX-Austin")
    assert a != b


def test_classify_remote_respects_negation_in_either_direction():
    assert (
        classify_remote("Engineer", "Bengaluru, India", "Remote work is not available.", None)
        == "onsite"
    )
    assert (
        classify_remote("Engineer", "Austin, TX", "This position is not remote.", None) == "onsite"
    )


def test_to_job_reports_none_when_salary_is_absent_everywhere():
    raw = RawJob(
        source="greenhouse",
        source_job_id="3",
        company_name="Acme",
        title="Engineer",
        location="Austin, TX",
        description="We offer competitive compensation.",
        apply_url="https://example.com/3",
        posted_at=None,
    )
    job = to_job(raw)
    assert (job.salary_min, job.salary_max) == (None, None)
    # "none" rather than "parsed": the prefilter must be able to tell a real
    # figure from an absent one, since unknown salary passes the gate.
    assert job.salary_source == "none"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.models'`

- [ ] **Step 3: Implement `pipeline/models.py`**

```python
"""Canonical job shapes. RawJob is what sources emit; Job is what we store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RawJob:
    """A posting as a source reported it, before any interpretation.

    Adapters do the minimum: map the source's field names onto these, and pass
    salary through only when the source states it structurally. Everything
    derived lives in normalize.py so it is derived one way for every source.
    """

    source: str
    source_job_id: str
    company_name: str
    title: str
    location: str | None
    description: str
    apply_url: str
    posted_at: datetime | None
    remote_hint: bool | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_source: str = "none"


@dataclass(frozen=True)
class Job:
    """A normalized posting, ready to persist."""

    fingerprint: str
    source: str
    source_job_id: str
    company_name: str
    normalized_company: str
    title: str
    location: str | None
    remote_type: str
    salary_min: int | None
    salary_max: int | None
    salary_source: str
    description: str
    apply_url: str
    posted_at: datetime | None
```

- [ ] **Step 4: Implement `pipeline/normalize.py`**

```python
"""Normalization and content-derived fingerprinting.

The fingerprint is what makes repost detection work (spec section 5): a job
relisted under a new source id collapses onto the existing row instead of
looking new. It must therefore ignore cosmetic variation while preserving every
difference that makes a posting a genuinely different job.
"""

from __future__ import annotations

import hashlib
import re

from pipeline.models import Job, RawJob
from pipeline.salary import parse_salary

# Stripped only when they stand as separate tokens, so "Coinbase" and
# "Incredible Machines" survive intact.
#
# Deliberately excluded: "group" and "holdings", which frequently distinguish
# genuinely different legal entities, and "spa"/"as"/"ab", which are ordinary
# words in some company names.
_LEGAL_SUFFIXES = {
    "inc",
    "llc",
    "llp",
    "lp",
    "ltd",
    "corp",
    "corporation",
    "company",
    "co",
    "pbc",
    "gmbh",
    "plc",
    "sa",
    "sas",
    "ag",
    "bv",
    "nv",
    "srl",
    "pte",
    "pvt",
}

_PAREN_RE = re.compile(r"\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\b(?:fully\s+)?remote\b", re.IGNORECASE)

# Negation appears on both sides of the word in real postings:
#   "this role is not remote"        -> negation first
#   "remote work is not available"   -> negation second
# Matching only one direction leaves the other reading as remote.
_NOT_REMOTE_RE = re.compile(
    r"\b(?:not|non|no|isn't)[\s-]+(?:\w+\s+){0,2}?remote\b"
    r"|\bremote\b(?:\s+\w+){0,2}?\s+(?:is\s+)?(?:not|un)(?:\s+|available|supported)",
    re.IGNORECASE,
)

# A character that cannot survive _squash, so it cannot appear inside any field
# and cannot be forged to shift a boundary.
_FIELD_SEP = "|"


def _squash(text: str) -> str:
    """Lowercase, replace every run of non-alphanumerics with a single space."""
    return " ".join(_NON_ALNUM_RE.sub(" ", text.lower()).split())


def normalize_company(name: str) -> str:
    squashed = _squash(name)
    tokens = [t for t in squashed.split() if t not in _LEGAL_SUFFIXES]
    # A name made entirely of legal suffixes ("Inc") must not become "", which
    # would collide with every other such company under a single key.
    return " ".join(tokens) if tokens else squashed


def normalize_title(title: str) -> str:
    return _squash(_PAREN_RE.sub("", title))


def normalize_city(location: str | None) -> str:
    """Extract the city from the many shapes a location field takes.

    Most sources put the city first ("San Francisco, CA, USA"). Workday-style
    strings invert it ("US-CA-San Francisco"), and taking the first component
    there yields "us" for every posting — which would collapse same-titled jobs
    in different cities onto one fingerprint.
    """
    if not location:
        return ""

    parts = [p for p in (_squash(p) for p in re.split(r"[,\-–—/]", location)) if p]
    if not parts:
        return ""
    # A short leading component is a country or state code, not a city name.
    # The threshold is 3 and must not be raised: at 4 this would rewrite
    # "Mesa, AZ" to "az". Region codes longer than that (EMEA, APAC) stay
    # misread, which is acceptable — those postings are outside the US scope.
    if len(parts) > 1 and len(parts[0]) <= 3:
        return parts[-1]
    return parts[0]


def compute_fingerprint(company: str, title: str, location: str | None) -> str:
    """sha256 over the normalized (company, title, city) triple."""
    payload = _FIELD_SEP.join(
        (normalize_company(company), normalize_title(title), normalize_city(location))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_remote(
    title: str,
    location: str | None,
    description: str,
    remote_hint: bool | None,
) -> str:
    """Return "remote", "hybrid", or "onsite".

    Hybrid wins over remote: it is the more restrictive claim, and a posting that
    mentions both is in practice hybrid. The title is consulted because
    "(Remote)" there is one of the most common signals, and the location field
    frequently still names an office.
    """
    strong = f"{title} {location or ''}"

    if _HYBRID_RE.search(f"{strong} {description}"):
        return "hybrid"
    if remote_hint or _REMOTE_RE.search(strong):
        return "remote"
    # The description is the weakest signal, so it is the one that needs the
    # negation guard — boilerplate like "this role is not remote" is common.
    if _REMOTE_RE.search(description) and not _NOT_REMOTE_RE.search(description):
        return "remote"
    return "onsite"


def to_job(raw: RawJob) -> Job:
    salary_min, salary_max, salary_source = (raw.salary_min, raw.salary_max, raw.salary_source)
    if salary_min is None and salary_max is None:
        salary_min, salary_max = parse_salary(raw.description)
        salary_source = "parsed" if salary_min is not None else "none"

    return Job(
        fingerprint=compute_fingerprint(raw.company_name, raw.title, raw.location),
        source=raw.source,
        source_job_id=raw.source_job_id,
        company_name=raw.company_name.strip(),
        normalized_company=normalize_company(raw.company_name),
        title=raw.title.strip(),
        location=raw.location,
        remote_type=classify_remote(raw.title, raw.location, raw.description, raw.remote_hint),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_source=salary_source,
        description=raw.description,
        apply_url=raw.apply_url,
        posted_at=raw.posted_at,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_normalize.py -v`
Expected: PASS (21 tests)

- [ ] **Step 6: Commit**

```bash
git add pipeline/models.py pipeline/normalize.py tests/test_normalize.py
git commit -m "feat: canonical job models, normalization, fingerprinting"
```

---

### Task 4: PoliteSession

**Files:**
- Create: `pipeline/http.py`
- Test: `tests/test_http.py`

**Interfaces:**
- Consumes: `pipeline.config.Settings`, `psycopg.AsyncConnection`
- Produces:
  - `pipeline.http.HostBlockedError(Exception)` — raised on 403; carries `.host`
  - `pipeline.http.PoliteSession(user_agent: str, conn: AsyncConnection | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, default_delay: float = 1.0, host_delays: dict[str, float] | None = None)` — also an async context manager
  - `await PoliteSession.get_json(url: str, *, params: dict | None = None, headers: dict | None = None) -> Any | None` — parsed JSON, or `None` on 304
  - `PoliteSession.delay_for(host: str) -> float`
  - `PoliteSession.blocked_hosts: set[str]`
  - `await PoliteSession.aclose() -> None`
  - `pipeline.http.MAX_RETRIES`, `pipeline.http.JITTER` — module constants; tests pin `JITTER` to 0

`_request` takes a method argument so a POST helper is a two-line addition later, but **no `post_json` is exposed in this phase** — nothing in Phase 1 posts. It arrives with the Workday adapter, which is the only POST source.

This is the §7 politeness layer. The `sleep` parameter is injected so most tests assert on delays without waiting.

**The async rewrite introduces a correctness bug that the synchronous version could not have.** The gate reads `_last_hit`, decides how long to wait, sleeps, then writes `_last_hit`. Under `asyncio.gather`, coroutines 2 and 3 both read the same stale value, both sleep concurrently, and both fire at once — so three same-host requests cost one delay instead of two and the rate limit silently halves, under exactly the concurrency the crawl depends on. A `defaultdict[str, asyncio.Lock]` around the gate fixes it. The lock is released once the wait completes rather than held across the request: the constraint is request *rate*, not concurrency.

**Testing that lock requires real sleeps and real elapsed time.** An injected fake sleeper that never awaits does not yield to the event loop, so no interleaving occurs and the test passes with or without the lock — verified by deleting the lock and watching the test still pass. The working version pins `JITTER` to 0 and measures wall-clock time, which separates locked (~2 delays) from unlocked (~1 delay) unambiguously. Confirmed to fail at 0.063s against a 0.090s floor when the lock is removed.

**`Retry-After` is honoured over our own backoff schedule.** When a server states how long to wait, guessing shorter is the fastest way to turn a soft rate-limit into a durable block. Capped at 120s, and an HTTP-date value falls back to the exponential schedule.

- [ ] **Step 1: Write the failing test**

`tests/test_http.py`:

```python
import asyncio
import time

import httpx
import pytest

from pipeline.http import MAX_RETRIES, HostBlockedError, PoliteSession


@pytest.fixture
def recorder():
    """An async sleep that records durations instead of waiting."""
    calls: list[float] = []

    async def sleeper(duration: float) -> None:
        calls.append(duration)

    return calls, sleeper


async def test_rate_limits_per_host(httpx_mock, recorder):
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/1", json={"ok": 1})
    httpx_mock.add_response(url="https://a.test/2", json={"ok": 2})
    async with PoliteSession("ua/1.0", sleep=sleeper, default_delay=1.0) as s:
        await s.get_json("https://a.test/1")
        await s.get_json("https://a.test/2")
    assert any(d > 0 for d in slept), "second same-host request should be delayed"


async def test_does_not_delay_across_different_hosts(httpx_mock, recorder):
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/1", json={})
    httpx_mock.add_response(url="https://b.test/1", json={})
    async with PoliteSession("ua/1.0", sleep=sleeper, default_delay=1.0) as s:
        await s.get_json("https://a.test/1")
        await s.get_json("https://b.test/1")
    assert not [d for d in slept if d > 0]


async def test_concurrent_same_host_requests_are_serialized(httpx_mock, monkeypatch):
    """The guarantee that async breaks if the gate is not locked.

    Without a per-host lock, coroutines 2 and 3 both compute their wait from the
    same stale _last_hit, sleep concurrently, and fire together — so three
    requests cost one delay instead of two and the rate limit silently halves.

    This has to use real sleeps and real elapsed time. A fake sleeper that never
    yields to the event loop prevents the interleaving entirely, so the test
    would pass with or without the lock. Jitter is pinned to 0 so the locked and
    unlocked timings cannot overlap.
    """
    monkeypatch.setattr("pipeline.http.JITTER", 0.0)
    delay = 0.05
    for i in range(3):
        httpx_mock.add_response(url=f"https://a.test/{i}", json={"i": i})

    started = time.monotonic()
    async with PoliteSession("ua/1.0", default_delay=delay) as s:
        await asyncio.gather(*(s.get_json(f"https://a.test/{i}") for i in range(3)))
    elapsed = time.monotonic() - started

    # Serialized: two full gaps. Unlocked would land near one.
    assert elapsed >= delay * 1.8, f"rate limit collapsed under concurrency ({elapsed:.3f}s)"


async def test_concurrent_different_hosts_are_not_serialized(httpx_mock, monkeypatch):
    monkeypatch.setattr("pipeline.http.JITTER", 0.0)
    delay = 0.05
    for host in ("a", "b", "c"):
        httpx_mock.add_response(url=f"https://{host}.test/x", json={})

    started = time.monotonic()
    async with PoliteSession("ua/1.0", default_delay=delay) as s:
        await asyncio.gather(*(s.get_json(f"https://{h}.test/x") for h in ("a", "b", "c")))
    elapsed = time.monotonic() - started

    # Distinct hosts must not wait on each other, or the crawl loses the
    # parallelism that makes it fit inside one bounded invocation.
    assert elapsed < delay, f"different hosts blocked each other ({elapsed:.3f}s)"


def test_workday_uses_slower_lane():
    s = PoliteSession("ua/1.0", host_delays={"x.wd1.myworkdayjobs.com": 2.0})
    assert s.delay_for("x.wd1.myworkdayjobs.com") == 2.0
    assert s.delay_for("boards-api.greenhouse.io") == 1.0


def test_workday_slow_lane_applies_without_explicit_config():
    # Spec section 7: Workday rate-limits by source IP across all tenants, so
    # the slow lane must not depend on someone remembering to configure it.
    s = PoliteSession("ua/1.0")
    assert s.delay_for("acme.wd5.myworkdayjobs.com") == 2.0


async def test_403_raises_and_marks_host_blocked(httpx_mock):
    httpx_mock.add_response(url="https://blocked.test/x", status_code=403)
    async with PoliteSession("ua/1.0") as s:
        with pytest.raises(HostBlockedError):
            await s.get_json("https://blocked.test/x")
        assert "blocked.test" in s.blocked_hosts


async def test_blocked_host_is_not_retried(httpx_mock):
    httpx_mock.add_response(url="https://blocked.test/x", status_code=403)
    async with PoliteSession("ua/1.0") as s:
        with pytest.raises(HostBlockedError):
            await s.get_json("https://blocked.test/x")
        with pytest.raises(HostBlockedError):
            await s.get_json("https://blocked.test/y")
    # Only the first request ever left the process. Retrying into a block is
    # what turns a soft refusal into a durable one.
    assert len(httpx_mock.get_requests()) == 1


async def test_429_backs_off_then_succeeds(httpx_mock, recorder):
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/x", status_code=429)
    httpx_mock.add_response(url="https://a.test/x", json={"ok": True})
    async with PoliteSession("ua/1.0", sleep=sleeper) as s:
        assert await s.get_json("https://a.test/x") == {"ok": True}
    assert len(httpx_mock.get_requests()) == 2


async def test_429_honours_retry_after(httpx_mock, recorder):
    # When a server states how long to wait, guessing a shorter backoff is how
    # a soft rate-limit escalates into a block.
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/x", status_code=429, headers={"Retry-After": "7"})
    httpx_mock.add_response(url="https://a.test/x", json={"ok": True})
    async with PoliteSession("ua/1.0", sleep=sleeper, default_delay=1.0) as s:
        await s.get_json("https://a.test/x")
    assert any(d >= 7 for d in slept)


async def test_gives_up_after_max_retries(httpx_mock, recorder):
    _, sleeper = recorder
    for _ in range(MAX_RETRIES):
        httpx_mock.add_response(url="https://a.test/x", status_code=503)
    async with PoliteSession("ua/1.0", sleep=sleeper) as s:
        with pytest.raises(httpx.HTTPStatusError):
            await s.get_json("https://a.test/x")
    # Exactly MAX_RETRIES attempts: a persistent 503 must not become an
    # unbounded hammer on a struggling host.
    assert len(httpx_mock.get_requests()) == MAX_RETRIES


async def test_sends_user_agent(httpx_mock):
    httpx_mock.add_response(url="https://a.test/x", json={})
    async with PoliteSession("jobhunt/0.1 (contact)") as s:
        await s.get_json("https://a.test/x")
    assert httpx_mock.get_requests()[0].headers["user-agent"] == "jobhunt/0.1 (contact)"


async def test_sends_etag_on_second_request_and_returns_none_on_304(httpx_mock, db):
    httpx_mock.add_response(url="https://a.test/x", json={"v": 1}, headers={"ETag": 'W/"abc"'})
    httpx_mock.add_response(url="https://a.test/x", status_code=304)

    async with PoliteSession("ua/1.0", conn=db) as s:
        assert await s.get_json("https://a.test/x") == {"v": 1}
        assert await s.get_json("https://a.test/x") is None

    assert httpx_mock.get_requests()[1].headers["if-none-match"] == 'W/"abc"'


async def test_sends_if_modified_since_when_only_last_modified_is_offered(httpx_mock, db):
    stamp = "Wed, 30 Jul 2026 12:00:00 GMT"
    httpx_mock.add_response(url="https://a.test/y", json={"v": 1}, headers={"Last-Modified": stamp})
    httpx_mock.add_response(url="https://a.test/y", status_code=304)

    async with PoliteSession("ua/1.0", conn=db) as s:
        await s.get_json("https://a.test/y")
        await s.get_json("https://a.test/y")

    assert httpx_mock.get_requests()[1].headers["if-modified-since"] == stamp


async def test_validators_are_upserted_not_duplicated(httpx_mock, db):
    httpx_mock.add_response(url="https://a.test/z", json={"v": 1}, headers={"ETag": '"one"'})
    httpx_mock.add_response(url="https://a.test/z", json={"v": 2}, headers={"ETag": '"two"'})

    async with PoliteSession("ua/1.0", conn=db) as s:
        await s.get_json("https://a.test/z")
        await s.get_json("https://a.test/z")

    cur = await db.execute("SELECT etag FROM http_cache WHERE url = 'https://a.test/z'")
    rows = await cur.fetchall()
    assert len(rows) == 1 and rows[0]["etag"] == '"two"'


async def test_works_without_a_connection(httpx_mock):
    # Fixture capture and ad-hoc probing run with no database at all.
    httpx_mock.add_response(url="https://a.test/x", json={"ok": True})
    async with PoliteSession("ua/1.0") as s:
        assert await s.get_json("https://a.test/x") == {"ok": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_http.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.http'`

- [ ] **Step 3: Implement `pipeline/http.py`**

```python
"""The single outbound HTTP path for the whole pipeline.

Everything in spec section 7 lives here: per-host rate limiting with jitter,
conditional requests via stored ETags, exponential backoff on 429/503, and a
hard stop on 403 so we never retry into a block.

A bare httpx call anywhere else in the pipeline is a defect. Politeness is only
a guarantee if there is exactly one way out.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from psycopg import AsyncConnection

DEFAULT_DELAY = 1.0
WORKDAY_DELAY = 2.0
MAX_RETRIES = 3
JITTER = 0.3

# Beyond this we are being told to go away for longer than a run is worth.
MAX_RETRY_AFTER = 120.0

_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class HostBlockedError(Exception):
    """Raised when a host returns 403. We stop rather than retry into a block."""

    def __init__(self, host: str) -> None:
        super().__init__(f"host refused requests (403): {host}")
        self.host = host


class PoliteSession:
    """Rate-limited, conditional-request HTTP client.

    `sleep` is injected so tests can assert on delays without waiting, and so a
    run can be given a budget-aware sleeper later.
    """

    def __init__(
        self,
        user_agent: str,
        conn: AsyncConnection | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        default_delay: float = DEFAULT_DELAY,
        host_delays: dict[str, float] | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=True,
        )
        self._conn = conn
        self._sleep = sleep
        self._default_delay = default_delay
        self._host_delays = host_delays or {}
        self._last_hit: dict[str, float] = {}
        # One lock per host. This is what makes the rate limit survive
        # concurrency: without it, coroutines racing through the gate all read
        # _last_hit before any writes it and none of them waits.
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.blocked_hosts: set[str] = set()

    async def __aenter__(self) -> PoliteSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def delay_for(self, host: str) -> float:
        if host in self._host_delays:
            return self._host_delays[host]
        # Not configurable away: Workday rate-limits by source IP across every
        # tenant, so one misconfigured run poisons all of them (spec section 7).
        if ".myworkdayjobs.com" in host:
            return WORKDAY_DELAY
        return self._default_delay

    async def _wait_turn(self, host: str) -> None:
        """Hold this host's turnstile until the inter-request delay has elapsed.

        The lock is released once the wait is done rather than held across the
        request: the constraint is request *rate*, not concurrency, so there is
        no reason to make a slow response block the next scheduled one.
        """
        async with self._locks[host]:
            base = self.delay_for(host)
            elapsed = time.monotonic() - self._last_hit.get(host, -float("inf"))
            wait = base - elapsed
            if wait > 0:
                await self._sleep(wait * (1 + random.uniform(-JITTER, JITTER)))
            self._last_hit[host] = time.monotonic()

    async def _cached_validators(self, url: str) -> dict[str, str]:
        if self._conn is None:
            return {}
        cur = await self._conn.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = %s", (url,)
        )
        row = await cur.fetchone()
        if row is None:
            return {}
        headers: dict[str, str] = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    async def _store_validators(self, url: str, response: httpx.Response) -> None:
        if self._conn is None:
            return
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if not etag and not last_modified:
            return
        await self._conn.execute(
            "INSERT INTO http_cache (url, etag, last_modified, fetched_at)"
            " VALUES (%s, %s, %s, now())"
            " ON CONFLICT (url) DO UPDATE SET etag = excluded.etag,"
            " last_modified = excluded.last_modified, fetched_at = excluded.fetched_at",
            (url, etag, last_modified),
        )

    def _backoff_for(self, response: httpx.Response, attempt: int) -> float:
        """Prefer the server's own Retry-After over our guess.

        Guessing a shorter wait than the server asked for is the fastest way to
        turn a soft rate-limit into a hard block.
        """
        header = response.headers.get("Retry-After")
        if header:
            try:
                requested = float(header)
            except ValueError:
                requested = 0.0  # HTTP-date form; fall back to our own schedule
            if requested > 0:
                return min(requested, MAX_RETRY_AFTER)
        return (2**attempt) * self._default_delay

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any | None:
        host = urlsplit(url).netloc
        if host in self.blocked_hosts:
            raise HostBlockedError(host)

        headers = dict(kwargs.pop("headers", None) or {})
        if method == "GET":
            headers.update(await self._cached_validators(url))

        for attempt in range(MAX_RETRIES):
            await self._wait_turn(host)
            response = await self._client.request(method, url, headers=headers, **kwargs)

            if response.status_code == 403:
                self.blocked_hosts.add(host)
                raise HostBlockedError(host)
            if response.status_code == 304:
                return None
            if response.status_code in _RETRYABLE:
                if attempt == MAX_RETRIES - 1:
                    response.raise_for_status()
                backoff = self._backoff_for(response, attempt)
                await self._sleep(backoff * (1 + random.uniform(0, JITTER)))
                continue

            response.raise_for_status()
            if method == "GET":
                await self._store_validators(url, response)
            return response.json()

        return None

    async def get_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> Any | None:
        """GET and parse JSON. Returns None on 304 (content unchanged)."""
        return await self._request("GET", url, params=params, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_http.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Verify the concurrency test is not vacuous**

Delete the `async with self._locks[host]:` line, re-run, and confirm
`test_concurrent_same_host_requests_are_serialized` **fails**. Restore it.
A concurrency test that passes either way is worse than no test — it certifies
a guarantee nobody is holding.

- [ ] **Step 6: Commit**

```bash
git add pipeline/http.py tests/test_http.py
git commit -m "feat: PoliteSession with rate limiting, ETags, backoff, 403 stop"
```

---

### Task 5: Source protocol, fixture capture, and the Greenhouse adapter

**Files:**
- Create: `pipeline/sources/__init__.py`, `pipeline/sources/base.py`, `pipeline/sources/greenhouse.py`, `pipeline/sources/registry.py`, `scripts/capture_fixture.py`, `profile.example/targets.yaml`
- Test: `tests/sources/test_greenhouse.py`, `tests/sources/test_registry.py`, `tests/fixtures/greenhouse_board.json`

**Interfaces:**
- Consumes: `PoliteSession`, `RawJob`, `Settings`
- Produces:
  - `pipeline.sources.base.SourceConfig` — dataclass: `session: PoliteSession`, `targets: dict[str, list[str]]`, `settings: Settings`
  - `pipeline.sources.base.Source` — Protocol with `name: str` and `fetch(cfg: SourceConfig) -> AsyncIterator[RawJob]`
  - `pipeline.sources.greenhouse.GreenhouseSource` — `name = "greenhouse"`
  - `pipeline.sources.registry.SOURCES: dict[str, Source]`
  - `pipeline.sources.registry.load_targets(path: Path) -> dict[str, list[str]]`

`fetch` is an **async generator**, not a function returning a list. The orchestrator can then stop mid-source when its wall-clock budget expires without the source having fetched everything first.

**What the captured fixture changed.** Two field choices came from reading a real response, and neither is the obvious guess:

- **`company_name` exists in the payload.** It carries `"Stripe"`; the board token is `"stripe"`. The display name is what a human reads in the UI and what goes on an application. Fall back to the token only when absent.
- **`first_published` is the posting date; `updated_at` moves on any edit.** In the captured board, one job was published 2026-06-02 and updated 2026-07-27 — using `updated_at` would make a two-month-old posting look four days old, directly corrupting the freshness score that spec §8 depends on.

**Greenhouse does not paginate.** Verified live against two boards: `meta.total` equalled `len(jobs)` at 548 and 184. The adapter logs a truncation warning if that ever stops holding, because silently ingesting a first page would look like a shrinking job market rather than a bug.

**403 stops the whole source, not just one board.** Every board shares `boards-api.greenhouse.io`, so continuing after a 403 is retrying into a block (spec §7). An ordinary failure — a 404 from a dead token — skips only that board.

- [ ] **Step 1: Write the capture script**

`scripts/capture_fixture.py`. It goes through `PoliteSession` like everything else, so capturing obeys the same rate limits as the crawl.

```python
"""Record a live API response as a test fixture.

Fixtures are captured, never hand-written: a hand-written fixture encodes a
guess about the API's shape, and the tests then verify the guess rather than
reality. When a captured fixture disagrees with an adapter, the fixture wins.

Goes through PoliteSession like everything else, so capturing fixtures obeys the
same rate limits as the crawl (spec section 7).

Usage:
    python scripts/capture_fixture.py greenhouse_board \\
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true"

    # Keep only the first N items under a key, so tests stay fast:
    python scripts/capture_fixture.py greenhouse_board "<url>" --trim jobs=5
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pipeline.config import USER_AGENT
from pipeline.http import PoliteSession

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def _trim(payload: Any, spec: str | None) -> Any:
    """Apply "key=n" (or bare "n" for a top-level list) to shrink the payload."""
    if not spec:
        return payload
    if "=" in spec:
        key, count = spec.split("=", 1)
        if isinstance(payload, dict) and isinstance(payload.get(key), list):
            payload[key] = payload[key][: int(count)]
        return payload
    if isinstance(payload, list):
        return payload[: int(spec)]
    return payload


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="fixture filename, without .json")
    parser.add_argument("url")
    parser.add_argument("--trim", help='e.g. "jobs=5" or "5" for a top-level list')
    args = parser.parse_args()

    async with PoliteSession(USER_AGENT) as session:
        payload = await session.get_json(args.url)

    if payload is None:
        print("no body returned (304?) — nothing captured")
        return 1

    payload = _trim(payload, args.trim)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{args.name}.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
```

- [ ] **Step 2: Capture a real fixture**

```bash
./venv/bin/python scripts/capture_fixture.py greenhouse_board \
  "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true" --trim jobs=4
```

Then open `tests/fixtures/greenhouse_board.json` and read the actual field names.
**If the shape differs from what the adapter assumes, the fixture is right and the adapter must change.**

- [ ] **Step 3: Write the failing tests**

`tests/sources/test_greenhouse.py`:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.sources.base import SourceConfig
from pipeline.sources.greenhouse import GreenhouseSource

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse_board.json"
BOARD = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true"


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=_noop_sleep),
        targets={"greenhouse": ["stripe"]},
        settings=load_settings(env={"DATABASE_URL": "postgresql://x/y"}),
    )


async def _collect(source, cfg):
    return [job async for job in source.fetch(cfg)]


async def test_fetch_yields_rawjobs_from_captured_fixture(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, json=json.loads(FIXTURE.read_text()))
    jobs = await _collect(GreenhouseSource(), cfg)

    assert jobs, "fixture should contain at least one job"
    for job in jobs:
        assert job.source == "greenhouse"
        assert job.source_job_id
        assert job.title
        assert job.apply_url.startswith("http")
        assert job.posted_at is not None and job.posted_at.tzinfo is not None


async def test_uses_the_display_company_name_not_the_board_token(httpx_mock, cfg):
    # The payload carries "Stripe"; the token is "stripe". The display name is
    # what a human reads in the UI and what goes on an application.
    httpx_mock.add_response(url=BOARD, json=json.loads(FIXTURE.read_text()))
    jobs = await _collect(GreenhouseSource(), cfg)
    assert {j.company_name for j in jobs} == {"Stripe"}


async def test_falls_back_to_the_token_when_company_name_is_absent(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BOARD,
        json={
            "jobs": [
                {
                    "id": 1,
                    "title": "Engineer",
                    "absolute_url": "https://x/1",
                    "location": {"name": "Remote"},
                    "updated_at": "2026-07-30T00:00:00Z",
                    "content": "hi",
                }
            ]
        },
    )
    jobs = await _collect(GreenhouseSource(), cfg)
    assert jobs[0].company_name == "stripe"


async def test_posted_at_uses_first_published_not_updated_at(httpx_mock, cfg):
    """A job republished after an edit must not read as freshly posted.

    Spec section 8 scores freshness, and application timing is one of the
    strongest predictors of a response. Using updated_at would make a two-month
    old posting that received a typo fix look four days old.
    """
    httpx_mock.add_response(
        url=BOARD,
        json={
            "jobs": [
                {
                    "id": 1,
                    "title": "Engineer",
                    "absolute_url": "https://x/1",
                    "location": {"name": "Remote"},
                    "first_published": "2026-06-02T08:58:57-04:00",
                    "updated_at": "2026-07-27T11:17:30-04:00",
                    "content": "hi",
                }
            ]
        },
    )
    jobs = await _collect(GreenhouseSource(), cfg)
    assert jobs[0].posted_at == datetime.fromisoformat("2026-06-02T08:58:57-04:00")


async def test_posted_at_falls_back_to_updated_at(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BOARD,
        json={
            "jobs": [
                {
                    "id": 1,
                    "title": "Engineer",
                    "absolute_url": "https://x/1",
                    "location": {"name": "Remote"},
                    "updated_at": "2026-07-27T11:17:30+00:00",
                    "content": "hi",
                }
            ]
        },
    )
    jobs = await _collect(GreenhouseSource(), cfg)
    assert jobs[0].posted_at == datetime(2026, 7, 27, 11, 17, 30, tzinfo=UTC)


async def test_description_html_entities_are_unescaped(httpx_mock, cfg):
    """Greenhouse returns entity-escaped HTML (spec section A)."""
    httpx_mock.add_response(
        url=BOARD,
        json={
            "jobs": [
                {
                    "id": 1,
                    "title": "Engineer",
                    "absolute_url": "https://x/1",
                    "location": {"name": "Remote"},
                    "updated_at": "2026-07-30T00:00:00Z",
                    "content": "&lt;p&gt;Pay is $150,000&lt;/p&gt;",
                }
            ]
        },
    )
    jobs = await _collect(GreenhouseSource(), cfg)
    assert "&lt;" not in jobs[0].description
    assert "<p>" not in jobs[0].description
    assert "$150,000" in jobs[0].description


async def test_real_fixture_descriptions_contain_no_markup(httpx_mock, cfg):
    # The escaping is real, not hypothetical: assert it against captured bytes.
    httpx_mock.add_response(url=BOARD, json=json.loads(FIXTURE.read_text()))
    jobs = await _collect(GreenhouseSource(), cfg)
    for job in jobs:
        assert "&lt;" not in job.description
        assert "&amp;" not in job.description
        assert "<p>" not in job.description and "<div" not in job.description
        assert job.description.strip()


async def test_blocked_host_does_not_abort_other_boards(httpx_mock, cfg):
    cfg.targets["greenhouse"] = ["blocked", "ok"]
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/blocked/jobs?content=true",
        status_code=403,
    )
    jobs = await _collect(GreenhouseSource(), cfg)
    # A 403 stops the whole source: every board shares one host, so continuing
    # would be retrying into a block (spec section 7).
    assert jobs == []


async def test_one_bad_board_does_not_kill_the_rest(httpx_mock, cfg):
    cfg.targets["greenhouse"] = ["missing", "ok"]
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/missing/jobs?content=true",
        status_code=404,
    )
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/ok/jobs?content=true",
        json={
            "jobs": [
                {
                    "id": 9,
                    "title": "Dev",
                    "absolute_url": "https://x/9",
                    "location": {"name": "Remote"},
                    "updated_at": "2026-07-30T00:00:00Z",
                    "content": "hi",
                }
            ]
        },
    )
    jobs = await _collect(GreenhouseSource(), cfg)
    assert [j.source_job_id for j in jobs] == ["9"]


async def test_304_yields_nothing_without_error(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, status_code=304)
    assert await _collect(GreenhouseSource(), cfg) == []


async def test_missing_location_is_tolerated(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BOARD,
        json={
            "jobs": [
                {
                    "id": 1,
                    "title": "Engineer",
                    "absolute_url": "https://x/1",
                    "location": None,
                    "updated_at": "2026-07-30T00:00:00Z",
                    "content": "hi",
                }
            ]
        },
    )
    jobs = await _collect(GreenhouseSource(), cfg)
    assert jobs[0].location is None


async def test_warns_when_the_board_looks_truncated(httpx_mock, cfg, caplog):
    """Greenhouse returns whole boards in one response, verified against two
    live boards. If that ever changes, silently ingesting the first page would
    look like a shrinking job market rather than a bug."""
    httpx_mock.add_response(
        url=BOARD,
        json={
            "jobs": [
                {
                    "id": 1,
                    "title": "Engineer",
                    "absolute_url": "https://x/1",
                    "location": {"name": "Remote"},
                    "updated_at": "2026-07-30T00:00:00Z",
                    "content": "hi",
                }
            ],
            "meta": {"total": 500},
        },
    )
    with caplog.at_level("WARNING"):
        await _collect(GreenhouseSource(), cfg)
    assert any("truncated" in r.message.lower() for r in caplog.records)
```

`tests/sources/test_registry.py`:

```python
from pathlib import Path

from pipeline.sources.registry import SOURCES, load_targets

EXAMPLE = Path(__file__).resolve().parents[2] / "profile.example" / "targets.yaml"


def test_every_registered_source_answers_to_its_own_key():
    # A mismatch here silently disables a source: the orchestrator looks it up
    # by key, the adapter reads cfg.targets by self.name, and nothing errors.
    for key, source in SOURCES.items():
        assert source.name == key


def test_load_targets_reads_the_example_file():
    targets = load_targets(EXAMPLE)
    assert "stripe" in targets["greenhouse"]


def test_load_targets_returns_empty_for_a_missing_file(tmp_path):
    # profile/ is gitignored and absent on a fresh clone; that must not crash.
    assert load_targets(tmp_path / "nope.yaml") == {}


def test_load_targets_tolerates_an_empty_section(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text("greenhouse:\nlever:\n  - netflix\n")
    assert load_targets(path) == {"greenhouse": [], "lever": ["netflix"]}


def test_example_targets_cover_every_registered_source():
    targets = load_targets(EXAMPLE)
    for key in SOURCES:
        assert targets.get(key), f"{key} has no example tokens"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/sources/ -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.sources.base'`

- [ ] **Step 5: Implement the source base**

`pipeline/sources/__init__.py` and `tests/sources/__init__.py` — empty files.

`pipeline/sources/base.py`:

```python
"""The contract every source implements. Adding a source is one new module."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from pipeline.config import Settings
from pipeline.http import PoliteSession
from pipeline.models import RawJob


@dataclass
class SourceConfig:
    session: PoliteSession
    targets: dict[str, list[str]]
    settings: Settings


class Source(Protocol):
    """A source yields RawJob and does no interpretation of its own.

    Yielding rather than returning a list matters: the orchestrator can stop
    mid-source when its wall-clock budget expires, without the source having
    fetched everything first.
    """

    name: str

    def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]: ...
```

- [ ] **Step 6: Implement the Greenhouse adapter**

`pipeline/sources/greenhouse.py`:

```python
"""Greenhouse job board API. Unauthenticated; whole board in one response.

Field names here were read off a captured live response, not assumed. Two of
them differ from the obvious guess and both matter -- see posted_at and
company_name below.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig

log = logging.getLogger(__name__)

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(raw: str) -> str:
    """Greenhouse double-escapes: unescape entities, then drop tags."""
    text = html.unescape(raw or "")
    text = _TAG_RE.sub(" ", text)
    # A second unescape catches entities that were themselves escaped, e.g.
    # "&amp;lt;" surviving the first pass.
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GreenhouseSource:
    name = "greenhouse"

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        for token in cfg.targets.get(self.name, []):
            try:
                payload = await cfg.session.get_json(
                    BOARD_URL.format(token=token), params={"content": "true"}
                )
            except HostBlockedError:
                # Every board shares one host, so continuing would be retrying
                # into a block. Stop the source, not just this board.
                log.warning("greenhouse host blocked; stopping source")
                return
            except Exception as exc:  # one bad board must not kill the run
                log.warning("greenhouse board %s failed: %s", token, exc)
                continue

            if payload is None:  # 304 Not Modified
                continue

            jobs = payload.get("jobs", [])
            total = (payload.get("meta") or {}).get("total")
            if total is not None and total != len(jobs):
                # Verified against two live boards that the full board arrives
                # in one response. If that changes, quietly taking the first
                # page would look like a shrinking job market, not a bug.
                log.warning(
                    "greenhouse board %s looks truncated: got %d of %d",
                    token,
                    len(jobs),
                    total,
                )

            for item in jobs:
                location = (item.get("location") or {}).get("name")
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    # The payload carries a properly-cased display name; the
                    # token is a slug. Fall back only when it is absent.
                    company_name=item.get("company_name") or token,
                    title=item["title"],
                    location=location,
                    description=_strip_html(item.get("content", "")),
                    apply_url=item["absolute_url"],
                    # first_published is when the job was posted; updated_at
                    # moves on any edit. Freshness scoring (spec section 8)
                    # needs the former, or a typo fix makes an old job look new.
                    posted_at=_parse_ts(item.get("first_published"))
                    or _parse_ts(item.get("updated_at")),
                )
```

- [ ] **Step 7: Implement the registry and target list**

`pipeline/sources/registry.py`:

```python
"""Name to source mapping, and the target-employer list loader."""

from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.sources.base import Source
from pipeline.sources.greenhouse import GreenhouseSource

SOURCES: dict[str, Source] = {
    GreenhouseSource.name: GreenhouseSource(),
}


def load_targets(path: Path) -> dict[str, list[str]]:
    """Read the per-ATS board token lists. A missing file is not an error."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {k: list(v or []) for k, v in data.items()}
```

`profile.example/targets.yaml` — **verify every token before committing it.** A wrong token is a silent 404 that only shows up as a logged warning:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  https://boards-api.greenhouse.io/v1/boards/<token>/jobs
```

```yaml
# Board tokens per ATS. Copy to profile/targets.yaml and edit.
#
#   mkdir -p profile && cp profile.example/targets.yaml profile/targets.yaml
#
# Finding a token: open a company's careers page and read the URL.
#   job-boards.greenhouse.io/stripe        -> greenhouse: stripe
#   jobs.lever.co/netflix                  -> lever:      netflix
#   jobs.ashbyhq.com/ramp                  -> ashby:      ramp
#
# Verifying a token before adding it (a wrong one is a silent 404, and the run
# will just log a warning and move on):
#   curl -s -o /dev/null -w '%{http_code}\n' \
#     https://boards-api.greenhouse.io/v1/boards/<token>/jobs
#
# Every token below was verified live on 2026-08-01 and returned a real board.
# Companies do migrate between ATSes, so re-check any token that starts
# returning nothing.

greenhouse:
  - stripe # 548 open roles
  - figma # 176
  - gitlab # 184
  - databricks # 803
  - anthropic # 400
  - cloudflare # 285
  - robinhood # 128
  - affirm # 181
  - brex # 302
  - samsara # 308

# Populated in Task 6. Tokens here are examples, not yet verified.
lever:
  - netflix

ashby:
  - ramp
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/sources/ -v`
Expected: PASS (17 tests)

- [ ] **Step 9: Run the adapter against a live board and look at the output**

Unit tests pass against fixtures the adapter already agrees with. Running the
full path — fetch, `to_job`, then count — is what surfaces normalization bugs,
and it found two that every test had missed:

```bash
# fetch a board, print remote_type distribution and fingerprint collisions
```

- **All 400 Anthropic roles classified `hybrid`**, because their JD boilerplate
  restates a hybrid office policy. The description was outranking the location.
  Fixed by ranking signals strongest-first in `classify_remote` (Task 3).
- **`normalize_title` stripped every parenthetical**, collapsing
  "Research Engineer, ML (Reinforcement Learning)" onto "(RL Velocity)" — two
  real open roles, one of which then never reaches the queue. Fixed to drop only
  work-arrangement parentheticals.

After both fixes, across Anthropic + GitLab: 584 jobs, 579 unique fingerprints,
and **all 5 collisions verified as true duplicates** — identical title at
identical location. Salary parsed from prose for 79% of postings.

- [ ] **Step 10: Commit**

```bash
git add pipeline/sources/ scripts/capture_fixture.py profile.example/ tests/sources/ tests/fixtures/
git commit -m "feat: source protocol, fixture capture, Greenhouse adapter"
```

---

### Task 6: Lever and Ashby adapters

**Files:**
- Create: `pipeline/text.py`, `pipeline/sources/lever.py`, `pipeline/sources/ashby.py`
- Modify: `pipeline/models.py`, `pipeline/normalize.py`, `pipeline/sources/greenhouse.py`, `pipeline/sources/registry.py`, `profile.example/targets.yaml`
- Test: `tests/sources/test_lever.py`, `tests/sources/test_ashby.py`, fixtures for both

**Interfaces:**
- Consumes: `SourceConfig`, `RawJob`, `HostBlockedError`
- Produces:
  - `LeverSource` (`name = "lever"`), `AshbySource` (`name = "ashby"`)
  - `pipeline.text.strip_html(raw: str | None) -> str`, `pipeline.text.join_sections(*parts: str | None) -> str`
  - `pipeline.normalize.normalize_arrangement(value: str | None) -> str | None`
  - **Model change:** `RawJob.remote_hint: bool | None` becomes `RawJob.remote_type_hint: str | None`, and `classify_remote`'s fourth parameter changes to match.

**Why the model change.** Both Lever and Ashby publish a structured `workplaceType` — a full three-way arrangement, not a boolean. A bool cannot carry "hybrid", and Ashby's own `isRemote` field demonstrates why that matters: the captured fixture has `isRemote: true` alongside `workplaceType: "Hybrid"` on the same job, so trusting the boolean systematically over-reports remote. `classify_remote` now believes a stated arrangement outright and only falls back to text inference when no source supplied one.

**What the captured fixtures changed:**

- **Lever's payload is a bare JSON array**, not an object with a `jobs` key.
- **Lever's `createdAt` is epoch milliseconds** — spec §A was right. Reading it as seconds puts every posting in 1970 and makes the whole board look ancient. The adapter rejects implausibly small values rather than emitting a 1970 date.
- **Lever exposes pre-rendered `*Plain` variants**, so no HTML stripping is needed for the body. But compensation routinely lives in a `lists` block that is HTML-only, so parsing `descriptionPlain` alone loses salary on exactly the US postings that matter.
- **Ashby's compensation components are unordered**, and an `EquityPercentage` entry with null values comes first in two of four captured jobs. Taking `components[0]` reads equity as pay. Filter on `compensationType == "Salary"`.
- **Ashby's `interval` is not always yearly**, so raw `minValue`/`maxValue` cannot be used as annual figures.

**Two measured corrections to the spec** (§6 now records both):

1. **Ashby structured salary is per-employer, not per-source.** Across ten live boards: Ramp 95%, OpenAI 80%, Perplexity 82%, Vanta 62% — but Linear, Notion, Cursor, ClickHouse and PostHog publish **none**. The adapter must degrade to text parsing rather than assume the field exists.
2. **A retired board answers 200 with an empty list, not 404.** Verified on Lever (`plaid`, `mistral`, `voleon`) and Ashby (`deel`). That is indistinguishable from "nothing open", so both adapters log a warning — otherwise a dead token contributes nothing forever and nobody finds out.

- [ ] **Step 1: Capture both fixtures**

```bash
./venv/bin/python scripts/capture_fixture.py lever_postings \
  "https://api.lever.co/v0/postings/spotify?mode=json" --trim 4
./venv/bin/python scripts/capture_fixture.py ashby_board \
  "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true" --trim jobs=4
```

The plan originally named `netflix` and `ramp` for Lever; both 404. **Verify a token before building a fixture on it.**

Read both files. **If the captured shape disagrees with the adapter, the fixture wins.**

- [ ] **Step 2: Change the arrangement hint from bool to str**

In `pipeline/models.py`, replace `remote_hint: bool | None = None` with:

```python
    # Lever and Ashby both publish a structured work arrangement. It beats any
    # text inference, and it is a string rather than a bool on purpose: Ashby
    # reports isRemote=True alongside workplaceType="Hybrid" for the same job,
    # so a boolean would systematically over-report remote.
    remote_type_hint: str | None = None
```

In `pipeline/normalize.py`, add the vocabulary mapper and give a stated arrangement top priority in `classify_remote`:

```python
_ARRANGEMENT_ALIASES = {
    "remote": "remote",
    "fullyremote": "remote",
    "remotefirst": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "inoffice": "onsite",
    "office": "onsite",
}


def normalize_arrangement(value: str | None) -> str | None:
    """Map a source's structured work-arrangement string onto our vocabulary."""
    if not value:
        return None
    key = re.sub(r"[^a-z]", "", value.lower())
    return _ARRANGEMENT_ALIASES.get(key)
```

An unrecognised value must return `None` and fall through to inference — never
default to `"onsite"`, or a source inventing a new label silently marks every
one of its jobs onsite.

- [ ] **Step 3: Extract the shared text helpers**

`pipeline/text.py` — Greenhouse's private `_strip_html` moves here and all three adapters import it:

```python
"""Text cleanup shared by the source adapters."""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(raw: str | None) -> str:
    """Turn a description fragment into plain text.

    Unescapes twice on purpose: Greenhouse serves entity-escaped markup, so the
    first pass produces tags and the second catches entities that were
    themselves escaped ("&amp;lt;").
    """
    text = html.unescape(raw or "")
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def join_sections(*parts: str | None) -> str:
    """Concatenate description fragments, dropping empties.

    Sources split a posting across several fields and compensation frequently
    lives in one of the trailing ones, so parsing only the main body loses it.
    """
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
```

- [ ] **Step 4: Write the failing tests**

`tests/sources/test_lever.py`:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.sources.base import SourceConfig
from pipeline.sources.lever import LeverSource

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lever_postings.json"
BOARD = "https://api.lever.co/v0/postings/spotify?mode=json"


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=_noop_sleep),
        targets={"lever": ["spotify"]},
        settings=load_settings(env={"DATABASE_URL": "postgresql://x/y"}),
    )


async def _collect(cfg):
    return [job async for job in LeverSource().fetch(cfg)]


def _posting(**overrides):
    base = {
        "id": "abc-123",
        "text": "Engineer",
        "categories": {"location": "London"},
        "descriptionPlain": "Build things.",
        "hostedUrl": "https://jobs.lever.co/spotify/abc-123",
        "applyUrl": "https://jobs.lever.co/spotify/abc-123/apply",
        "createdAt": 1784569799619,
    }
    return {**base, **overrides}


async def test_fetch_yields_rawjobs_from_captured_fixture(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, json=json.loads(FIXTURE.read_text()))
    jobs = await _collect(cfg)

    assert jobs
    for job in jobs:
        assert job.source == "lever"
        assert job.source_job_id and job.title
        assert job.apply_url.startswith("http")
        assert job.posted_at is not None and job.posted_at.tzinfo is not None
        assert job.description.strip()


async def test_created_at_is_epoch_milliseconds(httpx_mock, cfg):
    """Spec section A says milliseconds, and the captured payload confirms it.

    Reading it as seconds places every posting in 1970, which would make the
    freshness score treat the entire board as ancient.
    """
    httpx_mock.add_response(url=BOARD, json=[_posting(createdAt=1784569799619)])
    jobs = await _collect(cfg)
    assert jobs[0].posted_at == datetime.fromtimestamp(1784569799.619, tz=UTC)
    assert jobs[0].posted_at.year == 2026


async def test_implausible_timestamp_becomes_none_rather_than_1970(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, json=[_posting(createdAt=1784569799)])
    jobs = await _collect(cfg)
    assert jobs[0].posted_at is None


async def test_missing_timestamp_is_tolerated(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, json=[_posting(createdAt=None)])
    jobs = await _collect(cfg)
    assert jobs[0].posted_at is None


async def test_workplace_type_is_passed_through_as_a_hint(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, json=[_posting(workplaceType="hybrid")])
    jobs = await _collect(cfg)
    assert jobs[0].remote_type_hint == "hybrid"


async def test_description_includes_list_sections(httpx_mock, cfg):
    """Compensation routinely lives in a `lists` block rather than the body.

    Parsing only descriptionPlain loses salary on exactly the US postings we
    care about most.
    """
    httpx_mock.add_response(
        url=BOARD,
        json=[
            _posting(
                descriptionPlain="Build things.",
                lists=[
                    {
                        "text": "Compensation",
                        "content": "<li>The base range is $180,000 - $220,000</li>",
                    }
                ],
            )
        ],
    )
    jobs = await _collect(cfg)
    assert "$180,000" in jobs[0].description
    assert "<li>" not in jobs[0].description


async def test_description_falls_back_to_html_when_plain_is_absent(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BOARD,
        json=[_posting(descriptionPlain=None, description="<p>Build &amp; ship</p>")],
    )
    jobs = await _collect(cfg)
    assert "Build & ship" in jobs[0].description
    assert "<p>" not in jobs[0].description


async def test_empty_board_warns_about_a_possibly_retired_token(httpx_mock, cfg, caplog):
    """A retired Lever token answers 200 with [], not 404.

    Verified live: plaid and mistral both do this. Silence here means a dead
    token contributes nothing forever and nobody finds out.
    """
    httpx_mock.add_response(url=BOARD, json=[])
    with caplog.at_level("WARNING"):
        assert await _collect(cfg) == []
    assert any("retired" in r.message.lower() for r in caplog.records)


async def test_unexpected_payload_shape_is_survived(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, json={"jobs": []})
    assert await _collect(cfg) == []


async def test_blocked_host_stops_the_source(httpx_mock, cfg):
    cfg.targets["lever"] = ["blocked", "spotify"]
    httpx_mock.add_response(
        url="https://api.lever.co/v0/postings/blocked?mode=json", status_code=403
    )
    assert await _collect(cfg) == []


async def test_one_bad_board_does_not_kill_the_rest(httpx_mock, cfg):
    cfg.targets["lever"] = ["missing", "spotify"]
    httpx_mock.add_response(
        url="https://api.lever.co/v0/postings/missing?mode=json", status_code=404
    )
    httpx_mock.add_response(url=BOARD, json=[_posting(id="9")])
    jobs = await _collect(cfg)
    assert [j.source_job_id for j in jobs] == ["9"]


async def test_304_yields_nothing(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, status_code=304)
    assert await _collect(cfg) == []
```

`tests/sources/test_ashby.py`:

```python
import json
from pathlib import Path

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.sources.ashby import AshbySource
from pipeline.sources.base import SourceConfig

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ashby_board.json"
BOARD = "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=_noop_sleep),
        targets={"ashby": ["ramp"]},
        settings=load_settings(env={"DATABASE_URL": "postgresql://x/y"}),
    )


async def _collect(cfg):
    return [job async for job in AshbySource().fetch(cfg)]


def _job(**overrides):
    base = {
        "id": "abc-123",
        "title": "Engineer",
        "location": "New York, NY",
        "descriptionPlain": "Build things.",
        "jobUrl": "https://jobs.ashbyhq.com/ramp/abc-123",
        "applyUrl": "https://jobs.ashbyhq.com/ramp/abc-123/application",
        "publishedAt": "2026-04-07T17:12:35.753+00:00",
        "isListed": True,
        "shouldDisplayCompensationOnJobPostings": True,
    }
    return {**base, **overrides}


def _salary_tier(components):
    return {"compensationTiers": [{"components": components}]}


SALARY = {
    "compensationType": "Salary",
    "interval": "1 YEAR",
    "currencyCode": "USD",
    "minValue": 211400,
    "maxValue": 290600,
}
EQUITY = {
    "compensationType": "EquityPercentage",
    "interval": "NONE",
    "currencyCode": None,
    "minValue": None,
    "maxValue": None,
}


async def test_fetch_yields_rawjobs_from_captured_fixture(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, json=json.loads(FIXTURE.read_text()))
    jobs = await _collect(cfg)

    assert jobs
    for job in jobs:
        assert job.source == "ashby"
        assert job.source_job_id and job.title
        assert job.apply_url.startswith("http")
        assert job.posted_at is not None and job.posted_at.tzinfo is not None


async def test_captured_fixture_yields_structured_salary_for_every_job(httpx_mock, cfg):
    """Asserted against captured bytes, not a hand-built case.

    Ramp publishes compensation on 95% of postings. Note this is a per-employer
    opt-in, not an Ashby guarantee: Linear, Notion, Cursor, ClickHouse and
    PostHog all publish none, so the adapter must degrade to unknown rather
    than assume the field is there.
    """
    httpx_mock.add_response(url=BOARD, json=json.loads(FIXTURE.read_text()))
    jobs = await _collect(cfg)
    assert all(j.salary_source == "structured" for j in jobs)
    assert all(j.salary_min and j.salary_max and j.salary_min <= j.salary_max for j in jobs)


async def test_equity_component_is_not_read_as_salary(httpx_mock, cfg):
    """Components are unordered and an equity entry with null values often
    comes first. Taking components[0] silently reports equity as pay."""
    httpx_mock.add_response(
        url=BOARD, json={"jobs": [_job(compensation=_salary_tier([EQUITY, SALARY]))]}
    )
    jobs = await _collect(cfg)
    assert (jobs[0].salary_min, jobs[0].salary_max) == (211_400, 290_600)
    assert jobs[0].salary_source == "structured"


async def test_equity_only_compensation_yields_no_salary(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, json={"jobs": [_job(compensation=_salary_tier([EQUITY]))]})
    jobs = await _collect(cfg)
    assert (jobs[0].salary_min, jobs[0].salary_max) == (None, None)
    assert jobs[0].salary_source == "none"


async def test_hourly_compensation_is_annualized(httpx_mock, cfg):
    hourly = {**SALARY, "interval": "1 HOUR", "minValue": 75, "maxValue": 95}
    httpx_mock.add_response(url=BOARD, json={"jobs": [_job(compensation=_salary_tier([hourly]))]})
    jobs = await _collect(cfg)
    assert (jobs[0].salary_min, jobs[0].salary_max) == (75 * 2080, 95 * 2080)


async def test_monthly_compensation_is_annualized(httpx_mock, cfg):
    monthly = {**SALARY, "interval": "1 MONTH", "minValue": 12_000, "maxValue": 15_000}
    httpx_mock.add_response(url=BOARD, json={"jobs": [_job(compensation=_salary_tier([monthly]))]})
    jobs = await _collect(cfg)
    assert (jobs[0].salary_min, jobs[0].salary_max) == (144_000, 180_000)


async def test_non_usd_compensation_is_refused(httpx_mock, cfg):
    eur = {**SALARY, "currencyCode": "EUR"}
    httpx_mock.add_response(url=BOARD, json={"jobs": [_job(compensation=_salary_tier([eur]))]})
    jobs = await _collect(cfg)
    assert jobs[0].salary_source == "none"


async def test_unpublished_compensation_is_not_used(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BOARD,
        json={
            "jobs": [
                _job(
                    shouldDisplayCompensationOnJobPostings=False,
                    compensation=_salary_tier([SALARY]),
                )
            ]
        },
    )
    jobs = await _collect(cfg)
    # Unknown salary passes the prefilter, so declining to use an internal
    # range costs nothing and avoids asserting a figure the employer withheld.
    assert jobs[0].salary_source == "none"


async def test_unlisted_jobs_are_skipped(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BOARD, json={"jobs": [_job(id="1", isListed=False), _job(id="2", isListed=True)]}
    )
    jobs = await _collect(cfg)
    assert [j.source_job_id for j in jobs] == ["2"]


async def test_workplace_type_is_passed_through_as_a_hint(httpx_mock, cfg):
    # The captured fixture has isRemote=True alongside workplaceType="Hybrid";
    # the string is the accurate one.
    httpx_mock.add_response(url=BOARD, json={"jobs": [_job(isRemote=True, workplaceType="Hybrid")]})
    jobs = await _collect(cfg)
    assert jobs[0].remote_type_hint == "Hybrid"


async def test_missing_compensation_object_is_tolerated(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, json={"jobs": [_job()]})
    jobs = await _collect(cfg)
    assert jobs[0].salary_source == "none"


async def test_blocked_host_stops_the_source(httpx_mock, cfg):
    cfg.targets["ashby"] = ["blocked", "ramp"]
    httpx_mock.add_response(
        url="https://api.ashbyhq.com/posting-api/job-board/blocked?includeCompensation=true",
        status_code=403,
    )
    assert await _collect(cfg) == []


async def test_304_yields_nothing(httpx_mock, cfg):
    httpx_mock.add_response(url=BOARD, status_code=304)
    assert await _collect(cfg) == []


async def test_empty_board_warns_about_a_possibly_retired_token(httpx_mock, cfg, caplog):
    # Verified live: the deel board answers 200 with zero jobs.
    httpx_mock.add_response(url=BOARD, json={"jobs": []})
    with caplog.at_level("WARNING"):
        assert await _collect(cfg) == []
    assert any("retired" in r.message.lower() for r in caplog.records)
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/sources/ -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.sources.lever'`

- [ ] **Step 6: Implement the Lever adapter**

```python
"""Lever postings API. Unauthenticated; whole board in one JSON array.

Field names were read off a captured live response. Two things differ from the
obvious guess: the payload is a bare array rather than an object, and createdAt
is epoch milliseconds rather than an ISO string.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig
from pipeline.text import join_sections, strip_html

log = logging.getLogger(__name__)

POSTINGS_URL = "https://api.lever.co/v0/postings/{token}"

# Milliseconds since epoch; anything below this is a seconds-based value that
# would place the posting in 1970 and make every job look impossibly stale.
_MIN_PLAUSIBLE_MS = 1_000_000_000_000


def _parse_epoch_ms(value: object) -> datetime | None:
    if not isinstance(value, int | float) or value < _MIN_PLAUSIBLE_MS:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _description(item: dict) -> str:
    """Assemble the full posting text.

    Lever exposes pre-rendered plain-text variants, so no HTML stripping is
    needed for those. The `lists` blocks are HTML-only and routinely hold the
    compensation range, so skipping them loses salary on US postings.
    """
    lists = item.get("lists") or []
    sections = [strip_html(f"{blk.get('text', '')} {blk.get('content', '')}") for blk in lists]
    return join_sections(
        item.get("descriptionPlain") or strip_html(item.get("description")),
        *sections,
        item.get("additionalPlain") or strip_html(item.get("additional")),
    )


class LeverSource:
    name = "lever"

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        for token in cfg.targets.get(self.name, []):
            try:
                payload = await cfg.session.get_json(
                    POSTINGS_URL.format(token=token), params={"mode": "json"}
                )
            except HostBlockedError:
                log.warning("lever host blocked; stopping source")
                return
            except Exception as exc:  # one bad board must not kill the run
                log.warning("lever board %s failed: %s", token, exc)
                continue

            if payload is None:  # 304 Not Modified
                continue
            if not isinstance(payload, list):
                log.warning("lever board %s returned %s, expected a list", token, type(payload))
                continue
            if not payload:
                # A retired board answers 200 with [], which is indistinguishable
                # from a real board that happens to have nothing open. Say so,
                # or a dead token silently contributes nothing forever.
                log.warning("lever board %s returned no postings; token may be retired", token)
                continue

            for item in payload:
                categories = item.get("categories") or {}
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    # Lever does not carry a display name; the token is all we get.
                    company_name=token,
                    title=item["text"],
                    location=categories.get("location"),
                    description=_description(item),
                    apply_url=item.get("hostedUrl") or item["applyUrl"],
                    posted_at=_parse_epoch_ms(item.get("createdAt")),
                    remote_type_hint=item.get("workplaceType"),
                )
```

- [ ] **Step 7: Implement the Ashby adapter**

```python
"""Ashby job board API. The best structured-compensation source we have.

Spec section 6 calls this the source with reliable structured salary. Measured
against ten live boards on 2026-08-01, that is true per-employer rather than
per-source: publishing compensation is an opt-in Ashby setting. Ramp exposes it
on 95% of postings, OpenAI 80%, Perplexity 82%, Vanta 62% -- while Linear,
Notion, Cursor, ClickHouse and PostHog expose it on none at all.

So this adapter sets salary_source="structured" when the numbers are there and
otherwise leaves salary unknown, which lets pipeline.normalize fall back to
parsing the description like every other source.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig
from pipeline.text import strip_html

log = logging.getLogger(__name__)

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"

# Multipliers onto an annual figure, keyed by Ashby's `interval` value.
_INTERVAL_FACTORS = {
    "1 YEAR": 1,
    "1 MONTH": 12,
    "1 WEEK": 52,
    "1 DAY": 260,
    "1 HOUR": 40 * 52,
}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _structured_salary(item: dict) -> tuple[int | None, int | None]:
    """Pull an annualized USD range out of the compensation object.

    Two traps here, both live in the captured fixture:

    - A tier's components are not ordered, and an EquityPercentage component
      with null values frequently comes first. Taking components[0] reads
      equity as salary. Filter on compensationType == "Salary".
    - The interval is not always yearly, so raw minValue/maxValue cannot be
      used as annual figures.

    Non-USD is refused rather than converted, matching pipeline.salary.
    """
    compensation = item.get("compensation") or {}
    if not item.get("shouldDisplayCompensationOnJobPostings", True):
        # The employer chose not to publish a range; anything present is
        # internal. Fall through to "unknown", which passes the salary gate.
        return (None, None)

    for tier in compensation.get("compensationTiers") or []:
        for component in tier.get("components") or []:
            if component.get("compensationType") != "Salary":
                continue
            if (component.get("currencyCode") or "USD") != "USD":
                continue
            factor = _INTERVAL_FACTORS.get(component.get("interval") or "1 YEAR")
            if factor is None:
                continue
            low, high = component.get("minValue"), component.get("maxValue")
            if low is None and high is None:
                continue
            low = int(low * factor) if low is not None else None
            high = int(high * factor) if high is not None else None
            if low is not None and high is not None:
                return (min(low, high), max(low, high))
            value = low if low is not None else high
            return (value, value)

    return (None, None)


class AshbySource:
    name = "ashby"

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        for token in cfg.targets.get(self.name, []):
            try:
                payload = await cfg.session.get_json(
                    BOARD_URL.format(token=token), params={"includeCompensation": "true"}
                )
            except HostBlockedError:
                log.warning("ashby host blocked; stopping source")
                return
            except Exception as exc:  # one bad board must not kill the run
                log.warning("ashby board %s failed: %s", token, exc)
                continue

            if payload is None:  # 304 Not Modified
                continue

            jobs = payload.get("jobs", [])
            if not jobs:
                # Like Lever, a retired board answers 200 with nothing rather
                # than 404, so a dead token would contribute silently forever.
                log.warning("ashby board %s returned no jobs; token may be retired", token)
                continue

            for item in jobs:
                # isListed=False is a posting Ashby is still serving but no
                # longer showing. Applying to one is wasted effort.
                if item.get("isListed") is False:
                    continue

                low, high = _structured_salary(item)
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    company_name=token,
                    title=item["title"],
                    location=item.get("location"),
                    description=item.get("descriptionPlain")
                    or strip_html(item.get("descriptionHtml")),
                    apply_url=item.get("jobUrl") or item["applyUrl"],
                    posted_at=_parse_ts(item.get("publishedAt")),
                    remote_type_hint=item.get("workplaceType"),
                    salary_min=low,
                    salary_max=high,
                    salary_source="structured" if low is not None else "none",
                )
```

- [ ] **Step 8: Register both and extend the target list**

```python
"""Name to source mapping, and the target-employer list loader."""

from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.sources.ashby import AshbySource
from pipeline.sources.base import Source
from pipeline.sources.greenhouse import GreenhouseSource
from pipeline.sources.lever import LeverSource

SOURCES: dict[str, Source] = {
    GreenhouseSource.name: GreenhouseSource(),
    LeverSource.name: LeverSource(),
    AshbySource.name: AshbySource(),
}


def load_targets(path: Path) -> dict[str, list[str]]:
    """Read the per-ATS board token lists. A missing file is not an error."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {k: list(v or []) for k, v in data.items()}
```

`profile.example/targets.yaml` — every token verified live, with its count:

```yaml
# Board tokens per ATS. Copy to profile/targets.yaml and edit.
#
#   mkdir -p profile && cp profile.example/targets.yaml profile/targets.yaml
#
# Finding a token: open a company's careers page and read the URL.
#   job-boards.greenhouse.io/stripe        -> greenhouse: stripe
#   jobs.lever.co/spotify                  -> lever:      spotify
#   jobs.ashbyhq.com/ramp                  -> ashby:      ramp
#
# Verify before adding. A wrong token fails in two different ways, and only one
# of them is loud:
#   * 404            -> logged as a failed board
#   * 200 with []    -> a retired board, indistinguishable from "nothing open"
# The adapters warn on the empty case, but check the count yourself first:
#   curl -s "https://api.lever.co/v0/postings/<token>?mode=json" | head -c 200
#
# Every token below was verified live on 2026-08-01 with the count shown.
# Companies migrate between ATSes, so re-check any token that goes quiet.

greenhouse:
  - stripe # 548 open roles
  - figma # 176
  - gitlab # 184
  - databricks # 803
  - anthropic # 400
  - cloudflare # 285
  - robinhood # 128
  - affirm # 181
  - brex # 302
  - samsara # 308

lever:
  - spotify # 105
  - matchgroup # 83
  - fetchpackage # 24

# Publishing compensation is a per-employer Ashby setting, not a platform
# guarantee. The percentage is how many of that board's postings carried
# structured salary when checked -- boards at 0% still contribute jobs, their
# salary just falls back to text parsing like everywhere else.
ashby:
  - ramp # 126 roles, 95% with structured compensation
  - openai # 753, 80%
  - perplexity # 87, 82%
  - vanta # 100, 62%
  - clickhouse # 166, 0%
  - notion # 109, 0%
  - cursor # 120, 0%
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/sources/ -v`
Expected: PASS (43 tests)

- [ ] **Step 10: Run all three sources live and inspect the totals**

```
greenhouse  n= 400  salary: structured=  0 parsed=318 none= 82  {'hybrid': 354, 'remote': 46}
lever       n= 105  salary: structured=  0 parsed= 33 none= 72  {'hybrid': 47, 'onsite': 25, 'remote': 33}
ashby       n= 292  salary: structured=113 parsed=  1 none=178  {'hybrid': 104, 'remote': 84, 'onsite': 104}

total 797, unique fingerprints 708
posted_at years: {2023: 1, 2024: 6, 2025: 87, 2026: 703}, none missing
```

Check the collisions before trusting that 708. Of 40 colliding groups, **39 are
identical titles at identical locations** (ClickHouse posts one role five
times), and the 40th is a double-space typo that normalization correctly
collapses. Zero genuine over-collapses. A collision count alone does not tell
you whether dedup is working or destroying data — the breakdown does.

- [ ] **Step 11: Commit**

```bash
git add pipeline/ tests/ profile.example/
git commit -m "feat: Lever and Ashby adapters, structured work-arrangement hint"
```

---

### Task 7: Remotive and HN "Who is Hiring" adapters

**Files:**
- Create: `pipeline/sources/remotive.py`, `pipeline/sources/hn_algolia.py`
- Modify: `pipeline/sources/registry.py`, `tests/sources/test_registry.py`
- Test: `tests/sources/test_remotive.py`, `tests/sources/test_hn_algolia.py`, fixtures for both

**Interfaces:**
- Consumes: `SourceConfig`, `RawJob`, `HostBlockedError`
- Produces:
  - `RemotiveSource` (`name = "remotive"`), `RemotiveSource.MIN_INTERVAL_SECONDS`
  - `HNAlgoliaSource` (`name = "hn_algolia"`), `pipeline.sources.hn_algolia.parse_hn_comment(comment: dict) -> RawJob | None`
  - `pipeline.sources.registry.TOKENLESS_SOURCES: frozenset[str]`

These are aggregators, not ATSes: they cover the market rather than a company list, so they need no `targets.yaml` entry. `TOKENLESS_SOURCES` makes that explicit — otherwise the registry test demanding example tokens for every source fails on two sources that cannot have any.

HN is the one that proves the `Source` protocol is not accidentally ATS-shaped: jobs there are free-text comments written to a loose human convention.

**Remotive: two findings that change the adapter.**

1. **Its filter parameters do not work.** `?category=software-development`, `?category=devops` and `?search=engineer` all return results identical to the unfiltered endpoint — verified live, all 34 jobs, all 13 categories, every time. The plan originally specified `?category=software-dev&limit=20`, which is doubly wrong: that slug does not exist (`software-development` does), and **an unrecognised category is silently ignored rather than rejected**. Sending it would give false confidence that sales and design postings had been excluded. The adapter sends nothing and lets the pre-filter work.
2. **Its response body carries a legal notice with a rate expectation**: *"there is absolutely no need to request Remotive job data too frequently... we advise max. 4 times a day... excessive requests will be blocked."* That is stricter than PoliteSession's per-request delay can express, so it is declared as `MIN_INTERVAL_SECONDS = 6h` for the orchestrator. The same notice requires attribution and a link back, which we satisfy by storing Remotive's own `url` and tagging rows `source="remotive"`.

Also: Remotive's `publication_date` is **naive**. Left alone it raises at insert time against a `TIMESTAMPTZ` column rather than merely being wrong.

**HN: three traps, all found by reading a live thread.**

1. **`whoishiring` posts "Who is hiring?" and "Who wants to be hired?" on the same day.** The second is job *seekers*. Ingesting it fills the queue with candidates advertising themselves. Match on the title, and exclude the sibling threads.
2. **Half the comments are replies, not postings.** Only `parent_id == story_id` is a job. On the measured thread that was exactly 50%.
3. **Detect the company by position, not by content.** The convention is `Company | Role | ...`. A content-based detector reads "TypeSafe AI" and "Cora AI" as role text and drops those employers — it cost 10 points of yield (69% vs 79%) in a prototype.

**Measured on the July 2026 thread:** 49 top-level comments in the first page, 39 parsed. Over the full thread, 160 jobs. Every skip was a post naming no company, naming no role, using a non-pipe format, or — in one case — a top-level comment that was a C++ book recommendation rather than a job. Chasing the remaining 21% means loosening the parser toward garbage; spec §6 asks it to degrade gracefully instead.

- [ ] **Step 1: Capture both fixtures**

```bash
./venv/bin/python scripts/capture_fixture.py remotive_jobs \
  "https://remotive.com/api/remote-jobs?category=software-development" --trim jobs=4
```

For HN the thread must be found before its comments can be fetched, and the
default relevance-ranked search returns threads from 2016–2020. Query by date:

```bash
./venv/bin/python scripts/capture_fixture.py hn_hiring_thread \
  "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=8"
# read the newest hit whose title matches "Who is hiring?", then:
./venv/bin/python scripts/capture_fixture.py hn_hiring_comments \
  "https://hn.algolia.com/api/v1/search?tags=comment,story_<ID>&hitsPerPage=6" --trim hits=6
```

- [ ] **Step 2: Write the failing tests**

`tests/sources/test_remotive.py`:

```python
import json
from pathlib import Path

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.normalize import to_job
from pipeline.sources.base import SourceConfig
from pipeline.sources.remotive import MIN_INTERVAL_SECONDS, RemotiveSource

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "remotive_jobs.json"
JOBS = "https://remotive.com/api/remote-jobs"


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=_noop_sleep),
        targets={},
        settings=load_settings(env={"DATABASE_URL": "postgresql://x/y"}),
    )


async def _collect(cfg):
    return [job async for job in RemotiveSource().fetch(cfg)]


def _job(**overrides):
    base = {
        "id": 1,
        "url": "https://remotive.com/remote-jobs/x-1",
        "title": "Senior Backend Engineer",
        "company_name": "Acme",
        "description": "<p>Build things</p>",
        "publication_date": "2026-07-28T14:23:05",
        "candidate_required_location": "USA",
    }
    return {**base, **overrides}


async def test_needs_no_target_tokens(httpx_mock, cfg):
    # An aggregator covers the market, not a company list. cfg.targets is empty.
    httpx_mock.add_response(url=JOBS, json=json.loads(FIXTURE.read_text()))
    assert await _collect(cfg)


async def test_fetch_yields_rawjobs_from_captured_fixture(httpx_mock, cfg):
    httpx_mock.add_response(url=JOBS, json=json.loads(FIXTURE.read_text()))
    jobs = await _collect(cfg)
    for job in jobs:
        assert job.source == "remotive"
        assert job.source_job_id and job.title and job.company_name
        assert job.apply_url.startswith("http")
        assert job.posted_at is not None


async def test_naive_publication_date_becomes_utc_aware(httpx_mock, cfg):
    """Remotive publishes naive timestamps.

    Leaving them naive puts a tz-aware column and a tz-naive value in the same
    comparison, which raises at insert time rather than merely being wrong.
    """
    httpx_mock.add_response(url=JOBS, json={"jobs": [_job(publication_date="2026-07-28T14:23:05")]})
    jobs = await _collect(cfg)
    assert jobs[0].posted_at.tzinfo is not None
    assert jobs[0].posted_at.utcoffset().total_seconds() == 0


async def test_every_job_is_marked_remote(httpx_mock, cfg):
    httpx_mock.add_response(url=JOBS, json={"jobs": [_job()]})
    jobs = await _collect(cfg)
    assert jobs[0].remote_type_hint == "remote"
    assert to_job(jobs[0]).remote_type == "remote"


async def test_description_html_is_stripped(httpx_mock, cfg):
    httpx_mock.add_response(
        url=JOBS, json={"jobs": [_job(description="<p>Pay is $150,000 &amp; up</p>")]}
    )
    jobs = await _collect(cfg)
    assert "<p>" not in jobs[0].description
    assert "$150,000 & up" in jobs[0].description


async def test_no_filter_parameters_are_sent(httpx_mock, cfg):
    """Remotive's category and search parameters are non-functional.

    Verified live: ?category=software-development, ?category=devops and
    ?search=engineer all return byte-identical results to the bare endpoint.
    Sending one would imply a filter that never ran, so sales and design
    postings would silently be treated as pre-filtered software roles.
    """
    httpx_mock.add_response(url=JOBS, json={"jobs": [_job()]})
    await _collect(cfg)
    request = httpx_mock.get_requests()[0]
    assert request.url.params.get("category") is None
    assert request.url.params.get("search") is None


def test_declares_the_rate_limit_their_terms_ask_for():
    # Their legal notice: "we advise max. 4 times a day... excessive requests
    # will be blocked." Six hours between calls satisfies that.
    assert MIN_INTERVAL_SECONDS >= 6 * 60 * 60


async def test_missing_company_name_does_not_crash(httpx_mock, cfg):
    httpx_mock.add_response(url=JOBS, json={"jobs": [_job(company_name=None)]})
    jobs = await _collect(cfg)
    assert jobs[0].company_name == "unknown"


async def test_blocked_host_stops_the_source(httpx_mock, cfg):
    httpx_mock.add_response(url=JOBS, status_code=403)
    assert await _collect(cfg) == []


async def test_304_yields_nothing(httpx_mock, cfg):
    httpx_mock.add_response(url=JOBS, status_code=304)
    assert await _collect(cfg) == []


async def test_server_error_does_not_kill_the_run(httpx_mock, cfg):
    for _ in range(3):
        httpx_mock.add_response(url=JOBS, status_code=500)
    assert await _collect(cfg) == []


def test_captured_fixture_proves_the_filter_is_ignored():
    """The fixture was captured with ?category=software-development.

    It still contains non-software categories. This asserts against captured
    bytes so the finding cannot quietly rot into an assumption.
    """
    payload = json.loads(FIXTURE.read_text())
    categories = {j["category"] for j in payload["jobs"]}
    assert categories - {"Software Development"}, (
        "fixture should still contain non-software categories"
    )
```

`tests/sources/test_hn_algolia.py`:

```python
import json
from pathlib import Path

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.sources.base import SourceConfig
from pipeline.sources.hn_algolia import HNAlgoliaSource, parse_hn_comment

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hn_hiring_comments.json"
BY_DATE = (
    "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=20"
)
STORY_ID = "48747976"


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=_noop_sleep),
        targets={},
        settings=load_settings(env={"DATABASE_URL": "postgresql://x/y"}),
    )


def _comments_url(story_id=STORY_ID, page=0):
    return (
        f"https://hn.algolia.com/api/v1/search?tags=comment,story_{story_id}"
        f"&hitsPerPage=1000&page={page}"
    )


def _thread(*titles):
    return {
        "hits": [
            {"objectID": f"{9000 + i}", "title": t, "num_comments": 100}
            for i, t in enumerate(titles)
        ]
    }


def _comment(text, object_id="1", story_id=STORY_ID, parent_id=None):
    return {
        "objectID": object_id,
        "comment_text": text,
        "story_id": story_id,
        "parent_id": parent_id if parent_id is not None else story_id,
        "created_at": "2026-07-01T16:00:00Z",
    }


async def _collect(cfg):
    return [job async for job in HNAlgoliaSource().fetch(cfg)]


# --- parse_hn_comment: a pure function, so most cases need no network --------


def test_parses_the_standard_header():
    job = parse_hn_comment(
        _comment("Portless | AI Engineer | Remote (North America) | $180k-$230k | Full-time")
    )
    assert job.company_name == "Portless"
    assert job.title == "AI Engineer"
    assert job.location == "Remote (North America)"
    assert job.apply_url == "https://news.ycombinator.com/item?id=1"


def test_company_is_taken_by_position_not_by_content():
    """Detecting the company by content misfires on AI-suffixed names.

    "TypeSafe AI" and "Cora AI" both read as role text, which pushed a working
    prototype from 79% yield down to 69% and silently dropped real employers.
    """
    job = parse_hn_comment(
        _comment("TypeSafe AI | AI research & engineering | San Francisco, CA | ONSITE | Full-time")
    )
    assert job.company_name == "TypeSafe AI"
    assert job.title == "AI research & engineering"


def test_strips_a_url_parenthetical_from_the_company():
    job = parse_hn_comment(
        _comment("Chronograph ( https://chronograph.pe ) | Platform Engineer | Remote (US)")
    )
    assert job.company_name == "Chronograph"


def test_strips_markdown_emphasis():
    job = parse_hn_comment(_comment("*OneChronos | Technical Lead | NYC (HQ) | Full-Time*"))
    assert job.company_name == "OneChronos"


def test_skips_a_post_with_no_role_in_the_header():
    assert parse_hn_comment(_comment("Marketron | REMOTE (US) | Full-time | 70k - 90k")) is None


def test_skips_a_post_with_no_company():
    # Here the first segment is the role, so no employer was named. The Sharia
    # screen is per-company (spec section 9), so this can never clear it.
    assert (
        parse_hn_comment(_comment("Engineering Manager | Remote (North America) | $180k")) is None
    )


def test_skips_non_pipe_formats_rather_than_guessing():
    assert (
        parse_hn_comment(_comment("Proton:Senior Foundation Engineer (Drive):Geneva, ONSITE"))
        is None
    )
    assert parse_hn_comment(_comment("This book was one of the most useful C++ resources")) is None


def test_employment_type_alone_is_not_a_title():
    assert parse_hn_comment(_comment("Acme | Full-time | Senior Roles | India")) is None


def test_reads_only_the_first_paragraph_as_the_header():
    job = parse_hn_comment(
        _comment("Acme | Backend Engineer | Remote<p>We are a great place | with pipes | in prose")
    )
    assert job.title == "Backend Engineer"


def test_unescapes_entities_in_the_description():
    job = parse_hn_comment(
        _comment("Acme | Backend Engineer | Remote<p>Stack is React&#x2F;Node &amp; Python")
    )
    assert "React/Node & Python" in job.description
    assert "&#x2F;" not in job.description


def test_arrangement_hint_comes_from_the_location_segment():
    assert parse_hn_comment(_comment("Acme | Engineer | ONSITE")).remote_type_hint == "ONSITE"
    assert parse_hn_comment(_comment("Acme | Engineer | Remote (US)")).remote_type_hint == (
        "Remote (US)"
    )


def test_rejects_an_absurdly_long_company_segment():
    long_name = "x" * 80
    assert parse_hn_comment(_comment(f"{long_name} | Backend Engineer | Remote")) is None


def test_parses_every_top_level_comment_in_the_captured_fixture():
    payload = json.loads(FIXTURE.read_text())
    top = [h for h in payload["hits"] if h.get("parent_id") == h.get("story_id")]
    parsed = [parse_hn_comment(h) for h in top]
    # Whatever parses must be well-formed; unparseable posts return None.
    for job in filter(None, parsed):
        assert job.company_name and job.title and job.apply_url.startswith("http")


# --- fetch: thread selection and reply filtering -----------------------------


async def test_picks_the_hiring_thread_not_the_job_seekers_thread(httpx_mock, cfg):
    """whoishiring posts both on the same day.

    "Who wants to be hired?" is candidates advertising themselves. Ingesting it
    fills the queue with people rather than jobs.
    """
    httpx_mock.add_response(
        url=BY_DATE,
        json={
            "hits": [
                {"objectID": "111", "title": "Ask HN: Who wants to be hired? (July 2026)"},
                {"objectID": STORY_ID, "title": "Ask HN: Who is hiring? (July 2026)"},
            ]
        },
    )
    httpx_mock.add_response(
        url=_comments_url(),
        json={"hits": [_comment("Acme | Backend Engineer | Remote")], "nbPages": 1},
    )
    jobs = await _collect(cfg)
    assert [j.company_name for j in jobs] == ["Acme"]


async def test_skips_the_freelancer_thread(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BY_DATE,
        json=_thread("Ask HN: Freelancer? Seeking freelancer? (July 2026)"),
    )
    assert await _collect(cfg) == []


async def test_replies_are_not_treated_as_job_posts(httpx_mock, cfg):
    """Exactly half the comments on the measured thread were replies."""
    httpx_mock.add_response(
        url=BY_DATE, json={"hits": [{"objectID": STORY_ID, "title": "Ask HN: Who is hiring?"}]}
    )
    httpx_mock.add_response(
        url=_comments_url(),
        json={
            "hits": [
                _comment("Acme | Backend Engineer | Remote", object_id="1"),
                _comment("Globex | Frontend Engineer | Remote", object_id="2", parent_id=99999),
            ],
            "nbPages": 1,
        },
    )
    jobs = await _collect(cfg)
    assert [j.source_job_id for j in jobs] == ["1"]


async def test_no_hiring_thread_found_yields_nothing(httpx_mock, cfg):
    httpx_mock.add_response(url=BY_DATE, json={"hits": []})
    assert await _collect(cfg) == []


async def test_paginates_until_the_last_page(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BY_DATE, json={"hits": [{"objectID": STORY_ID, "title": "Ask HN: Who is hiring?"}]}
    )
    httpx_mock.add_response(
        url=_comments_url(page=0),
        json={"hits": [_comment("A | Backend Engineer | Remote", object_id="1")], "nbPages": 2},
    )
    httpx_mock.add_response(
        url=_comments_url(page=1),
        json={"hits": [_comment("B | Frontend Engineer | Remote", object_id="2")], "nbPages": 2},
    )
    jobs = await _collect(cfg)
    assert [j.source_job_id for j in jobs] == ["1", "2"]


async def test_blocked_host_stops_the_source(httpx_mock, cfg):
    httpx_mock.add_response(url=BY_DATE, status_code=403)
    assert await _collect(cfg) == []


def test_strips_domain_parentheticals_with_any_tld():
    # A hardcoded TLD list left "Chronograph (chronograph.pe)" unstripped in a
    # live run; country-code TLDs are too common to enumerate.
    for header in (
        "Chronograph (chronograph.pe) | Platform Engineer | Remote (US)",
        "Chronograph ( https://chronograph.pe ) | Platform Engineer | Remote (US)",
        "Chronograph (chronograph.co.uk/careers) | Platform Engineer | Remote",
    ):
        assert parse_hn_comment(_comment(header)).company_name == "Chronograph"


def test_does_not_strip_a_meaningful_parenthetical_from_the_company():
    job = parse_hn_comment(_comment("Acme (YC W21) | Backend Engineer | Remote"))
    assert job.company_name == "Acme (YC W21)"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/sources/ -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.sources.remotive'`

- [ ] **Step 4: Implement the Remotive adapter**

```python
"""Remotive aggregator. Remote-only board, no per-company target list.

Two things about this API are load-bearing, both read off a live response:

Its filter parameters do not work. `?category=software-development`,
`?category=devops` and `?search=engineer` all return byte-identical results to
the unfiltered endpoint. Passing them would give false confidence that sales and
design postings had been excluded, so this fetches everything once and lets the
deterministic pre-filter do the work it already does for every other source.

Its response carries a legal notice with a rate expectation: "there is
absolutely no need to request Remotive job data too frequently... we advise max.
4 times a day... excessive requests will be blocked." That is a stricter budget
than PoliteSession's per-request delay expresses, so it is declared here as
MIN_INTERVAL_SECONDS for the orchestrator to honour.

The same notice requires attribution and a link back. We store Remotive's own
`url` as the apply link and tag every row with source="remotive", so the UI
attributes it. Nothing is ever republished anywhere.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig
from pipeline.text import strip_html

log = logging.getLogger(__name__)

JOBS_URL = "https://remotive.com/api/remote-jobs"

# Their published guidance is a maximum of four calls per day.
MIN_INTERVAL_SECONDS = 6 * 60 * 60


def _parse_ts(value: str | None) -> datetime | None:
    """Remotive publishes naive timestamps; they are UTC.

    Leaving them naive would put a tz-aware column and a tz-naive value in the
    same comparison and raise at insert time.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


class RemotiveSource:
    name = "remotive"

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        try:
            payload = await cfg.session.get_json(JOBS_URL)
        except HostBlockedError:
            log.warning("remotive host blocked; stopping source")
            return
        except Exception as exc:
            log.warning("remotive fetch failed: %s", exc)
            return

        if payload is None:  # 304 Not Modified
            return

        for item in payload.get("jobs", []):
            yield RawJob(
                source=self.name,
                source_job_id=str(item["id"]),
                company_name=item.get("company_name") or "unknown",
                title=item["title"],
                # Not a city: this is the geography a candidate may sit in.
                location=item.get("candidate_required_location"),
                description=strip_html(item.get("description")),
                apply_url=item["url"],
                posted_at=_parse_ts(item.get("publication_date")),
                # Remotive lists remote work exclusively.
                remote_type_hint="remote",
            )
```

- [ ] **Step 5: Implement the HN adapter**

```python
"""Hacker News "Ask HN: Who is hiring?" via the Algolia search API.

Structurally unlike the ATS sources, which is why it earns a place in Phase 1:
if the Source protocol can carry this, it is not accidentally ATS-shaped. Jobs
here are free-text comments written by humans to a loose convention, so parsing
is heuristic and must refuse rather than guess.

Three traps, all found by reading a live thread:

1. `whoishiring` posts "Who is hiring?" and "Who wants to be hired?" on the same
   day. The second is job *seekers*. Ingesting it fills the queue with
   candidates advertising themselves.
2. Half the comments on the thread are replies, not postings. Only top-level
   comments -- `parent_id == story_id` -- are jobs.
3. The header convention is `Company | Role | Location | Type | Salary`, but
   field order varies and both company and role are sometimes missing entirely.

Measured on the July 2026 thread: 49 top-level comments, 39 parsed (79%). Every
skip was a post with no company named, no role in the header, a non-pipe format,
or -- in one case -- a comment that was not a job at all.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig
from pipeline.text import strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"

# Algolia caps a page at 1000; threads run to several hundred comments.
_PAGE_SIZE = 1000
_MAX_PAGES = 3

# "Who is hiring?" only. The sibling threads are job seekers and freelancers.
_HIRING_TITLE_RE = re.compile(r"who\s+is\s+hiring", re.IGNORECASE)
_NOT_HIRING_TITLE_RE = re.compile(r"wants\s+to\s+be\s+hired|freelancer", re.IGNORECASE)

_ROLE_RE = re.compile(
    r"\b(engineer(ing)?|developer|scientist|architect|sre|devops|programmer|analyst"
    r"|designer|manager|lead|founder|cto|full[\s-]?stack|backend|frontend"
    r"|infrastructure|security|researcher)\b",
    re.IGNORECASE,
)
_ARRANGEMENT_RE = re.compile(r"\b(remote|onsite|on-site|hybrid)\b", re.IGNORECASE)
_EMPLOYMENT_TYPE_RE = re.compile(
    r"^\s*(full[\s-]?time|part[\s-]?time|contract|intern(ship)?|freelance|flexible|permanent)\s*$",
    re.IGNORECASE,
)
# "Acme ( https://acme.com )" and "Acme (acme.io)" -> "Acme".
# Matches any domain-shaped parenthetical rather than a TLD allow-list: a list
# left "Chronograph (chronograph.pe)" unstripped, and country-code TLDs are
# common enough in company URLs that enumerating them is a losing game.
_URL_PAREN_RE = re.compile(
    r"\(\s*(?:https?://)?[\w.-]+\.[a-z]{2,}(?:/[^)\s]*)?\s*\)",
    re.IGNORECASE,
)

_MAX_COMPANY_LEN = 60


def _header_line(comment_html: str | None) -> str:
    """The first paragraph of a comment, which is where the header lives."""
    head = re.split(r"<p>", comment_html or "", maxsplit=1)[0]
    return strip_html(head)


def parse_hn_comment(comment: dict) -> RawJob | None:
    """Turn one top-level thread comment into a RawJob, or None if unparseable.

    Returns None generously. A malformed row costs review time on every future
    digest, whereas a skipped post costs one job we never knew about.
    """
    header = _header_line(comment.get("comment_text"))
    segments = [s.strip(" *_·-") for s in header.split("|")]
    segments = [s for s in segments if s]
    if len(segments) < 2:
        return None

    # By convention the company comes first. Detecting it by content instead
    # misfires badly: "TypeSafe AI" and "Cora AI" read as role text.
    company = _URL_PAREN_RE.sub("", segments[0]).strip(" -–—,")
    if not company or len(company) > _MAX_COMPANY_LEN:
        return None

    title = next(
        (s for s in segments[1:] if _ROLE_RE.search(s) and not _EMPLOYMENT_TYPE_RE.match(s)),
        None,
    )
    if not title:
        # Either the post names no role, or the company slot actually held the
        # role and no employer was given. Both are unusable: the Sharia screen
        # (spec section 9) is per-company, so a job with no employer cannot
        # clear it, and a job with no role cannot be scored.
        return None

    location = next(
        (s for s in segments[1:] if s != title and _ARRANGEMENT_RE.search(s)),
        None,
    )

    object_id = str(comment["objectID"])
    return RawJob(
        source=HNAlgoliaSource.name,
        source_job_id=object_id,
        company_name=company,
        title=title,
        location=location,
        description=strip_html(comment.get("comment_text")),
        apply_url=ITEM_URL.format(id=object_id),
        posted_at=_parse_ts(comment.get("created_at")),
        remote_type_hint=location,
    )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class HNAlgoliaSource:
    name = "hn_algolia"

    async def _latest_thread_id(self, cfg: SourceConfig) -> str | None:
        payload = await cfg.session.get_json(
            SEARCH_BY_DATE_URL,
            params={"tags": "story,author_whoishiring", "hitsPerPage": "20"},
        )
        if not payload:
            return None
        for hit in payload.get("hits", []):
            title = hit.get("title") or ""
            if _HIRING_TITLE_RE.search(title) and not _NOT_HIRING_TITLE_RE.search(title):
                log.info("hn: using thread %s (%s)", hit.get("objectID"), title)
                return str(hit["objectID"])
        log.warning("hn: no 'Who is hiring' thread found")
        return None

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        try:
            story_id = await self._latest_thread_id(cfg)
            if story_id is None:
                return

            for page in range(_MAX_PAGES):
                payload = await cfg.session.get_json(
                    SEARCH_URL,
                    params={
                        "tags": f"comment,story_{story_id}",
                        "hitsPerPage": str(_PAGE_SIZE),
                        "page": str(page),
                    },
                )
                if not payload:
                    return

                hits = payload.get("hits", [])
                for hit in hits:
                    # Replies are discussion, not postings. On the thread we
                    # measured, exactly half the comments were replies.
                    if hit.get("parent_id") != hit.get("story_id"):
                        continue
                    job = parse_hn_comment(hit)
                    if job is not None:
                        yield job

                if page + 1 >= payload.get("nbPages", 1):
                    return
        except HostBlockedError:
            log.warning("hn host blocked; stopping source")
            return
        except Exception as exc:
            log.warning("hn fetch failed: %s", exc)
            return
```

- [ ] **Step 6: Register both and teach the registry test about tokenless sources**

```python
"""Name to source mapping, and the target-employer list loader."""

from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.sources.ashby import AshbySource
from pipeline.sources.base import Source
from pipeline.sources.greenhouse import GreenhouseSource
from pipeline.sources.hn_algolia import HNAlgoliaSource
from pipeline.sources.lever import LeverSource
from pipeline.sources.remotive import RemotiveSource

SOURCES: dict[str, Source] = {
    GreenhouseSource.name: GreenhouseSource(),
    LeverSource.name: LeverSource(),
    AshbySource.name: AshbySource(),
    RemotiveSource.name: RemotiveSource(),
    HNAlgoliaSource.name: HNAlgoliaSource(),
}

# Aggregators cover the whole market rather than a company list, so they need no
# targets.yaml entry. Kept explicit so a missing section never reads as a
# misconfiguration.
TOKENLESS_SOURCES = frozenset({RemotiveSource.name, HNAlgoliaSource.name})


def load_targets(path: Path) -> dict[str, list[str]]:
    """Read the per-ATS board token lists. A missing file is not an error."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {k: list(v or []) for k, v in data.items()}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/sources/ -v`
Expected: PASS (96 tests)

- [ ] **Step 8: Run all five sources live**

```
greenhouse  n= 576  salary={'parsed': 427, 'none': 149}          remote={'hybrid': 379, 'onsite': 144, 'remote': 53}
lever       n= 105  salary={'none': 72, 'parsed': 33}            remote={'hybrid': 47, 'onsite': 25, 'remote': 33}
ashby       n= 126  salary={'structured': 113, 'none': 13}       remote={'hybrid': 101, 'remote': 16, 'onsite': 9}
remotive    n=  34  salary={'none': 17, 'parsed': 17}            remote={'remote': 34}
hn_algolia  n= 160  salary={'none': 117, 'parsed': 43}           remote={'remote': 87, 'onsite': 47, 'hybrid': 26}

TOTAL 1001, unique fingerprints 1000
```

Inspect the output, not just the counts. This run surfaced a company name of
`Chronograph (chronograph.pe)` — the domain-stripping regex used a hardcoded TLD
allow-list and `.pe` was not on it. Replaced with a generic domain shape, since
country-code TLDs in company URLs are too common to enumerate.

- [ ] **Step 9: Commit**

```bash
git add pipeline/sources/ tests/sources/ tests/fixtures/
git commit -m "feat: Remotive and HN Who-is-Hiring adapters"
```

---

### Task 8: Deterministic pre-filter

**Files:**
- Create: `pipeline/filters/__init__.py`, `pipeline/filters/prefilter.py`
- Test: `tests/test_prefilter.py`

**Interfaces:**
- Consumes: `Job`, `Settings`
- Produces:
  - `pipeline.filters.prefilter.FilterResult` — frozen dataclass: `passed: bool`, `reason: str | None`
  - `pipeline.filters.prefilter.prefilter(job: Job, settings: Settings) -> FilterResult`
  - `pipeline.filters.prefilter.TARGET_TITLE_PATTERNS: list[re.Pattern]`

This is spec §8 gate 1 — the free stage that must kill ~70% of intake before anything paid runs.

**The errors here are asymmetric, and that decides the whole design.** A false accept costs a little scoring compute and gets corrected by the embedding and LLM stages downstream. A false reject is silent, permanent, and unrecoverable — the job simply never existed as far as the rest of the system is concerned. So when a rule is arguable, it errs toward passing.

Two consequences:

- **Unknown salary must pass.** Measured across five live sources, a third of postings carried no parseable figure — 149 from Greenhouse, 117 from HN. Rejecting on unknown discards most of real intake.
- **Clearance and citizenship language must pass** (spec §15.11). Jarra is a US-born citizen, so "US Citizenship required", "must be able to obtain a security clearance", and "ITAR" are eligibility *matches*. There is deliberately no rule keyed on those words; the module docstring and a regression test exist so nobody adds one.

**Reject reasons are distinct on purpose.** `management_role` and `seniority_mismatch` are different failures — management is a different job, not the same job at a higher level — and keeping them apart is what lets the `run_log` breakdown answer "wrong function" versus "right function, wrong level". The plan's original tests and implementation disagreed here: the tests expected `"Engineering Manager"` to yield `title_not_target` while the implementation returned `seniority_mismatch`.

**Match against the role head, not the whole title.** Everything after the first comma or dash is a team, product, or location. `"Software Engineer, Ads Manager"` is an IC engineering job on the ads product; matching the full string rejected it as management. Found by reading rejections from a live corpus, not by unit testing.

- [ ] **Step 1: Write the failing test**

`tests/test_prefilter.py`:

```python
import pytest

from pipeline.config import load_settings
from pipeline.filters.prefilter import prefilter
from pipeline.models import Job

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y"})


def make_job(**overrides) -> Job:
    base = dict(
        fingerprint="f" * 64,
        source="greenhouse",
        source_job_id="1",
        company_name="Acme",
        normalized_company="acme",
        title="Senior Software Engineer",
        location="Remote",
        remote_type="remote",
        salary_min=None,
        salary_max=None,
        salary_source="none",
        description="Python, React, AWS, Docker.",
        apply_url="https://example.com/1",
        posted_at=None,
    )
    base.update(overrides)
    return Job(**base)


def test_relevant_role_passes():
    assert prefilter(make_job(), SETTINGS).passed


def test_reason_is_none_when_passed():
    assert prefilter(make_job(), SETTINGS).reason is None


# --- salary ------------------------------------------------------------------


def test_salary_below_floor_is_rejected():
    result = prefilter(
        make_job(salary_min=90_000, salary_max=110_000, salary_source="structured"), SETTINGS
    )
    assert not result.passed
    assert result.reason == "salary_below_floor"


def test_salary_at_floor_passes():
    assert prefilter(
        make_job(salary_min=125_000, salary_max=140_000, salary_source="structured"), SETTINGS
    ).passed


def test_unknown_salary_passes():
    """The single most consequential rule in this gate.

    Measured across five live sources, 355 of 1001 postings carried no
    parseable salary at all — including 149 from Greenhouse and 117 from HN.
    Rejecting on unknown would discard a third of real intake.
    """
    assert prefilter(make_job(salary_source="none"), SETTINGS).passed


def test_max_above_floor_passes_even_if_min_below():
    # A $115k-$160k band is worth seeing; the floor is about the ceiling.
    assert prefilter(
        make_job(salary_min=115_000, salary_max=160_000, salary_source="parsed"), SETTINGS
    ).passed


def test_single_figure_below_floor_is_rejected():
    assert (
        prefilter(
            make_job(salary_min=95_000, salary_max=95_000, salary_source="parsed"), SETTINGS
        ).reason
        == "salary_below_floor"
    )


# --- titles ------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Full Stack Developer",
        "Full-Stack Engineer",
        "Backend Engineer, Platform",
        "Frontend Engineer",
        "Cloud Infrastructure Engineer",
        "DevOps Engineer",
        "Machine Learning Engineer",
        "AI Engineer",
        "Embedded Software Engineer",
        "Firmware Engineer",
        "Computer Engineer",
        "Site Reliability Engineer",
        "Platform Engineer",
        "Software Engineer II",
        "Engineer, Backend",
        "Senior Python Engineer",
    ],
)
def test_target_titles_pass(title):
    assert prefilter(make_job(title=title), SETTINGS).passed, title


@pytest.mark.parametrize(
    "title",
    [
        "Registered Nurse",
        "Account Executive",
        "Warehouse Associate",
        "Senior Graphic Designer",
        "Business Development Representative",
        "Technical Recruiter",
    ],
)
def test_off_target_titles_are_rejected(title):
    result = prefilter(make_job(title=title), SETTINGS)
    assert not result.passed
    assert result.reason == "title_not_target"


@pytest.mark.parametrize(
    "title",
    ["Engineering Manager", "VP of Engineering", "Director of Product", "Head of Platform"],
)
def test_management_roles_are_rejected_as_off_target(title):
    # Not a seniority mismatch: management is a different job, not the same job
    # at a higher level. Keeping the reasons distinct is what makes the run_log
    # breakdown answer "wrong level" vs "wrong function".
    result = prefilter(make_job(title=title), SETTINGS)
    assert not result.passed
    assert result.reason == "management_role"


@pytest.mark.parametrize(
    "title", ["Senior Staff Engineer", "Principal Engineer", "Distinguished Engineer"]
)
def test_far_above_level_titles_are_rejected(title):
    result = prefilter(make_job(title=title), SETTINGS)
    assert not result.passed
    assert result.reason == "seniority_mismatch"


def test_internship_is_rejected():
    assert prefilter(make_job(title="Software Engineering Intern"), SETTINGS).reason == (
        "title_not_target"
    )


def test_senior_is_not_treated_as_over_level():
    # "Senior" is the target band, not above it.
    assert prefilter(make_job(title="Senior Backend Engineer"), SETTINGS).passed


# --- clearance and citizenship (spec section 15.11) --------------------------


@pytest.mark.parametrize(
    "text",
    [
        "US Citizenship required for this position.",
        "Must be able to obtain a security clearance.",
        "This role is subject to ITAR regulations.",
        "Candidates must be U.S. Persons under ITAR.",
        "Ability to obtain and maintain a TS/SCI clearance.",
    ],
)
def test_clearance_and_citizenship_language_passes(text):
    """Jarra is a US-born citizen, so these are eligibility *matches*.

    "Clearance" reads like an exclusion term and is exactly the rule a future
    edit would add by mistake. The defense and aerospace embedded roles that
    carry this language also face a structurally smaller applicant pool.
    """
    result = prefilter(make_job(description=text), SETTINGS)
    assert result.passed, f"{text!r} was rejected as {result.reason}"


def test_clearance_in_the_title_also_passes():
    assert prefilter(
        make_job(title="Embedded Software Engineer - TS/SCI Required"), SETTINGS
    ).passed


# --- ordering ----------------------------------------------------------------


def test_title_is_checked_before_salary():
    # A nurse posting with a great salary is still a nurse posting, and the
    # reason recorded should say so.
    result = prefilter(
        make_job(
            title="Registered Nurse",
            salary_min=200_000,
            salary_max=250_000,
            salary_source="structured",
        ),
        SETTINGS,
    )
    assert result.reason == "title_not_target"


# --- the role head names the job; what follows a comma names the team --------


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer, Ads Manager",
        "Backend Engineer, Director Tools",
        "Full Stack Engineer - Principal Product Line",
        "Software Engineer, Staff Scheduling",
    ],
)
def test_management_or_seniority_words_after_a_comma_do_not_reject(title):
    # These are IC engineering roles whose *product* is named after a manager,
    # director, or staffing concept. Matching the whole title rejected them.
    assert prefilter(make_job(title=title), SETTINGS).passed, title


@pytest.mark.parametrize(
    "title",
    ["Manager, Forward Deployed Engineer", "Product Manager, Developer Productivity"],
)
def test_management_in_the_head_still_rejects(title):
    assert prefilter(make_job(title=title), SETTINGS).reason == "management_role"


@pytest.mark.parametrize(
    "title", ["Founding Engineer", "Research Engineer", "Data Engineer", "Security Engineer"]
)
def test_adjacent_engineering_families_pass(title):
    # This gate errs toward passing: a false accept is corrected by scoring,
    # a false reject is silent and permanent.
    assert prefilter(make_job(title=title), SETTINGS).passed, title


@pytest.mark.parametrize(
    "title",
    [
        "Solutions Engineer, Pre-Sales",
        "Field Service Engineer (Automotive)",
        "Product Support Engineer",
        "Technical Recruiter",
    ],
)
def test_customer_facing_engineering_titles_are_still_rejected(title):
    # "Engineer" in the title does not make it an engineering job.
    assert not prefilter(make_job(title=title), SETTINGS).passed, title
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_prefilter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.filters.prefilter'`

- [ ] **Step 3: Implement the pre-filter**

Create empty `pipeline/filters/__init__.py`, then `pipeline/filters/prefilter.py`:

```python
"""Gate 1 of the scoring pipeline: free, deterministic, runs on everything.

Spec section 8 expects this to reject roughly 70% of intake so the paid stages
see very few jobs.

Two rules here are load-bearing and both cut against intuition:

- **Unknown salary passes.** Measured across five live sources, a third of all
  postings carried no parseable figure. Rejecting on unknown discards most of
  the real pipeline.
- **Clearance and citizenship language passes.** Jarra is a US-born citizen
  (spec section 15.11), so "US Citizenship required" and "must be able to obtain
  a security clearance" are eligibility matches, not disqualifiers. There is
  deliberately no rule keyed on those words -- this docstring and the regression
  tests exist so nobody adds one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.config import Settings
from pipeline.models import Job

TARGET_TITLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(software|full[\s-]?stack|back[\s-]?end|front[\s-]?end|web|product)\s+"
        r"(engineer|developer)\b",
        r"\b(cloud|infrastructure|platform|devops|site\s+reliability|sre|systems?)\s+engineer\b",
        r"\b(machine\s+learning|ml|ai|applied\s+scientist)\s+engineer\b",
        r"\b(embedded|firmware|systems)\s+(software\s+)?engineer\b",
        r"\bcomputer\s+engineer\b",
        r"\b(software|application)\s+developer\b",
        r"\bengineer,?\s+(software|platform|infrastructure|back[\s-]?end|front[\s-]?end)\b",
        # Language- and stack-named roles: "Senior Python Engineer".
        r"\b(python|javascript|typescript|react|node|go(?:lang)?|rust|java|c\+\+)\s+"
        r"(engineer|developer)\b",
        # Adjacent families that appear constantly on real boards. Included
        # because the errors here are asymmetric -- see the note below.
        r"\b(founding|research|data|security)\s+engineer\b",
    )
]

# This gate errs toward passing, deliberately. A false accept costs a little
# scoring compute and gets caught by the embedding and LLM stages downstream. A
# false reject is silent, permanent, and unrecoverable -- the job simply never
# existed as far as the rest of the system is concerned. When a family is
# genuinely arguable, it belongs here and Phase 2 can rank it down.

# Management is a different function, not the same function at a higher level.
# Kept separate from seniority so the run_log breakdown can distinguish
# "wrong job" from "right job, wrong level".
MANAGEMENT_RE = re.compile(
    r"\b(manager|director|head\s+of|vp|vice\s+president|chief|cto|ceo)\b",
    re.IGNORECASE,
)

# Individual-contributor levels well above ~3 years of experience. The stretch
# allowance in scoring (spec section 8) reintroduces a small random slice later.
SENIORITY_EXCLUDE_RE = re.compile(
    r"\b(principal|distinguished|staff|fellow|architect)\b",
    re.IGNORECASE,
)

NON_ENGINEERING_RE = re.compile(
    r"\b(nurse|sales|account\s+executive|warehouse|driver|recruiter|recruiting|"
    r"marketing|paralegal|teacher|intern|internship|designer|"
    r"business\s+development|customer\s+success|support\s+specialist)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str | None = None


def _role_head(title: str) -> str:
    """The part of a title that names the role.

    Everything after the first comma or dash is a team, product, or location:
    "Software Engineer, Ads Manager" is an IC engineering job on the ads
    product, not a management job. Matching the whole string rejected it.
    """
    return re.split(r"[,–—]|\s+-\s+", title, maxsplit=1)[0]


def prefilter(job: Job, settings: Settings) -> FilterResult:
    """Cheap, deterministic accept/reject. Never calls out to anything."""
    title = job.title
    head = _role_head(title)

    # Title checks run before salary so a rejected job records *why* it was
    # wrong rather than the first rule that happened to fire.
    if NON_ENGINEERING_RE.search(head):
        return FilterResult(False, "title_not_target")

    if MANAGEMENT_RE.search(head):
        return FilterResult(False, "management_role")

    if SENIORITY_EXCLUDE_RE.search(head):
        return FilterResult(False, "seniority_mismatch")

    if not any(p.search(title) for p in TARGET_TITLE_PATTERNS):
        return FilterResult(False, "title_not_target")

    # Unknown salary passes: most ATSes do not publish it, and rejecting on
    # unknown would discard the majority of real intake.
    if job.salary_source != "none":
        ceiling = job.salary_max if job.salary_max is not None else job.salary_min
        if ceiling is not None and ceiling < settings.salary_floor:
            return FilterResult(False, "salary_below_floor")

    return FilterResult(True, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_prefilter.py -v`
Expected: PASS (60 tests)

- [ ] **Step 5: Measure it against a real corpus, then read the rejections**

Unit tests only prove the rules do what you wrote. Cache a few thousand real
postings from all five sources and run the gate over them:

```
intake 2614  passed 505  kill rate 80%
  title_not_target    1308
  management_role      614
  seniority_mismatch   185
  salary_below_floor     2

per-source pass rate:
  greenhouse   165/1353 (12%)     lever       32/188 (17%)
  ashby        219/ 879 (24%)     remotive     4/ 34 (11%)
  hn_algolia    85/ 160 (53%)
```

Then list every *rejected* title containing "engineer" or "developer" and check
each one is genuinely unwanted. That is what surfaced the role-head bug, and
what showed four adjacent families (`founding`, `research`, `data`, `security`
engineer) were being dropped despite being reachable from Jarra's résumé.

After the fixes, the surviving engineer-titled rejections are all correct:
Forward Deployed, Solutions, Field Service, Product Support and AI Deployment
engineers are customer-facing roles; Staff-level is the deliberate seniority
cut; interns are interns.

- [ ] **Step 6: Commit**

```bash
git add pipeline/filters/ tests/test_prefilter.py
git commit -m "feat: deterministic pre-filter"
```

---

### Task 9: Persistence layer

**Files:**
- Create: `pipeline/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Job`, `psycopg.AsyncConnection`
- Produces:
  - `await pipeline.store.upsert_company(conn, name: str, *, ats_type: str | None = None, board_token: str | None = None) -> int`
  - `await pipeline.store.upsert_job(conn, job: Job, company_id: int, *, filter_reason: str | None) -> tuple[int, bool]` — returns `(job_id, is_new)`
  - `await pipeline.store.start_run(conn) -> int`
  - `await pipeline.store.finish_run(conn, run_id, *, jobs_seen, jobs_new, jobs_filtered, errors, duration_ms=None, budget_hit=False, notes=None) -> None`

The upsert is where repost handling lands: a second sighting must update `last_seen_at` and report `is_new=False`, never create a duplicate row.

**`jobs` has two unique keys and `ON CONFLICT` can only target one.** `fingerprint` is unique, and so is `(source, source_job_id)`. The escape hatch is a posting whose content changed to match a *different* existing row: it conflicts on fingerprint with that row and on the source id with its own. Naively this raises a `UniqueViolation` mid-run.

**And the fallback has to use a savepoint.** A bare `UniqueViolation` inside a transaction poisons it — every subsequent statement fails with `InFailedSqlTransaction`, so one awkward posting loses the whole batch. Verified by deleting the savepoint and watching the test fail exactly that way. Wrapping the upsert in `async with conn.transaction():` confines the damage; identical content is the same job, so the fallback resolves onto the fingerprint match.

**`is_new` comes from `RETURNING (xmax = 0)`.** `xmax` is 0 only on a genuine insert; on the update path it holds the locking transaction id. One statement, no read-before-write race.

**`upsert_company` uses COALESCE, not assignment**, on `ats_type` and `board_token`. An employer seen first on Greenhouse and later on HN — which carries no ATS — must not have its board token erased by the second sighting. The `sharia_*` columns are never touched: spec §9 makes a user verdict permanent, and re-billing a decision we already hold is what the cache exists to prevent.

`first_seen_at` is deliberately absent from the refresh list. It records when *we* first saw the posting and must survive a repost, or freshness scoring resets every time an employer relists.

- [ ] **Step 1: Write the failing test**

`tests/test_store.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from pipeline.models import Job
from pipeline.store import finish_run, start_run, upsert_company, upsert_job


def make_job(**overrides) -> Job:
    base = dict(
        fingerprint="a" * 64,
        source="greenhouse",
        source_job_id="1",
        company_name="Acme, Inc.",
        normalized_company="acme",
        title="Senior Software Engineer",
        location="San Francisco, CA",
        remote_type="hybrid",
        salary_min=150_000,
        salary_max=200_000,
        salary_source="parsed",
        description="Python, React, AWS.",
        apply_url="https://example.com/1",
        posted_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    base.update(overrides)
    return Job(**base)


# --- companies ---------------------------------------------------------------


async def test_upsert_company_inserts_and_returns_id(db):
    company_id = await upsert_company(db, "Acme, Inc.", ats_type="greenhouse", board_token="acme")
    assert isinstance(company_id, int)
    cur = await db.execute("SELECT name, normalized_name, ats_type FROM companies")
    row = await cur.fetchone()
    assert row["name"] == "Acme, Inc."
    assert row["normalized_name"] == "acme"
    assert row["ats_type"] == "greenhouse"


async def test_upsert_company_is_idempotent(db):
    first = await upsert_company(db, "Acme, Inc.")
    second = await upsert_company(db, "ACME Inc")
    assert first == second
    cur = await db.execute("SELECT count(*) AS n FROM companies")
    assert (await cur.fetchone())["n"] == 1


async def test_upsert_company_never_downgrades_a_known_ats(db):
    # HN gives no ATS; an earlier Greenhouse sighting must not be erased by a
    # later HN one for the same employer.
    await upsert_company(db, "Acme", ats_type="greenhouse", board_token="acme")
    await upsert_company(db, "Acme", ats_type=None, board_token=None)
    cur = await db.execute("SELECT ats_type, board_token FROM companies")
    row = await cur.fetchone()
    assert row["ats_type"] == "greenhouse"
    assert row["board_token"] == "acme"


async def test_upsert_company_preserves_the_sharia_verdict(db):
    # Spec section 9: a user verdict is permanent and must never be re-billed.
    company_id = await upsert_company(db, "Acme")
    await db.execute(
        "UPDATE companies SET sharia_verdict = 'excluded', sharia_source = 'user'"
        " WHERE company_id = %s",
        (company_id,),
    )
    await upsert_company(db, "Acme", ats_type="lever")
    cur = await db.execute("SELECT sharia_verdict, sharia_source FROM companies")
    row = await cur.fetchone()
    assert (row["sharia_verdict"], row["sharia_source"]) == ("excluded", "user")


# --- jobs --------------------------------------------------------------------


async def test_upsert_job_inserts_as_new(db):
    company_id = await upsert_company(db, "Acme")
    job_id, is_new = await upsert_job(db, make_job(), company_id, filter_reason=None)
    assert isinstance(job_id, int)
    assert is_new is True


async def test_second_sighting_is_not_new_and_reuses_the_row(db):
    company_id = await upsert_company(db, "Acme")
    first_id, first_new = await upsert_job(db, make_job(), company_id, filter_reason=None)
    second_id, second_new = await upsert_job(db, make_job(), company_id, filter_reason=None)
    assert (second_id, second_new) == (first_id, False)
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 1


async def test_second_sighting_advances_last_seen_but_not_first_seen(db):
    company_id = await upsert_company(db, "Acme")
    job_id, _ = await upsert_job(db, make_job(), company_id, filter_reason=None)
    await db.execute(
        "UPDATE jobs SET first_seen_at = %s, last_seen_at = %s WHERE job_id = %s",
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC), job_id),
    )
    await upsert_job(db, make_job(), company_id, filter_reason=None)

    cur = await db.execute(
        "SELECT first_seen_at, last_seen_at FROM jobs WHERE job_id = %s", (job_id,)
    )
    row = await cur.fetchone()
    assert row["first_seen_at"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert row["last_seen_at"] > row["first_seen_at"]


async def test_a_repost_under_a_new_source_id_collapses_onto_the_same_row(db):
    """Spec section 5: reposts must not appear as new jobs.

    The employer relists the identical role with a fresh ATS id. Content-derived
    fingerprinting is what makes that one row instead of two.
    """
    company_id = await upsert_company(db, "Acme")
    first_id, _ = await upsert_job(db, make_job(source_job_id="1"), company_id, filter_reason=None)
    second_id, is_new = await upsert_job(
        db, make_job(source_job_id="99"), company_id, filter_reason=None
    )
    assert second_id == first_id
    assert is_new is False

    cur = await db.execute("SELECT count(*) AS n, max(source_job_id) AS sid FROM jobs")
    row = await cur.fetchone()
    assert row["n"] == 1
    assert row["sid"] == "99", "the row should track the live posting id"


async def test_mutable_fields_refresh_on_a_second_sighting(db):
    # A repost frequently carries an updated salary band.
    company_id = await upsert_company(db, "Acme")
    await upsert_job(
        db, make_job(salary_min=150_000, salary_max=200_000), company_id, filter_reason=None
    )
    await upsert_job(
        db,
        make_job(
            source_job_id="2", salary_min=170_000, salary_max=220_000, salary_source="structured"
        ),
        company_id,
        filter_reason=None,
    )
    cur = await db.execute("SELECT salary_min, salary_max, salary_source FROM jobs")
    row = await cur.fetchone()
    assert (row["salary_min"], row["salary_max"]) == (170_000, 220_000)
    assert row["salary_source"] == "structured"


async def test_filter_result_is_written_atomically(db):
    company_id = await upsert_company(db, "Acme")
    await upsert_job(db, make_job(), company_id, filter_reason="title_not_target")
    cur = await db.execute("SELECT filtered_out, filter_reason FROM jobs")
    row = await cur.fetchone()
    assert row["filtered_out"] is True
    assert row["filter_reason"] == "title_not_target"


async def test_a_job_can_stop_being_filtered(db):
    # Widening the pre-filter must un-filter previously rejected rows rather
    # than leaving them invisible forever.
    company_id = await upsert_company(db, "Acme")
    await upsert_job(db, make_job(), company_id, filter_reason="title_not_target")
    await upsert_job(db, make_job(), company_id, filter_reason=None)
    cur = await db.execute("SELECT filtered_out, filter_reason FROM jobs")
    row = await cur.fetchone()
    assert row["filtered_out"] is False
    assert row["filter_reason"] is None


async def test_distinct_jobs_stay_distinct(db):
    company_id = await upsert_company(db, "Acme")
    await upsert_job(
        db, make_job(fingerprint="a" * 64, source_job_id="1"), company_id, filter_reason=None
    )
    await upsert_job(
        db, make_job(fingerprint="b" * 64, source_job_id="2"), company_id, filter_reason=None
    )
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 2


async def test_two_unique_constraints_can_collide_without_raising(db):
    """`jobs` has two unique keys and ON CONFLICT can only target one.

    Set up the awkward case: job A holds source_job_id 1, job B holds
    fingerprint Y. Now A's content changes to match B, so the incoming row
    conflicts with B on fingerprint *and* with A on (source, source_job_id).
    Naively this raises a UniqueViolation mid-run and loses the whole batch.
    """
    company_id = await upsert_company(db, "Acme")
    a_id, _ = await upsert_job(
        db, make_job(fingerprint="a" * 64, source_job_id="1"), company_id, filter_reason=None
    )
    b_id, _ = await upsert_job(
        db, make_job(fingerprint="b" * 64, source_job_id="2"), company_id, filter_reason=None
    )

    job_id, is_new = await upsert_job(
        db, make_job(fingerprint="b" * 64, source_job_id="1"), company_id, filter_reason=None
    )
    # Identical content is the same job, so it resolves onto B.
    assert job_id == b_id
    assert is_new is False
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 2


async def test_upsert_survives_inside_an_outer_transaction(db):
    # The collision fallback must use a savepoint: a plain UniqueViolation
    # inside a transaction poisons it and every later statement fails.
    company_id = await upsert_company(db, "Acme")
    await upsert_job(
        db, make_job(fingerprint="a" * 64, source_job_id="1"), company_id, filter_reason=None
    )
    await upsert_job(
        db, make_job(fingerprint="b" * 64, source_job_id="2"), company_id, filter_reason=None
    )

    async with db.transaction():
        await upsert_job(
            db, make_job(fingerprint="b" * 64, source_job_id="1"), company_id, filter_reason=None
        )
        # The connection must still be usable after the swallowed conflict.
        cur = await db.execute("SELECT count(*) AS n FROM jobs")
        assert (await cur.fetchone())["n"] == 2


# --- idempotency: the Vercel duplicate-cron requirement ----------------------


async def test_running_the_same_batch_twice_converges(db):
    """Spec section 4.2: Vercel delivers cron duplicates, so every write must
    reconcile rather than accumulate."""
    jobs = [make_job(fingerprint=f"{i:064d}", source_job_id=str(i)) for i in range(25)]

    async def run():
        company_id = await upsert_company(db, "Acme")
        return [await upsert_job(db, j, company_id, filter_reason=None) for j in jobs]

    first = await run()
    second = await run()

    assert all(is_new for _, is_new in first)
    assert not any(is_new for _, is_new in second)
    assert [i for i, _ in first] == [i for i, _ in second]

    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 25
    cur = await db.execute("SELECT count(*) AS n FROM companies")
    assert (await cur.fetchone())["n"] == 1


# --- run_log -----------------------------------------------------------------


async def test_start_run_returns_an_id_and_records_a_start(db):
    run_id = await start_run(db)
    cur = await db.execute(
        "SELECT started_at, finished_at FROM run_log WHERE run_id = %s", (run_id,)
    )
    row = await cur.fetchone()
    assert row["started_at"] is not None
    assert row["finished_at"] is None


async def test_finish_run_records_the_totals(db):
    run_id = await start_run(db)
    await finish_run(
        db,
        run_id,
        jobs_seen=100,
        jobs_new=12,
        jobs_filtered=80,
        errors=1,
        duration_ms=4321,
        budget_hit=True,
        notes="hit the wall-clock budget",
    )
    cur = await db.execute("SELECT * FROM run_log WHERE run_id = %s", (run_id,))
    row = await cur.fetchone()
    assert row["finished_at"] is not None
    assert (row["jobs_seen"], row["jobs_new"], row["jobs_filtered"]) == (100, 12, 80)
    assert row["errors"] == 1
    assert row["duration_ms"] == 4321
    assert row["budget_hit"] is True
    assert row["notes"] == "hit the wall-clock budget"


async def test_finish_run_defaults_budget_hit_to_false(db):
    run_id = await start_run(db)
    await finish_run(db, run_id, jobs_seen=1, jobs_new=1, jobs_filtered=0, errors=0)
    cur = await db.execute("SELECT budget_hit FROM run_log WHERE run_id = %s", (run_id,))
    assert (await cur.fetchone())["budget_hit"] is False


async def test_runs_are_independent(db):
    a = await start_run(db)
    b = await start_run(db)
    assert a != b
    await finish_run(db, a, jobs_seen=1, jobs_new=1, jobs_filtered=0, errors=0)
    cur = await db.execute("SELECT finished_at FROM run_log WHERE run_id = %s", (b,))
    assert (await cur.fetchone())["finished_at"] is None


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=1)])
async def test_last_seen_is_timezone_aware(db, delta):
    company_id = await upsert_company(db, "Acme")
    job_id, _ = await upsert_job(db, make_job(), company_id, filter_reason=None)
    cur = await db.execute("SELECT last_seen_at FROM jobs WHERE job_id = %s", (job_id,))
    assert (await cur.fetchone())["last_seen_at"].tzinfo is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.store'`

- [ ] **Step 3: Implement the store**

```python
"""Persistence. Every write here reconciles rather than accumulates.

Vercel delivers cron duplicates and can skip a scheduled run entirely (spec
section 4.2), so "run it twice, get the same rows" is a hard requirement rather
than a nicety. Nothing in this module increments or appends.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import AsyncConnection

from pipeline.models import Job
from pipeline.normalize import normalize_company

log = logging.getLogger(__name__)

# Fields refreshed on every sighting. first_seen_at is deliberately absent: it
# records when we first saw the posting and must survive a repost.
_JOB_REFRESH = """
    company_id    = EXCLUDED.company_id,
    source        = EXCLUDED.source,
    source_job_id = EXCLUDED.source_job_id,
    title         = EXCLUDED.title,
    location      = EXCLUDED.location,
    remote_type   = EXCLUDED.remote_type,
    salary_min    = EXCLUDED.salary_min,
    salary_max    = EXCLUDED.salary_max,
    salary_source = EXCLUDED.salary_source,
    description   = EXCLUDED.description,
    apply_url     = EXCLUDED.apply_url,
    posted_at     = EXCLUDED.posted_at,
    filtered_out  = EXCLUDED.filtered_out,
    filter_reason = EXCLUDED.filter_reason,
    last_seen_at  = now()
"""

_UPSERT_JOB = f"""
INSERT INTO jobs (
    fingerprint, company_id, source, source_job_id, title, location, remote_type,
    salary_min, salary_max, salary_source, description, apply_url, posted_at,
    first_seen_at, last_seen_at, filtered_out, filter_reason
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s)
ON CONFLICT (fingerprint) DO UPDATE SET {_JOB_REFRESH}
-- xmax is 0 only on a genuine insert; on the update path it holds the
-- transaction id that locked the row. This is how one statement reports
-- whether it created or matched.
RETURNING job_id, (xmax = 0) AS is_new
"""


async def upsert_company(
    conn: AsyncConnection,
    name: str,
    *,
    ats_type: str | None = None,
    board_token: str | None = None,
) -> int:
    """Insert or match a company by its normalized name, returning company_id.

    COALESCE rather than assignment on ats_type and board_token: an employer
    seen first on Greenhouse and later on HN (which carries no ATS) must not
    have its board token erased by the second sighting.

    The sharia_* columns are never touched here. Spec section 9 makes a user
    verdict permanent, and re-billing a decision we already have is exactly what
    the cache exists to prevent.
    """
    cur = await conn.execute(
        """
        INSERT INTO companies (name, normalized_name, ats_type, board_token)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (normalized_name) DO UPDATE SET
            name        = EXCLUDED.name,
            ats_type    = COALESCE(companies.ats_type, EXCLUDED.ats_type),
            board_token = COALESCE(companies.board_token, EXCLUDED.board_token)
        RETURNING company_id
        """,
        (name, normalize_company(name), ats_type, board_token),
    )
    row = await cur.fetchone()
    return row["company_id"]


async def upsert_job(
    conn: AsyncConnection,
    job: Job,
    company_id: int,
    *,
    filter_reason: str | None,
) -> tuple[int, bool]:
    """Insert or match a job by content fingerprint. Returns (job_id, is_new).

    Matching on fingerprint rather than source id is what makes a repost
    collapse onto the existing row (spec section 5).

    `filter_reason` carries the pre-filter verdict so `filtered_out` and its
    reason are written in the same statement. The CHECK constraint requires them
    to agree, and a second write could not satisfy it atomically.
    """
    params = (
        job.fingerprint,
        company_id,
        job.source,
        job.source_job_id,
        job.title,
        job.location,
        job.remote_type,
        job.salary_min,
        job.salary_max,
        job.salary_source,
        job.description,
        job.apply_url,
        job.posted_at,
        filter_reason is not None,
        filter_reason,
    )

    try:
        # A savepoint, not a bare statement: if this raises inside an outer
        # transaction, the whole transaction is poisoned and every later
        # statement fails too. The savepoint confines the damage.
        async with conn.transaction():
            cur = await conn.execute(_UPSERT_JOB, params)
            row = await cur.fetchone()
            return row["job_id"], row["is_new"]
    except psycopg.errors.UniqueViolation:
        # `jobs` has two unique keys -- fingerprint, and (source, source_job_id)
        # -- and ON CONFLICT can only target one. The escape hatch is a posting
        # whose content changed to match a *different* existing row: it
        # conflicts on fingerprint with that row and on the source id with its
        # own. Identical content is the same job by definition, so resolve onto
        # the fingerprint match and leave the source ids alone.
        log.info(
            "job %s/%s collides on both unique keys; resolving by fingerprint",
            job.source,
            job.source_job_id,
        )
        cur = await conn.execute(
            "UPDATE jobs SET last_seen_at = now(), filtered_out = %s, filter_reason = %s"
            " WHERE fingerprint = %s RETURNING job_id",
            (filter_reason is not None, filter_reason, job.fingerprint),
        )
        row = await cur.fetchone()
        return row["job_id"], False


async def start_run(conn: AsyncConnection) -> int:
    cur = await conn.execute("INSERT INTO run_log (started_at) VALUES (now()) RETURNING run_id")
    row = await cur.fetchone()
    return row["run_id"]


async def finish_run(
    conn: AsyncConnection,
    run_id: int,
    *,
    jobs_seen: int,
    jobs_new: int,
    jobs_filtered: int,
    errors: int,
    duration_ms: int | None = None,
    budget_hit: bool = False,
    notes: str | None = None,
) -> None:
    """Close out a run.

    A run with no row here was either killed mid-invocation or never delivered.
    Vercel writes no log at all for an undelivered cron, so the absence of this
    row is the only evidence that a run went missing (spec section 13.5).
    """
    await conn.execute(
        """
        UPDATE run_log SET
            finished_at   = now(),
            jobs_seen     = %s,
            jobs_new      = %s,
            jobs_filtered = %s,
            errors        = %s,
            duration_ms   = %s,
            budget_hit    = %s,
            notes         = %s
        WHERE run_id = %s
        """,
        (jobs_seen, jobs_new, jobs_filtered, errors, duration_ms, budget_hit, notes, run_id),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_store.py -v`
Expected: PASS (21 tests)

- [ ] **Step 5: Verify the savepoint test is not vacuous**

Replace `async with conn.transaction():` with `if True:`, re-run, and confirm
`test_upsert_survives_inside_an_outer_transaction` fails with
`psycopg.errors.InFailedSqlTransaction`. Restore it.

- [ ] **Step 6: Prove idempotency against a real corpus**

Unit tests show the upsert reconciles. Run the cached corpus through
pre-filter and store, twice, into real Postgres:

```
first run   seen=2614  new= 2518  rows= 2518  companies= 186  live(unfiltered)=492
second run  seen=2614  new=    0  rows= 2518  companies= 186  live(unfiltered)=492
converged: True
```

2,614 postings produce 2,518 rows — 96 collapse onto existing fingerprints as
reposts — and a second identical pass creates nothing. That is the property
spec §4.2 requires, measured rather than asserted.

- [ ] **Step 7: Commit**

```bash
git add pipeline/store.py tests/test_store.py
git commit -m "feat: persistence with fingerprint upsert and run log"
```

---

### Task 10: Orchestration and CLI

**Files:**
- Create: `migrations/002_source_state.sql`, `pipeline/run_discover.py`
- Modify: `pipeline/sources/base.py`, every source module, `pipeline/store.py`, `pipeline/http.py`, `README.md`, `tests/conftest.py`
- Test: `tests/test_run_discover.py`

**Interfaces:**
- Produces:
  - `pipeline.run_discover.RunStats` — frozen dataclass: `jobs_seen`, `jobs_new`, `jobs_filtered`, `errors`, `budget_hit`, `sources_skipped`, `duration_ms`
  - `await pipeline.run_discover.run(conn, cfg, sources, *, budget_seconds=None) -> RunStats`
  - `pipeline.run_discover.main(argv=None) -> int`
  - `await pipeline.store.claim_source(conn, source, min_interval_seconds) -> bool`
  - `await pipeline.store.record_source_result(conn, source, *, ok) -> None`
  - `Source.min_interval_seconds: float` added to the protocol

CLI: `python -m pipeline.run_discover [--sources a,b] [--dry-run] [--budget N] [-v]`

**Sources run concurrently, writes run serially.** Different sources are different hosts, so fetching in parallel is what lets a crawl finish inside a bounded invocation — `PoliteSession` still rate-limits each host independently. But a psycopg connection cannot serve concurrent operations, so producers push onto a bounded `asyncio.Queue` and a single consumer performs every write.

**A run stops on a wall-clock budget rather than trying to finish.** Vercel terminates at 800s and writes no log for a killed invocation, so returning early with `budget_hit=True` is strictly better than being cut off: the work already done is durable, the next tick resumes, and the fact is visible. **Both the producer and the consumer must flag it** — a run whose budget expires before anything is dequeued would otherwise report `budget_hit=False` while having silently done nothing.

**Per-source intervals need real state, which is why migration 002 exists.** Remotive's own API response asks for at most four calls a day and the discover cron ticks every ten minutes (spec §4.3), so a `source_state` table plus a single-statement `claim_source` is the minimum that keeps us inside their terms. The claim is one `INSERT … ON CONFLICT … WHERE` so two overlapping cron invocations cannot both fetch the same source.

- [ ] **Step 1: Add the source_state migration**

```sql
-- Per-source fetch scheduling.
--
-- Spec section 4.3: the cron tick is a clock, not a crawl frequency -- each
-- source carries its own interval. Remotive makes this mandatory rather than
-- optional: its own API response states "we advise max. 4 times a day...
-- excessive requests will be blocked", so a 10-minute tick that fetched it
-- every time would breach their terms within the hour.

CREATE TABLE source_state (
  source                TEXT PRIMARY KEY,
  last_fetch_started_at TIMESTAMPTZ,
  last_fetch_ok_at      TIMESTAMPTZ,
  consecutive_errors    INTEGER NOT NULL DEFAULT 0
);
```

Add `source_state` to `_TABLES` in `tests/conftest.py`.

- [ ] **Step 2: Declare per-source cadence**

Add `min_interval_seconds: float` to the `Source` protocol and a concrete value
to every adapter — `0.0` everywhere except Remotive, which uses its
`MIN_INTERVAL_SECONDS`.

- [ ] **Step 3: Add claim_source and record_source_result to the store**

- [ ] **Step 4: Write the failing test**

`tests/test_run_discover.py`:

```python
import asyncio
from datetime import UTC, datetime

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.models import RawJob
from pipeline.run_discover import RunStats, run
from pipeline.sources.base import SourceConfig
from pipeline.store import claim_source


async def _noop_sleep(_: float) -> None:
    return None


def raw(**overrides) -> RawJob:
    base = dict(
        source="fake",
        source_job_id="1",
        company_name="Acme",
        title="Senior Software Engineer",
        location="Remote",
        description="Python, React, AWS. $160,000 - $190,000",
        apply_url="https://example.com/1",
        posted_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    base.update(overrides)
    return RawJob(**base)


class FakeSource:
    """A source that yields a fixed list, optionally raising or stalling."""

    min_interval_seconds = 0.0

    def __init__(self, name, jobs=(), exc=None, delay=0.0):
        self.name = name
        self._jobs = list(jobs)
        self._exc = exc
        self._delay = delay

    async def fetch(self, cfg):
        if self._exc:
            raise self._exc
        for job in self._jobs:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield job


@pytest.fixture
def cfg(migrated_db):
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=_noop_sleep),
        targets={},
        settings=load_settings(env={"DATABASE_URL": migrated_db}),
    )


async def test_run_persists_and_reports(db, cfg):
    jobs = [
        raw(source="fake", source_job_id=str(i), title=f"Backend Engineer, Team {i}")
        for i in range(5)
    ]
    stats = await run(db, cfg, {"fake": FakeSource("fake", jobs)})

    assert stats.jobs_seen == 5
    assert stats.jobs_new == 5
    assert stats.errors == 0
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 5


async def test_run_counts_filtered_jobs(db, cfg):
    jobs = [
        raw(source_job_id="1", title="Senior Software Engineer"),
        raw(source_job_id="2", title="Registered Nurse"),
        raw(source_job_id="3", title="Account Executive"),
    ]
    stats = await run(db, cfg, {"fake": FakeSource("fake", jobs)})
    assert stats.jobs_seen == 3
    assert stats.jobs_filtered == 2

    cur = await db.execute("SELECT count(*) AS n FROM jobs WHERE filtered_out = false")
    assert (await cur.fetchone())["n"] == 1


async def test_a_broken_source_does_not_kill_the_run(db, cfg):
    sources = {
        "bad": FakeSource("bad", exc=RuntimeError("boom")),
        "good": FakeSource("good", [raw(source="good", title="Backend Engineer")]),
    }
    stats = await run(db, cfg, sources)
    assert stats.errors == 1
    assert stats.jobs_seen == 1, "the healthy source must still have been ingested"


async def test_broken_source_increments_consecutive_errors(db, cfg):
    await run(db, cfg, {"bad": FakeSource("bad", exc=RuntimeError("boom"))})
    cur = await db.execute("SELECT consecutive_errors FROM source_state WHERE source = 'bad'")
    assert (await cur.fetchone())["consecutive_errors"] == 1


async def test_a_recovered_source_resets_its_error_count(db, cfg):
    await run(db, cfg, {"s": FakeSource("s", exc=RuntimeError("boom"))})
    await run(db, cfg, {"s": FakeSource("s", [raw(title="Backend Engineer")])})
    cur = await db.execute(
        "SELECT consecutive_errors, last_fetch_ok_at FROM source_state WHERE source = 's'"
    )
    row = await cur.fetchone()
    assert row["consecutive_errors"] == 0
    assert row["last_fetch_ok_at"] is not None


async def test_run_writes_a_run_log_row(db, cfg):
    stats = await run(db, cfg, {"fake": FakeSource("fake", [raw(title="Backend Engineer")])})
    cur = await db.execute("SELECT * FROM run_log ORDER BY run_id DESC LIMIT 1")
    row = await cur.fetchone()
    assert row["finished_at"] is not None
    assert row["jobs_seen"] == stats.jobs_seen
    assert row["duration_ms"] is not None


async def test_running_twice_converges(db, cfg):
    jobs = [raw(source_job_id=str(i), title=f"Backend Engineer, Team {i}") for i in range(10)]
    first = await run(db, cfg, {"fake": FakeSource("fake", jobs)})
    second = await run(db, cfg, {"fake": FakeSource("fake", jobs)})

    assert first.jobs_new == 10
    assert second.jobs_new == 0
    assert second.jobs_seen == 10
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 10


# --- wall-clock budget -------------------------------------------------------


async def test_an_expired_budget_returns_cleanly_and_flags_itself(db, cfg):
    """Vercel records nothing for a killed invocation.

    Returning early with budget_hit=True keeps the work durable and makes the
    fact visible, which a hard termination at 800s would not.
    """
    jobs = [raw(source_job_id=str(i), title=f"Backend Engineer, Team {i}") for i in range(20)]
    stats = await run(db, cfg, {"fake": FakeSource("fake", jobs)}, budget_seconds=0.0)

    assert stats.budget_hit is True
    assert stats.jobs_seen == 0
    cur = await db.execute("SELECT budget_hit FROM run_log ORDER BY run_id DESC LIMIT 1")
    assert (await cur.fetchone())["budget_hit"] is True


async def test_a_generous_budget_is_not_flagged(db, cfg):
    stats = await run(
        db, cfg, {"fake": FakeSource("fake", [raw(title="Backend Engineer")])}, budget_seconds=30.0
    )
    assert stats.budget_hit is False


async def test_partial_work_before_the_budget_expires_is_kept(db, cfg):
    # 40 jobs at 5ms each against a 60ms budget: some land, the rest do not,
    # and what landed is durable rather than rolled back.
    jobs = [raw(source_job_id=str(i), title=f"Backend Engineer, Team {i}") for i in range(40)]
    stats = await run(db, cfg, {"slow": FakeSource("slow", jobs, delay=0.005)}, budget_seconds=0.06)
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    stored = (await cur.fetchone())["n"]
    assert 0 < stored < 40
    assert stats.budget_hit is True


# --- per-source intervals ----------------------------------------------------


async def test_a_source_within_its_interval_is_skipped(db, cfg):
    """Remotive's own API asks for at most four calls a day, and the discover
    cron ticks every ten minutes (spec section 4.3)."""

    class Throttled(FakeSource):
        min_interval_seconds = 3600.0

    jobs = [raw(title="Backend Engineer")]
    first = await run(db, cfg, {"t": Throttled("t", jobs)})
    second = await run(db, cfg, {"t": Throttled("t", jobs)})

    assert first.jobs_seen == 1
    assert second.jobs_seen == 0
    assert second.sources_skipped == 1


async def test_a_zero_interval_source_runs_every_time(db, cfg):
    jobs = [raw(title="Backend Engineer")]
    await run(db, cfg, {"fake": FakeSource("fake", jobs)})
    second = await run(db, cfg, {"fake": FakeSource("fake", jobs)})
    assert second.sources_skipped == 0
    assert second.jobs_seen == 1


async def test_claim_source_is_a_claim_not_a_read(db):
    # Two overlapping cron invocations must not both fetch the same source.
    assert await claim_source(db, "remotive", 3600.0) is True
    assert await claim_source(db, "remotive", 3600.0) is False


async def test_claim_source_allows_a_due_source(db):
    assert await claim_source(db, "greenhouse", 0.0) is True
    assert await claim_source(db, "greenhouse", 0.0) is True


# --- shape -------------------------------------------------------------------


def test_runstats_defaults_are_zero():
    stats = RunStats()
    assert (stats.jobs_seen, stats.jobs_new, stats.errors) == (0, 0, 0)
    assert stats.budget_hit is False


async def test_no_sources_is_not_an_error(db, cfg):
    stats = await run(db, cfg, {})
    assert stats == RunStats(duration_ms=stats.duration_ms)


async def test_per_board_errors_reach_the_run_stats(db, cfg):
    class Reporting(FakeSource):
        async def fetch(self, config):
            config.errors.append("fake:board-a")
            config.errors.append("fake:board-b")
            return
            yield  # pragma: no cover - makes this an async generator

    stats = await run(db, cfg, {"fake": Reporting("fake")})
    assert stats.errors == 2
```

- [ ] **Step 5: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_run_discover.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.run_discover'`

- [ ] **Step 6: Implement the orchestrator**

```python
"""Discovery orchestration.

Callable two ways and identical in both: from the CLI during development, and
from the cron route once deployed. Nothing here knows which one invoked it.

Two properties shape the design:

**Sources run concurrently, writes run serially.** Different sources are
different hosts, so fetching them in parallel is what lets the crawl finish
inside a bounded invocation -- PoliteSession still rate-limits each host
independently. But a psycopg connection cannot serve concurrent operations, so
producers push onto a queue and a single consumer does every write.

**A run stops on a wall-clock budget rather than trying to finish.** Vercel
terminates a function at 800s and records nothing for a killed invocation, so
returning early with `budget_hit=True` is strictly better than being cut off:
the work is durable, the next tick resumes, and we find out the budget was hit.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass

from psycopg import AsyncConnection

from pipeline.config import Settings, load_settings
from pipeline.filters.prefilter import prefilter
from pipeline.models import RawJob
from pipeline.normalize import to_job
from pipeline.sources.base import Source, SourceConfig
from pipeline.store import (
    claim_source,
    finish_run,
    record_source_result,
    start_run,
    upsert_company,
    upsert_job,
)

log = logging.getLogger(__name__)

# Bounded so a fast source cannot pull an entire board into memory while the
# consumer is still writing the previous one.
_QUEUE_SIZE = 200

_DONE = object()


@dataclass(frozen=True)
class RunStats:
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_filtered: int = 0
    errors: int = 0
    budget_hit: bool = False
    sources_skipped: int = 0
    duration_ms: int = 0


async def _produce(
    source: Source,
    cfg: SourceConfig,
    queue: asyncio.Queue,
    deadline: float,
    errors: list[str],
    counters: dict[str, int],
) -> None:
    """Fetch one source onto the queue, stopping at the deadline.

    The producer flags the budget as well as the consumer. A run whose budget
    expires before anything is dequeued would otherwise report budget_hit=False
    while having silently done nothing -- the exact case that makes a missed
    run indistinguishable from an empty one.
    """
    try:
        async for raw in source.fetch(cfg):
            if time.monotonic() >= deadline:
                log.info("%s: stopping on time budget", source.name)
                counters["budget_hit"] = 1
                return
            await queue.put(raw)
    except Exception as exc:  # a broken source must not take down the run
        log.warning("%s: fetch failed: %s", source.name, exc)
        errors.append(source.name)


async def _consume(
    conn: AsyncConnection,
    queue: asyncio.Queue,
    settings: Settings,
    deadline: float,
    counters: dict[str, int],
) -> None:
    """Normalize, filter, and persist. The only writer."""
    while True:
        item = await queue.get()
        try:
            if item is _DONE:
                return
            if time.monotonic() >= deadline:
                counters["budget_hit"] = 1
                continue  # drain without writing; the next run refetches

            raw: RawJob = item
            job = to_job(raw)
            verdict = prefilter(job, settings)

            company_id = await upsert_company(
                conn,
                job.company_name,
                ats_type=raw.source if raw.source in _ATS_SOURCES else None,
            )
            _, is_new = await upsert_job(conn, job, company_id, filter_reason=verdict.reason)

            counters["seen"] += 1
            counters["new"] += int(is_new)
            counters["filtered"] += int(not verdict.passed)
        except Exception as exc:
            log.warning("failed to store a job: %s", exc)
            counters["errors"] += 1
        finally:
            queue.task_done()


# Sources whose name is also the employer's ATS. Aggregators are not.
_ATS_SOURCES = frozenset({"greenhouse", "lever", "ashby"})


async def run(
    conn: AsyncConnection,
    cfg: SourceConfig,
    sources: dict[str, Source],
    *,
    budget_seconds: float | None = None,
) -> RunStats:
    """Fetch every due source, persist what they yield, and record the run."""
    started = time.monotonic()
    budget = budget_seconds if budget_seconds is not None else cfg.settings.run_budget_seconds
    deadline = started + budget

    run_id = await start_run(conn)

    due: list[Source] = []
    skipped = 0
    for source in sources.values():
        if await claim_source(conn, source.name, source.min_interval_seconds):
            due.append(source)
        else:
            skipped += 1
            log.info("%s: not due yet, skipping", source.name)

    counters = {"seen": 0, "new": 0, "filtered": 0, "errors": 0, "budget_hit": 0}
    errors: list[str] = []

    if due:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        consumer = asyncio.create_task(_consume(conn, queue, cfg.settings, deadline, counters))
        await asyncio.gather(*(_produce(s, cfg, queue, deadline, errors, counters) for s in due))
        await queue.put(_DONE)
        await consumer

        for source in due:
            await record_source_result(conn, source.name, ok=source.name not in errors)

    stats = RunStats(
        jobs_seen=counters["seen"],
        jobs_new=counters["new"],
        jobs_filtered=counters["filtered"],
        errors=counters["errors"] + len(errors) + len(cfg.errors),
        budget_hit=bool(counters["budget_hit"]),
        sources_skipped=skipped,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    await finish_run(
        conn,
        run_id,
        jobs_seen=stats.jobs_seen,
        jobs_new=stats.jobs_new,
        jobs_filtered=stats.jobs_filtered,
        errors=stats.errors,
        duration_ms=stats.duration_ms,
        budget_hit=stats.budget_hit,
        notes=f"sources={[s.name for s in due]} skipped={skipped}" if due or skipped else None,
    )
    return stats


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one discovery pass.")
    parser.add_argument("--sources", help="comma-separated subset, e.g. greenhouse,lever")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run everything, then roll back — nothing is persisted",
    )
    parser.add_argument("--budget", type=float, help="wall-clock seconds before stopping")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Imported here so the module can be imported without a profile present.
    from pipeline.http import PoliteSession
    from pipeline.sources.registry import SOURCES, load_targets

    settings = load_settings()
    selected = SOURCES
    if args.sources:
        names = [n.strip() for n in args.sources.split(",") if n.strip()]
        unknown = set(names) - SOURCES.keys()
        if unknown:
            parser.error(f"unknown sources: {', '.join(sorted(unknown))}")
        selected = {n: SOURCES[n] for n in names}

    targets = load_targets(settings.profile_dir / "targets.yaml")
    if not targets:
        log.warning("no targets.yaml found in %s; ATS sources will be empty", settings.profile_dir)

    conn = await AsyncConnection.connect(settings.database_url, autocommit=True)
    try:
        async with PoliteSession(settings.user_agent, conn=conn) as session:
            cfg = SourceConfig(session=session, targets=targets, settings=settings)
            # force_rollback undoes everything at exit, so a dry run exercises
            # the real write path instead of a parallel one that could drift.
            async with conn.transaction(force_rollback=args.dry_run):
                stats = await run(conn, cfg, selected, budget_seconds=args.budget)
    finally:
        await conn.close()

    print(
        f"seen={stats.jobs_seen} new={stats.jobs_new} filtered={stats.jobs_filtered} "
        f"errors={stats.errors} skipped_sources={stats.sources_skipped} "
        f"budget_hit={stats.budget_hit} duration={stats.duration_ms}ms"
        + (" (DRY RUN — rolled back)" if args.dry_run else "")
    )
    return 1 if stats.errors else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_run_discover.py -v`
Expected: PASS (17 tests)

- [ ] **Step 8: Run the CLI, twice, and read the output**

The suite passes; run it anyway. Two bugs survived 245 green tests and both
appeared within seconds of running the CLI:

1. **`TypeError: tuple indices must be integers`** in `store.py`, then again in
   `PoliteSession`'s ETag lookup. The test fixture builds its connection with
   `row_factory=dict_row`; the CLI did not. Any code reading rows by name off a
   caller-supplied connection has this bug and no fixture will find it. Fix by
   pinning `dict_row` on the cursor rather than inheriting — the same fix
   `apply_migrations` already needed in Task 1.
2. **Twenty boards failed and the run reported `errors=0`.** Adapters swallow
   per-board failures so one dead token cannot kill the run, which means the
   failure never reaches the stats. A run that ingested nothing looked healthy.
   `SourceConfig` gains an `errors: list[str]` that adapters append to.

```
run 1   seen=5182 new=4953 filtered=4278 errors=0 skipped=0 budget_hit=False 9926ms
run 2   seen= 160 new=   0 filtered=  75 errors=0 skipped=1 budget_hit=False 8824ms
run 3   seen=   0 new=   0 filtered=   0 errors=0 skipped=0 budget_hit=True  1407ms   (--budget 0.3)
```

Run 2 ingesting only 160 is **correct, not a regression**: Greenhouse, Lever and
Ashby all returned `304 Not Modified` against stored ETags, Remotive was skipped
on its six-hour interval, and only HN — which sends no validators — refetched.
That is the conditional-request layer working end to end.

- [ ] **Step 9: Confirm the final state**

```
http_cache    21 urls cached, 20 with etag
source_state  all five sources, consecutive_errors = 0
live jobs     813        companies  197
```

`SELECT count(*) FROM jobs WHERE filtered_out = false` is the number that
decides whether Phase 2 is worth building.

- [ ] **Step 10: Write the README and commit**

```bash
git add migrations/ pipeline/ tests/ README.md
git commit -m "feat: discovery orchestration, per-source intervals, CLI"
```
