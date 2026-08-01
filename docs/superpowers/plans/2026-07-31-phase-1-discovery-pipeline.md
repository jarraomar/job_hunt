# Phase 1: Discovery Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A locally-runnable, fully-tested Python package that ingests jobs from public ATS and aggregator APIs, deduplicates them, applies the deterministic pre-filter, and persists results to SQLite with a per-run log.

**Architecture:** One Python package, `pipeline/`. Every source is a module implementing a common `Source` protocol and returning `RawJob` objects; `normalize.py` converts those to canonical `Job` objects with a content-derived fingerprint; `store.py` upserts into SQLite; `filters/prefilter.py` applies free deterministic rules. All outbound HTTP goes through a single `PoliteSession` that enforces per-host rate limiting, conditional requests, and backoff. No LLM calls, no document generation, no web UI in this phase.

**Tech Stack:** Python 3.11+, httpx, PyYAML, pytest, pytest-httpx, SQLite (stdlib `sqlite3`).

## Global Constraints

These apply to every task. Copied from `docs/superpowers/specs/2026-07-31-job-hunt-system-design.md`.

- **No browser dependency may enter the tree, ever.** Playwright, Selenium, Puppeteer, and pyppeteer are prohibited. This is the structural enforcement of spec §3.
- **No authenticated job-platform requests.** Public unauthenticated endpoints only. LinkedIn, Indeed, Glassdoor, ZipRecruiter, and Wellfound are out of scope entirely.
- **All outbound HTTP goes through `PoliteSession`.** A bare `httpx.get` in a source module is a defect.
- **SQLite: WAL mode, `busy_timeout=5000`.** Set on every connection.
- **Workday gets a 2.0s/host delay; everything else 1.0s** (spec §7).
- Salary floor: **125000**. Target: 150000.
- Home location for proximity ranking: **San Leandro, CA**.
- `profile/` is gitignored and must never be committed. `profile.example/` holds committed templates with no personal data.
- Python **3.11+**. Type hints on all public functions.
- Timestamps stored as **ISO 8601 UTC strings** in SQLite.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, pytest config |
| `.gitignore` | Excludes `profile/`, `seed/`, `*.db`, `.env` |
| `migrations/001_initial.sql` | Phase-1 subset of the spec §5 schema |
| `pipeline/config.py` | `Settings` dataclass, env + YAML loading |
| `pipeline/db.py` | Connection factory, migration runner |
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
| `pipeline/run_daily.py` | Orchestration + CLI |
| `scripts/capture_fixture.py` | Records live API responses as test fixtures |
| `tests/` | Mirrors the package layout |

**Fixtures are captured from live endpoints, never hand-written.** Task 5 builds `scripts/capture_fixture.py` for this. Hand-writing a fixture from memory bakes in a guess about an API's shape; capturing it makes the tests reflect reality.

---

### Task 1: Project scaffolding and database layer

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `migrations/001_initial.sql`, `pipeline/__init__.py`, `pipeline/db.py`, `pipeline/config.py`
- Test: `tests/test_db.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `pipeline.db.connect(db_path: Path) -> sqlite3.Connection`
  - `pipeline.db.apply_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[str]` — returns names of migrations applied this call
  - `pipeline.config.Settings` frozen dataclass with fields: `db_path: Path`, `migrations_dir: Path`, `profile_dir: Path`, `salary_floor: int`, `home_city: str`, `home_state: str`, `user_agent: str`
  - `pipeline.config.load_settings(env: Mapping[str, str] | None = None) -> Settings`

- [ ] **Step 1: Create the project skeleton**

`pyproject.toml`:

```toml
[project]
name = "jobhunt"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-httpx>=0.30"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["pipeline*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

`.gitignore`:

```
profile/
seed/
*.db
*.db-wal
*.db-shm
.env
venv/
__pycache__/
*.egg-info/
.pytest_cache/
```

Create empty `pipeline/__init__.py`.

- [ ] **Step 2: Write the migration**

`migrations/001_initial.sql` — the Phase-1 subset of spec §5. Later phases add `scores`, `applications`, `work_queue`, `answer_bank`, `unmapped_questions`, `llm_spend` in their own migrations.

```sql
CREATE TABLE companies (
  company_id        INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  normalized_name   TEXT NOT NULL UNIQUE,
  domain            TEXT,
  ats_type          TEXT,
  board_token       TEXT,
  sharia_verdict    TEXT NOT NULL DEFAULT 'unknown',
  sharia_sector     TEXT,
  sharia_reason     TEXT,
  sharia_source     TEXT,
  sharia_decided_at TEXT
);

CREATE TABLE jobs (
  job_id        INTEGER PRIMARY KEY,
  fingerprint   TEXT NOT NULL UNIQUE,
  company_id    INTEGER NOT NULL REFERENCES companies(company_id),
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
  posted_at     TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  closed_at     TEXT,
  filtered_out  INTEGER NOT NULL DEFAULT 0,
  filter_reason TEXT,
  UNIQUE(source, source_job_id)
);
CREATE INDEX idx_jobs_company ON jobs(company_id);
CREATE INDEX idx_jobs_posted ON jobs(posted_at);

CREATE TABLE run_log (
  run_id         INTEGER PRIMARY KEY,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  jobs_seen      INTEGER NOT NULL DEFAULT 0,
  jobs_new       INTEGER NOT NULL DEFAULT 0,
  jobs_filtered  INTEGER NOT NULL DEFAULT 0,
  errors         INTEGER NOT NULL DEFAULT 0,
  peak_rss_mb    INTEGER,
  notes          TEXT
);

CREATE TABLE http_cache (
  url           TEXT PRIMARY KEY,
  etag          TEXT,
  last_modified TEXT,
  fetched_at    TEXT NOT NULL
);
```

- [ ] **Step 3: Write the failing tests**

`tests/test_db.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from pipeline.db import apply_migrations, connect

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def test_connect_enables_wal_and_busy_timeout(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_connect_returns_dict_like_rows(tmp_path):
    conn = connect(tmp_path / "t.db")
    row = conn.execute("SELECT 1 AS answer").fetchone()
    assert row["answer"] == 1


def test_apply_migrations_creates_tables(tmp_path):
    conn = connect(tmp_path / "t.db")
    applied = apply_migrations(conn, MIGRATIONS)
    assert "001_initial.sql" in applied
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"companies", "jobs", "run_log", "http_cache"} <= names


def test_apply_migrations_is_idempotent(tmp_path):
    conn = connect(tmp_path / "t.db")
    first = apply_migrations(conn, MIGRATIONS)
    second = apply_migrations(conn, MIGRATIONS)
    assert first == ["001_initial.sql"]
    assert second == []


def test_jobs_fingerprint_is_unique(tmp_path):
    conn = connect(tmp_path / "t.db")
    apply_migrations(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')"
    )
    row = (
        "fp1", 1, "greenhouse", "1", "Engineer", "SF", "onsite",
        None, None, "none", "desc", "https://x", None, "t", "t",
    )
    sql = (
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " location, remote_type, salary_min, salary_max, salary_source, description,"
        " apply_url, posted_at, first_seen_at, last_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("fp1", 1, "lever", "2") + row[4:])
```

`tests/test_config.py`:

```python
from pathlib import Path

from pipeline.config import load_settings


def test_defaults_match_spec():
    s = load_settings(env={})
    assert s.salary_floor == 125_000
    assert s.home_city == "San Leandro"
    assert s.home_state == "CA"
    assert "jobhunt" in s.user_agent.lower()


def test_env_overrides_defaults(tmp_path):
    s = load_settings(env={"JOBHUNT_DB_PATH": str(tmp_path / "x.db"),
                           "JOBHUNT_SALARY_FLOOR": "140000"})
    assert s.db_path == tmp_path / "x.db"
    assert s.salary_floor == 140_000


def test_settings_is_frozen():
    s = load_settings(env={})
    try:
        s.salary_floor = 1  # type: ignore[misc]
    except Exception as exc:
        assert "frozen" in str(exc).lower() or exc.__class__.__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("Settings should be immutable")
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
python -m venv venv && ./venv/bin/pip install -e ".[dev]"
./venv/bin/pytest tests/test_db.py tests/test_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.db'`

- [ ] **Step 5: Implement `pipeline/db.py`**

```python
"""SQLite connection and migration handling."""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  name       TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
)
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the settings every caller in this system needs."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[str]:
    """Apply any *.sql in migrations_dir not yet recorded. Returns those applied."""
    conn.execute(_SCHEMA_VERSION_DDL)
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_version")}
    applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in done:
            continue
        conn.execute("BEGIN")
        try:
            conn.executescript(path.read_text())
            conn.execute(
                "INSERT INTO schema_version (name, applied_at)"
                " VALUES (?, datetime('now'))",
                (path.name,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(path.name)
    return applied
```

- [ ] **Step 6: Implement `pipeline/config.py`**

```python
"""Runtime settings, loaded from the environment with spec-derived defaults."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

USER_AGENT = (
    "jobhunt/0.1 (personal job search; contact: developer@cloudbaseservices.com)"
)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    migrations_dir: Path
    profile_dir: Path
    salary_floor: int
    home_city: str
    home_state: str
    user_agent: str


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    return Settings(
        db_path=Path(e.get("JOBHUNT_DB_PATH", _ROOT / "jobhunt.db")),
        migrations_dir=Path(e.get("JOBHUNT_MIGRATIONS_DIR", _ROOT / "migrations")),
        profile_dir=Path(e.get("JOBHUNT_PROFILE_DIR", _ROOT / "profile")),
        salary_floor=int(e.get("JOBHUNT_SALARY_FLOOR", "125000")),
        home_city=e.get("JOBHUNT_HOME_CITY", "San Leandro"),
        home_state=e.get("JOBHUNT_HOME_STATE", "CA"),
        user_agent=e.get("JOBHUNT_USER_AGENT", USER_AGENT),
    )
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_db.py tests/test_config.py -v`
Expected: PASS (8 tests)

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore migrations/ pipeline/ tests/
git commit -m "feat: project scaffolding, SQLite layer, settings"
```

---

### Task 2: Salary parsing

**Files:**
- Create: `pipeline/salary.py`
- Test: `tests/test_salary.py`

**Interfaces:**
- Consumes: nothing
- Produces: `pipeline.salary.parse_salary(text: str) -> tuple[int | None, int | None]` — returns `(min, max)` annualized USD, or `(None, None)`. A single figure returns `(n, n)`.

Spec §6 notes Greenhouse and Workday almost never expose structured salary, so this parses free text. It must not guess: a wrong salary silently drops good jobs at the §8 gate.

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
        ("USD 145,000 — 185,000 annually", (145_000, 185_000)),
        ("The base pay range is $128,000—$164,000.", (128_000, 164_000)),
        ("Compensation: $180,000", (180_000, 180_000)),
        ("$95/hour", (197_600, 197_600)),          # 95 * 40 * 52
        ("$75 - $95 per hour", (156_000, 197_600)),
        ("Base salary of 210000 USD", (210_000, 210_000)),
        ("We offer competitive compensation.", (None, None)),
        ("", (None, None)),
        ("Founded in 2011, we serve 150,000 customers", (None, None)),
        ("401k matching up to 5%", (None, None)),
        ("€120,000 - €150,000", (None, None)),      # non-USD: refuse
    ],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_min_never_exceeds_max():
    lo, hi = parse_salary("$200,000 - $150,000")
    assert lo is not None and hi is not None and lo <= hi


def test_implausible_values_rejected():
    assert parse_salary("$12 - $19") == (None, None)          # too low even hourly
    assert parse_salary("$5,000,000 - $9,000,000") == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_salary.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.salary'`

- [ ] **Step 3: Implement `pipeline/salary.py`**

```python
"""Parse annualized USD salary ranges out of free-text job descriptions.

Biased toward refusing rather than guessing: a wrong number silently drops a
good job at the salary gate, which is worse than leaving salary unknown.
"""
from __future__ import annotations

import re

_HOURS_PER_YEAR = 40 * 52
_MIN_ANNUAL = 30_000
_MAX_ANNUAL = 1_000_000
_MIN_HOURLY = 20
_MAX_HOURLY = 500

# Only match ranges/figures anchored to a currency marker, so counts like
# "150,000 customers" are never picked up.
_AMOUNT = r"(?:US[D$]|\$|USD)\s?(\d{2,3}(?:,\d{3})*(?:\.\d+)?|\d{2,3}k|\d{5,7})"
_SEP = r"\s*(?:-|–|—|to)\s*"

_RANGE_RE = re.compile(_AMOUNT + _SEP + _AMOUNT, re.IGNORECASE)
_SINGLE_RE = re.compile(_AMOUNT, re.IGNORECASE)
_HOURLY_RE = re.compile(r"(?:per\s+hour|/\s?h(?:ou)?r\b|hourly)", re.IGNORECASE)
_NON_USD_RE = re.compile(r"[€£¥]")


def _to_number(token: str) -> float:
    token = token.replace(",", "").lower()
    if token.endswith("k"):
        return float(token[:-1]) * 1_000
    return float(token)


def _annualize(value: float, hourly: bool) -> int | None:
    if hourly:
        if not (_MIN_HOURLY <= value <= _MAX_HOURLY):
            return None
        value *= _HOURS_PER_YEAR
    if not (_MIN_ANNUAL <= value <= _MAX_ANNUAL):
        return None
    return int(round(value))


def parse_salary(text: str) -> tuple[int | None, int | None]:
    if not text or _NON_USD_RE.search(text):
        return (None, None)

    hourly = bool(_HOURLY_RE.search(text))

    match = _RANGE_RE.search(text)
    if match:
        lo = _annualize(_to_number(match.group(1)), hourly)
        hi = _annualize(_to_number(match.group(2)), hourly)
        if lo is None or hi is None:
            return (None, None)
        return (min(lo, hi), max(lo, hi))

    match = _SINGLE_RE.search(text)
    if match:
        value = _annualize(_to_number(match.group(1)), hourly)
        if value is None:
            return (None, None)
        return (value, value)

    return (None, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_salary.py -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Commit**

```bash
git add pipeline/salary.py tests/test_salary.py
git commit -m "feat: free-text salary parsing"
```

---

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
  - `pipeline.normalize.classify_remote(location: str | None, description: str, remote_hint: bool | None) -> str` — returns `"remote" | "hybrid" | "onsite"`
  - `pipeline.normalize.to_job(raw: RawJob) -> Job`

Fingerprinting is what makes repost-dedup work (spec §5). It must collapse trivial variants without collapsing genuinely different roles.

- [ ] **Step 1: Write the failing test**

`tests/test_normalize.py`:

```python
from datetime import datetime, timezone

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


def test_normalize_title_strips_noise():
    assert normalize_title("Senior Software Engineer (Remote)") == \
        "senior software engineer"
    assert normalize_title("Software Engineer II - Backend") == \
        "software engineer ii backend"
    assert normalize_title("  Full-Stack   Engineer  ") == "full stack engineer"


def test_normalize_city_extracts_first_component():
    assert normalize_city("San Francisco, CA, USA") == "san francisco"
    assert normalize_city("Remote - US") == "remote"
    assert normalize_city(None) == ""


def test_fingerprint_collapses_trivial_variants():
    a = compute_fingerprint("Acme, Inc.", "Senior Software Engineer (Remote)",
                            "San Francisco, CA, USA")
    b = compute_fingerprint("ACME Inc", "Senior Software Engineer",
                            "San Francisco, CA")
    assert a == b


def test_fingerprint_separates_real_differences():
    base = compute_fingerprint("Acme", "Software Engineer", "San Francisco, CA")
    assert base != compute_fingerprint("Acme", "Staff Software Engineer",
                                       "San Francisco, CA")
    assert base != compute_fingerprint("Acme", "Software Engineer", "Austin, TX")
    assert base != compute_fingerprint("Globex", "Software Engineer",
                                       "San Francisco, CA")


def test_fingerprint_is_stable_and_hex():
    fp = compute_fingerprint("Acme", "Engineer", "SF")
    assert fp == compute_fingerprint("Acme", "Engineer", "SF")
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


def test_classify_remote():
    assert classify_remote("Remote - US", "", None) == "remote"
    assert classify_remote("San Francisco, CA", "", True) == "remote"
    assert classify_remote("San Francisco, CA", "This is a hybrid role, 3 days"
                           " in office", None) == "hybrid"
    assert classify_remote("San Francisco, CA", "Onsite position", None) == "onsite"
    assert classify_remote(None, "", None) == "onsite"


def test_to_job_parses_salary_from_description_when_absent():
    raw = RawJob(
        source="greenhouse", source_job_id="1", company_name="Acme, Inc.",
        title="Senior Software Engineer (Remote)", location="San Francisco, CA",
        description="The base pay range is $150,000 - $200,000.",
        apply_url="https://example.com/1",
        posted_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
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
        source="ashby", source_job_id="2", company_name="Globex", title="Engineer",
        location="Remote", description="No numbers here.",
        apply_url="https://example.com/2", posted_at=None,
        salary_min=160_000, salary_max=190_000, salary_source="structured",
    )
    job = to_job(raw)
    assert (job.salary_min, job.salary_max) == (160_000, 190_000)
    assert job.salary_source == "structured"
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

The fingerprint is what makes repost detection work: a job relisted under a
new source id collapses onto the existing row instead of looking new.
"""
from __future__ import annotations

import hashlib
import re

from pipeline.models import Job, RawJob
from pipeline.salary import parse_salary

_LEGAL_SUFFIXES = {
    "inc", "llc", "ltd", "corp", "corporation", "co", "gmbh", "plc", "sa", "ag",
}
_PAREN_RE = re.compile(r"\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\b(?:fully\s+)?remote\b", re.IGNORECASE)


def _squash(text: str) -> str:
    return _NON_ALNUM_RE.sub(" ", text.lower()).strip()


def normalize_company(name: str) -> str:
    tokens = [t for t in _squash(name).split() if t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def normalize_title(title: str) -> str:
    return " ".join(_squash(_PAREN_RE.sub("", title)).split())


def normalize_city(location: str | None) -> str:
    if not location:
        return ""
    first = re.split(r"[,\-–—/]", location)[0]
    return " ".join(_squash(first).split())


def compute_fingerprint(company: str, title: str, location: str | None) -> str:
    payload = "|".join(
        (normalize_company(company), normalize_title(title), normalize_city(location))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_remote(
    location: str | None, description: str, remote_hint: bool | None
) -> str:
    haystack = f"{location or ''} {description}"
    if _HYBRID_RE.search(haystack):
        return "hybrid"
    if remote_hint or _REMOTE_RE.search(location or ""):
        return "remote"
    if _REMOTE_RE.search(description):
        return "remote"
    return "onsite"


def to_job(raw: RawJob) -> Job:
    salary_min, salary_max, salary_source = (
        raw.salary_min, raw.salary_max, raw.salary_source
    )
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
        remote_type=classify_remote(raw.location, raw.description, raw.remote_hint),
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
Expected: PASS (10 tests)

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
- Consumes: `pipeline.config.Settings`
- Produces:
  - `pipeline.http.HostBlockedError(Exception)` — raised on 403; carries `.host`
  - `pipeline.http.PoliteSession(user_agent: str, conn: sqlite3.Connection | None = None, sleep: Callable[[float], None] = time.sleep, default_delay: float = 1.0, host_delays: dict[str, float] | None = None)`
  - `PoliteSession.get_json(url: str, *, params: dict | None = None, headers: dict | None = None) -> Any | None` — returns parsed JSON, or `None` on 304
  - `PoliteSession.delay_for(host: str) -> float`
  - `PoliteSession.blocked_hosts: set[str]`
  - `PoliteSession.close() -> None`

`_request` takes a method argument so a POST helper is a two-line addition later, but **no `post_json` is exposed in this phase** — nothing in Phase 1 posts. It arrives with the Workday adapter, which is the only POST source.

This is the §7 politeness layer. The `sleep` parameter is injected so tests assert on delays without actually waiting.

- [ ] **Step 1: Write the failing test**

`tests/test_http.py`:

```python
import pytest

from pipeline.db import apply_migrations, connect
from pipeline.http import HostBlockedError, PoliteSession
from tests.conftest import MIGRATIONS


@pytest.fixture
def recorder():
    calls: list[float] = []
    return calls, calls.append


def test_rate_limits_per_host(httpx_mock, recorder):
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/1", json={"ok": 1})
    httpx_mock.add_response(url="https://a.test/2", json={"ok": 2})
    s = PoliteSession("ua/1.0", sleep=sleeper, default_delay=1.0)
    s.get_json("https://a.test/1")
    s.get_json("https://a.test/2")
    assert any(d > 0 for d in slept), "second same-host request should be delayed"


def test_does_not_delay_across_different_hosts(httpx_mock, recorder):
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/1", json={})
    httpx_mock.add_response(url="https://b.test/1", json={})
    s = PoliteSession("ua/1.0", sleep=sleeper, default_delay=1.0)
    s.get_json("https://a.test/1")
    s.get_json("https://b.test/1")
    assert not [d for d in slept if d > 0]


def test_workday_uses_slower_lane(recorder):
    slept, sleeper = recorder
    s = PoliteSession("ua/1.0", sleep=sleeper,
                      host_delays={"x.wd1.myworkdayjobs.com": 2.0})
    assert s.delay_for("x.wd1.myworkdayjobs.com") == 2.0
    assert s.delay_for("boards-api.greenhouse.io") == 1.0


def test_403_raises_and_marks_host_blocked(httpx_mock):
    httpx_mock.add_response(url="https://blocked.test/x", status_code=403)
    s = PoliteSession("ua/1.0", sleep=lambda _: None)
    with pytest.raises(HostBlockedError):
        s.get_json("https://blocked.test/x")
    assert "blocked.test" in s.blocked_hosts


def test_blocked_host_is_not_retried(httpx_mock):
    httpx_mock.add_response(url="https://blocked.test/x", status_code=403)
    s = PoliteSession("ua/1.0", sleep=lambda _: None)
    with pytest.raises(HostBlockedError):
        s.get_json("https://blocked.test/x")
    with pytest.raises(HostBlockedError):
        s.get_json("https://blocked.test/y")
    # Only the first request ever left the process.
    assert len(httpx_mock.get_requests()) == 1


def test_429_backs_off_then_succeeds(httpx_mock, recorder):
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/x", status_code=429)
    httpx_mock.add_response(url="https://a.test/x", json={"ok": True})
    s = PoliteSession("ua/1.0", sleep=sleeper)
    assert s.get_json("https://a.test/x") == {"ok": True}
    assert len(httpx_mock.get_requests()) == 2


def test_sends_user_agent(httpx_mock):
    httpx_mock.add_response(url="https://a.test/x", json={})
    s = PoliteSession("jobhunt/0.1 (contact)", sleep=lambda _: None)
    s.get_json("https://a.test/x")
    assert httpx_mock.get_requests()[0].headers["user-agent"] == \
        "jobhunt/0.1 (contact)"


def test_sends_etag_on_second_request_and_returns_none_on_304(httpx_mock, tmp_path):
    conn = connect(tmp_path / "t.db")
    apply_migrations(conn, MIGRATIONS)
    httpx_mock.add_response(url="https://a.test/x", json={"v": 1},
                            headers={"ETag": 'W/"abc"'})
    httpx_mock.add_response(url="https://a.test/x", status_code=304)

    s = PoliteSession("ua/1.0", conn=conn, sleep=lambda _: None)
    assert s.get_json("https://a.test/x") == {"v": 1}
    assert s.get_json("https://a.test/x") is None
    assert httpx_mock.get_requests()[1].headers["if-none-match"] == 'W/"abc"'
```

Also create `tests/conftest.py`:

```python
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
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
"""
from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_DELAY = 1.0
WORKDAY_DELAY = 2.0
MAX_RETRIES = 3
JITTER = 0.3


class HostBlockedError(Exception):
    """Raised when a host returns 403. We stop rather than retry into a block."""

    def __init__(self, host: str) -> None:
        super().__init__(f"host refused requests (403): {host}")
        self.host = host


class PoliteSession:
    def __init__(
        self,
        user_agent: str,
        conn: sqlite3.Connection | None = None,
        sleep: Callable[[float], None] = time.sleep,
        default_delay: float = DEFAULT_DELAY,
        host_delays: dict[str, float] | None = None,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=True,
        )
        self._conn = conn
        self._sleep = sleep
        self._default_delay = default_delay
        self._host_delays = host_delays or {}
        self._last_hit: dict[str, float] = {}
        self.blocked_hosts: set[str] = set()

    def delay_for(self, host: str) -> float:
        if host in self._host_delays:
            return self._host_delays[host]
        if ".myworkdayjobs.com" in host:
            return WORKDAY_DELAY
        return self._default_delay

    def _wait_turn(self, host: str) -> None:
        base = self.delay_for(host)
        elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
        wait = base - elapsed
        if wait > 0:
            self._sleep(wait * (1 + random.uniform(-JITTER, JITTER)))
        self._last_hit[host] = time.monotonic()

    def _cached_validators(self, url: str) -> dict[str, str]:
        if self._conn is None:
            return {}
        row = self._conn.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return {}
        headers = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    def _store_validators(self, url: str, response: httpx.Response) -> None:
        if self._conn is None:
            return
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if not etag and not last_modified:
            return
        self._conn.execute(
            "INSERT INTO http_cache (url, etag, last_modified, fetched_at)"
            " VALUES (?, ?, ?, datetime('now'))"
            " ON CONFLICT(url) DO UPDATE SET etag=excluded.etag,"
            " last_modified=excluded.last_modified, fetched_at=excluded.fetched_at",
            (url, etag, last_modified),
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> Any | None:
        host = urlsplit(url).netloc
        if host in self.blocked_hosts:
            raise HostBlockedError(host)

        headers = dict(kwargs.pop("headers", None) or {})
        if method == "GET":
            headers.update(self._cached_validators(url))

        for attempt in range(MAX_RETRIES):
            self._wait_turn(host)
            response = self._client.request(method, url, headers=headers, **kwargs)

            if response.status_code == 403:
                self.blocked_hosts.add(host)
                raise HostBlockedError(host)
            if response.status_code == 304:
                return None
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES - 1:
                    response.raise_for_status()
                backoff = (2**attempt) * self._default_delay
                self._sleep(backoff * (1 + random.uniform(0, JITTER)))
                continue

            response.raise_for_status()
            if method == "GET":
                self._store_validators(url, response)
            return response.json()

        return None

    def get_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> Any | None:
        return self._request("GET", url, params=params, headers=headers)

    def close(self) -> None:
        self._client.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_http.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add pipeline/http.py tests/test_http.py tests/conftest.py
git commit -m "feat: PoliteSession with rate limiting, ETags, backoff, 403 stop"
```

---

### Task 5: Source protocol, fixture capture, and the Greenhouse adapter

**Files:**
- Create: `pipeline/sources/__init__.py`, `pipeline/sources/base.py`, `pipeline/sources/greenhouse.py`, `pipeline/sources/registry.py`, `scripts/capture_fixture.py`, `profile.example/targets.yaml`
- Test: `tests/sources/test_greenhouse.py`, `tests/fixtures/greenhouse_board.json`

**Interfaces:**
- Consumes: `PoliteSession`, `RawJob`, `Settings`
- Produces:
  - `pipeline.sources.base.SourceConfig` — dataclass: `session: PoliteSession`, `targets: dict[str, list[str]]`, `settings: Settings`
  - `pipeline.sources.base.Source` — Protocol with `name: str` and `fetch(cfg: SourceConfig) -> Iterator[RawJob]`
  - `pipeline.sources.greenhouse.GreenhouseSource` — `name = "greenhouse"`
  - `pipeline.sources.registry.SOURCES: dict[str, Source]`
  - `pipeline.sources.registry.load_targets(path: Path) -> dict[str, list[str]]`

- [ ] **Step 1: Capture a real fixture**

Write `scripts/capture_fixture.py`:

```python
"""Record a live API response as a test fixture.

Fixtures are captured, never hand-written: a hand-written fixture encodes a
guess about the API's shape, and the tests then verify the guess rather than
reality.

Usage:
    python scripts/capture_fixture.py greenhouse_board \\
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

from pipeline.config import USER_AGENT

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    name, url = sys.argv[1], sys.argv[2]
    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0,
                         follow_redirects=True)
    response.raise_for_status()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{name}.json"
    out.write_text(json.dumps(response.json(), indent=2)[:2_000_000])
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run it:

```bash
./venv/bin/python scripts/capture_fixture.py greenhouse_board \
  "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true"
```

Then open `tests/fixtures/greenhouse_board.json` and read the actual field names. If the shape differs from what the adapter below assumes, **the fixture is right and the adapter must change**. Trim the file to ~5 jobs so tests stay fast.

- [ ] **Step 2: Write the failing test**

`tests/sources/test_greenhouse.py`:

```python
import json
from pathlib import Path

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.sources.base import SourceConfig
from pipeline.sources.greenhouse import GreenhouseSource

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse_board.json"


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=lambda _: None),
        targets={"greenhouse": ["stripe"]},
        settings=load_settings(env={}),
    )


def test_fetch_yields_rawjobs(httpx_mock, cfg):
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true",
        json=json.loads(FIXTURE.read_text()),
    )
    jobs = list(GreenhouseSource().fetch(cfg))
    assert jobs, "fixture should contain at least one job"
    for job in jobs:
        assert job.source == "greenhouse"
        assert job.source_job_id
        assert job.title
        assert job.apply_url.startswith("http")
        assert job.company_name == "stripe"


def test_description_html_entities_are_unescaped(httpx_mock, cfg):
    """Greenhouse returns entity-escaped HTML (spec section A)."""
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true",
        json={"jobs": [{
            "id": 1, "title": "Engineer", "absolute_url": "https://x/1",
            "location": {"name": "Remote"}, "updated_at": "2026-07-30T00:00:00Z",
            "content": "&lt;p&gt;Pay is $150,000&lt;/p&gt;",
        }]},
    )
    job = next(iter(GreenhouseSource().fetch(cfg)))
    assert "&lt;" not in job.description
    assert "$150,000" in job.description


def test_blocked_host_does_not_abort_other_boards(httpx_mock, cfg):
    cfg.targets["greenhouse"] = ["blocked", "ok"]
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/blocked/jobs?content=true",
        status_code=403,
    )
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/ok/jobs?content=true",
        json={"jobs": [{"id": 9, "title": "Dev", "absolute_url": "https://x/9",
                        "location": {"name": "Remote"},
                        "updated_at": "2026-07-30T00:00:00Z", "content": "hi"}]},
    )
    jobs = list(GreenhouseSource().fetch(cfg))
    assert [j.source_job_id for j in jobs] == ["9"]


def test_304_yields_nothing_without_error(httpx_mock, cfg):
    httpx_mock.add_response(
        url="https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true",
        status_code=304,
    )
    assert list(GreenhouseSource().fetch(cfg)) == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/pytest tests/sources/test_greenhouse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.sources'`

- [ ] **Step 4: Implement the source base**

`pipeline/sources/__init__.py` — empty file.

`pipeline/sources/base.py`:

```python
"""The contract every source implements. Adding a source is one new module."""
from __future__ import annotations

from collections.abc import Iterator
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
    name: str

    def fetch(self, cfg: SourceConfig) -> Iterator[RawJob]: ...
```

- [ ] **Step 5: Implement the Greenhouse adapter**

`pipeline/sources/greenhouse.py`:

```python
"""Greenhouse job board API. Unauthenticated; whole board in one response."""
from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator
from datetime import datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig

log = logging.getLogger(__name__)

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(raw: str) -> str:
    """Greenhouse double-escapes: unescape entities, then drop tags."""
    return html.unescape(_TAG_RE.sub(" ", html.unescape(raw or ""))).strip()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GreenhouseSource:
    name = "greenhouse"

    def fetch(self, cfg: SourceConfig) -> Iterator[RawJob]:
        for token in cfg.targets.get(self.name, []):
            try:
                payload = cfg.session.get_json(
                    BOARD_URL.format(token=token), params={"content": "true"}
                )
            except HostBlockedError:
                log.warning("greenhouse host blocked; stopping source")
                return
            except Exception as exc:  # one bad board must not kill the run
                log.warning("greenhouse board %s failed: %s", token, exc)
                continue

            if payload is None:  # 304 Not Modified
                continue

            for item in payload.get("jobs", []):
                location = (item.get("location") or {}).get("name")
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    company_name=token,
                    title=item["title"],
                    location=location,
                    description=_strip_html(item.get("content", "")),
                    apply_url=item["absolute_url"],
                    posted_at=_parse_ts(item.get("updated_at")),
                )
```

Note the `except HostBlockedError` placement: a 403 stops *that source*, while an ordinary failure skips only *that board*. This matches spec §7's "hard stop on 403" without letting one dead board take down the run.

- [ ] **Step 6: Implement the registry and target list**

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
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {k: list(v or []) for k, v in data.items()}
```

`profile.example/targets.yaml`:

```yaml
# Board tokens per ATS. Copy to profile/targets.yaml and fill in.
# Find a token from a company's careers URL, e.g.
#   job-boards.greenhouse.io/stripe  ->  stripe
greenhouse:
  - stripe
  - figma
lever:
  - netflix
ashby:
  - ramp
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/sources/ -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
git add pipeline/sources/ scripts/ profile.example/ tests/sources/ tests/fixtures/
git commit -m "feat: source protocol, fixture capture, Greenhouse adapter"
```

---

### Task 6: Lever and Ashby adapters

**Files:**
- Create: `pipeline/sources/lever.py`, `pipeline/sources/ashby.py`
- Modify: `pipeline/sources/registry.py`
- Test: `tests/sources/test_lever.py`, `tests/sources/test_ashby.py`, fixtures for both

**Interfaces:**
- Consumes: `SourceConfig`, `RawJob`, `HostBlockedError`
- Produces: `LeverSource` (`name = "lever"`), `AshbySource` (`name = "ashby"`)

Ashby is the one source with reliable structured compensation (spec §6), so its adapter must populate `salary_source="structured"` rather than falling through to text parsing.

- [ ] **Step 1: Capture both fixtures**

```bash
./venv/bin/python scripts/capture_fixture.py lever_postings \
  "https://api.lever.co/v0/postings/netflix?mode=json"
./venv/bin/python scripts/capture_fixture.py ashby_board \
  "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"
```

Read both files. Field names below are the expected shape — **if the captured fixture disagrees, the fixture wins.** Note in particular whether Lever's `createdAt` is epoch milliseconds (spec §A says it is) and what Ashby's compensation object actually nests.

- [ ] **Step 2: Write the failing tests**

`tests/sources/test_lever.py`:

```python
import json
from pathlib import Path

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.sources.base import SourceConfig
from pipeline.sources.lever import LeverSource

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lever_postings.json"


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=lambda _: None),
        targets={"lever": ["netflix"]},
        settings=load_settings(env={}),
    )


def test_fetch_yields_rawjobs(httpx_mock, cfg):
    httpx_mock.add_response(
        url="https://api.lever.co/v0/postings/netflix?mode=json",
        json=json.loads(FIXTURE.read_text()),
    )
    jobs = list(LeverSource().fetch(cfg))
    assert jobs
    for job in jobs:
        assert job.source == "lever"
        assert job.apply_url.startswith("http")


def test_epoch_millis_converted_to_datetime(httpx_mock, cfg):
    httpx_mock.add_response(
        url="https://api.lever.co/v0/postings/netflix?mode=json",
        json=[{"id": "abc", "text": "Engineer", "hostedUrl": "https://x/abc",
               "createdAt": 1753920000000, "descriptionPlain": "desc",
               "categories": {"location": "Remote"}}],
    )
    job = next(iter(LeverSource().fetch(cfg)))
    assert job.posted_at is not None
    assert job.posted_at.year == 2025
    assert job.posted_at.tzinfo is not None


def test_missing_categories_does_not_crash(httpx_mock, cfg):
    httpx_mock.add_response(
        url="https://api.lever.co/v0/postings/netflix?mode=json",
        json=[{"id": "abc", "text": "Engineer", "hostedUrl": "https://x/abc",
               "createdAt": None, "descriptionPlain": "desc"}],
    )
    job = next(iter(LeverSource().fetch(cfg)))
    assert job.location is None
    assert job.posted_at is None
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
URL = "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=lambda _: None),
        targets={"ashby": ["ramp"]},
        settings=load_settings(env={}),
    )


def test_fetch_yields_rawjobs(httpx_mock, cfg):
    httpx_mock.add_response(url=URL, json=json.loads(FIXTURE.read_text()))
    jobs = list(AshbySource().fetch(cfg))
    assert jobs
    assert all(j.source == "ashby" for j in jobs)


def test_structured_compensation_is_marked_structured(httpx_mock, cfg):
    httpx_mock.add_response(url=URL, json={"jobs": [{
        "id": "x1", "title": "Engineer", "jobUrl": "https://x/1",
        "location": "Remote", "isRemote": True, "descriptionPlain": "no numbers",
        "publishedAt": "2026-07-30T00:00:00Z",
        "compensation": {"compensationTierSummary": "$180,000 - $220,000"},
    }]})
    job = next(iter(AshbySource().fetch(cfg)))
    assert job.salary_source == "structured"
    assert job.salary_min == 180_000
    assert job.salary_max == 220_000


def test_missing_compensation_leaves_salary_unset(httpx_mock, cfg):
    httpx_mock.add_response(url=URL, json={"jobs": [{
        "id": "x2", "title": "Engineer", "jobUrl": "https://x/2",
        "location": "Remote", "isRemote": True, "descriptionPlain": "text",
        "publishedAt": "2026-07-30T00:00:00Z",
    }]})
    job = next(iter(AshbySource().fetch(cfg)))
    assert job.salary_source == "none"
    assert job.salary_min is None


def test_is_remote_flag_becomes_remote_hint(httpx_mock, cfg):
    httpx_mock.add_response(url=URL, json={"jobs": [{
        "id": "x3", "title": "Engineer", "jobUrl": "https://x/3",
        "location": "New York", "isRemote": True, "descriptionPlain": "t",
        "publishedAt": "2026-07-30T00:00:00Z",
    }]})
    assert next(iter(AshbySource().fetch(cfg))).remote_hint is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/sources/test_lever.py tests/sources/test_ashby.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.sources.lever'`

- [ ] **Step 4: Implement `pipeline/sources/lever.py`**

```python
"""Lever postings API. Unauthenticated; flat JSON array per company."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig

log = logging.getLogger(__name__)

POSTINGS_URL = "https://api.lever.co/v0/postings/{company}"


def _from_epoch_ms(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


class LeverSource:
    name = "lever"

    def fetch(self, cfg: SourceConfig) -> Iterator[RawJob]:
        for company in cfg.targets.get(self.name, []):
            try:
                payload = cfg.session.get_json(
                    POSTINGS_URL.format(company=company), params={"mode": "json"}
                )
            except HostBlockedError:
                log.warning("lever host blocked; stopping source")
                return
            except Exception as exc:
                log.warning("lever company %s failed: %s", company, exc)
                continue

            if payload is None:
                continue

            for item in payload:
                categories = item.get("categories") or {}
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    company_name=company,
                    title=item["text"],
                    location=categories.get("location"),
                    description=item.get("descriptionPlain", ""),
                    apply_url=item["hostedUrl"],
                    posted_at=_from_epoch_ms(item.get("createdAt")),
                )
```

- [ ] **Step 5: Implement `pipeline/sources/ashby.py`**

```python
"""Ashby job board API. The one source with dependable structured salary."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.salary import parse_salary
from pipeline.sources.base import SourceConfig

log = logging.getLogger(__name__)

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _structured_salary(item: dict) -> tuple[int | None, int | None, str]:
    """Read Ashby's compensation summary. Falls back to 'none', never guesses."""
    comp = item.get("compensation") or {}
    summary = comp.get("compensationTierSummary") or ""
    if not summary:
        return (None, None, "none")
    low, high = parse_salary(summary)
    if low is None:
        return (None, None, "none")
    return (low, high, "structured")


class AshbySource:
    name = "ashby"

    def fetch(self, cfg: SourceConfig) -> Iterator[RawJob]:
        for board in cfg.targets.get(self.name, []):
            try:
                payload = cfg.session.get_json(
                    BOARD_URL.format(board=board),
                    params={"includeCompensation": "true"},
                )
            except HostBlockedError:
                log.warning("ashby host blocked; stopping source")
                return
            except Exception as exc:
                log.warning("ashby board %s failed: %s", board, exc)
                continue

            if payload is None:
                continue

            for item in payload.get("jobs", []):
                low, high, source = _structured_salary(item)
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    company_name=board,
                    title=item["title"],
                    location=item.get("location"),
                    description=item.get("descriptionPlain", ""),
                    apply_url=item["jobUrl"],
                    posted_at=_parse_ts(item.get("publishedAt")),
                    remote_hint=item.get("isRemote"),
                    salary_min=low,
                    salary_max=high,
                    salary_source=source,
                )
```

- [ ] **Step 6: Register both sources**

In `pipeline/sources/registry.py`, replace the imports and `SOURCES`:

```python
from pipeline.sources.ashby import AshbySource
from pipeline.sources.greenhouse import GreenhouseSource
from pipeline.sources.lever import LeverSource

SOURCES: dict[str, Source] = {
    GreenhouseSource.name: GreenhouseSource(),
    LeverSource.name: LeverSource(),
    AshbySource.name: AshbySource(),
}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/sources/ -v`
Expected: PASS (11 tests)

- [ ] **Step 8: Commit**

```bash
git add pipeline/sources/ tests/sources/ tests/fixtures/
git commit -m "feat: Lever and Ashby source adapters"
```

---

### Task 7: Remotive and HN "Who is Hiring" adapters

**Files:**
- Create: `pipeline/sources/remotive.py`, `pipeline/sources/hn_algolia.py`
- Modify: `pipeline/sources/registry.py`
- Test: `tests/sources/test_remotive.py`, `tests/sources/test_hn_algolia.py`, fixtures for both

**Interfaces:**
- Consumes: `SourceConfig`, `RawJob`, `HostBlockedError`
- Produces: `RemotiveSource` (`name = "remotive"`), `HNAlgoliaSource` (`name = "hn_algolia"`)

These are aggregators, not ATSes: they need no per-company target list. HN is the odd one — jobs are free-text comments in a monthly thread, so parsing is heuristic and must degrade gracefully rather than emit garbage.

- [ ] **Step 1: Capture both fixtures**

```bash
./venv/bin/python scripts/capture_fixture.py remotive_jobs \
  "https://remotive.com/api/remote-jobs?category=software-dev&limit=20"
./venv/bin/python scripts/capture_fixture.py hn_hiring_thread \
  "https://hn.algolia.com/api/v1/search?query=Ask%20HN%20Who%20is%20hiring&tags=story&hitsPerPage=3"
```

Read both. For HN, the first call finds the thread's `objectID`; a second call fetches its comments — capture that too:

```bash
./venv/bin/python scripts/capture_fixture.py hn_hiring_comments \
  "https://hn.algolia.com/api/v1/search?tags=comment,story_<OBJECT_ID>&hitsPerPage=20"
```

- [ ] **Step 2: Write the failing tests**

`tests/sources/test_remotive.py`:

```python
import json
from pathlib import Path

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.sources.base import SourceConfig
from pipeline.sources.remotive import RemotiveSource

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "remotive_jobs.json"


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=lambda _: None),
        targets={},
        settings=load_settings(env={}),
    )


def test_needs_no_target_list(httpx_mock, cfg):
    httpx_mock.add_response(url__regex=r"https://remotive\.com/api/remote-jobs.*",
                            json=json.loads(FIXTURE.read_text()))
    jobs = list(RemotiveSource().fetch(cfg))
    assert jobs
    assert all(j.source == "remotive" for j in jobs)
    assert all(j.company_name for j in jobs)


def test_company_name_comes_from_payload(httpx_mock, cfg):
    httpx_mock.add_response(url__regex=r"https://remotive\.com/api/remote-jobs.*",
                            json={"jobs": [{
                                "id": 7, "title": "Backend Engineer",
                                "company_name": "Globex",
                                "candidate_required_location": "USA",
                                "description": "<p>$140,000</p>",
                                "url": "https://remotive.com/j/7",
                                "publication_date": "2026-07-29T12:00:00",
                            }]})
    job = next(iter(RemotiveSource().fetch(cfg)))
    assert job.company_name == "Globex"
    assert job.remote_hint is True
    assert "<p>" not in job.description


def test_malformed_entry_is_skipped_not_fatal(httpx_mock, cfg):
    httpx_mock.add_response(url__regex=r"https://remotive\.com/api/remote-jobs.*",
                            json={"jobs": [
                                {"id": 1},  # missing everything
                                {"id": 2, "title": "Dev", "company_name": "OK",
                                 "candidate_required_location": "USA",
                                 "description": "d", "url": "https://x/2",
                                 "publication_date": "2026-07-29T12:00:00"},
                            ]})
    jobs = list(RemotiveSource().fetch(cfg))
    assert [j.source_job_id for j in jobs] == ["2"]
```

`tests/sources/test_hn_algolia.py`:

```python
import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.sources.base import SourceConfig
from pipeline.sources.hn_algolia import HNAlgoliaSource, parse_hn_comment

SEARCH = r"https://hn\.algolia\.com/api/v1/search.*"


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=lambda _: None),
        targets={},
        settings=load_settings(env={}),
    )


@pytest.mark.parametrize(
    "text,company,title",
    [
        ("Acme Corp | Senior Backend Engineer | Remote | $150k-$200k",
         "Acme Corp", "Senior Backend Engineer"),
        ("Globex | San Francisco, CA | Full Stack Engineer | ONSITE",
         "Globex", "San Francisco, CA"),
        ("Initech &#x2F; Platform Engineer &#x2F; Remote",
         "Initech", "Platform Engineer"),
    ],
)
def test_parse_hn_comment_extracts_company_and_title(text, company, title):
    parsed = parse_hn_comment(text)
    assert parsed is not None
    assert parsed[0] == company
    assert parsed[1] == title


def test_parse_hn_comment_returns_none_for_prose():
    assert parse_hn_comment("Does anyone know if this thread is still active?") is None
    assert parse_hn_comment("") is None


def test_fetch_finds_thread_then_comments(httpx_mock, cfg):
    httpx_mock.add_response(url__regex=SEARCH, json={"hits": [
        {"objectID": "111", "title": "Ask HN: Who is hiring? (July 2026)"}]})
    httpx_mock.add_response(url__regex=SEARCH, json={"hits": [
        {"objectID": "222",
         "comment_text": "Acme Corp | Backend Engineer | Remote | $160k",
         "created_at": "2026-07-01T10:00:00.000Z"}]})
    jobs = list(HNAlgoliaSource().fetch(cfg))
    assert len(jobs) == 1
    assert jobs[0].company_name == "Acme Corp"
    assert jobs[0].source == "hn_algolia"
    assert jobs[0].apply_url == "https://news.ycombinator.com/item?id=222"


def test_unparseable_comments_are_dropped(httpx_mock, cfg):
    httpx_mock.add_response(url__regex=SEARCH, json={"hits": [
        {"objectID": "111", "title": "Ask HN: Who is hiring? (July 2026)"}]})
    httpx_mock.add_response(url__regex=SEARCH, json={"hits": [
        {"objectID": "1", "comment_text": "great thread!",
         "created_at": "2026-07-01T10:00:00.000Z"},
        {"objectID": "2", "comment_text": "Acme | Dev | Remote",
         "created_at": "2026-07-01T10:00:00.000Z"}]})
    assert [j.source_job_id for j in HNAlgoliaSource().fetch(cfg)] == ["2"]


def test_no_thread_found_yields_nothing(httpx_mock, cfg):
    httpx_mock.add_response(url__regex=SEARCH, json={"hits": []})
    assert list(HNAlgoliaSource().fetch(cfg)) == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/sources/test_remotive.py tests/sources/test_hn_algolia.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.sources.remotive'`

- [ ] **Step 4: Implement `pipeline/sources/remotive.py`**

```python
"""Remotive aggregator. Public JSON feed, no per-company targets needed."""
from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator
from datetime import datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig

log = logging.getLogger(__name__)

FEED_URL = "https://remotive.com/api/remote-jobs"
CATEGORY = "software-dev"
LIMIT = 100
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class RemotiveSource:
    name = "remotive"

    def fetch(self, cfg: SourceConfig) -> Iterator[RawJob]:
        try:
            payload = cfg.session.get_json(
                FEED_URL, params={"category": CATEGORY, "limit": LIMIT}
            )
        except HostBlockedError:
            log.warning("remotive host blocked; stopping source")
            return
        except Exception as exc:
            log.warning("remotive feed failed: %s", exc)
            return

        if payload is None:
            return

        for item in payload.get("jobs", []):
            try:
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    company_name=item["company_name"],
                    title=item["title"],
                    location=item.get("candidate_required_location"),
                    description=html.unescape(
                        _TAG_RE.sub(" ", item.get("description", ""))
                    ).strip(),
                    apply_url=item["url"],
                    posted_at=_parse_ts(item.get("publication_date")),
                    remote_hint=True,  # every Remotive listing is remote
                )
            except KeyError as exc:
                log.debug("remotive entry missing %s; skipped", exc)
                continue
```

- [ ] **Step 5: Implement `pipeline/sources/hn_algolia.py`**

```python
"""HN "Ask HN: Who is hiring?" via the public Algolia API.

Posts are free-text comments in a loose "Company | Role | Location | ..."
convention. Parsing is heuristic by necessity, so it errs toward dropping a
comment rather than emitting a garbage job.
"""
from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator
from datetime import datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig

log = logging.getLogger(__name__)

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"
_TAG_RE = re.compile(r"<[^>]+>")
_SPLIT_RE = re.compile(r"\s*(?:\||/|—|–)\s*")
_MAX_FIELD = 80


def _clean(raw: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", html.unescape(raw or ""))).strip()


def parse_hn_comment(comment_html: str) -> tuple[str, str, str] | None:
    """Return (company, title, body) or None when the comment isn't a posting."""
    text = _clean(comment_html)
    if not text:
        return None
    first_line = text.split("\n", 1)[0]
    parts = [p.strip() for p in _SPLIT_RE.split(first_line) if p.strip()]
    if len(parts) < 2:
        return None
    company, title = parts[0], parts[1]
    if len(company) > _MAX_FIELD or len(title) > _MAX_FIELD:
        return None
    return (company, title, text)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class HNAlgoliaSource:
    name = "hn_algolia"

    def fetch(self, cfg: SourceConfig) -> Iterator[RawJob]:
        try:
            thread = cfg.session.get_json(
                SEARCH_URL,
                params={"query": "Ask HN Who is hiring", "tags": "story",
                        "hitsPerPage": 1},
            )
        except HostBlockedError:
            log.warning("hn algolia host blocked; stopping source")
            return
        except Exception as exc:
            log.warning("hn thread lookup failed: %s", exc)
            return

        if not thread or not thread.get("hits"):
            return
        story_id = thread["hits"][0]["objectID"]

        try:
            comments = cfg.session.get_json(
                SEARCH_URL,
                params={"tags": f"comment,story_{story_id}", "hitsPerPage": 200},
            )
        except Exception as exc:
            log.warning("hn comment fetch failed: %s", exc)
            return

        if not comments:
            return

        for hit in comments.get("hits", []):
            parsed = parse_hn_comment(hit.get("comment_text", ""))
            if parsed is None:
                continue
            company, title, body = parsed
            yield RawJob(
                source=self.name,
                source_job_id=str(hit["objectID"]),
                company_name=company,
                title=title,
                location=None,
                description=body,
                apply_url=ITEM_URL.format(id=hit["objectID"]),
                posted_at=_parse_ts(hit.get("created_at")),
            )
```

- [ ] **Step 6: Register both sources**

In `pipeline/sources/registry.py`, add the imports and entries:

```python
from pipeline.sources.hn_algolia import HNAlgoliaSource
from pipeline.sources.remotive import RemotiveSource

# ...inside SOURCES:
    RemotiveSource.name: RemotiveSource(),
    HNAlgoliaSource.name: HNAlgoliaSource(),
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/sources/ -v`
Expected: PASS (21 tests)

- [ ] **Step 8: Commit**

```bash
git add pipeline/sources/ tests/sources/ tests/fixtures/
git commit -m "feat: Remotive and HN Who-is-Hiring source adapters"
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

This is spec §8 gate 1 — the free stage that must kill ~70% before anything paid runs. **Unknown salary must pass, not fail:** Greenhouse and Workday rarely publish salary, so failing on unknown would discard most of the pipeline.

- [ ] **Step 1: Write the failing test**

`tests/test_prefilter.py`:

```python
import pytest

from pipeline.config import load_settings
from pipeline.filters.prefilter import prefilter
from pipeline.models import Job

SETTINGS = load_settings(env={})


def make_job(**overrides) -> Job:
    base = dict(
        fingerprint="f" * 64, source="greenhouse", source_job_id="1",
        company_name="Acme", normalized_company="acme",
        title="Senior Software Engineer", location="Remote",
        remote_type="remote", salary_min=None, salary_max=None,
        salary_source="none", description="Python, React, AWS, Docker.",
        apply_url="https://example.com/1", posted_at=None,
    )
    base.update(overrides)
    return Job(**base)


def test_relevant_role_passes():
    assert prefilter(make_job(), SETTINGS).passed


def test_salary_below_floor_is_rejected():
    result = prefilter(make_job(salary_min=90_000, salary_max=110_000,
                                salary_source="structured"), SETTINGS)
    assert not result.passed
    assert result.reason == "salary_below_floor"


def test_salary_at_floor_passes():
    assert prefilter(make_job(salary_min=125_000, salary_max=140_000,
                              salary_source="structured"), SETTINGS).passed


def test_unknown_salary_passes():
    """Greenhouse and Workday rarely publish salary; unknown must not reject."""
    assert prefilter(make_job(salary_source="none"), SETTINGS).passed


def test_max_above_floor_passes_even_if_min_below():
    assert prefilter(make_job(salary_min=115_000, salary_max=160_000,
                              salary_source="parsed"), SETTINGS).passed


@pytest.mark.parametrize("title", [
    "Senior Software Engineer",
    "Full Stack Developer",
    "Backend Engineer, Platform",
    "Cloud Infrastructure Engineer",
    "DevOps Engineer",
    "Machine Learning Engineer",
    "Embedded Software Engineer",
    "Computer Engineer",
    "Site Reliability Engineer",
])
def test_target_titles_pass(title):
    assert prefilter(make_job(title=title), SETTINGS).passed


@pytest.mark.parametrize("title", [
    "Registered Nurse",
    "Account Executive",
    "Warehouse Associate",
    "Engineering Manager",
    "VP of Engineering",
    "Director of Product",
])
def test_off_target_titles_are_rejected(title):
    result = prefilter(make_job(title=title), SETTINGS)
    assert not result.passed
    assert result.reason == "title_not_target"


@pytest.mark.parametrize("title", [
    "Senior Staff Engineer",
    "Principal Engineer",
    "Distinguished Engineer",
])
def test_far_above_level_titles_are_rejected(title):
    result = prefilter(make_job(title=title), SETTINGS)
    assert not result.passed
    assert result.reason == "seniority_mismatch"


def test_internship_and_contract_rejected():
    assert prefilter(make_job(title="Software Engineering Intern"),
                     SETTINGS).reason == "title_not_target"


def test_reason_is_none_when_passed():
    assert prefilter(make_job(), SETTINGS).reason is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_prefilter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.filters'`

- [ ] **Step 3: Implement the pre-filter**

Create empty `pipeline/filters/__init__.py`, then `pipeline/filters/prefilter.py`:

```python
"""Gate 1 of the scoring pipeline: free, deterministic, runs on everything.

Spec section 8 expects this to reject roughly 70% of intake so the paid
stages see very few jobs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.config import Settings
from pipeline.models import Job

TARGET_TITLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(software|full[\s-]?stack|back[\s-]?end|front[\s-]?end|web)\s+"
        r"(engineer|developer)\b",
        r"\b(cloud|infrastructure|platform|devops|site\s+reliability|sre)\s+engineer\b",
        r"\b(machine\s+learning|ml|ai|applied\s+scientist)\s+engineer\b",
        r"\b(embedded|firmware|systems)\s+(software\s+)?engineer\b",
        r"\bcomputer\s+engineer\b",
        r"\b(software|application)\s+developer\b",
        r"\bengineer,\s+(software|platform|infrastructure|backend|frontend)\b",
    )
]

# Roles far above ~3 years of experience. The stretch allowance in scoring
# (spec section 8) reintroduces a small random slice of these later.
SENIORITY_EXCLUDE_RE = re.compile(
    r"\b(principal|distinguished|staff|fellow|architect|manager|director|"
    r"head\s+of|vp|vice\s+president|chief)\b",
    re.IGNORECASE,
)

NON_ENGINEERING_RE = re.compile(
    r"\b(nurse|sales|account\s+executive|warehouse|driver|recruiter|"
    r"marketing|paralegal|teacher|intern|internship)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str | None = None


def prefilter(job: Job, settings: Settings) -> FilterResult:
    title = job.title

    if NON_ENGINEERING_RE.search(title):
        return FilterResult(False, "title_not_target")

    if SENIORITY_EXCLUDE_RE.search(title):
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
Expected: PASS (25 tests)

- [ ] **Step 5: Commit**

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
- Consumes: `Job`, `sqlite3.Connection`
- Produces:
  - `pipeline.store.upsert_company(conn, name: str, ats_type: str | None = None, board_token: str | None = None) -> int`
  - `pipeline.store.upsert_job(conn, job: Job, company_id: int, filter_reason: str | None) -> tuple[int, bool]` — returns `(job_id, is_new)`
  - `pipeline.store.start_run(conn) -> int`
  - `pipeline.store.finish_run(conn, run_id: int, *, jobs_seen: int, jobs_new: int, jobs_filtered: int, errors: int, peak_rss_mb: int | None = None, notes: str | None = None) -> None`

The upsert is where repost handling lands: a second sighting must update `last_seen_at` and report `is_new=False`, never create a duplicate row.

- [ ] **Step 1: Write the failing test**

`tests/test_store.py`:

```python
from datetime import datetime, timezone

import pytest

from pipeline.db import apply_migrations, connect
from pipeline.models import Job
from pipeline.store import (
    finish_run,
    start_run,
    upsert_company,
    upsert_job,
)
from tests.conftest import MIGRATIONS


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    apply_migrations(c, MIGRATIONS)
    return c


def make_job(**overrides) -> Job:
    base = dict(
        fingerprint="a" * 64, source="greenhouse", source_job_id="1",
        company_name="Acme", normalized_company="acme",
        title="Software Engineer", location="Remote", remote_type="remote",
        salary_min=None, salary_max=None, salary_source="none",
        description="d", apply_url="https://example.com/1",
        posted_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Job(**base)


def test_upsert_company_is_idempotent(conn):
    first = upsert_company(conn, "Acme, Inc.", "greenhouse", "acme")
    second = upsert_company(conn, "ACME Inc", "greenhouse", "acme")
    assert first == second
    assert conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"] == 1


def test_upsert_job_inserts_then_reports_not_new(conn):
    cid = upsert_company(conn, "Acme")
    job_id, is_new = upsert_job(conn, make_job(), cid, None)
    assert is_new is True
    same_id, is_new_again = upsert_job(conn, make_job(), cid, None)
    assert same_id == job_id
    assert is_new_again is False
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 1


def test_repost_under_new_source_id_collapses(conn):
    """Same content, new source id: must not create a second row."""
    cid = upsert_company(conn, "Acme")
    first_id, _ = upsert_job(conn, make_job(source_job_id="1"), cid, None)
    second_id, is_new = upsert_job(conn, make_job(source_job_id="999"), cid, None)
    assert second_id == first_id
    assert is_new is False


def test_upsert_job_advances_last_seen(conn):
    cid = upsert_company(conn, "Acme")
    upsert_job(conn, make_job(), cid, None)
    conn.execute("UPDATE jobs SET last_seen_at = '2000-01-01T00:00:00Z'")
    upsert_job(conn, make_job(), cid, None)
    row = conn.execute("SELECT first_seen_at, last_seen_at FROM jobs").fetchone()
    assert row["last_seen_at"] > "2000-01-01T00:00:00Z"


def test_filter_reason_is_recorded(conn):
    cid = upsert_company(conn, "Acme")
    upsert_job(conn, make_job(), cid, "salary_below_floor")
    row = conn.execute("SELECT filtered_out, filter_reason FROM jobs").fetchone()
    assert row["filtered_out"] == 1
    assert row["filter_reason"] == "salary_below_floor"


def test_posted_at_stored_as_iso_utc(conn):
    cid = upsert_company(conn, "Acme")
    upsert_job(conn, make_job(), cid, None)
    value = conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"]
    assert value.startswith("2026-07-30T")


def test_run_log_roundtrip(conn):
    run_id = start_run(conn)
    finish_run(conn, run_id, jobs_seen=100, jobs_new=20, jobs_filtered=70,
               errors=1, peak_rss_mb=412)
    row = conn.execute("SELECT * FROM run_log WHERE run_id = ?",
                       (run_id,)).fetchone()
    assert row["jobs_seen"] == 100
    assert row["jobs_new"] == 20
    assert row["jobs_filtered"] == 70
    assert row["errors"] == 1
    assert row["peak_rss_mb"] == 412
    assert row["finished_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.store'`

- [ ] **Step 3: Implement `pipeline/store.py`**

```python
"""Persistence. The upsert here is what makes repost dedup work."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pipeline.models import Job
from pipeline.normalize import normalize_company


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    ats_type: str | None = None,
    board_token: str | None = None,
) -> int:
    normalized = normalize_company(name)
    row = conn.execute(
        "SELECT company_id FROM companies WHERE normalized_name = ?", (normalized,)
    ).fetchone()
    if row is not None:
        return int(row["company_id"])
    cursor = conn.execute(
        "INSERT INTO companies (name, normalized_name, ats_type, board_token)"
        " VALUES (?, ?, ?, ?)",
        (name.strip(), normalized, ats_type, board_token),
    )
    return int(cursor.lastrowid)


def upsert_job(
    conn: sqlite3.Connection,
    job: Job,
    company_id: int,
    filter_reason: str | None,
) -> tuple[int, bool]:
    """Insert or refresh a job. Returns (job_id, is_new).

    Matching is by fingerprint, so a repost under a new source id updates the
    existing row rather than creating a duplicate.
    """
    now = _now()
    existing = conn.execute(
        "SELECT job_id FROM jobs WHERE fingerprint = ?", (job.fingerprint,)
    ).fetchone()

    if existing is not None:
        conn.execute(
            "UPDATE jobs SET last_seen_at = ?, salary_min = ?, salary_max = ?,"
            " salary_source = ?, filtered_out = ?, filter_reason = ?"
            " WHERE job_id = ?",
            (now, job.salary_min, job.salary_max, job.salary_source,
             1 if filter_reason else 0, filter_reason, existing["job_id"]),
        )
        return (int(existing["job_id"]), False)

    cursor = conn.execute(
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " location, remote_type, salary_min, salary_max, salary_source,"
        " description, apply_url, posted_at, first_seen_at, last_seen_at,"
        " filtered_out, filter_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job.fingerprint, company_id, job.source, job.source_job_id, job.title,
         job.location, job.remote_type, job.salary_min, job.salary_max,
         job.salary_source, job.description, job.apply_url, _iso(job.posted_at),
         now, now, 1 if filter_reason else 0, filter_reason),
    )
    return (int(cursor.lastrowid), True)


def start_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO run_log (started_at) VALUES (?)", (_now(),)
    )
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    jobs_seen: int,
    jobs_new: int,
    jobs_filtered: int,
    errors: int,
    peak_rss_mb: int | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        "UPDATE run_log SET finished_at = ?, jobs_seen = ?, jobs_new = ?,"
        " jobs_filtered = ?, errors = ?, peak_rss_mb = ?, notes = ?"
        " WHERE run_id = ?",
        (_now(), jobs_seen, jobs_new, jobs_filtered, errors, peak_rss_mb,
         notes, run_id),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_store.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add pipeline/store.py tests/test_store.py
git commit -m "feat: persistence layer with fingerprint-based repost dedup"
```

---

### Task 10: Orchestration and CLI

**Files:**
- Create: `pipeline/run_daily.py`
- Test: `tests/test_run_daily.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above
- Produces:
  - `pipeline.run_daily.RunStats` — frozen dataclass: `jobs_seen: int`, `jobs_new: int`, `jobs_filtered: int`, `errors: int`
  - `pipeline.run_daily.run(conn, cfg: SourceConfig, source_names: list[str] | None = None) -> RunStats`
  - `pipeline.run_daily.main(argv: list[str] | None = None) -> int`

CLI: `python -m pipeline.run_daily [--sources greenhouse,lever] [--dry-run]`

- [ ] **Step 1: Write the failing test**

`tests/test_run_daily.py`:

```python
from datetime import datetime, timezone

import pytest

from pipeline.config import load_settings
from pipeline.db import apply_migrations, connect
from pipeline.http import PoliteSession
from pipeline.models import RawJob
from pipeline.run_daily import run
from pipeline.sources.base import SourceConfig
from tests.conftest import MIGRATIONS


class FakeSource:
    def __init__(self, name, jobs, exc=None):
        self.name = name
        self._jobs = jobs
        self._exc = exc

    def fetch(self, cfg):
        if self._exc:
            raise self._exc
        yield from self._jobs


def raw(**overrides) -> RawJob:
    base = dict(
        source="fake", source_job_id="1", company_name="Acme",
        title="Software Engineer", location="Remote",
        description="Python React AWS", apply_url="https://example.com/1",
        posted_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return RawJob(**base)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    apply_migrations(c, MIGRATIONS)
    return c


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=lambda _: None),
        targets={}, settings=load_settings(env={}),
    )


def test_run_persists_jobs_and_counts(conn, cfg, monkeypatch):
    monkeypatch.setattr(
        "pipeline.run_daily.SOURCES",
        {"fake": FakeSource("fake", [raw(), raw(source_job_id="2",
                                            title="DevOps Engineer")])},
    )
    stats = run(conn, cfg)
    assert stats.jobs_seen == 2
    assert stats.jobs_new == 2
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 2


def test_run_counts_filtered_jobs(conn, cfg, monkeypatch):
    monkeypatch.setattr(
        "pipeline.run_daily.SOURCES",
        {"fake": FakeSource("fake", [raw(), raw(source_job_id="2",
                                               title="Registered Nurse")])},
    )
    stats = run(conn, cfg)
    assert stats.jobs_seen == 2
    assert stats.jobs_filtered == 1
    row = conn.execute(
        "SELECT filter_reason FROM jobs WHERE filtered_out = 1"
    ).fetchone()
    assert row["filter_reason"] == "title_not_target"


def test_second_run_reports_no_new_jobs(conn, cfg, monkeypatch):
    monkeypatch.setattr("pipeline.run_daily.SOURCES",
                        {"fake": FakeSource("fake", [raw()])})
    run(conn, cfg)
    stats = run(conn, cfg)
    assert stats.jobs_seen == 1
    assert stats.jobs_new == 0


def test_failing_source_is_counted_not_fatal(conn, cfg, monkeypatch):
    monkeypatch.setattr(
        "pipeline.run_daily.SOURCES",
        {
            "bad": FakeSource("bad", [], exc=RuntimeError("boom")),
            "good": FakeSource("good", [raw()]),
        },
    )
    stats = run(conn, cfg)
    assert stats.errors == 1
    assert stats.jobs_new == 1


def test_source_names_restricts_which_sources_run(conn, cfg, monkeypatch):
    monkeypatch.setattr(
        "pipeline.run_daily.SOURCES",
        {"a": FakeSource("a", [raw()]),
         "b": FakeSource("b", [raw(source_job_id="2", company_name="Globex")])},
    )
    stats = run(conn, cfg, source_names=["a"])
    assert stats.jobs_seen == 1


def test_run_writes_a_run_log_row(conn, cfg, monkeypatch):
    monkeypatch.setattr("pipeline.run_daily.SOURCES",
                        {"fake": FakeSource("fake", [raw()])})
    run(conn, cfg)
    row = conn.execute("SELECT * FROM run_log ORDER BY run_id DESC").fetchone()
    assert row["finished_at"] is not None
    assert row["jobs_seen"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_run_daily.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.run_daily'`

- [ ] **Step 3: Implement `pipeline/run_daily.py`**

```python
"""Phase 1 entry point: discover, normalize, pre-filter, persist."""
from __future__ import annotations

import argparse
import logging
import resource
import sys
from dataclasses import dataclass

import sqlite3

from pipeline.config import load_settings
from pipeline.db import apply_migrations, connect
from pipeline.filters.prefilter import prefilter
from pipeline.http import PoliteSession
from pipeline.normalize import to_job
from pipeline.sources.base import SourceConfig
from pipeline.sources.registry import SOURCES, load_targets
from pipeline.store import finish_run, start_run, upsert_company, upsert_job

log = logging.getLogger("jobhunt")


@dataclass(frozen=True)
class RunStats:
    jobs_seen: int
    jobs_new: int
    jobs_filtered: int
    errors: int


def _peak_rss_mb() -> int:
    """Peak RSS this process. Linux reports KiB, macOS reports bytes."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak / 1024) if sys.platform == "linux" else int(peak / 1_048_576)


def run(
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    source_names: list[str] | None = None,
) -> RunStats:
    run_id = start_run(conn)
    seen = new = filtered = errors = 0

    selected = source_names or list(SOURCES)
    for name in selected:
        source = SOURCES.get(name)
        if source is None:
            log.warning("unknown source %s; skipped", name)
            continue
        try:
            for raw_job in source.fetch(cfg):
                seen += 1
                job = to_job(raw_job)
                result = prefilter(job, cfg.settings)
                if not result.passed:
                    filtered += 1
                company_id = upsert_company(conn, job.company_name, job.source)
                _, is_new = upsert_job(conn, job, company_id, result.reason)
                if is_new:
                    new += 1
        except Exception as exc:
            errors += 1
            log.warning("source %s failed: %s", name, exc)

    finish_run(conn, run_id, jobs_seen=seen, jobs_new=new, jobs_filtered=filtered,
               errors=errors, peak_rss_mb=_peak_rss_mb())
    return RunStats(seen, new, filtered, errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.run_daily")
    parser.add_argument("--sources", help="comma-separated source names")
    parser.add_argument("--dry-run", action="store_true",
                        help="use a throwaway in-memory database")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    settings = load_settings()
    conn = connect(settings.db_path) if not args.dry_run else sqlite3.connect(
        ":memory:"
    )
    if args.dry_run:
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
    apply_migrations(conn, settings.migrations_dir)

    session = PoliteSession(settings.user_agent, conn=conn)
    cfg = SourceConfig(
        session=session,
        targets=load_targets(settings.profile_dir / "targets.yaml"),
        settings=settings,
    )

    names = args.sources.split(",") if args.sources else None
    try:
        stats = run(conn, cfg, names)
    finally:
        session.close()

    log.info(
        "seen=%d new=%d filtered=%d errors=%d peak_rss=%dMB",
        stats.jobs_seen, stats.jobs_new, stats.jobs_filtered, stats.errors,
        _peak_rss_mb(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_run_daily.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the whole suite**

Run: `./venv/bin/pytest -v`
Expected: PASS (~100 tests), no network access during the run.

- [ ] **Step 6: Do a real end-to-end run**

```bash
mkdir -p profile
cp profile.example/targets.yaml profile/targets.yaml
# Edit profile/targets.yaml: add 10-20 real board tokens.
./venv/bin/python -m pipeline.run_daily --dry-run -v
```

Confirm from the log line that `seen` is in the hundreds and `filtered` is roughly 60–80% of it. **If `filtered` is over ~95%, the title patterns in Task 8 are too narrow** — widen them against the actual titles now in the database rather than guessing:

```bash
./venv/bin/python -m pipeline.run_daily -v
sqlite3 jobhunt.db "SELECT filter_reason, COUNT(*) FROM jobs GROUP BY 1;"
sqlite3 jobhunt.db "SELECT title FROM jobs WHERE filtered_out=0 LIMIT 40;"
```

- [ ] **Step 7: Update the README**

Replace `README.md` with:

```markdown
# job_hunt

Automated job discovery and application preparation. Discovery is fully
automated; **submission is always done by hand** — see
`docs/superpowers/specs/2026-07-31-job-hunt-system-design.md` section 3.

## Setup

    python -m venv venv
    ./venv/bin/pip install -e ".[dev]"
    mkdir -p profile
    cp profile.example/targets.yaml profile/targets.yaml
    # edit profile/targets.yaml with real board tokens

## Run

    ./venv/bin/python -m pipeline.run_daily            # persist to jobhunt.db
    ./venv/bin/python -m pipeline.run_daily --dry-run  # throwaway in-memory db
    ./venv/bin/python -m pipeline.run_daily --sources greenhouse,lever

## Test

    ./venv/bin/pytest

Tests never hit the network. Fixtures are captured from live endpoints with
`scripts/capture_fixture.py` and replayed via `pytest-httpx`.

## Status

Phase 1 (discovery) complete. Phases 2-6 — scoring, Sharia screen, web UI,
document generation, digest — are specified but not yet built.
```

- [ ] **Step 8: Commit**

```bash
git add pipeline/run_daily.py tests/test_run_daily.py README.md
git commit -m "feat: pipeline orchestration and CLI"
```

---

## Phase 1 Done Criteria

- `./venv/bin/pytest` passes with no network access.
- `python -m pipeline.run_daily` ingests from at least Greenhouse, Lever, Ashby, Remotive, and HN in one run.
- A second consecutive run reports `jobs_new=0` — dedup works.
- `run_log` has one row per run with a populated `peak_rss_mb`.
- The pre-filter rejects roughly 60–80% of intake (spec §8 expects ~70%).
- **`sqlite3 jobhunt.db "SELECT COUNT(*) FROM jobs WHERE filtered_out=0"` after one day is the number that decides Phase 2.** Spec §16: under ~30 relevant jobs/day means adding a paid source, not more engineering.

## Explicitly Out of Scope for Phase 1

Deferred per spec §16: embeddings and scoring (Phase 2), Sharia screen (Phase 2), web UI (Phase 3), `work_queue` and document generation (Phase 4), digest email (Phase 5), eager generation (Phase 6). **No Anthropic API key is needed to complete or run Phase 1.**

**Sources deliberately deferred, not forgotten.** Spec §6 lists fifteen; this plan builds five — Greenhouse, Lever, and Ashby (the highest-signal ATSes, and the three whose adapters establish every pattern the rest reuse) plus Remotive and HN (two structurally different aggregators, which proves the `Source` protocol isn't accidentally ATS-shaped).

The remaining ten are each one module against the now-proven protocol, and the volume measured in the done-criteria decides how many are worth writing:

| Deferred | Why it waits |
|---|---|
| SmartRecruiters, Recruitee, Workable | Same shape as the three built; pure repetition, add on demand |
| Workday | Only POST source and the only one with cross-tenant IP rate limiting. `PoliteSession` already carries its 2s lane; the adapter needs its own care. |
| Arbeitnow, Himalayas, Jobicy, WeWorkRemotely, The Muse | Breadth, overlapping heavily with Remotive |
| Adzuna, USAJobs | Need API keys (spec §15.5, §15.6). Adzuna's free-tier ceiling is unverified and may make it daily-only. |
