# Phase 3: Web UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ranked queue reviewable in ten minutes over morning coffee, and make marking a job applied take one click.

**Architecture:** Server-rendered FastAPI + Jinja2, HTMX for status mutations and polling, Tailwind v4 for styling. Four routes (spec §12). The same function that runs the crons serves the UI — Vercel bundles one FastAPI app, so routes share models and database code by import rather than over a wire.

**Tech Stack:** FastAPI, Jinja2, HTMX (vendored), Tailwind v4 (standalone CLI, CSS committed), PostgreSQL.

## Global Constraints

Everything in the Phase 1 and Phase 2 Global Constraints still applies. Additionally:

- **No JavaScript build step at deploy time** (spec §12). This is why the deployment stays one Python function instead of two runtimes. Tailwind runs through its standalone binary locally and the output CSS is committed; CI asserts the committed file is current.
- **No client-side framework.** HTMX is vendored as a single file, not fetched from a CDN — an external `<script src>` would make the UI fail whenever that CDN does, and would leak page loads to a third party.
- **Every mutation is a POST, never a GET.** A status change behind a GET gets fired by link prefetchers and crawlers.
- **The UI reads and mutates; it never crawls, judges, or generates.** Those are cron work. A route that triggers a crawl would put a third-party rate limit behind a page refresh.
- **No automated process ever authenticates as Jarra on any job platform** (spec §3). "Open apply page" is a plain `target="_blank"` link. There is no browser in the dependency tree and none is added here.
- **Access control is Vercel Authentication, not application code.** Verified in Task 7 before any personal data is rendered in production.

## Scope

Spec §12's job-detail route lists an inline PDF preview and downloads of both documents. **Document generation is Phase 4** (spec §10, fpdf2) — it does not exist yet. Phase 3 builds the detail page around what does exist: the JD, the match rationale, the apply link, the answer-bank panel, and status buttons. The document panel is added in Phase 4 rather than stubbed here, so nothing renders an empty box that looks broken.

---

## File Structure

| File | Responsibility |
|---|---|
| `migrations/004_applications.sql` | `applications` and `answer_bank` tables |
| `web/__init__.py` | Router assembly, Jinja2 environment, template filters |
| `web/queue.py` | `/` — the ranked queue |
| `web/detail.py` | `/job/{id}` — detail and status mutations |
| `web/tracker.py` | `/tracker` — funnel and conversion |
| `web/settings.py` | `/settings` — answer bank, overrides, run log |
| `web/templates/base.html` | Shell: nav, Tailwind link, vendored HTMX |
| `web/templates/*.html` | One per route, plus `_partials/` for HTMX fragments |
| `web/static/app.css` | Tailwind output — generated, committed |
| `web/static/htmx.min.js` | Vendored, pinned |
| `pipeline/store.py` | Query helpers the routes call |
| `scripts/build_css.sh` | Runs the Tailwind standalone CLI |
| `tests/test_web_*.py` | One per route module |

---

### Task 1: Application and answer-bank schema

**Files:**
- Create: `migrations/004_applications.sql`
- Modify: `tests/conftest.py`
- Test: `tests/test_applications_schema.py`

**Interfaces:**
- Produces: the `applications` and `answer_bank` tables every route below reads.

The tracker funnel is `queued → applied → responded → interview → rejected` (spec §12). **Status lives in its own table, not a column on `jobs`.** A job row is rewritten by every discovery pass — it is upserted on fingerprint — and putting mutable human state in a machine-owned row means one repost wipes it.

- [ ] **Step 1: Write the migration**

`migrations/004_applications.sql`:

```sql
-- Phase 3: human-owned state. Deliberately separate from `jobs`, which every
-- discovery pass rewrites via ON CONFLICT -- a status column there would be
-- erased by the next repost of the same posting.

CREATE TABLE applications (
  job_id        BIGINT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
  status        TEXT NOT NULL DEFAULT 'queued',
  applied_at    TIMESTAMPTZ,
  responded_at  TIMESTAMPTZ,
  interview_at  TIMESTAMPTZ,
  closed_at     TIMESTAMPTZ,
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- A free-text status would break every funnel query the moment a typo
  -- reached the database.
  CONSTRAINT status_is_known CHECK (
    status IN ('queued', 'applied', 'responded', 'interview', 'rejected', 'dismissed')
  ),
  -- "applied" without a timestamp makes the conversion stats silently wrong.
  CONSTRAINT applied_has_a_timestamp CHECK (
    status = 'queued' OR status = 'dismissed' OR applied_at IS NOT NULL
  )
);

CREATE INDEX idx_applications_status ON applications(status);
CREATE INDEX idx_applications_applied_at ON applications(applied_at) WHERE applied_at IS NOT NULL;

-- Reusable answers to the questions application forms keep asking.
CREATE TABLE answer_bank (
  answer_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question    TEXT NOT NULL UNIQUE,
  answer      TEXT NOT NULL,
  category    TEXT,
  used_count  INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Questions seen on forms with no stored answer yet. Surfaced in /settings so
-- the bank grows from real forms rather than from guesses about them.
CREATE TABLE unmapped_questions (
  question    TEXT PRIMARY KEY,
  seen_count  INTEGER NOT NULL DEFAULT 1,
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Add `applications`, `answer_bank`, and `unmapped_questions` to `_TABLES` in `tests/conftest.py`.

- [ ] **Step 2: Write the failing test**

`tests/test_applications_schema.py`:

```python
import psycopg
import pytest


async def _job(db, fingerprint="fp") -> int:
    await db.execute(
        "INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')"
        " ON CONFLICT DO NOTHING"
    )
    cur = await db.execute(
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at)"
        " VALUES (%s, 1, 'greenhouse', '1', 'Engineer', 'd', 'https://x', now(), now())"
        " RETURNING job_id",
        (fingerprint,),
    )
    return (await cur.fetchone())["job_id"]


async def test_tables_exist(db):
    cur = await db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    names = {r["tablename"] for r in await cur.fetchall()}
    assert {"applications", "answer_bank", "unmapped_questions"} <= names


async def test_a_new_application_defaults_to_queued(db):
    job_id = await _job(db)
    await db.execute("INSERT INTO applications (job_id) VALUES (%s)", (job_id,))
    cur = await db.execute("SELECT status, applied_at FROM applications")
    row = await cur.fetchone()
    assert row["status"] == "queued"
    assert row["applied_at"] is None


async def test_an_unknown_status_is_rejected(db):
    """A typo reaching the database breaks every funnel query silently."""
    job_id = await _job(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(
            "INSERT INTO applications (job_id, status, applied_at) VALUES (%s, 'aplied', now())",
            (job_id,),
        )


async def test_applied_without_a_timestamp_is_rejected(db):
    # Otherwise the conversion stats count an application that has no date.
    job_id = await _job(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(
            "INSERT INTO applications (job_id, status) VALUES (%s, 'applied')", (job_id,)
        )


async def test_dismissed_needs_no_timestamp(db):
    # Dismissing a job is not applying to it.
    job_id = await _job(db)
    await db.execute(
        "INSERT INTO applications (job_id, status) VALUES (%s, 'dismissed')", (job_id,)
    )


async def test_status_survives_the_job_being_re_upserted(db):
    """The reason this table exists.

    Discovery upserts jobs on fingerprint every pass. A status column on `jobs`
    would be erased by the next repost of the same posting.
    """
    job_id = await _job(db)
    await db.execute(
        "INSERT INTO applications (job_id, status, applied_at) VALUES (%s, 'applied', now())",
        (job_id,),
    )
    await db.execute(
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at)"
        " VALUES ('fp', 1, 'greenhouse', '1', 'Engineer (Reposted)', 'd2', 'https://x',"
        " now(), now())"
        " ON CONFLICT (fingerprint) DO UPDATE SET title = EXCLUDED.title,"
        " description = EXCLUDED.description, last_seen_at = now()"
    )
    cur = await db.execute("SELECT status FROM applications WHERE job_id = %s", (job_id,))
    assert (await cur.fetchone())["status"] == "applied"


async def test_answer_bank_questions_are_unique(db):
    await db.execute(
        "INSERT INTO answer_bank (question, answer) VALUES ('Work authorization?', 'US citizen')"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        await db.execute(
            "INSERT INTO answer_bank (question, answer) VALUES ('Work authorization?', 'different')"
        )
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_applications_schema.py -v`
Expected: FAIL — `relation "applications" does not exist`

- [ ] **Step 4: Apply and verify**

```bash
DATABASE_URL="postgresql://jobhunt@localhost:5433/jobhunt_test" ./venv/bin/python scripts/migrate.py
./venv/bin/pytest tests/test_applications_schema.py -v
```

Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add migrations/004_applications.sql tests/conftest.py tests/test_applications_schema.py
git commit -m "feat: applications and answer-bank schema"
```

---

### Task 2: Template shell and the CSS pipeline

**Files:**
- Create: `web/__init__.py`, `web/templates/base.html`, `web/templates/input.css`, `web/static/htmx.min.js`, `scripts/build_css.sh`
- Modify: `pyproject.toml`, `api/index.py`, `.github/workflows/ci.yml`
- Test: `tests/test_web_shell.py`

**Interfaces:**
- Produces:
  - `web.templates_env` — configured `Jinja2Templates`
  - `web.router` — the `APIRouter` the app mounts
  - Jinja filters: `ago`, `money`, `pct`

**Tailwind runs locally and its output is committed.** Spec §12 forbids a
JavaScript build step at deploy time, which is what keeps this one Python
function instead of two runtimes. The standalone Tailwind binary needs no
`node_modules`, so `scripts/build_css.sh` is a local step and CI asserts the
committed CSS is current — the failure mode of a stale stylesheet is a page
that silently loses its styling weeks later.

- [ ] **Step 1: Vendor HTMX**

```bash
mkdir -p web/static
curl -fsSL https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js -o web/static/htmx.min.js
shasum -a 256 web/static/htmx.min.js
```

Record the hash in a comment in `base.html`. Vendored rather than linked: a
CDN `<script src>` makes the UI break whenever that CDN does, and reports every
page load to a third party.

- [ ] **Step 2: Write the CSS build script**

`scripts/build_css.sh`:

```bash
#!/usr/bin/env bash
# Build Tailwind CSS with the standalone binary -- no node_modules, no npm.
#
# Deliberately a LOCAL step. Spec section 12 rules out a deploy-time JS build,
# which is what keeps the deployment one Python function rather than two
# runtimes. The output is committed and CI checks it is current.
set -euo pipefail

VERSION="v4.1.11"
BIN="${JOBHUNT_TAILWIND_BIN:-$HOME/.local/bin/tailwindcss}"

if [ ! -x "$BIN" ]; then
  case "$(uname -sm)" in
    "Darwin arm64") ASSET="tailwindcss-macos-arm64" ;;
    "Darwin x86_64") ASSET="tailwindcss-macos-x64" ;;
    "Linux x86_64") ASSET="tailwindcss-linux-x64" ;;
    *) echo "no standalone Tailwind for $(uname -sm)" >&2; exit 1 ;;
  esac
  mkdir -p "$(dirname "$BIN")"
  curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/${VERSION}/${ASSET}" -o "$BIN"
  chmod +x "$BIN"
fi

"$BIN" -i web/templates/input.css -o web/static/app.css --minify
echo "wrote web/static/app.css"
```

`web/templates/input.css`:

```css
@import "tailwindcss";
@source "../templates/**/*.html";
```

- [ ] **Step 3: Write the failing test**

`tests/test_web_shell.py`:

```python
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.index import app

client = TestClient(app)


def test_static_css_is_served():
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_htmx_is_vendored_not_linked():
    """A CDN <script src> makes the UI fail whenever that CDN does, and reports
    every page load to a third party."""
    assert Path("web/static/htmx.min.js").exists()
    base = Path("web/templates/base.html").read_text()
    assert "unpkg.com" not in base
    assert "cdn.jsdelivr" not in base
    assert "/static/htmx.min.js" in base


def test_no_tailwind_cdn_script():
    # The browser build compiles CSS at runtime on every page load.
    base = Path("web/templates/base.html").read_text()
    assert "cdn.tailwindcss.com" not in base


def test_committed_css_is_current():
    """A stale stylesheet fails silently -- the page just loses its styling,
    weeks after the template change that caused it."""
    if not Path("web/static/app.css").exists():
        pytest.fail("run ./scripts/build_css.sh")
    before = Path("web/static/app.css").read_bytes()
    subprocess.run(["./scripts/build_css.sh"], check=True, capture_output=True)
    assert Path("web/static/app.css").read_bytes() == before, (
        "web/static/app.css is stale — run ./scripts/build_css.sh and commit"
    )


# --- filters -----------------------------------------------------------------


@pytest.mark.parametrize(
    "hours,expected",
    [(0.5, "just now"), (3, "3h ago"), (30, "1d ago"), (24 * 9, "9d ago")],
)
def test_ago_filter(hours, expected):
    from datetime import UTC, datetime, timedelta

    from web import ago

    assert ago(datetime.now(UTC) - timedelta(hours=hours)) == expected


def test_ago_handles_none():
    # HN and parts of Lever omit posted_at; the template must not crash on it.
    from web import ago

    assert ago(None) == "unknown"


@pytest.mark.parametrize(
    "low,high,expected",
    [
        (150_000, 180_000, "$150k–$180k"),
        (150_000, None, "$150k+"),
        (None, None, "not listed"),
    ],
)
def test_money_filter(low, high, expected):
    from web import money

    assert money(low, high) == expected
```

- [ ] **Step 4: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_web_shell.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web'`

- [ ] **Step 5: Implement `web/__init__.py`**

```python
"""Web UI: routers, the Jinja environment, and template filters.

Server-rendered throughout. HTMX handles status mutations and polling; there is
no client-side framework and no deploy-time JavaScript build, which is what
keeps the deployment one Python function rather than two runtimes (spec 12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates_env = Jinja2Templates(directory=str(TEMPLATE_DIR))


def ago(when: datetime | None) -> str:
    """Human-readable age. Never raises on a missing timestamp."""
    if when is None:
        # HN and parts of Lever omit posted_at entirely.
        return "unknown"
    delta = datetime.now(UTC) - when
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


def money(low: int | None, high: int | None) -> str:
    """Salary range. Two thirds of postings have nothing to show here."""
    if low is None and high is None:
        return "not listed"
    if high is None:
        return f"${low // 1000:,}k+"
    if low is None:
        return f"up to ${high // 1000:,}k"
    if low == high:
        return f"${low // 1000:,}k"
    return f"${low // 1000:,}k–${high // 1000:,}k"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


templates_env.env.filters["ago"] = ago
templates_env.env.filters["money"] = money
templates_env.env.filters["pct"] = pct

router = APIRouter()


def register(app) -> None:
    """Mount static files and every route module onto the FastAPI app."""
    from fastapi.staticfiles import StaticFiles

    from web import detail, queue, settings, tracker

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for module in (queue, detail, tracker, settings):
        app.include_router(module.router)
```

- [ ] **Step 6: Write `web/templates/base.html`**

```html
<!doctype html>
<html lang="en" class="h-full">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}job_hunt{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
  {# Vendored, not CDN-linked: an external <script src> makes the UI fail
     whenever that CDN does, and reports every page load to a third party.
     htmx 2.0.4, sha256 recorded in the commit that added it. #}
  <script src="/static/htmx.min.js" defer></script>
</head>
<body class="h-full bg-stone-50 text-stone-900">
  <nav class="border-b border-stone-200 bg-white">
    <div class="mx-auto flex max-w-5xl gap-6 px-4 py-3 text-sm">
      <a href="/" class="font-semibold">job_hunt</a>
      <a href="/" class="hover:underline">Queue</a>
      <a href="/tracker" class="hover:underline">Tracker</a>
      <a href="/settings" class="hover:underline">Settings</a>
    </div>
  </nav>
  <main class="mx-auto max-w-5xl px-4 py-6">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 7: Wire it into the app**

In `api/index.py`, replace the placeholder `root()` with the registration:

```python
from web import register

register(app)
```

Add to `pyproject.toml` dependencies: `"jinja2>=3.1"`, `"python-multipart>=0.0.9"`
(FastAPI needs the latter to parse the form posts the status buttons send).

In `vercel.json`, `web/**` must not be excluded — check `excludeFiles` and
remove any glob that would catch `web/static` or `web/templates`.

- [ ] **Step 8: Build the CSS and run the tests**

```bash
chmod +x scripts/build_css.sh
./scripts/build_css.sh
./venv/bin/pytest tests/test_web_shell.py -v
```

Expected: PASS (10 tests)

- [ ] **Step 9: Add the CI drift check**

In `.github/workflows/ci.yml`, after the Lint step:

```yaml
      - name: Check committed CSS is current
        # A stale stylesheet fails silently -- the page just loses its styling.
        run: |
          ./scripts/build_css.sh
          git diff --exit-code web/static/app.css
```

- [ ] **Step 10: Commit**

```bash
git add web/ scripts/build_css.sh pyproject.toml api/index.py vercel.json \
        .github/workflows/ci.yml tests/test_web_shell.py
git commit -m "feat: web shell, template filters, and the Tailwind pipeline"
```

---

### Task 3: The ranked queue

**Files:**
- Create: `web/queue.py`, `web/templates/queue.html`, `web/templates/_partials/job_card.html`
- Modify: `pipeline/store.py`
- Test: `tests/test_web_queue.py`

**Interfaces:**
- Consumes: `web.templates_env`, `scores`, `jobs`, `companies`, `applications`
- Produces:
  - `web.queue.router` — `GET /`
  - `await pipeline.store.queue_page(conn, *, limit, offset, min_score, include_dismissed) -> list[dict]`

Cards carry title, company, salary, location, score, relative age, and a Sharia
badge when flagged (spec §12).

**Excluded companies never appear; flagged ones appear with a badge.** The
distinction is the whole point of the three-tier screen — an `excluded` verdict
is a decision, a `flagged` one is a question for Jarra, and collapsing them
would either hide jobs that need a human or show ones that do not.

- [ ] **Step 1: Write the failing test**

`tests/test_web_queue.py`:

```python
from fastapi.testclient import TestClient

from api.index import app

client = TestClient(app)


async def _seed(db, *, title="Backend Engineer", score=0.8, sharia="allowed", status=None):
    cur = await db.execute(
        "INSERT INTO companies (name, normalized_name, sharia_verdict, sharia_source,"
        " sharia_reason) VALUES (%s, %s, %s, 'llm', 'because')"
        " ON CONFLICT (normalized_name) DO UPDATE SET sharia_verdict = EXCLUDED.sharia_verdict"
        " RETURNING company_id",
        (f"Co {title}", title.lower().replace(" ", "-"), sharia),
    )
    company_id = (await cur.fetchone())["company_id"]
    cur = await db.execute(
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title, location,"
        " remote_type, salary_min, salary_max, salary_source, description, apply_url,"
        " posted_at, first_seen_at, last_seen_at)"
        " VALUES (%s, %s, 'greenhouse', '1', %s, 'Remote', 'remote', 150000, 180000,"
        " 'structured', 'Build things.', 'https://example.com/1', now(), now(), now())"
        " RETURNING job_id",
        (title.ljust(64, "x")[:64], company_id, title),
    )
    job_id = (await cur.fetchone())["job_id"]
    await db.execute(
        "INSERT INTO scores (job_id, total_score, embed_similarity, relevance_verdict,"
        " rationale, model, judged_at)"
        " VALUES (%s, %s, 0.7, 'strong', 'Matches your Python and AWS work.',"
        " 'claude-haiku-4-5', now())",
        (job_id, score),
    )
    if status:
        await db.execute(
            "INSERT INTO applications (job_id, status, applied_at)"
            " VALUES (%s, %s, CASE WHEN %s IN ('queued','dismissed') THEN NULL ELSE now() END)",
            (job_id, status, status),
        )
    return job_id


async def test_queue_renders(db):
    await _seed(db)
    response = client.get("/")
    assert response.status_code == 200
    assert "Backend Engineer" in response.text


async def test_jobs_are_ordered_by_score_descending(db):
    await _seed(db, title="Low Match", score=0.2)
    await _seed(db, title="High Match", score=0.9)
    body = client.get("/").text
    assert body.index("High Match") < body.index("Low Match")


async def test_the_rationale_is_shown_on_the_card(db):
    """The rationale is what makes the queue reviewable; the number is not."""
    await _seed(db)
    assert "Matches your Python and AWS work." in client.get("/").text


async def test_salary_is_formatted_not_raw(db):
    await _seed(db)
    body = client.get("/").text
    assert "$150k–$180k" in body
    assert "150000" not in body


async def test_excluded_companies_are_absent(db):
    await _seed(db, title="Casino Job", sharia="excluded")
    assert "Casino Job" not in client.get("/").text


async def test_flagged_companies_appear_with_a_badge(db):
    """An excluded verdict is a decision; a flagged one is a question for Jarra.

    Collapsing the two either hides jobs that need a human or shows ones that
    do not.
    """
    await _seed(db, title="Fintech Job", sharia="flagged")
    body = client.get("/").text
    assert "Fintech Job" in body
    assert "flagged" in body.lower()


async def test_filtered_out_jobs_never_appear(db):
    job_id = await _seed(db, title="Sales Role")
    await db.execute(
        "UPDATE jobs SET filtered_out = true, filter_reason = 'title_not_target' WHERE job_id = %s",
        (job_id,),
    )
    assert "Sales Role" not in client.get("/").text


async def test_applied_jobs_leave_the_queue(db):
    # The queue is what is left to review, not an archive.
    await _seed(db, title="Already Applied", status="applied")
    assert "Already Applied" not in client.get("/").text


async def test_dismissed_jobs_leave_the_queue(db):
    await _seed(db, title="Not Interested", status="dismissed")
    assert "Not Interested" not in client.get("/").text


async def test_an_unjudged_job_still_renders(db):
    """Only the top N get judged. The rest must still be visible, not blank."""
    job_id = await _seed(db, title="Unjudged Role")
    await db.execute(
        "UPDATE scores SET relevance_verdict = NULL, rationale = NULL, model = NULL,"
        " judged_at = NULL WHERE job_id = %s",
        (job_id,),
    )
    body = client.get("/")
    assert body.status_code == 200
    assert "Unjudged Role" in body.text


async def test_an_empty_queue_says_so_rather_than_rendering_nothing(db):
    body = client.get("/").text
    assert "nothing" in body.lower() or "no jobs" in body.lower()


async def test_a_missing_posted_at_does_not_break_the_card(db):
    job_id = await _seed(db, title="No Date")
    await db.execute("UPDATE jobs SET posted_at = NULL WHERE job_id = %s", (job_id,))
    response = client.get("/")
    assert response.status_code == 200
    assert "unknown" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_web_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.queue'`

- [ ] **Step 3: Add the store query**

Append to `pipeline/store.py`:

```python
async def queue_page(
    conn: AsyncConnection,
    *,
    limit: int = 50,
    offset: int = 0,
    min_score: float = 0.0,
) -> list[dict]:
    """The ranked queue: scored, live, not yet acted on.

    `excluded` companies are absent; `flagged` ones are present and badged. An
    excluded verdict is a decision, a flagged one is a question for Jarra, and
    collapsing them either hides jobs needing a human or shows ones that do not.
    """
    return await _fetch_all(
        conn,
        """
        SELECT j.job_id, j.title, j.location, j.remote_type, j.salary_min, j.salary_max,
               j.apply_url, j.posted_at,
               c.name AS company_name, c.sharia_verdict, c.sharia_reason,
               s.total_score, s.embed_similarity, s.relevance_verdict, s.rationale,
               s.is_stretch
        FROM scores s
        JOIN jobs j USING (job_id)
        JOIN companies c USING (company_id)
        LEFT JOIN applications a USING (job_id)
        WHERE j.filtered_out = false
          AND c.sharia_verdict <> 'excluded'
          AND s.total_score >= %s
          -- The queue is what is left to review, not an archive: anything
          -- already acted on has left it.
          AND (a.status IS NULL OR a.status = 'queued')
        ORDER BY s.total_score DESC, j.posted_at DESC NULLS LAST
        LIMIT %s OFFSET %s
        """,
        (min_score, limit, offset),
    )
```

- [ ] **Step 4: Implement `web/queue.py`**

```python
"""GET / — the ranked queue (spec section 12).

Sorted by total_score. Everything already acted on has left; the queue is what
remains to review, not an archive.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pipeline.db import connection
from pipeline.store import queue_page
from web import templates_env

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, min_score: float = 0.0, offset: int = 0) -> HTMLResponse:
    async with connection() as conn:
        jobs = await queue_page(conn, limit=50, offset=offset, min_score=min_score)
    return templates_env.TemplateResponse(
        request=request,
        name="queue.html",
        context={"jobs": jobs, "min_score": min_score, "offset": offset},
    )
```

- [ ] **Step 5: Write the templates**

`web/templates/_partials/job_card.html`:

```html
<article class="rounded-lg border border-stone-200 bg-white p-4">
  <div class="flex items-baseline justify-between gap-4">
    <h2 class="text-base font-semibold">
      <a href="/job/{{ job.job_id }}" class="hover:underline">{{ job.title }}</a>
    </h2>
    <span class="shrink-0 text-sm tabular-nums text-stone-500">
      {{ job.total_score | pct }}
    </span>
  </div>

  <p class="mt-1 text-sm text-stone-600">
    {{ job.company_name }} · {{ job.location or 'location not listed' }}
    · {{ job.remote_type }} · {{ job.salary_min | money(job.salary_max) }}
    · {{ job.posted_at | ago }}
  </p>

  {% if job.rationale %}
    {# The rationale is what makes the queue reviewable in ten minutes. The
       score alone tells you nothing the ranking did not already say. #}
    <p class="mt-2 text-sm text-stone-800">{{ job.rationale }}</p>
  {% endif %}

  <div class="mt-3 flex flex-wrap items-center gap-2 text-xs">
    {% if job.relevance_verdict %}
      <span class="rounded bg-stone-100 px-2 py-0.5">{{ job.relevance_verdict }}</span>
    {% endif %}
    {% if job.is_stretch %}
      <span class="rounded bg-amber-100 px-2 py-0.5 text-amber-900">stretch</span>
    {% endif %}
    {% if job.sharia_verdict == 'flagged' %}
      <span class="rounded bg-orange-100 px-2 py-0.5 text-orange-900"
            title="{{ job.sharia_reason }}">sharia: flagged — needs your call</span>
    {% endif %}
  </div>
</article>
```

`web/templates/queue.html`:

```html
{% extends "base.html" %}
{% block title %}Queue — job_hunt{% endblock %}
{% block content %}
  <h1 class="mb-4 text-lg font-semibold">Queue <span class="text-stone-400">({{ jobs | length }})</span></h1>

  {% if not jobs %}
    <p class="rounded-lg border border-dashed border-stone-300 p-8 text-center text-stone-500">
      Nothing to review. The next discovery run is within 10 minutes.
    </p>
  {% else %}
    <div class="space-y-3">
      {% for job in jobs %}{% include "_partials/job_card.html" %}{% endfor %}
    </div>
  {% endif %}
{% endblock %}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_web_queue.py -v`
Expected: PASS (12 tests)

- [ ] **Step 7: Look at it**

```bash
DATABASE_URL="postgresql://jobhunt@localhost:5433/jobhunt_dev" \
  ./venv/bin/uvicorn api.index:app --reload --port 8000
```

Open `http://localhost:8000`. The test that matters is not automatable: **can
you triage the top twenty in ten minutes?** If a card needs a click to be
judged, the wrong fields are on it.

- [ ] **Step 8: Commit**

```bash
git add web/queue.py web/templates/ pipeline/store.py tests/test_web_queue.py
git commit -m "feat: ranked queue"
```

---

### Task 4: Job detail and status mutations

**Files:**
- Create: `web/detail.py`, `web/templates/detail.html`, `web/templates/_partials/status_buttons.html`
- Modify: `pipeline/store.py`
- Test: `tests/test_web_detail.py`

**Interfaces:**
- Consumes: `queue_page`'s tables, `answer_bank`
- Produces:
  - `web.detail.router` — `GET /job/{job_id}`, `POST /job/{job_id}/status`
  - `await pipeline.store.job_detail(conn, job_id) -> dict | None`
  - `await pipeline.store.set_status(conn, job_id, status) -> None`
  - `await pipeline.store.answer_bank_all(conn) -> list[dict]`

Status buttons post via HTMX and swap the button row in place — no page reload,
no client-side state.

**Every mutation is a POST.** A status change behind a GET gets fired by link
prefetchers, by the browser's history restore, and by anything that crawls the
page.

- [ ] **Step 1: Write the failing test**

`tests/test_web_detail.py`:

```python
from fastapi.testclient import TestClient

from api.index import app
from tests.test_web_queue import _seed

client = TestClient(app)


async def test_detail_renders_the_description(db):
    job_id = await _seed(db)
    response = client.get(f"/job/{job_id}")
    assert response.status_code == 200
    assert "Build things." in response.text


async def test_the_rationale_is_shown(db):
    job_id = await _seed(db)
    assert "Matches your Python and AWS work." in client.get(f"/job/{job_id}").text


async def test_the_apply_link_opens_in_a_new_tab(db):
    """Spec section 3: no automated process ever authenticates as Jarra on a
    job platform. This is a plain link and stays one."""
    job_id = await _seed(db)
    body = client.get(f"/job/{job_id}").text
    assert 'href="https://example.com/1"' in body
    assert 'target="_blank"' in body
    assert 'rel="noopener' in body


async def test_a_missing_job_is_a_404(db):
    assert client.get("/job/999999").status_code == 404


async def test_marking_applied_sets_the_timestamp(db):
    job_id = await _seed(db)
    response = client.post(f"/job/{job_id}/status", data={"status": "applied"})
    assert response.status_code == 200
    cur = await db.execute(
        "SELECT status, applied_at FROM applications WHERE job_id = %s", (job_id,)
    )
    row = await cur.fetchone()
    assert row["status"] == "applied"
    assert row["applied_at"] is not None


async def test_status_can_be_changed_twice(db):
    # The first POST inserts, the second updates. A plain INSERT would 500.
    job_id = await _seed(db)
    client.post(f"/job/{job_id}/status", data={"status": "applied"})
    client.post(f"/job/{job_id}/status", data={"status": "responded"})
    cur = await db.execute("SELECT status FROM applications WHERE job_id = %s", (job_id,))
    assert (await cur.fetchone())["status"] == "responded"


async def test_applied_at_is_preserved_across_later_transitions(db):
    """Conversion stats measure from the application date. Overwriting it on
    every transition would silently reset every funnel interval."""
    job_id = await _seed(db)
    client.post(f"/job/{job_id}/status", data={"status": "applied"})
    cur = await db.execute("SELECT applied_at FROM applications WHERE job_id = %s", (job_id,))
    first = (await cur.fetchone())["applied_at"]

    client.post(f"/job/{job_id}/status", data={"status": "interview"})
    cur = await db.execute("SELECT applied_at FROM applications WHERE job_id = %s", (job_id,))
    assert (await cur.fetchone())["applied_at"] == first


async def test_an_unknown_status_is_rejected_with_400_not_500(db):
    job_id = await _seed(db)
    response = client.post(f"/job/{job_id}/status", data={"status": "wat"})
    assert response.status_code == 400


async def test_status_cannot_be_changed_by_a_get(db):
    """A GET mutation gets fired by prefetchers, history restore, and crawlers."""
    job_id = await _seed(db)
    assert client.get(f"/job/{job_id}/status?status=applied").status_code == 405
    cur = await db.execute("SELECT count(*) AS n FROM applications")
    assert (await cur.fetchone())["n"] == 0


async def test_the_status_post_returns_a_fragment_not_a_full_page(db):
    # HTMX swaps this into the button row; a full document would nest <html>.
    job_id = await _seed(db)
    body = client.post(f"/job/{job_id}/status", data={"status": "applied"}).text
    assert "<html" not in body.lower()


async def test_the_answer_bank_panel_is_rendered(db):
    await db.execute(
        "INSERT INTO answer_bank (question, answer)"
        " VALUES ('Work authorization?', 'US-born citizen')"
    )
    job_id = await _seed(db)
    body = client.get(f"/job/{job_id}").text
    assert "Work authorization?" in body
    assert "US-born citizen" in body


async def test_a_flagged_company_shows_its_reason_on_the_detail_page(db):
    job_id = await _seed(db, title="Fintech Job", sharia="flagged")
    body = client.get(f"/job/{job_id}").text
    assert "because" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_web_detail.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.detail'`

- [ ] **Step 3: Add the store helpers**

```python
async def job_detail(conn: AsyncConnection, job_id: int) -> dict | None:
    return await _fetch_one(
        conn,
        """
        SELECT j.*, c.name AS company_name, c.sharia_verdict, c.sharia_reason,
               c.sharia_source,
               s.total_score, s.embed_similarity, s.rule_score, s.freshness_score,
               s.relevance_verdict, s.rationale, s.is_stretch,
               COALESCE(a.status, 'queued') AS status, a.applied_at, a.notes
        FROM jobs j
        JOIN companies c USING (company_id)
        LEFT JOIN scores s USING (job_id)
        LEFT JOIN applications a USING (job_id)
        WHERE j.job_id = %s
        """,
        (job_id,),
    )


VALID_STATUSES = frozenset({"queued", "applied", "responded", "interview", "rejected", "dismissed"})


async def set_status(conn: AsyncConnection, job_id: int, status: str) -> None:
    """Upsert the application row.

    applied_at is set on the first transition out of queued and never rewritten:
    conversion stats measure intervals from the application date, so overwriting
    it on a later transition would silently reset every one of them.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {status!r}")

    needs_timestamp = status not in ("queued", "dismissed")
    await conn.execute(
        """
        INSERT INTO applications (job_id, status, applied_at, updated_at)
        VALUES (%s, %s, CASE WHEN %s THEN now() ELSE NULL END, now())
        ON CONFLICT (job_id) DO UPDATE SET
            status     = EXCLUDED.status,
            applied_at = CASE
                WHEN %s THEN COALESCE(applications.applied_at, now())
                ELSE applications.applied_at
            END,
            updated_at = now()
        """,
        (job_id, status, needs_timestamp, needs_timestamp),
    )


async def answer_bank_all(conn: AsyncConnection) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT answer_id, question, answer, category FROM answer_bank"
        " ORDER BY category NULLS LAST, question",
        (),
    )
```

- [ ] **Step 4: Implement `web/detail.py`**

```python
"""GET /job/{id} and POST /job/{id}/status (spec section 12).

Mutations are POST only. A status change behind a GET gets fired by link
prefetchers, by history restore, and by anything that crawls the page.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from pipeline.db import connection
from pipeline.store import answer_bank_all, job_detail, set_status
from web import templates_env

router = APIRouter()


@router.get("/job/{job_id}", response_class=HTMLResponse)
async def detail(request: Request, job_id: int) -> HTMLResponse:
    async with connection() as conn:
        job = await job_detail(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        answers = await answer_bank_all(conn)

    return templates_env.TemplateResponse(
        request=request,
        name="detail.html",
        context={"job": job, "answers": answers},
    )


@router.post("/job/{job_id}/status", response_class=HTMLResponse)
async def update_status(
    request: Request, job_id: int, status: Annotated[str, Form()]
) -> HTMLResponse:
    """Set the status and return just the button row for HTMX to swap in."""
    async with connection() as conn:
        if await job_detail(conn, job_id) is None:
            raise HTTPException(status_code=404, detail="no such job")
        try:
            await set_status(conn, job_id, status)
        except ValueError as exc:
            # 400, not a 500 from the CHECK constraint downstream.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return templates_env.TemplateResponse(
        request=request,
        name="_partials/status_buttons.html",
        context={"job": {"job_id": job_id, "status": status}},
    )
```

- [ ] **Step 5: Write the templates**

`web/templates/_partials/status_buttons.html`:

```html
<div id="status-buttons" class="flex flex-wrap gap-2">
  {% for option in ['applied', 'responded', 'interview', 'rejected', 'dismissed'] %}
    <button
      hx-post="/job/{{ job.job_id }}/status"
      hx-vals='{"status": "{{ option }}"}'
      hx-target="#status-buttons"
      hx-swap="outerHTML"
      class="rounded border px-3 py-1.5 text-sm
             {% if job.status == option %}
               border-stone-900 bg-stone-900 text-white
             {% else %}
               border-stone-300 bg-white hover:bg-stone-100
             {% endif %}">
      {{ option }}
    </button>
  {% endfor %}
</div>
```

`web/templates/detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ job.title }} — job_hunt{% endblock %}
{% block content %}
  <a href="/" class="text-sm text-stone-500 hover:underline">← queue</a>

  <h1 class="mt-2 text-xl font-semibold">{{ job.title }}</h1>
  <p class="mt-1 text-sm text-stone-600">
    {{ job.company_name }} · {{ job.location or 'location not listed' }}
    · {{ job.remote_type }} · {{ job.salary_min | money(job.salary_max) }}
    · posted {{ job.posted_at | ago }}
  </p>

  {% if job.sharia_verdict == 'flagged' %}
    <div class="mt-4 rounded border border-orange-300 bg-orange-50 p-3 text-sm">
      <strong>Sharia screen: flagged.</strong> {{ job.sharia_reason }}
      <span class="text-stone-600">Set an override in Settings to decide permanently.</span>
    </div>
  {% endif %}

  {% if job.rationale %}
    <div class="mt-4 rounded border border-stone-200 bg-white p-3">
      <h2 class="text-sm font-semibold">Why this matched</h2>
      <p class="mt-1 text-sm">{{ job.rationale }}</p>
      <p class="mt-2 text-xs text-stone-500">
        {{ job.relevance_verdict }} · score {{ job.total_score | pct }}
        · similarity {{ job.embed_similarity | pct }}
      </p>
    </div>
  {% endif %}

  <div class="mt-4 flex items-center gap-3">
    {# A plain link, in a new tab. Spec section 3: no automated process ever
       authenticates as Jarra on a job platform, so this is never a form post
       and there is no browser in the dependency tree. #}
    <a href="{{ job.apply_url }}" target="_blank" rel="noopener noreferrer"
       class="rounded bg-stone-900 px-4 py-2 text-sm text-white hover:bg-stone-700">
      Open apply page ↗
    </a>
    {% include "_partials/status_buttons.html" %}
  </div>

  {% if answers %}
    <details class="mt-6 rounded border border-stone-200 bg-white p-3">
      <summary class="cursor-pointer text-sm font-semibold">
        Answer bank ({{ answers | length }})
      </summary>
      <dl class="mt-3 space-y-3">
        {% for entry in answers %}
          <div>
            <dt class="text-sm font-medium">{{ entry.question }}</dt>
            <dd class="mt-0.5 flex items-start gap-2">
              <span class="flex-1 text-sm text-stone-700">{{ entry.answer }}</span>
              <button type="button"
                      class="shrink-0 rounded border border-stone-300 px-2 py-0.5 text-xs"
                      onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent.trim())">
                copy
              </button>
            </dd>
          </div>
        {% endfor %}
      </dl>
    </details>
  {% endif %}

  <section class="mt-6 rounded border border-stone-200 bg-white p-4">
    <h2 class="text-sm font-semibold">Job description</h2>
    <div class="prose prose-sm mt-2 max-w-none whitespace-pre-wrap">{{ job.description }}</div>
  </section>
{% endblock %}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_web_detail.py -v`
Expected: PASS (12 tests)

- [ ] **Step 7: Commit**

```bash
git add web/detail.py web/templates/ pipeline/store.py tests/test_web_detail.py
git commit -m "feat: job detail and status mutations"
```

---

### Task 5: The tracker

**Files:**
- Create: `web/tracker.py`, `web/templates/tracker.html`
- Modify: `pipeline/store.py`
- Test: `tests/test_web_tracker.py`

**Interfaces:**
- Produces:
  - `web.tracker.router` — `GET /tracker`
  - `await pipeline.store.funnel_counts(conn) -> dict[str, int]`
  - `await pipeline.store.weekly_conversion(conn, weeks=8) -> list[dict]`

Funnel `queued → applied → responded → interview → rejected`, plus weekly
conversion (spec §12).

**Conversion is measured against applications sent that week, not responses
received that week.** A response arrives one to three weeks after the
application; bucketing by response date attributes it to a week whose
application volume has nothing to do with it, which makes the rate meaningless
exactly when it matters.

- [ ] **Step 1: Write the failing test**

`tests/test_web_tracker.py`:

```python
from fastapi.testclient import TestClient

from api.index import app
from tests.test_web_queue import _seed

client = TestClient(app)


async def test_tracker_renders_with_no_data(db):
    response = client.get("/tracker")
    assert response.status_code == 200
    # A zero-state that renders nothing looks like a crash.
    assert "0" in response.text


async def test_funnel_counts_each_status(db):
    for status in ("applied", "responded", "interview", "rejected"):
        job_id = await _seed(db, title=f"Job {status}")
        await db.execute(
            "INSERT INTO applications (job_id, status, applied_at) VALUES (%s, %s, now())",
            (job_id, status),
        )
    from pipeline.store import funnel_counts

    counts = await funnel_counts(db)
    assert counts["applied"] == 1
    assert counts["interview"] == 1


async def test_dismissed_jobs_are_not_in_the_funnel(db):
    """Dismissing is not a funnel stage — counting it would deflate every rate."""
    job_id = await _seed(db, title="Dismissed")
    await db.execute(
        "INSERT INTO applications (job_id, status) VALUES (%s, 'dismissed')", (job_id,)
    )
    from pipeline.store import funnel_counts

    assert await funnel_counts(db) == {} or "dismissed" not in await funnel_counts(db)


async def test_conversion_buckets_by_application_date_not_response_date(db):
    """A response arrives 1-3 weeks after the application.

    Bucketing by response date attributes it to a week whose application volume
    has nothing to do with it, which makes the rate meaningless exactly when it
    matters.
    """
    job_id = await _seed(db, title="Slow Response")
    await db.execute(
        "INSERT INTO applications (job_id, status, applied_at, responded_at)"
        " VALUES (%s, 'responded', now() - interval '21 days', now())",
        (job_id,),
    )
    from pipeline.store import weekly_conversion

    weeks = await weekly_conversion(db, weeks=8)
    three_weeks_ago = [w for w in weeks if w["applied"] == 1]
    assert len(three_weeks_ago) == 1
    assert three_weeks_ago[0]["responded"] == 1


async def test_a_week_with_no_applications_reports_no_rate_not_a_division_error(db):
    from pipeline.store import weekly_conversion

    weeks = await weekly_conversion(db, weeks=8)
    for week in weeks:
        assert week["response_rate"] is None or 0.0 <= week["response_rate"] <= 1.0


async def test_the_page_shows_the_funnel_stages(db):
    body = client.get("/tracker").text
    for stage in ("queued", "applied", "responded", "interview"):
        assert stage in body.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_web_tracker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.tracker'`

- [ ] **Step 3: Add the store helpers**

```python
FUNNEL_STAGES = ("queued", "applied", "responded", "interview", "rejected")


async def funnel_counts(conn: AsyncConnection) -> dict[str, int]:
    """Count per funnel stage. `dismissed` is excluded — it is not a stage, and
    counting it would deflate every rate."""
    rows = await _fetch_all(
        conn,
        "SELECT status, count(*) AS n FROM applications"
        " WHERE status <> 'dismissed' GROUP BY status",
        (),
    )
    return {row["status"]: row["n"] for row in rows}


async def weekly_conversion(conn: AsyncConnection, weeks: int = 8) -> list[dict]:
    """Applications per week and what came of them.

    Bucketed by applied_at, NOT by responded_at. A response arrives one to three
    weeks after the application; bucketing by response date attributes it to a
    week whose application volume has nothing to do with it.
    """
    return await _fetch_all(
        conn,
        """
        SELECT date_trunc('week', applied_at) AS week,
               count(*) AS applied,
               count(*) FILTER (WHERE status IN ('responded', 'interview')) AS responded,
               count(*) FILTER (WHERE status = 'interview') AS interviewed,
               CASE WHEN count(*) = 0 THEN NULL
                    ELSE count(*) FILTER (WHERE status IN ('responded', 'interview'))::float
                         / count(*)
               END AS response_rate
        FROM applications
        WHERE applied_at IS NOT NULL
          AND applied_at >= date_trunc('week', now()) - (%s * interval '1 week')
        GROUP BY week
        ORDER BY week DESC
        """,
        (weeks,),
    )
```

- [ ] **Step 4: Implement `web/tracker.py`**

```python
"""GET /tracker — funnel and weekly conversion (spec section 12)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pipeline.db import connection
from pipeline.store import FUNNEL_STAGES, funnel_counts, weekly_conversion
from web import templates_env

router = APIRouter()


@router.get("/tracker", response_class=HTMLResponse)
async def tracker(request: Request) -> HTMLResponse:
    async with connection() as conn:
        counts = await funnel_counts(conn)
        weeks = await weekly_conversion(conn, weeks=8)

    return templates_env.TemplateResponse(
        request=request,
        name="tracker.html",
        context={
            # Every stage is present with an explicit zero: a stage that
            # disappears when empty makes the funnel look shorter than it is.
            "stages": [(stage, counts.get(stage, 0)) for stage in FUNNEL_STAGES],
            "weeks": weeks,
        },
    )
```

- [ ] **Step 5: Write `web/templates/tracker.html`**

```html
{% extends "base.html" %}
{% block title %}Tracker — job_hunt{% endblock %}
{% block content %}
  <h1 class="mb-4 text-lg font-semibold">Tracker</h1>

  <div class="grid grid-cols-2 gap-3 sm:grid-cols-5">
    {% for stage, count in stages %}
      <div class="rounded-lg border border-stone-200 bg-white p-3">
        <div class="text-2xl font-semibold tabular-nums">{{ count }}</div>
        <div class="text-xs uppercase tracking-wide text-stone-500">{{ stage }}</div>
      </div>
    {% endfor %}
  </div>

  <h2 class="mt-8 mb-2 text-sm font-semibold">Weekly conversion</h2>
  {% if not weeks %}
    <p class="text-sm text-stone-500">No applications sent yet.</p>
  {% else %}
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="text-left text-xs uppercase tracking-wide text-stone-500">
          <tr>
            <th class="py-2">Week of</th><th>Applied</th><th>Responded</th>
            <th>Interviews</th><th>Response rate</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-stone-200">
          {% for week in weeks %}
            <tr>
              <td class="py-2">{{ week.week.strftime('%b %-d') }}</td>
              <td class="tabular-nums">{{ week.applied }}</td>
              <td class="tabular-nums">{{ week.responded }}</td>
              <td class="tabular-nums">{{ week.interviewed }}</td>
              <td class="tabular-nums">{{ week.response_rate | pct }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <p class="mt-2 text-xs text-stone-500">
      Bucketed by the week the application was sent, not the week a reply arrived —
      replies land one to three weeks later.
    </p>
  {% endif %}
{% endblock %}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_web_tracker.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Commit**

```bash
git add web/tracker.py web/templates/tracker.html pipeline/store.py tests/test_web_tracker.py
git commit -m "feat: application tracker"
```

---

### Task 6: Settings

**Files:**
- Create: `web/settings.py`, `web/templates/settings.html`
- Modify: `pipeline/store.py`
- Test: `tests/test_web_settings.py`

**Interfaces:**
- Produces:
  - `web.settings.router` — `GET /settings`, `POST /settings/answer`, `POST /settings/sharia`
  - `await pipeline.store.upsert_answer(conn, question, answer, category) -> None`
  - `await pipeline.store.set_sharia_override(conn, company_id, verdict) -> None`
  - `await pipeline.store.recent_runs(conn, limit) -> list[dict]`
  - `await pipeline.store.unmapped_questions(conn) -> list[dict]`

Answer bank, Sharia overrides, target employer list, unmapped questions, and
the run log with duration and budget-hit flags (spec §12).

**A Sharia override written here sets `sharia_source='user'`, which Phase 2's
screen treats as permanent** — never re-billed, never re-evaluated. That is the
mechanism by which an LLM cannot silently make a religious ruling that stands
uncorrected.

- [ ] **Step 1: Write the failing test**

`tests/test_web_settings.py`:

```python
from fastapi.testclient import TestClient

from api.index import app
from tests.test_web_queue import _seed

client = TestClient(app)


async def test_settings_renders(db):
    assert client.get("/settings").status_code == 200


async def test_an_answer_can_be_added(db):
    response = client.post(
        "/settings/answer",
        data={
            "question": "Work authorization?",
            "answer": "US-born citizen",
            "category": "eligibility",
        },
    )
    assert response.status_code in (200, 303)
    cur = await db.execute(
        "SELECT answer FROM answer_bank WHERE question = %s", ("Work authorization?",)
    )
    assert (await cur.fetchone())["answer"] == "US-born citizen"


async def test_re_posting_a_question_updates_rather_than_erroring(db):
    # question is UNIQUE; a plain INSERT would 500 on the second edit.
    for answer in ("First", "Second"):
        client.post("/settings/answer", data={"question": "Q?", "answer": answer})
    cur = await db.execute("SELECT answer FROM answer_bank WHERE question = 'Q?'")
    assert (await cur.fetchone())["answer"] == "Second"


async def test_an_empty_answer_is_rejected(db):
    # An empty stored answer would be pasted into a real application form.
    response = client.post("/settings/answer", data={"question": "Q?", "answer": "   "})
    assert response.status_code == 400
    cur = await db.execute("SELECT count(*) AS n FROM answer_bank")
    assert (await cur.fetchone())["n"] == 0


async def test_a_sharia_override_is_marked_as_user_sourced(db):
    """Phase 2 treats sharia_source='user' as permanent — never re-billed,
    never re-evaluated. This is how an LLM ruling stays correctable."""
    job_id = await _seed(db, title="Fintech Job", sharia="flagged")
    cur = await db.execute("SELECT company_id FROM jobs WHERE job_id = %s", (job_id,))
    company_id = (await cur.fetchone())["company_id"]

    client.post("/settings/sharia", data={"company_id": company_id, "verdict": "allowed"})

    cur = await db.execute(
        "SELECT sharia_verdict, sharia_source FROM companies WHERE company_id = %s",
        (company_id,),
    )
    row = await cur.fetchone()
    assert row["sharia_verdict"] == "allowed"
    assert row["sharia_source"] == "user"


async def test_an_override_survives_a_later_screen(db):
    from pipeline.config import load_settings
    from pipeline.filters.sharia import screen_company

    job_id = await _seed(db, title="Casino Job", sharia="flagged")
    cur = await db.execute("SELECT company_id FROM jobs WHERE job_id = %s", (job_id,))
    company_id = (await cur.fetchone())["company_id"]

    client.post("/settings/sharia", data={"company_id": company_id, "verdict": "allowed"})

    async def never(conn, name, description, s):
        raise AssertionError("a user verdict must never be re-judged")

    verdict = await screen_company(
        db,
        company_id,
        "Casino Job",
        "Online betting.",
        load_settings(env={"DATABASE_URL": "postgresql://x/y"}),
        judge=never,
    )
    assert verdict == "allowed"


async def test_an_unknown_verdict_is_rejected(db):
    job_id = await _seed(db)
    cur = await db.execute("SELECT company_id FROM jobs WHERE job_id = %s", (job_id,))
    company_id = (await cur.fetchone())["company_id"]
    response = client.post(
        "/settings/sharia", data={"company_id": company_id, "verdict": "halal-ish"}
    )
    assert response.status_code == 400


async def test_the_run_log_shows_duration_and_budget_hit(db):
    await db.execute(
        "INSERT INTO run_log (started_at, finished_at, jobs_seen, duration_ms, budget_hit)"
        " VALUES (now(), now(), 2614, 41000, true)"
    )
    body = client.get("/settings").text
    assert "2614" in body or "2,614" in body
    assert "budget" in body.lower()


async def test_unmapped_questions_are_listed(db):
    await db.execute(
        "INSERT INTO unmapped_questions (question, seen_count) VALUES ('Desired start date?', 4)"
    )
    body = client.get("/settings").text
    assert "Desired start date?" in body


async def test_settings_mutations_reject_get(db):
    assert client.get("/settings/answer?question=Q&answer=A").status_code == 405
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_web_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.settings'`

- [ ] **Step 3: Add the store helpers**

```python
async def upsert_answer(
    conn: AsyncConnection, question: str, answer: str, category: str | None = None
) -> None:
    if not answer.strip():
        # An empty stored answer gets pasted into a real application form.
        raise ValueError("answer cannot be empty")
    await conn.execute(
        "INSERT INTO answer_bank (question, answer, category) VALUES (%s, %s, %s)"
        " ON CONFLICT (question) DO UPDATE SET answer = EXCLUDED.answer,"
        " category = EXCLUDED.category, updated_at = now()",
        (question.strip(), answer.strip(), category or None),
    )
    # The question is answered now, so it is no longer unmapped.
    await conn.execute("DELETE FROM unmapped_questions WHERE question = %s", (question.strip(),))


async def set_sharia_override(conn: AsyncConnection, company_id: int, verdict: str) -> None:
    """Record a human ruling.

    sharia_source='user' is permanent: Phase 2's screen returns it without
    re-billing or re-evaluating. This is the mechanism that keeps an LLM from
    silently making a religious ruling that stands uncorrected.
    """
    from pipeline.filters.sharia import VERDICTS

    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict: {verdict!r}")
    await conn.execute(
        "UPDATE companies SET sharia_verdict = %s, sharia_source = 'user',"
        " sharia_reason = 'Set manually.', sharia_decided_at = now()"
        " WHERE company_id = %s",
        (verdict, company_id),
    )


async def recent_runs(conn: AsyncConnection, limit: int = 20) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT run_id, started_at, finished_at, jobs_seen, jobs_new, jobs_filtered,"
        " errors, duration_ms, budget_hit, notes"
        " FROM run_log ORDER BY run_id DESC LIMIT %s",
        (limit,),
    )


async def unmapped_questions(conn: AsyncConnection) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT question, seen_count, last_seen FROM unmapped_questions"
        " ORDER BY seen_count DESC, last_seen DESC",
        (),
    )


async def flagged_companies(conn: AsyncConnection) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT company_id, name, sharia_verdict, sharia_sector, sharia_reason, sharia_source"
        " FROM companies WHERE sharia_verdict IN ('flagged', 'excluded')"
        " ORDER BY sharia_verdict, name",
        (),
    )
```

- [ ] **Step 4: Implement `web/settings.py`**

```python
"""GET /settings and its mutations (spec section 12).

Answer bank, Sharia overrides, unmapped questions, and the run log.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pipeline.db import connection
from pipeline.store import (
    answer_bank_all,
    flagged_companies,
    recent_runs,
    set_sharia_override,
    unmapped_questions,
    upsert_answer,
)
from web import templates_env

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    async with connection() as conn:
        context = {
            "answers": await answer_bank_all(conn),
            "unmapped": await unmapped_questions(conn),
            "companies": await flagged_companies(conn),
            "runs": await recent_runs(conn, limit=20),
        }
    return templates_env.TemplateResponse(request=request, name="settings.html", context=context)


@router.post("/settings/answer")
async def save_answer(
    question: Annotated[str, Form()],
    answer: Annotated[str, Form()],
    category: Annotated[str, Form()] = "",
) -> RedirectResponse:
    async with connection() as conn:
        try:
            await upsert_answer(conn, question, answer, category)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 303 so a refresh does not re-post the form.
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/sharia")
async def save_override(
    company_id: Annotated[int, Form()], verdict: Annotated[str, Form()]
) -> RedirectResponse:
    async with connection() as conn:
        try:
            await set_sharia_override(conn, company_id, verdict)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/settings", status_code=303)
```

- [ ] **Step 5: Write `web/templates/settings.html`**

```html
{% extends "base.html" %}
{% block title %}Settings — job_hunt{% endblock %}
{% block content %}
  <h1 class="mb-4 text-lg font-semibold">Settings</h1>

  <section class="rounded-lg border border-stone-200 bg-white p-4">
    <h2 class="text-sm font-semibold">Answer bank</h2>
    <form method="post" action="/settings/answer" class="mt-3 grid gap-2 sm:grid-cols-[2fr_2fr_1fr_auto]">
      <input name="question" placeholder="Question" required
             class="rounded border border-stone-300 px-2 py-1 text-sm">
      <input name="answer" placeholder="Answer" required
             class="rounded border border-stone-300 px-2 py-1 text-sm">
      <input name="category" placeholder="Category"
             class="rounded border border-stone-300 px-2 py-1 text-sm">
      <button class="rounded bg-stone-900 px-3 py-1 text-sm text-white">Save</button>
    </form>
    <dl class="mt-4 space-y-2">
      {% for entry in answers %}
        <div class="text-sm">
          <dt class="font-medium">{{ entry.question }}</dt>
          <dd class="text-stone-700">{{ entry.answer }}</dd>
        </div>
      {% else %}
        <p class="text-sm text-stone-500">Empty. It fills up as you hit real forms.</p>
      {% endfor %}
    </dl>
  </section>

  {% if unmapped %}
    <section class="mt-4 rounded-lg border border-stone-200 bg-white p-4">
      <h2 class="text-sm font-semibold">Questions with no stored answer</h2>
      <ul class="mt-2 space-y-1 text-sm">
        {% for entry in unmapped %}
          <li>{{ entry.question }} <span class="text-stone-500">×{{ entry.seen_count }}</span></li>
        {% endfor %}
      </ul>
    </section>
  {% endif %}

  <section class="mt-4 rounded-lg border border-stone-200 bg-white p-4">
    <h2 class="text-sm font-semibold">Sharia screen</h2>
    <p class="mt-1 text-xs text-stone-500">
      Your ruling is permanent — it is never re-judged or re-billed.
    </p>
    <div class="mt-3 space-y-2">
      {% for company in companies %}
        <form method="post" action="/settings/sharia"
              class="flex flex-wrap items-center gap-2 text-sm">
          <input type="hidden" name="company_id" value="{{ company.company_id }}">
          <span class="min-w-40 font-medium">{{ company.name }}</span>
          <span class="rounded bg-stone-100 px-2 py-0.5 text-xs">
            {{ company.sharia_verdict }} ({{ company.sharia_source }})
          </span>
          <span class="flex-1 text-xs text-stone-600">{{ company.sharia_reason }}</span>
          <select name="verdict" class="rounded border border-stone-300 px-2 py-1 text-xs">
            <option value="allowed">allowed</option>
            <option value="excluded">excluded</option>
          </select>
          <button class="rounded border border-stone-300 px-2 py-1 text-xs">Set</button>
        </form>
      {% else %}
        <p class="text-sm text-stone-500">Nothing flagged or excluded yet.</p>
      {% endfor %}
    </div>
  </section>

  <section class="mt-4 rounded-lg border border-stone-200 bg-white p-4">
    <h2 class="text-sm font-semibold">Run log</h2>
    <div class="mt-2 overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="text-left text-xs uppercase tracking-wide text-stone-500">
          <tr><th class="py-1">Started</th><th>Seen</th><th>New</th><th>Errors</th>
              <th>Duration</th><th>Budget</th></tr>
        </thead>
        <tbody class="divide-y divide-stone-200">
          {% for run in runs %}
            <tr>
              <td class="py-1">{{ run.started_at | ago }}</td>
              <td class="tabular-nums">{{ run.jobs_seen }}</td>
              <td class="tabular-nums">{{ run.jobs_new }}</td>
              <td class="tabular-nums {% if run.errors %}text-red-700{% endif %}">{{ run.errors }}</td>
              <td class="tabular-nums">{{ (run.duration_ms or 0) // 1000 }}s</td>
              <td>{% if run.budget_hit %}<span class="text-amber-700">budget hit</span>{% endif %}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </section>
{% endblock %}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_web_settings.py -v`
Expected: PASS (10 tests)

- [ ] **Step 7: Commit**

```bash
git add web/settings.py web/templates/settings.html pipeline/store.py tests/test_web_settings.py
git commit -m "feat: settings — answer bank, Sharia overrides, run log"
```

---

### Task 7: Access control and deployment

**Files:**
- Modify: `vercel.json`, `docs/environment.md`
- Test: manual, against the deployed function

Until now the deployed function returned JSON stats. From this task on it
renders a home address, a salary floor, work-authorization answers, and a list
of every job Jarra is considering. **Access control has to be verified before
that first deploy, not after.**

There is no application-level login and none is being added: Vercel
Authentication gates the deployment at the edge using the Vercel account that
already owns the project. Adding a second password would mean storing a hash,
handling sessions, and getting cookie flags right — all to protect a
single-user app that already sits behind SSO.

- [ ] **Step 1: Confirm deployment protection is on before deploying**

```bash
vercel project ls
# In the dashboard: Settings → Deployment Protection.
# Vercel Authentication must be "Standard Protection" (all deployments,
# including the production domain). Pro plan required for production-domain
# protection — Hobby leaves the production URL public.
```

If protection is off, turn it on **before** step 3. A public production URL
serving this UI publishes personal data to anyone who finds the hostname.

- [ ] **Step 2: Confirm the cron routes still work behind protection**

Deployment protection blocks browsers, not Vercel's own scheduler — cron
invocations are internal and bypass it. Confirm rather than assume, because
the failure is silent: the UI would work and discovery would quietly stop.

```bash
vercel crons ls
# After the next tick:
vercel logs --since 15m | grep -i "discover\|score"
```

- [ ] **Step 3: Deploy and walk every route**

```bash
./scripts/build_css.sh
git status --short web/static/app.css   # must be clean — CI enforces this too
vercel build --prod && vercel deploy --prebuilt --prod
```

Then open each route in a browser and confirm:

- `/` — cards render, ordering is by score, no excluded companies
- `/job/{id}` — description renders, apply link opens a new tab, status buttons
  swap in place without a page reload
- `/tracker` — funnel counts match `SELECT status, count(*) FROM applications`
- `/settings` — the run log shows real runs with real durations

- [ ] **Step 4: Verify the mutation path end to end**

Mark a job applied in the browser, then confirm the write landed in Neon
production rather than only in the rendered HTML:

```sql
SELECT job_id, status, applied_at FROM applications ORDER BY updated_at DESC LIMIT 5;
```

- [ ] **Step 5: Document the access model**

Add a section to `docs/environment.md` recording that the UI has no application
login, that Vercel Authentication is the only gate, and that turning off
Deployment Protection publishes personal data. This is the kind of setting
that gets toggled during unrelated debugging months later.

- [ ] **Step 6: Commit**

```bash
git add vercel.json docs/environment.md
git commit -m "docs: access model for the web UI"
```

---

## Done criteria

Phase 3 is done when:

- All four routes render against production data.
- Triaging the top twenty takes under ten minutes. This is the actual goal; if
  a card needs a click to be judged, the wrong fields are on it.
- Marking a job applied is one click and survives the next discovery pass.
- The tracker's funnel counts reconcile with a direct `SELECT` against
  `applications`.
- Deployment Protection is confirmed on, and the crons still run behind it.
- `./scripts/build_css.sh` produces no diff — CI enforces this.

## What Phase 3 deliberately leaves out

- **Document generation and PDF preview** — Phase 4 (spec §10). The detail page
  gains a document panel then, rather than showing an empty box now.
- **The digest email** — Phase 4 (spec §10.3, `aiosmtplib` + Gmail App Password).
- **Auto-fill of application forms** — never. Spec §3: no automated process
  authenticates as Jarra on any job platform. The answer bank is
  copy-to-clipboard by design.
