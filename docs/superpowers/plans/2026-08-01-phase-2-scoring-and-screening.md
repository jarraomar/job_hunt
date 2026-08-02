# Phase 2: Scoring, Judging, and the Sharia Screen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the ~800 live jobs Phase 1 ingests each day into a ranked shortlist with a human-readable rationale, spending under $0.20/day to do it.

**Architecture:** Four gates, cheapest first (spec §8). The deterministic pre-filter from Phase 1 is gate 1. Gate 2 is a local `fastembed` cosine against the résumé vector — $0, ~450 docs/sec, and the gate that keeps Haiku off 800 jobs a day. Gate 3 is the Sharia business-activity screen, cached per company forever. Gate 4 is a Haiku 4.5 relevance judgement with a structured output, applied only to the top N by combined score.

**Tech Stack:** `fastembed` (ONNX, `bge-small-en-v1.5`), `anthropic` SDK with `messages.parse()` + Pydantic, PostgreSQL, existing Phase 1 pipeline.

## Global Constraints

Everything in the Phase 1 plan's Global Constraints still applies. Additionally:

- **`claude-haiku-4-5` and `claude-sonnet-5` are the only model IDs used.** Exact strings, no date suffixes.
- **`effort` / `output_config` must not be sent to Haiku 4.5** — it errors on that model. Effort is an Opus/Sonnet-tier parameter.
- **Thinking stays off for classification.** On Haiku 4.5 that means omitting the `thinking` parameter entirely (older models default to no thinking); do not send `{"type": "adaptive"}`, which is a 4.6+ shape.
- **Haiku 4.5's minimum cacheable prefix is 4096 tokens.** Below it, caching silently does nothing — `cache_creation_input_tokens` is 0 with no error. Assert, never assume.
- **`llm_spend` daily cap is enforced in the client wrapper**, not at call sites. A call past the cap raises rather than proceeding.
- **`companies.sharia_source='user'` always wins and is never re-billed or re-evaluated** (spec §9).
- **Every write stays idempotent** — Vercel delivers duplicate crons.
- **No profile data is read from disk when `VERCEL` is set** — the Phase 1 guard in `load_targets` extends to all profile loading.

---

## What the measurements changed

Three numbers were taken before this plan was written. Each moved a design decision.

**`fastembed` fits in a Vercel function, comfortably.** Measured locally: 179 MB of dependencies, 65 MB of model, **0.7 s cold-start cost** (the ONNX model loads in 0.16 s — it is memory-mapped, not parsed), and **449 docs/sec**. Scoring 800 jobs takes about two seconds. Bundle lands near 292 MB against a 500 MB Python limit. The whole cheap-gate design depends on this and it holds.

**The static cache prefix is ~2,900 tokens — below Haiku's 4,096 minimum.** So prompt caching, the headline cost lever in spec §11, would *silently not fire*. Task 5 measures it with `count_tokens` and asserts. The fix is counterintuitive and worth stating plainly:

| Configuration | Cost per judgement |
|---|---|
| Haiku 4.5, prefix below the minimum (caching no-ops) | ~$0.0054 |
| Haiku 4.5, prefix deliberately grown past 4096 | **~$0.0029** |
| Sonnet 5, caching works at its 1024 minimum | ~$0.0056 |

**Making the prompt bigger makes it cheaper**, because the whole prefix then bills at 0.1× instead of 1×. Switching to Sonnet to get a lower cache minimum is a wash — its input rate cancels the saving.

**Judging is capped, not universal.** At ~800 live jobs/day, judging everything costs ~$4/day even at the cached rate. `JOBHUNT_DAILY_JUDGE_LIMIT` defaults to 50 — the top 50 by combined embedding + rules + freshness score. That is ~$0.15/day, or ~$4/month, matching spec §11's estimate.

---

## File Structure

| File | Responsibility |
|---|---|
| `migrations/003_scoring.sql` | `scores` table; judge and Sharia columns |
| `profile.example/resume.json` | Committed template — structure only, no personal data |
| `profile.example/competency_bullets.yaml` | Committed template |
| `pipeline/profile.py` | Load profile from `PROFILE_JSON` or disk; refuse disk on Vercel |
| `pipeline/embed.py` | `fastembed` wrapper, résumé vector, cosine similarity |
| `pipeline/score.py` | Deterministic components + blend into `total_score` |
| `pipeline/llm.py` | Anthropic client, spend ceiling, cache assertion, structured outputs |
| `pipeline/filters/sharia.py` | Three-tier business-activity screen |
| `pipeline/judge.py` | Haiku relevance verdict + rationale |
| `pipeline/run_score.py` | Orchestration; callable from CLI and cron |
| `scripts/vendor_model.py` | Download the ONNX model into the build at deploy time |
| `tests/` | Mirrors the package layout |

---

### Task 1: Profile loading

**Files:**
- Create: `pipeline/profile.py`, `profile.example/resume.json`, `profile.example/competency_bullets.yaml`
- Test: `tests/test_profile.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `pipeline.profile.Profile` — frozen dataclass: `resume: dict`, `competency_bullets: list[dict]`, `identity: dict`
  - `pipeline.profile.load_profile(settings: Settings) -> Profile`
  - `pipeline.profile.ProfileUnavailableError(Exception)`
  - `pipeline.profile.resume_text(profile: Profile) -> str` — the flattened string that gets embedded

The résumé is the embedding target and the judge's context, so this has to exist before either. It follows the same rule Phase 1 established for `targets.yaml`: **`PROFILE_JSON` wins, and disk is never read when `VERCEL` is set.** That directory holds a home address, phone number, work-authorization answers, and a salary floor, and a Vercel build uploads the source tree.

- [ ] **Step 1: Write the committed templates**

`profile.example/resume.json` — structure only, no personal data:

```json
{
  "summary": "One-paragraph professional summary. Rewritten per role by Haiku; every claim must already be true here.",
  "skills": {
    "languages": ["Python", "TypeScript", "JavaScript", "C++"],
    "frontend": ["React", "Next.js", "Tailwind"],
    "backend": ["FastAPI", "Node.js", "PostgreSQL"],
    "cloud": ["AWS", "Docker", "GitHub Actions", "Terraform"],
    "ai": ["LLM integration", "prompt engineering", "RAG"]
  },
  "experience": [
    {
      "company": "Example Co",
      "title": "Software Engineer",
      "start": "2023-06",
      "end": null,
      "bullets": [
        "Shipped X, reducing Y from A to B.",
        "Built Z used by N customers."
      ]
    }
  ],
  "education": [
    {"school": "Example University", "degree": "BS Computer Engineering", "year": "2023"}
  ]
}
```

`profile.example/competency_bullets.yaml`:

```yaml
# The pool Sonnet selects four from when filling the cover letter (spec 10.1).
# Every technology and metric here must be true and evidenced in resume.json —
# the model may reword and reorder, never invent.
- label: Full-Stack Development
  text: >-
    Built and shipped end-to-end features across React/Next.js frontends and
    Python/FastAPI backends, owning schema design through deployment.
- label: AI and Automation
  text: >-
    Integrated LLM-backed workflows into production systems, cutting a
    five-hour manual process to four minutes.
- label: Measurable Product Impact
  text: >-
    Delivered work across 30 client engagements, with scope from prototype to
    production infrastructure.
- label: Cloud and DevOps
  text: >-
    Deployed and operated services on AWS with Docker and GitHub Actions CI/CD.
```

- [ ] **Step 2: Write the failing test**

`tests/test_profile.py`:

```python
import json

import pytest

from pipeline.config import load_settings
from pipeline.profile import (
    ProfileUnavailableError,
    load_profile,
    resume_text,
)

MINIMAL = {
    "resume": {
        "summary": "Engineer.",
        "skills": {"languages": ["Python"]},
        "experience": [{"company": "Acme", "title": "SWE", "bullets": ["Shipped X."]}],
        "education": [],
    },
    "competency_bullets": [{"label": "Full-Stack Development", "text": "Built things."}],
    "identity": {"email": "someone@example.com"},
}


def settings(**env):
    return load_settings(env={"DATABASE_URL": "postgresql://x/y", **env})


def test_loads_from_env_var():
    profile = load_profile(settings(PROFILE_JSON=json.dumps(MINIMAL)))
    assert profile.resume["summary"] == "Engineer."
    assert profile.competency_bullets[0]["label"] == "Full-Stack Development"


def test_env_var_wins_over_disk(tmp_path):
    (tmp_path / "resume.json").write_text(json.dumps({"summary": "from disk"}))
    profile = load_profile(
        settings(PROFILE_JSON=json.dumps(MINIMAL), JOBHUNT_PROFILE_DIR=str(tmp_path))
    )
    assert profile.resume["summary"] == "Engineer."


def test_disk_is_never_read_on_vercel(tmp_path, monkeypatch):
    """profile/ ships with a Vercel build and excludeFiles does not stop it.

    Confirmed in production during Phase 0. This directory holds a home
    address, phone number, work-authorization answers and a salary floor, so
    the deployed code must refuse to read it even when it is present.
    """
    (tmp_path / "resume.json").write_text(json.dumps({"summary": "secret"}))
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(ProfileUnavailableError, match="PROFILE_JSON"):
        load_profile(settings(JOBHUNT_PROFILE_DIR=str(tmp_path)))


def test_reads_from_disk_locally(tmp_path, monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    (tmp_path / "resume.json").write_text(json.dumps(MINIMAL["resume"]))
    (tmp_path / "competency_bullets.yaml").write_text(
        "- label: Full-Stack Development\n  text: Built things.\n"
    )
    profile = load_profile(settings(JOBHUNT_PROFILE_DIR=str(tmp_path)))
    assert profile.resume["summary"] == "Engineer."
    assert profile.competency_bullets[0]["label"] == "Full-Stack Development"


def test_missing_profile_raises_rather_than_defaulting(tmp_path, monkeypatch):
    # A silent empty profile would embed the empty string and score every job
    # identically — a failure that looks like "scoring is broken" much later.
    monkeypatch.delenv("VERCEL", raising=False)
    with pytest.raises(ProfileUnavailableError):
        load_profile(settings(JOBHUNT_PROFILE_DIR=str(tmp_path)))


def test_malformed_profile_json_raises():
    with pytest.raises(ProfileUnavailableError, match="not valid JSON"):
        load_profile(settings(PROFILE_JSON="{not json"))


def test_resume_text_includes_summary_skills_and_bullets():
    profile = load_profile(settings(PROFILE_JSON=json.dumps(MINIMAL)))
    text = resume_text(profile)
    assert "Engineer." in text
    assert "Python" in text
    assert "Shipped X." in text


def test_resume_text_is_deterministic():
    # The embedding is cached against this string; a set-ordering wobble would
    # silently invalidate it on every process start.
    profile = load_profile(settings(PROFILE_JSON=json.dumps(MINIMAL)))
    assert resume_text(profile) == resume_text(profile)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_profile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.profile'`

- [ ] **Step 4: Add `PROFILE_JSON` to Settings**

In `pipeline/config.py`, add the field to `Settings` and populate it in `load_settings`:

```python
    profile_json: str | None
```

```python
profile_json = (e.get("PROFILE_JSON") or None,)
```

- [ ] **Step 5: Implement `pipeline/profile.py`**

```python
"""Personal profile: the resume, the competency-bullet pool, and identity.

Loading follows the rule Phase 1 established for targets.yaml, for the same
reason and with higher stakes. `PROFILE_JSON` wins, and **disk is never read
when VERCEL is set** -- profile/ ships with a Vercel build (excludeFiles governs
the function bundle; the source tree uploads separately), and this directory
holds a home address, phone number, work-authorization answers and a salary
floor.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import yaml

from pipeline.config import Settings

log = logging.getLogger(__name__)


class ProfileUnavailableError(Exception):
    """No usable profile. Never fall back to an empty one."""


@dataclass(frozen=True)
class Profile:
    resume: dict[str, Any]
    competency_bullets: list[dict[str, str]]
    identity: dict[str, Any]


def load_profile(settings: Settings) -> Profile:
    if settings.profile_json:
        try:
            data = json.loads(settings.profile_json)
        except json.JSONDecodeError as exc:
            raise ProfileUnavailableError(f"PROFILE_JSON is not valid JSON: {exc}") from exc
        return _from_dict(data)

    if os.environ.get("VERCEL"):
        raise ProfileUnavailableError(
            "running on Vercel with no PROFILE_JSON; refusing to read the profile "
            "directory so personal data cannot leak via the build directory"
        )

    return _from_disk(settings)


def _from_dict(data: dict[str, Any]) -> Profile:
    resume = data.get("resume") or {}
    if not resume:
        raise ProfileUnavailableError("profile has no resume")
    return Profile(
        resume=resume,
        competency_bullets=list(data.get("competency_bullets") or []),
        identity=dict(data.get("identity") or {}),
    )


def _from_disk(settings: Settings) -> Profile:
    resume_path = settings.profile_dir / "resume.json"
    if not resume_path.exists():
        raise ProfileUnavailableError(
            f"no PROFILE_JSON and no {resume_path}. Copy profile.example/ into "
            f"{settings.profile_dir} and fill it in."
        )
    resume = json.loads(resume_path.read_text())

    bullets_path = settings.profile_dir / "competency_bullets.yaml"
    bullets = yaml.safe_load(bullets_path.read_text()) if bullets_path.exists() else []

    identity_path = settings.profile_dir / "identity.yaml"
    identity = yaml.safe_load(identity_path.read_text()) if identity_path.exists() else {}

    return Profile(resume=resume, competency_bullets=list(bullets or []), identity=identity or {})


def resume_text(profile: Profile) -> str:
    """Flatten the resume into the string that gets embedded.

    Deterministic by construction: dict iteration order is insertion order in
    Python 3.7+, and nothing here iterates a set. The embedding is cached
    against this exact string, so a reordering would silently invalidate it on
    every process start.
    """
    resume = profile.resume
    parts: list[str] = []

    if summary := resume.get("summary"):
        parts.append(str(summary))

    for group, items in (resume.get("skills") or {}).items():
        parts.append(f"{group}: {', '.join(items)}")

    for role in resume.get("experience") or []:
        header = " ".join(str(role.get(k, "")) for k in ("title", "company")).strip()
        parts.append(header)
        parts.extend(str(b) for b in role.get("bullets") or [])

    for school in resume.get("education") or []:
        parts.append(" ".join(str(school.get(k, "")) for k in ("degree", "school")).strip())

    return "\n".join(p for p in parts if p)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_profile.py -v`
Expected: PASS (8 tests)

- [ ] **Step 7: Commit**

```bash
git add pipeline/profile.py pipeline/config.py profile.example/ tests/test_profile.py
git commit -m "feat: profile loading, refused from disk on Vercel"
```

---

### Task 2: Scoring schema

**Files:**
- Create: `migrations/003_scoring.sql`
- Modify: `tests/conftest.py`
- Test: `tests/test_scoring_schema.py`

**Interfaces:**
- Produces: the `scores` table and the judge/Sharia columns every later task writes to.

`companies` already carries the `sharia_*` columns from migration 001 — this adds only what scoring needs.

- [ ] **Step 1: Write the migration**

`migrations/003_scoring.sql`:

```sql
-- Phase 2: scoring, judging, and the Sharia screen.

CREATE TABLE scores (
  job_id            BIGINT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
  embed_similarity  DOUBLE PRECISION,
  rule_score        DOUBLE PRECISION,
  freshness_score   DOUBLE PRECISION,
  total_score       DOUBLE PRECISION NOT NULL,
  relevance_verdict TEXT,
  rationale         TEXT,
  is_stretch        BOOLEAN NOT NULL DEFAULT FALSE,
  model             TEXT,
  judged_at         TIMESTAMPTZ,
  scored_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- A verdict without a model is a row we cannot attribute or re-bill
  -- correctly later; a model without a verdict means a judge call was paid
  -- for and thrown away.
  CONSTRAINT verdict_and_model_together
    CHECK ((relevance_verdict IS NULL) = (model IS NULL))
);

-- The queue reads this every page load: highest first, unjudged included.
CREATE INDEX idx_scores_rank ON scores(total_score DESC);
-- Finding what still needs a judge call is the hot path in the scoring cron.
CREATE INDEX idx_scores_unjudged ON scores(total_score DESC) WHERE judged_at IS NULL;

-- Per-call spend, so the daily ceiling is enforced against recorded fact
-- rather than an in-process counter that resets on every cold start.
CREATE TABLE llm_spend (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  day                 DATE NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
  model               TEXT NOT NULL,
  purpose             TEXT NOT NULL,
  input_tokens        INTEGER NOT NULL,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens       INTEGER NOT NULL,
  cost_usd            NUMERIC(10,6) NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_spend_day ON llm_spend(day);
```

Add `scores` and `llm_spend` to `_TABLES` in `tests/conftest.py`.

- [ ] **Step 2: Write the failing test**

`tests/test_scoring_schema.py`:

```python
import psycopg
import pytest


async def _job(db) -> int:
    await db.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    cur = await db.execute(
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at)"
        " VALUES ('fp', 1, 'greenhouse', '1', 'Engineer', 'd', 'https://x', now(), now())"
        " RETURNING job_id"
    )
    return (await cur.fetchone())["job_id"]


async def test_tables_exist(db):
    cur = await db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    names = {r["tablename"] for r in await cur.fetchall()}
    assert {"scores", "llm_spend"} <= names


async def test_scores_row_requires_only_a_total(db):
    job_id = await _job(db)
    await db.execute("INSERT INTO scores (job_id, total_score) VALUES (%s, 0.5)", (job_id,))
    cur = await db.execute("SELECT total_score, judged_at FROM scores")
    row = await cur.fetchone()
    assert row["total_score"] == 0.5
    assert row["judged_at"] is None


async def test_verdict_without_model_is_rejected(db):
    """A verdict we cannot attribute to a model is unusable for re-billing."""
    job_id = await _job(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(
            "INSERT INTO scores (job_id, total_score, relevance_verdict, model)"
            " VALUES (%s, 0.5, 'strong', NULL)",
            (job_id,),
        )


async def test_model_without_verdict_is_rejected(db):
    # A paid-for call whose result was dropped.
    job_id = await _job(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(
            "INSERT INTO scores (job_id, total_score, relevance_verdict, model)"
            " VALUES (%s, 0.5, NULL, 'claude-haiku-4-5')",
            (job_id,),
        )


async def test_scores_cascade_when_a_job_is_deleted(db):
    job_id = await _job(db)
    await db.execute("INSERT INTO scores (job_id, total_score) VALUES (%s, 0.5)", (job_id,))
    await db.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
    cur = await db.execute("SELECT count(*) AS n FROM scores")
    assert (await cur.fetchone())["n"] == 0


async def test_llm_spend_uses_exact_numeric_for_money(db):
    await db.execute(
        "INSERT INTO llm_spend (model, purpose, input_tokens, output_tokens, cost_usd)"
        " VALUES ('claude-haiku-4-5', 'judge', 1000, 200, 0.001234)"
    )
    cur = await db.execute("SELECT cost_usd FROM llm_spend")
    # NUMERIC, not float: a daily ceiling compared against accumulated float
    # error is a ceiling that drifts.
    from decimal import Decimal

    assert (await cur.fetchone())["cost_usd"] == Decimal("0.001234")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_scoring_schema.py -v`
Expected: FAIL — `relation "scores" does not exist`

- [ ] **Step 4: Apply and verify**

```bash
./scripts/dev_db.sh up
DATABASE_URL="postgresql://jobhunt@localhost:5433/jobhunt_test" ./venv/bin/python scripts/migrate.py
./venv/bin/pytest tests/test_scoring_schema.py -v
```

Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add migrations/003_scoring.sql tests/conftest.py tests/test_scoring_schema.py
git commit -m "feat: scoring schema"
```

---

### Task 3: Local embeddings

**Files:**
- Create: `pipeline/embed.py`
- Modify: `pyproject.toml`
- Test: `tests/test_embed.py`

**Interfaces:**
- Consumes: `Profile`, `resume_text`
- Produces:
  - `pipeline.embed.Embedder` — `embed(texts: list[str]) -> list[list[float]]`, `embed_one(text: str) -> list[float]`
  - `pipeline.embed.cosine(a: list[float], b: list[float]) -> float`
  - `pipeline.embed.get_embedder() -> Embedder` — process-wide singleton
  - `pipeline.embed.MODEL_NAME`, `pipeline.embed.DIMENSIONS`

This is gate 2 and the reason the whole design is affordable. **Measured before writing this task:** 0.7 s cold start (0.16 s model load — ONNX is memory-mapped, not parsed), 449 docs/sec, 65 MB model, ~292 MB total bundle against a 500 MB limit.

The embedder is a process-wide singleton for the same reason the psycopg pool is: a serverless instance serves many invocations, and re-loading the model per request would pay that cost every time instead of once.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to `dependencies`:

```toml
    "fastembed>=0.4",
```

- [ ] **Step 2: Write the failing test**

`tests/test_embed.py`:

```python
import math

import pytest

from pipeline.embed import DIMENSIONS, Embedder, cosine, get_embedder


@pytest.fixture(scope="module")
def embedder():
    return Embedder()


def test_embeds_to_the_expected_dimension(embedder):
    vec = embedder.embed_one("Senior Software Engineer, Python and React")
    assert len(vec) == DIMENSIONS


def test_embedding_is_deterministic(embedder):
    a = embedder.embed_one("Backend Engineer")
    b = embedder.embed_one("Backend Engineer")
    assert a == b


def test_related_text_scores_higher_than_unrelated(embedder):
    """The property the whole cheap gate rests on.

    If this does not hold, the embedding gate is not separating anything and
    every downstream cost estimate is wrong.
    """
    resume = "Full-stack engineer. Python, FastAPI, React, AWS, Docker, PostgreSQL."
    close = embedder.embed_one("Senior Full Stack Engineer — Python, React, AWS")
    far = embedder.embed_one("Registered Nurse, ICU night shift, BLS certification")
    base = embedder.embed_one(resume)
    assert cosine(base, close) > cosine(base, far)


def test_cosine_of_identical_vectors_is_one(embedder):
    vec = embedder.embed_one("anything")
    assert cosine(vec, vec) == pytest.approx(1.0, abs=1e-6)


def test_cosine_handles_a_zero_vector_without_dividing_by_zero():
    # An empty description would otherwise raise mid-run and lose the batch.
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_is_bounded():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert -1.0 <= cosine(a, b) <= 1.0
    assert cosine(a, b) == pytest.approx(-1.0)


def test_batch_embedding_matches_single(embedder):
    texts = ["Backend Engineer", "Frontend Engineer"]
    batch = embedder.embed(texts)
    assert len(batch) == 2
    assert batch[0] == embedder.embed_one(texts[0])


def test_empty_text_does_not_raise(embedder):
    vec = embedder.embed_one("")
    assert len(vec) == DIMENSIONS
    assert not any(math.isnan(x) for x in vec)


def test_get_embedder_returns_a_singleton():
    # A serverless instance serves many invocations; re-loading the model per
    # request would pay the load cost every time instead of once.
    assert get_embedder() is get_embedder()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_embed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.embed'`

- [ ] **Step 4: Implement `pipeline/embed.py`**

```python
"""Local embeddings. Gate 2 of the scoring pipeline (spec section 8).

This is what makes the design affordable: it costs nothing, needs no API key,
and cuts ~800 daily jobs down to the few dozen worth paying a model to judge.
Judging everything instead would run about $4/day.

Measured on the dev machine: 0.7s cold-start cost, 449 docs/sec, 65 MB model.
The ONNX model is memory-mapped rather than parsed, so the load itself is
0.16s -- almost all of the cold start is importing the library.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384

# Vendored into the bundle at build time (scripts/vendor_model.py) so a cold
# start never downloads 65 MB into an ephemeral /tmp.
_DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "model_cache"

_embedder: Embedder | None = None


class Embedder:
    """Thin wrapper over fastembed. Constructed once per process."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        # Imported lazily: the web routes that never score should not pay the
        # import cost, and tests that do not embed should not need the model.
        from fastembed import TextEmbedding

        resolved = cache_dir or Path(os.environ.get("JOBHUNT_MODEL_CACHE", _DEFAULT_CACHE))
        self._model = TextEmbedding(MODEL_NAME, cache_dir=str(resolved))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def get_embedder() -> Embedder:
    """The process-wide embedder, created on first use."""
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, returning 0.0 rather than raising on a zero vector.

    An empty job description embeds to something near zero; raising here would
    lose the whole batch for one bad row.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
./venv/bin/pip install -e ".[dev]"
./venv/bin/pytest tests/test_embed.py -v
```

Expected: PASS (9 tests). The first run downloads the model (~7 s); later runs load from cache in 0.16 s.

- [ ] **Step 6: Commit**

```bash
git add pipeline/embed.py pyproject.toml tests/test_embed.py
git commit -m "feat: local embeddings via fastembed"
```

---

### Task 4: Deterministic scoring and the blend

**Files:**
- Create: `pipeline/score.py`
- Test: `tests/test_score.py`

**Interfaces:**
- Consumes: `Job`, `Settings`, `Profile`, `pipeline.embed`
- Produces:
  - `pipeline.score.ScoreComponents` — frozen dataclass: `embed_similarity: float`, `rule_score: float`, `freshness_score: float`, `total_score: float`, `is_stretch: bool`
  - `pipeline.score.rule_score(job: Job, settings: Settings) -> float`
  - `pipeline.score.freshness_score(job: Job, *, now: datetime) -> float`
  - `pipeline.score.blend(embed_similarity, rule, freshness) -> float`
  - `pipeline.score.score_jobs(jobs, profile, settings, *, now) -> list[ScoreComponents]`
  - `pipeline.score.STRETCH_RATE`

Spec §8: ranking weights `total_score` and `posted_at` together, with a strong boost under 48 h, plus a 5–10% random stretch allowance for above-level roles.

**Freshness is a step function, not a decay curve.** Application timing is one of the strongest predictors of a response, and the difference between 6 h and 40 h old matters far more than between 20 and 25 days. A smooth exponential blurs exactly the distinction that matters.

**Unknown `posted_at` scores as mid-range, not zero.** HN comments and some Lever boards omit it; scoring those as stale would silently bury a whole source.

- [ ] **Step 1: Write the failing test**

`tests/test_score.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from pipeline.config import load_settings
from pipeline.models import Job
from pipeline.score import (
    STRETCH_RATE,
    blend,
    freshness_score,
    rule_score,
    score_jobs,
)

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y"})
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


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
        posted_at=NOW - timedelta(hours=6),
    )
    base.update(overrides)
    return Job(**base)


# --- freshness ---------------------------------------------------------------


def test_fresh_jobs_score_highest():
    assert freshness_score(make_job(posted_at=NOW - timedelta(hours=6)), now=NOW) == 1.0


def test_the_48_hour_cliff_is_sharp():
    """Application timing is a strong response predictor (spec section 8).

    A smooth decay would blur the one distinction that matters most.
    """
    just_inside = freshness_score(make_job(posted_at=NOW - timedelta(hours=47)), now=NOW)
    just_outside = freshness_score(make_job(posted_at=NOW - timedelta(hours=49)), now=NOW)
    assert just_inside == 1.0
    assert just_outside < 0.8


def test_old_jobs_score_low_but_not_zero():
    score = freshness_score(make_job(posted_at=NOW - timedelta(days=60)), now=NOW)
    assert 0.0 < score < 0.2


def test_unknown_posted_at_scores_mid_range():
    # HN and some Lever boards omit it. Scoring those as stale would bury an
    # entire source without anyone noticing.
    assert freshness_score(make_job(posted_at=None), now=NOW) == pytest.approx(0.5)


def test_a_future_posted_at_is_treated_as_fresh_not_negative():
    # Clock skew between a board and us should not produce a negative score.
    assert freshness_score(make_job(posted_at=NOW + timedelta(hours=2)), now=NOW) == 1.0


# --- rules -------------------------------------------------------------------


def test_remote_outranks_hybrid_outranks_onsite():
    remote = rule_score(make_job(remote_type="remote"), SETTINGS)
    hybrid = rule_score(make_job(remote_type="hybrid"), SETTINGS)
    onsite = rule_score(make_job(remote_type="onsite", location="Austin, TX"), SETTINGS)
    assert remote > hybrid > onsite


def test_bay_area_onsite_beats_distant_onsite():
    # Jarra is in San Leandro; a San Francisco onsite is commutable and a
    # Miami one is not.
    near = rule_score(make_job(remote_type="onsite", location="San Francisco, CA"), SETTINGS)
    far = rule_score(make_job(remote_type="onsite", location="Miami, FL"), SETTINGS)
    assert near > far


def test_salary_above_target_scores_higher_than_at_floor():
    high = rule_score(
        make_job(salary_min=180_000, salary_max=220_000, salary_source="structured"), SETTINGS
    )
    low = rule_score(
        make_job(salary_min=125_000, salary_max=135_000, salary_source="structured"), SETTINGS
    )
    assert high > low


def test_unknown_salary_scores_between_floor_and_target():
    """Two thirds of intake has no parseable figure.

    Scoring unknown as zero would rank every Greenhouse posting below every
    Ashby one purely on disclosure, not on fit.
    """
    unknown = rule_score(make_job(salary_source="none"), SETTINGS)
    at_floor = rule_score(
        make_job(salary_min=125_000, salary_max=125_000, salary_source="structured"), SETTINGS
    )
    at_target = rule_score(
        make_job(salary_min=150_000, salary_max=160_000, salary_source="structured"), SETTINGS
    )
    assert at_floor < unknown < at_target


def test_rule_score_is_bounded():
    for job in (make_job(), make_job(remote_type="onsite", location="Nowhere")):
        assert 0.0 <= rule_score(job, SETTINGS) <= 1.0


# --- blend -------------------------------------------------------------------


def test_blend_is_bounded_and_monotonic_in_similarity():
    low = blend(0.1, 0.5, 0.5)
    high = blend(0.9, 0.5, 0.5)
    assert 0.0 <= low <= 1.0 and 0.0 <= high <= 1.0
    assert high > low


def test_similarity_carries_more_weight_than_freshness():
    # A perfectly-matched month-old job should outrank a poorly-matched one
    # posted this morning.
    good_old = blend(0.9, 0.6, 0.1)
    bad_new = blend(0.2, 0.6, 1.0)
    assert good_old > bad_new


# --- end to end --------------------------------------------------------------


def test_score_jobs_returns_one_component_set_per_job(monkeypatch):
    from pipeline import profile as profile_mod

    prof = profile_mod.Profile(
        resume={"summary": "Full-stack engineer. Python, React, AWS."},
        competency_bullets=[],
        identity={},
    )
    jobs = [make_job(source_job_id="1"), make_job(source_job_id="2", title="Backend Engineer")]
    results = score_jobs(jobs, prof, SETTINGS, now=NOW)
    assert len(results) == 2
    for r in results:
        assert 0.0 <= r.total_score <= 1.0


def test_stretch_rate_is_within_the_specified_band():
    # Spec section 8 calls for a 5-10% random stretch allowance.
    assert 0.05 <= STRETCH_RATE <= 0.10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_score.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.score'`

- [ ] **Step 3: Implement `pipeline/score.py`**

```python
"""Gates 2 and the ranking blend (spec section 8).

Three components, combined into one total:

- **embed_similarity** -- cosine of the job description against the resume.
  Free, local, and the component that actually separates roles by fit.
- **rule_score** -- deterministic preferences: work arrangement, proximity to
  San Leandro, salary band.
- **freshness_score** -- a step function, not a decay curve. Application timing
  is one of the strongest predictors of a response, and the gap between 6 and
  40 hours matters far more than the gap between 20 and 25 days; an exponential
  blurs exactly the distinction worth keeping.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from pipeline.config import Settings
from pipeline.embed import cosine, get_embedder
from pipeline.models import Job
from pipeline.profile import Profile, resume_text

# Similarity dominates: fit is what we are ranking on. Freshness breaks ties
# among comparable jobs rather than promoting a poor match posted this morning.
WEIGHT_SIMILARITY = 0.55
WEIGHT_RULES = 0.25
WEIGHT_FRESHNESS = 0.20

# Spec section 8: a small random slice of above-level roles and adjacent
# pivots is let through, so the ranking cannot quietly become a filter bubble.
STRETCH_RATE = 0.07

# Bay Area cities within a commute of San Leandro. Onsite roles outside this
# set are not disqualified -- the pre-filter already passed them -- but they
# rank below ones that are.
_NEARBY = frozenset(
    {
        "san leandro",
        "oakland",
        "san francisco",
        "berkeley",
        "emeryville",
        "alameda",
        "hayward",
        "fremont",
        "san mateo",
        "palo alto",
        "mountain view",
        "sunnyvale",
        "santa clara",
        "san jose",
        "redwood city",
        "menlo park",
        "burlingame",
        "south san francisco",
        "walnut creek",
        "richmond",
    }
)

_ARRANGEMENT_SCORE = {"remote": 1.0, "hybrid": 0.7, "onsite": 0.4}

# Unknown salary sits between the floor and the target rather than at zero:
# two thirds of intake has no parseable figure, and scoring absence as failure
# would rank every Greenhouse posting below every Ashby one on disclosure
# alone rather than on fit.
_UNKNOWN_SALARY_SCORE = 0.55


@dataclass(frozen=True)
class ScoreComponents:
    embed_similarity: float
    rule_score: float
    freshness_score: float
    total_score: float
    is_stretch: bool


def freshness_score(job: Job, *, now: datetime) -> float:
    if job.posted_at is None:
        # Neither rewarded nor punished. HN and parts of Lever omit this.
        return 0.5

    age_hours = (now - job.posted_at).total_seconds() / 3600.0
    if age_hours <= 48:
        # Includes negative ages: a board a couple of hours ahead of our clock
        # is fresh, not invalid.
        return 1.0
    if age_hours <= 7 * 24:
        return 0.7
    if age_hours <= 30 * 24:
        return 0.4
    return 0.15


def rule_score(job: Job, settings: Settings) -> float:
    arrangement = _ARRANGEMENT_SCORE.get(job.remote_type, 0.4)

    proximity = 1.0
    if job.remote_type == "onsite":
        from pipeline.normalize import normalize_city

        proximity = 1.0 if normalize_city(job.location) in _NEARBY else 0.3

    if job.salary_source == "none":
        salary = _UNKNOWN_SALARY_SCORE
    else:
        ceiling = job.salary_max if job.salary_max is not None else job.salary_min
        if ceiling is None:
            salary = _UNKNOWN_SALARY_SCORE
        else:
            floor, target = settings.salary_floor, settings.salary_floor * 1.2
            salary = min(1.0, max(0.0, (ceiling - floor) / (target - floor)))

    return round(0.45 * arrangement + 0.20 * proximity + 0.35 * salary, 6)


def blend(embed_similarity: float, rule: float, freshness: float) -> float:
    total = (
        WEIGHT_SIMILARITY * embed_similarity + WEIGHT_RULES * rule + WEIGHT_FRESHNESS * freshness
    )
    return round(min(1.0, max(0.0, total)), 6)


def score_jobs(
    jobs: list[Job],
    profile: Profile,
    settings: Settings,
    *,
    now: datetime,
    rng: random.Random | None = None,
) -> list[ScoreComponents]:
    """Score a batch. Embedding is batched because per-call overhead dominates."""
    if not jobs:
        return []

    chooser = rng or random.Random()
    embedder = get_embedder()

    resume_vec = embedder.embed_one(resume_text(profile))
    # The title carries most of the signal and the description carries the
    # detail; truncating the description keeps one enormous posting from
    # dominating the vector.
    job_vecs = embedder.embed([f"{j.title}\n{j.description[:4000]}" for j in jobs])

    results: list[ScoreComponents] = []
    for job, vec in zip(jobs, job_vecs, strict=True):
        similarity = max(0.0, cosine(resume_vec, vec))
        rules = rule_score(job, settings)
        fresh = freshness_score(job, now=now)
        results.append(
            ScoreComponents(
                embed_similarity=round(similarity, 6),
                rule_score=rules,
                freshness_score=fresh,
                total_score=blend(similarity, rules, fresh),
                is_stretch=chooser.random() < STRETCH_RATE,
            )
        )
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_score.py -v`
Expected: PASS (15 tests)

- [ ] **Step 5: Score the real corpus and read the distribution**

Unit tests prove the arithmetic. Run it over the ~800 live jobs already in the
dev database and look at what it ranks. Create `scripts/score_report.py`:

```python
"""Score every live job and print the ranking, without writing anything."""

import asyncio
import os
from collections import Counter
from datetime import UTC, datetime

from pipeline.config import load_settings
from pipeline.db import connection
from pipeline.profile import load_profile
from pipeline.score import score_jobs
from pipeline.store import jobs_needing_scores


async def main() -> None:
    settings = load_settings()
    profile = load_profile(settings)
    async with connection() as conn:
        batch = await jobs_needing_scores(conn, 2000)

    jobs = [job for _, job in batch]
    results = score_jobs(jobs, profile, settings, now=datetime.now(UTC))
    ranked = sorted(zip(jobs, results), key=lambda p: -p[1].total_score)

    print(f"{len(ranked)} jobs scored\n")
    for job, s in ranked[:20]:
        print(
            f"{s.total_score:.3f}  sim={s.embed_similarity:.2f}"
            f"  rule={s.rule_score:.2f}  fresh={s.freshness_score:.2f}"
            f"  {job.title[:50]:52s} {job.company_name[:24]}"
        )

    print("\nhistogram:")
    buckets = Counter(int(s.total_score * 10) for _, s in ranked)
    for bucket in sorted(buckets):
        print(f"  {bucket / 10:.1f}-{(bucket + 1) / 10:.1f}  {'#' * buckets[bucket]}")


if __name__ == "__main__":
    os.environ.setdefault("JOBHUNT_PROFILE_DIR", "profile")
    asyncio.run(main())
```

```bash
DATABASE_URL="postgresql://jobhunt@localhost:5433/jobhunt_dev" \
  ./venv/bin/python scripts/score_report.py
```

Check three things, and adjust weights only against this evidence:

- The top 20 are jobs you would actually apply to. If they are not, the weights
  are wrong — not the model.
- The distribution is spread, not clustered. If every job scores 0.6–0.7 the
  gate cannot rank and the judge limit will select arbitrarily.
- Similarity separates: engineering roles should sit visibly above the sales
  and design postings that survived the pre-filter.

- [ ] **Step 6: Commit**

```bash
git add pipeline/score.py tests/test_score.py
git commit -m "feat: deterministic scoring and ranking blend"
```

---

### Task 5: The Anthropic client wrapper

**Files:**
- Create: `pipeline/llm.py`
- Modify: `pyproject.toml`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Settings`, `psycopg.AsyncConnection`
- Produces:
  - `pipeline.llm.SpendCapExceeded(Exception)`
  - `pipeline.llm.PRICING: dict[str, tuple[float, float]]` — per-MTok (input, output)
  - `await pipeline.llm.spend_today(conn) -> Decimal`
  - `await pipeline.llm.record_spend(conn, *, model, purpose, usage) -> Decimal`
  - `await pipeline.llm.call_structured(conn, *, model, purpose, system, user, output_format, max_tokens) -> tuple[BaseModel, Usage]`
  - `pipeline.llm.cache_prefix_is_effective(model: str, prefix_tokens: int) -> bool`
  - `pipeline.llm.MIN_CACHEABLE_TOKENS: dict[str, int]`

This is the only module that talks to Anthropic. Everything else calls through it, so the spend ceiling cannot be bypassed by adding a call site.

**Three model-specific rules, verified against the API docs rather than recalled:**

1. **Haiku 4.5's minimum cacheable prefix is 4096 tokens** (Sonnet 5's is 1024). Below it, caching silently no-ops — `cache_creation_input_tokens` is 0, no error, no warning.
2. **`effort` / `output_config` must not be sent to Haiku 4.5** — it errors on that model.
3. **Thinking stays off** for classification. On Haiku 4.5 that means omitting the `thinking` parameter; `{"type": "adaptive"}` is a 4.6+ shape.

**The ceiling reads from the database, not an in-process counter.** A serverless instance is recreated constantly; an in-memory tally resets to zero on every cold start, which is precisely no ceiling at all.

- [ ] **Step 1: Add the dependency**

```toml
    "anthropic>=0.40",
    "pydantic>=2.0",
```

- [ ] **Step 2: Verify the SDK bindings before writing against them**

Do not write `messages.parse()` from memory. Confirm the async binding and the
response attribute exist in the installed SDK:

```bash
./venv/bin/pip install -e ".[dev]"
./venv/bin/python - <<'PY'
import anthropic, inspect
c = anthropic.AsyncAnthropic(api_key="sk-ant-placeholder")
print("messages.parse present:", hasattr(c.messages, "parse"))
print(inspect.signature(c.messages.parse))
PY
```

Expected: `parse` is present and accepts `output_format`. If the installed SDK
differs, use `output_config={"format": {"type": "json_schema", "schema": ...}}`
on `messages.create()` and parse with `json.loads` — do **not** guess a binding.

- [ ] **Step 3: Write the failing test**

`tests/test_llm.py`:

```python
from decimal import Decimal

import pytest
from pydantic import BaseModel

from pipeline import llm
from pipeline.config import load_settings


class Verdict(BaseModel):
    verdict: str
    score: float
    rationale: str


class FakeUsage:
    def __init__(self, input_tokens=1000, output_tokens=200, cache_read=0, cache_write=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write


def settings(**env):
    return load_settings(env={"DATABASE_URL": "postgresql://x/y", **env})


# --- pricing and the ceiling -------------------------------------------------


def test_pricing_covers_every_model_we_use():
    assert "claude-haiku-4-5" in llm.PRICING
    assert "claude-sonnet-5" in llm.PRICING


def test_cost_uses_exact_arithmetic_not_float():
    cost = llm.cost_of("claude-haiku-4-5", FakeUsage(input_tokens=1_000_000, output_tokens=0))
    # $1.00 per MTok input on Haiku 4.5.
    assert cost == Decimal("1.000000")
    assert isinstance(cost, Decimal)


def test_cached_reads_are_billed_at_a_tenth():
    full = llm.cost_of("claude-haiku-4-5", FakeUsage(input_tokens=1_000_000, output_tokens=0))
    cached = llm.cost_of(
        "claude-haiku-4-5", FakeUsage(input_tokens=0, output_tokens=0, cache_read=1_000_000)
    )
    assert cached == full / 10


def test_cache_writes_cost_more_than_plain_input():
    # 1.25x. Caching only pays off from the second request onward.
    plain = llm.cost_of("claude-haiku-4-5", FakeUsage(input_tokens=1_000_000, output_tokens=0))
    write = llm.cost_of(
        "claude-haiku-4-5", FakeUsage(input_tokens=0, output_tokens=0, cache_write=1_000_000)
    )
    assert write > plain


async def test_spend_today_starts_at_zero(db):
    assert await llm.spend_today(db) == Decimal("0")


async def test_record_spend_accumulates(db):
    await llm.record_spend(db, model="claude-haiku-4-5", purpose="judge", usage=FakeUsage())
    await llm.record_spend(db, model="claude-haiku-4-5", purpose="judge", usage=FakeUsage())
    cur = await db.execute("SELECT count(*) AS n FROM llm_spend")
    assert (await cur.fetchone())["n"] == 2
    assert await llm.spend_today(db) > 0


async def test_the_ceiling_reads_from_the_database(db):
    """An in-process counter resets on every cold start, which is no ceiling.

    A serverless instance is recreated constantly; the only durable record of
    what has been spent today is the table.
    """
    for _ in range(5):
        await llm.record_spend(
            db,
            model="claude-haiku-4-5",
            purpose="judge",
            usage=FakeUsage(input_tokens=100_000_000, output_tokens=0),
        )
    with pytest.raises(llm.SpendCapExceeded):
        await llm.assert_under_cap(db, settings(DAILY_LLM_CAP_USD="1.50"))


async def test_spend_from_a_previous_day_does_not_count(db):
    await db.execute(
        "INSERT INTO llm_spend (day, model, purpose, input_tokens, output_tokens, cost_usd)"
        " VALUES (current_date - 1, 'claude-haiku-4-5', 'judge', 1, 1, 99.0)"
    )
    assert await llm.spend_today(db) == Decimal("0")


# --- the caching trap --------------------------------------------------------


def test_haiku_minimum_cacheable_prefix_is_4096():
    """The single most consequential number in this module.

    Below the minimum, caching silently does nothing: no error, no warning,
    cache_creation_input_tokens simply comes back 0.
    """
    assert llm.MIN_CACHEABLE_TOKENS["claude-haiku-4-5"] == 4096
    assert llm.MIN_CACHEABLE_TOKENS["claude-sonnet-5"] == 1024


def test_a_short_prefix_is_reported_as_ineffective():
    assert llm.cache_prefix_is_effective("claude-haiku-4-5", 2900) is False
    assert llm.cache_prefix_is_effective("claude-haiku-4-5", 4200) is True


def test_an_unknown_model_is_treated_as_uncacheable():
    # Better to skip caching than to claim a saving that is not happening.
    assert llm.cache_prefix_is_effective("claude-something-new", 100_000) is False
```

- [ ] **Step 4: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.llm'`

- [ ] **Step 5: Implement `pipeline/llm.py`**

```python
"""The only module that talks to Anthropic.

Every call goes through here so the daily spend ceiling cannot be bypassed by
adding a call site somewhere else.

Three model-specific rules, taken from the API documentation rather than
memory, each of which fails quietly if got wrong:

1. Haiku 4.5's minimum cacheable prefix is 4096 tokens (Sonnet 5's is 1024).
   Below it, caching does nothing at all -- no error, no warning,
   cache_creation_input_tokens simply returns 0.
2. `effort` / `output_config` must not be sent to Haiku 4.5; it errors.
3. Thinking stays off for classification. On Haiku 4.5 that means omitting the
   parameter entirely -- `{"type": "adaptive"}` is a 4.6-and-later shape.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, TypeVar

import anthropic
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from pipeline.config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# USD per million tokens: (input, output). Verified 2026-08-01.
# Sonnet 5 carries an introductory rate of $2/$10 through 2026-08-31; the
# standard rate is used here so the ceiling never under-counts.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
}

# Minimum prefix length for prompt caching to engage at all. Not monotonic
# across generations -- Haiku 4.5 needs four times what Sonnet 5 does.
MIN_CACHEABLE_TOKENS = {
    "claude-haiku-4-5": 4096,
    "claude-sonnet-5": 1024,
}

_CACHE_READ_MULTIPLIER = Decimal("0.1")
_CACHE_WRITE_MULTIPLIER = Decimal("1.25")
_MILLION = Decimal("1000000")


class SpendCapExceeded(Exception):
    """The daily ceiling is reached. Raised rather than proceeding."""


def cache_prefix_is_effective(model: str, prefix_tokens: int) -> bool:
    """Whether a prefix of this size will actually be cached on this model.

    An unknown model is reported as uncacheable: claiming a saving that is not
    happening is worse than skipping the optimisation.
    """
    minimum = MIN_CACHEABLE_TOKENS.get(model)
    return minimum is not None and prefix_tokens >= minimum


def cost_of(model: str, usage: Any) -> Decimal:
    """Exact cost for one call. Decimal throughout -- never float for money."""
    input_rate, output_rate = PRICING.get(model, (Decimal("0"), Decimal("0")))

    plain = Decimal(getattr(usage, "input_tokens", 0) or 0)
    cached_read = Decimal(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = Decimal(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    output = Decimal(getattr(usage, "output_tokens", 0) or 0)

    total = (
        plain * input_rate
        + cached_read * input_rate * _CACHE_READ_MULTIPLIER
        + cache_write * input_rate * _CACHE_WRITE_MULTIPLIER
        + output * output_rate
    ) / _MILLION
    return total.quantize(Decimal("0.000001"))


async def spend_today(conn: AsyncConnection) -> Decimal:
    """Today's spend, in UTC, read from the table.

    Deliberately not an in-process counter: a serverless instance is recreated
    constantly, and a tally that resets on every cold start is no ceiling.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT COALESCE(sum(cost_usd), 0) AS total FROM llm_spend"
            " WHERE day = (now() AT TIME ZONE 'UTC')::date"
        )
        return (await cur.fetchone())["total"]


async def record_spend(conn: AsyncConnection, *, model: str, purpose: str, usage: Any) -> Decimal:
    cost = cost_of(model, usage)
    await conn.execute(
        "INSERT INTO llm_spend (model, purpose, input_tokens, cached_input_tokens,"
        " cache_write_tokens, output_tokens, cost_usd)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            model,
            purpose,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            cost,
        ),
    )
    return cost


async def assert_under_cap(conn: AsyncConnection, settings: Settings) -> None:
    spent = await spend_today(conn)
    if spent >= settings.daily_llm_cap_usd:
        raise SpendCapExceeded(f"daily LLM cap reached: ${spent} >= ${settings.daily_llm_cap_usd}")


async def call_structured(
    conn: AsyncConnection,
    *,
    model: str,
    purpose: str,
    system: list[dict[str, Any]],
    user: str,
    output_format: type[T],
    settings: Settings,
    max_tokens: int = 1024,
) -> tuple[T, Any]:
    """One structured call, billed and recorded.

    `system` is a list of content blocks so the caller can mark the static
    prefix with `cache_control`. No `thinking` and no `output_config` are sent:
    this is classification, and Haiku 4.5 rejects `output_config` outright.
    """
    await assert_under_cap(conn, settings)

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=output_format,
    )

    await record_spend(conn, model=model, purpose=purpose, usage=response.usage)

    if getattr(response.usage, "cache_creation_input_tokens", 0) == 0 and any(
        "cache_control" in block for block in system
    ):
        # Spec section 11: caching failing silently is the risk, not caching
        # being unavailable. Say so once rather than quietly paying full price.
        log.warning(
            "%s: cache_control was set but nothing was written to cache — "
            "the prefix is probably below %s's %s-token minimum",
            purpose,
            model,
            MIN_CACHEABLE_TOKENS.get(model, "?"),
        )

    return response.parsed_output, response.usage
```

- [ ] **Step 6: Add the settings fields**

In `pipeline/config.py`, add to `Settings` and `load_settings`:

```python
    anthropic_api_key: str | None
    daily_llm_cap_usd: Decimal
    daily_judge_limit: int
```

```python
anthropic_api_key = (e.get("ANTHROPIC_API_KEY") or None,)
daily_llm_cap_usd = (Decimal(e.get("DAILY_LLM_CAP_USD", "1.50")),)
daily_judge_limit = (int(e.get("JOBHUNT_DAILY_JUDGE_LIMIT", "50")),)
```

- [ ] **Step 7: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_llm.py -v`
Expected: PASS (11 tests)

- [ ] **Step 8: Measure the real prefix and decide whether caching can work**

This is the measurement the whole cost model rests on. With `ANTHROPIC_API_KEY`
exported, count the actual static prefix:

```bash
./venv/bin/python - <<'PY'
import anthropic, os
from pipeline.config import load_settings
from pipeline.profile import load_profile, resume_text
# Build the exact system block the judge will send, then count it.
client = anthropic.Anthropic()
n = client.messages.count_tokens(
    model="claude-haiku-4-5",
    system=[{"type": "text", "text": STATIC_PREFIX}],
    messages=[{"role": "user", "content": "x"}],
).input_tokens
print(f"prefix: {n} tokens; Haiku minimum: 4096; caching {'WORKS' if n >= 4096 else 'NO-OPS'}")
PY
```

**If it comes back below 4096 — the estimate says ~2,900 — grow it rather than
dropping caching.** Counterintuitively, a bigger prompt is a cheaper one:

| Configuration | Cost per judgement |
|---|---|
| Prefix below the minimum (caching no-ops) | ~$0.0054 |
| Prefix grown past 4096 | **~$0.0029** |

The whole block then bills at 0.1× instead of 1×. Add genuinely useful content
— the full résumé rather than a summary, more competency bullets, two or three
worked scoring examples — not filler. Switching to Sonnet 5 for its lower 1024
minimum is a wash: its input rate cancels the saving.

Record the measured number in a comment next to the prefix so the next person
does not have to rediscover it.

- [ ] **Step 9: Commit**

```bash
git add pipeline/llm.py pipeline/config.py pyproject.toml tests/test_llm.py
git commit -m "feat: Anthropic client wrapper with spend ceiling and cache assertion"
```

---

### Task 6: The Sharia business-activity screen

**Files:**
- Create: `pipeline/filters/sharia.py`, `pipeline/filters/blocklist.yaml`
- Test: `tests/test_sharia.py`

**Interfaces:**
- Consumes: `llm.call_structured`, `psycopg.AsyncConnection`
- Produces:
  - `pipeline.filters.sharia.Verdict` — Pydantic model: `verdict: str`, `sector: str`, `reason: str`
  - `pipeline.filters.sharia.screen_blocklist(name, description) -> tuple[str, str, str] | None`
  - `await pipeline.filters.sharia.screen_company(conn, company_id, name, description, settings) -> str`
  - `pipeline.filters.sharia.VERDICTS = frozenset({"allowed", "excluded", "flagged", "unknown"})`

**Only the DJIM/AAOIFI business-activity screen applies** (spec §9). The financial-ratio screens — debt-to-market-cap thresholds and the like — exist for equity investing and are irrelevant to employment; applying them would wrongly exclude most leveraged companies. `IDEA.md` conflated the two.

Three tiers: static blocklist ($0) → cached Haiku classification → gray zone flagged for a human. **`sharia_source='user'` always wins and is never re-billed or re-evaluated.** An LLM must never silently make a religious ruling that cannot be corrected.

- [ ] **Step 1: Write the blocklist**

`pipeline/filters/blocklist.yaml`:

```yaml
# Tier 1 of the Sharia business-activity screen (spec section 9).
# Keyword hits are matched against the company name and self-description.
#
# Business activity only. The DJIM/AAOIFI financial-ratio screens are for
# equity investing and do not transfer to employment -- applying a debt ratio
# here would exclude most large employers for no defensible reason.
excluded:
  interest_finance:
    - bank
    - banking
    - lending
    - lender
    - mortgage
    - consumer credit
    - payday
    - interest rate
    - insurance
    - reinsurance
  alcohol:
    - brewery
    - brewing
    - distillery
    - winery
    - spirits brand
    - liquor
  gambling:
    - casino
    - sportsbook
    - betting
    - igaming
    - lottery
  adult:
    - adult entertainment
    - pornography
  weapons:
    - weapons manufacturer
    - munitions
    - firearms manufacturer
  pork:
    - pork producer
    - pork processing

# Matched but NOT excluded: these read as hits above while describing
# permissible work. Checked first, so "banking infrastructure for developers"
# is not dropped as a bank.
allowed_overrides:
  - blood bank
  - data bank
  - memory bank
  - bank of america stadium
  - world bank
  - image bank
```

- [ ] **Step 2: Write the failing test**

`tests/test_sharia.py`:

```python
import pytest

from pipeline.config import load_settings
from pipeline.filters.sharia import (
    VERDICTS,
    screen_blocklist,
    screen_company,
)

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y"})


async def _company(db, name="Acme", **cols) -> int:
    keys = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO companies (name, normalized_name{', ' + keys if cols else ''})"
    sql += f" VALUES (%s, %s{', ' + marks if cols else ''}) RETURNING company_id"
    cur = await db.execute(sql, (name, name.lower(), *cols.values()))
    return (await cur.fetchone())["company_id"]


# --- tier 1: the blocklist ---------------------------------------------------


@pytest.mark.parametrize(
    "name,description",
    [
        ("First National Bank", "Retail banking and mortgages."),
        ("Acme Casino Group", "Online sportsbook and casino games."),
        ("Golden Distillery", "We make small-batch whiskey."),
        ("SafeGuard Insurance", "Auto and home insurance."),
    ],
)
def test_clear_exclusions_are_caught_for_free(name, description):
    result = screen_blocklist(name, description)
    assert result is not None
    assert result[0] == "excluded"


@pytest.mark.parametrize(
    "name,description",
    [
        ("Stripe", "Payments infrastructure for the internet."),
        ("Anthropic", "AI safety research company."),
        ("Figma", "Collaborative design tool."),
    ],
)
def test_ordinary_companies_are_not_matched(name, description):
    assert screen_blocklist(name, description) is None


def test_allowed_overrides_beat_keyword_hits():
    """ "Blood bank" contains "bank"; it is not a bank.

    Without the override this drops an entire category of medical employers.
    """
    assert screen_blocklist("Regional Blood Bank", "We manage blood donation.") is None


def test_the_financial_ratio_screens_are_not_applied():
    """Spec section 9: business activity only.

    The DJIM/AAOIFI debt and market-cap screens exist for equity investing.
    Applying them to employment would exclude most leveraged companies for no
    defensible reason, and IDEA.md conflated the two.
    """
    from pipeline.filters import sharia

    source = (sharia.__file__ and open(sharia.__file__).read()) or ""
    for term in ("debt_ratio", "market_cap", "debt_to_equity", "interest_income_ratio"):
        assert term not in source


def test_verdict_vocabulary_is_closed():
    assert VERDICTS == frozenset({"allowed", "excluded", "flagged", "unknown"})


# --- tier 2 and 3: caching and the user override ----------------------------


async def test_a_blocklist_hit_is_cached_without_an_llm_call(db):
    company_id = await _company(db, "Acme Casino")
    verdict = await screen_company(
        db, company_id, "Acme Casino", "Online betting.", SETTINGS, judge=_never_called
    )
    assert verdict == "excluded"
    cur = await db.execute("SELECT sharia_verdict, sharia_source FROM companies")
    row = await cur.fetchone()
    assert row["sharia_verdict"] == "excluded"
    assert row["sharia_source"] == "blocklist"


async def test_a_cached_verdict_is_not_re_billed(db):
    company_id = await _company(
        db, "Acme", sharia_verdict="allowed", sharia_source="llm", sharia_decided_at="now()"
    )
    verdict = await screen_company(
        db, company_id, "Acme", "Software.", SETTINGS, judge=_never_called
    )
    assert verdict == "allowed"


async def test_a_user_verdict_always_wins_and_is_never_re_evaluated(db):
    """Spec section 9: an LLM must never silently make a religious ruling that
    cannot be corrected."""
    company_id = await _company(db, "Acme Casino", sharia_verdict="allowed", sharia_source="user")
    verdict = await screen_company(
        db, company_id, "Acme Casino", "Online betting.", SETTINGS, judge=_never_called
    )
    # The blocklist would say excluded. The user said allowed. The user wins.
    assert verdict == "allowed"


async def test_an_unresolved_company_reaches_the_judge(db):
    company_id = await _company(db, "Novel Corp")
    calls = []

    async def judge(conn, name, description, settings):
        calls.append(name)
        return "allowed", "software", "General-purpose SaaS."

    verdict = await screen_company(
        db, company_id, "Novel Corp", "We do something new.", SETTINGS, judge=judge
    )
    assert verdict == "allowed"
    assert calls == ["Novel Corp"]

    cur = await db.execute("SELECT sharia_source, sharia_reason FROM companies")
    row = await cur.fetchone()
    assert row["sharia_source"] == "llm"
    assert "SaaS" in row["sharia_reason"]


async def test_a_gray_zone_verdict_is_flagged_not_dropped(db):
    company_id = await _company(db, "Ambiguous Inc")

    async def judge(conn, name, description, settings):
        return "flagged", "fintech", "Payments processor with a lending arm."

    verdict = await screen_company(
        db, company_id, "Ambiguous Inc", "Fintech.", SETTINGS, judge=judge
    )
    assert verdict == "flagged"
    cur = await db.execute("SELECT sharia_reason FROM companies")
    # The reason is surfaced in the UI so the decision is Jarra's, with the
    # model's argument visible rather than hidden.
    assert "lending" in (await cur.fetchone())["sharia_reason"]


async def test_a_judge_failure_leaves_the_company_unknown_not_excluded(db):
    # Failing closed would silently delete employers on an API outage.
    company_id = await _company(db, "Novel Corp")

    async def judge(conn, name, description, settings):
        raise RuntimeError("api down")

    verdict = await screen_company(
        db, company_id, "Novel Corp", "Something.", SETTINGS, judge=judge
    )
    assert verdict == "unknown"


async def _never_called(conn, name, description, settings):
    raise AssertionError("the judge should not have been called")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_sharia.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.filters.sharia'`

- [ ] **Step 4: Implement `pipeline/filters/sharia.py`**

```python
"""The Sharia business-activity screen (spec section 9).

**Business activity only.** The DJIM/AAOIFI financial-ratio screens -- debt to
market cap and similar -- exist for equity investing and do not transfer to
employment; applying them would wrongly exclude most leveraged companies.
IDEA.md conflated the two. There is deliberately no ratio logic in this file,
and a test asserts its absence.

Three tiers:

1. Static blocklist -- free, deterministic, catches the clear cases.
2. Haiku classification -- cached per company forever, so the cost trends to
   zero after the first couple of weeks.
3. Gray zone -- flagged, never dropped, with the model's reasoning surfaced in
   the UI so the decision stays Jarra's.

`sharia_source='user'` always wins and is never re-billed or re-evaluated. An
LLM must never silently make a religious ruling that cannot be corrected.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from pipeline.config import Settings
from pipeline.llm import SpendCapExceeded, call_structured

log = logging.getLogger(__name__)

VERDICTS = frozenset({"allowed", "excluded", "flagged", "unknown"})

MODEL = "claude-haiku-4-5"

_BLOCKLIST_PATH = Path(__file__).with_name("blocklist.yaml")
_blocklist = yaml.safe_load(_BLOCKLIST_PATH.read_text())

_OVERRIDES = tuple(_blocklist.get("allowed_overrides") or [])
_EXCLUDED: tuple[tuple[str, str], ...] = tuple(
    (sector, term) for sector, terms in (_blocklist.get("excluded") or {}).items() for term in terms
)

_SYSTEM = """You classify an employer's primary business activity for a Sharia \
business-activity screen used to decide whether to apply for a job there.

Apply ONLY the business-activity screen. Do not consider financial ratios, debt \
levels, or market capitalisation — those apply to equity investing, not employment.

Return "excluded" only when the company's PRIMARY revenue comes from: \
interest-based finance, alcohol, gambling, adult content, weapons manufacture, \
or pork.

Return "allowed" for ordinary businesses, including ones that merely serve \
excluded industries as customers.

Return "flagged" when the primary activity is genuinely ambiguous — a payments \
company with a lending arm, a conglomerate with a mixed portfolio. Explain what \
makes it ambiguous; a human will decide.

Be concise. One or two sentences of reason."""


class Verdict(BaseModel):
    verdict: str
    sector: str
    reason: str


def screen_blocklist(name: str, description: str) -> tuple[str, str, str] | None:
    """Tier 1. Returns (verdict, sector, reason) or None if unresolved."""
    haystack = f"{name} {description}".lower()

    # Overrides first: "blood bank" contains "bank" but is not a bank, and
    # without this an entire category of medical employers disappears.
    for phrase in _OVERRIDES:
        if phrase in haystack:
            return None

    for sector, term in _EXCLUDED:
        if re.search(rf"\b{re.escape(term)}\b", haystack):
            return ("excluded", sector, f"Blocklist match on {term!r} ({sector}).")
    return None


async def judge_company(
    conn: AsyncConnection, name: str, description: str, settings: Settings
) -> tuple[str, str, str]:
    """Tier 2. One cached-forever Haiku classification."""
    parsed, _usage = await call_structured(
        conn,
        model=MODEL,
        purpose="sharia",
        system=[{"type": "text", "text": _SYSTEM}],
        user=f"Company: {name}\n\nSelf-description:\n{description[:2000]}",
        output_format=Verdict,
        settings=settings,
        max_tokens=300,
    )
    verdict = parsed.verdict if parsed.verdict in VERDICTS else "flagged"
    return verdict, parsed.sector, parsed.reason


async def screen_company(
    conn: AsyncConnection,
    company_id: int,
    name: str,
    description: str,
    settings: Settings,
    *,
    judge: Callable[..., Awaitable[tuple[str, str, str]]] = judge_company,
) -> str:
    """Resolve a company's verdict, using the cheapest tier that can decide."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sharia_verdict, sharia_source FROM companies WHERE company_id = %s",
            (company_id,),
        )
        row = await cur.fetchone()

    if row:
        # A user verdict is permanent and is never re-billed or re-evaluated.
        if row["sharia_source"] == "user":
            return row["sharia_verdict"]
        # Any other cached verdict stands: this is what makes the cost trend
        # to near zero after the first couple of weeks.
        if row["sharia_verdict"] != "unknown" and row["sharia_source"]:
            return row["sharia_verdict"]

    if resolved := screen_blocklist(name, description):
        verdict, sector, reason = resolved
        await _store(conn, company_id, verdict, sector, reason, "blocklist")
        return verdict

    try:
        verdict, sector, reason = await judge(conn, name, description, settings)
    except SpendCapExceeded:
        log.warning("sharia: daily cap reached; %s left unknown", name)
        return "unknown"
    except Exception as exc:
        # Leave it unknown rather than excluded. Failing closed would silently
        # delete employers during an API outage.
        log.warning("sharia: classification failed for %s: %s", name, exc)
        return "unknown"

    await _store(conn, company_id, verdict, sector, reason, "llm")
    return verdict


async def _store(
    conn: AsyncConnection, company_id: int, verdict: str, sector: str, reason: str, source: str
) -> None:
    await conn.execute(
        "UPDATE companies SET sharia_verdict = %s, sharia_sector = %s,"
        " sharia_reason = %s, sharia_source = %s, sharia_decided_at = now()"
        " WHERE company_id = %s AND COALESCE(sharia_source, '') <> 'user'",
        (verdict, sector, reason, source, company_id),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_sharia.py -v`
Expected: PASS (16 tests)

- [ ] **Step 6: Run the blocklist over the real company list**

197 companies are already in the dev database. Check what tier 1 resolves and
what it would send to Haiku. Create `scripts/sharia_report.py`:

```python
"""Dry-run the blocklist over every known company. Costs nothing, writes nothing."""

import asyncio

from pipeline.db import connection
from pipeline.filters.sharia import screen_blocklist


async def main() -> None:
    async with connection() as conn:
        # `companies` has no description column — the only self-description we
        # hold is the text of that company's job postings. One representative
        # posting is enough for a business-activity screen.
        cur = await conn.execute(
            "SELECT DISTINCT ON (c.company_id) c.company_id, c.name,"
            " COALESCE(j.description, '') AS description"
            " FROM companies c LEFT JOIN jobs j USING (company_id)"
            " ORDER BY c.company_id, j.last_seen_at DESC"
        )
        rows = await cur.fetchall()

    excluded, unresolved = [], []
    for row in rows:
        if result := screen_blocklist(row["name"], row["description"]):
            excluded.append((row["name"], result[1], result[2]))
        else:
            unresolved.append(row["name"])

    print(
        f"{len(rows)} companies: {len(excluded)} excluded by blocklist, "
        f"{len(unresolved)} would go to Haiku (~${len(unresolved) * 0.0005:.2f} once)\n"
    )
    print("EXCLUDED — read every one of these:")
    for name, sector, reason in excluded:
        print(f"  {name:36s} [{sector}] {reason}")


if __name__ == "__main__":
    asyncio.run(main())
```

```bash
DATABASE_URL="postgresql://jobhunt@localhost:5433/jobhunt_dev" \
  ./venv/bin/python scripts/sharia_report.py
```

Read every **excluded** result individually. A false exclusion silently deletes
an employer and nobody finds out — that is the expensive direction of error
here, and the `allowed_overrides` list exists because of it.

- [ ] **Step 7: Commit**

```bash
git add pipeline/filters/sharia.py pipeline/filters/blocklist.yaml tests/test_sharia.py
git commit -m "feat: Sharia business-activity screen"
```

---

### Task 7: The relevance judge

**Files:**
- Create: `pipeline/judge.py`
- Test: `tests/test_judge.py`

**Interfaces:**
- Consumes: `llm.call_structured`, `Profile`, `Job`
- Produces:
  - `pipeline.judge.Relevance` — Pydantic model: `verdict: str`, `score: float`, `rationale: str`
  - `pipeline.judge.build_static_prefix(profile: Profile) -> str`
  - `await pipeline.judge.judge_job(conn, job, profile, settings) -> Relevance | None`
  - `pipeline.judge.VERDICTS = frozenset({"strong", "possible", "weak"})`

Gate 4. Runs on the top `JOBHUNT_DAILY_JUDGE_LIMIT` jobs by combined score, never on everything.

**The rationale is the product, not the score.** A number tells you nothing you did not already have from the embedding; two sentences explaining *why* this job matches is what makes the queue reviewable in the morning.

- [ ] **Step 1: Write the failing test**

`tests/test_judge.py`:

```python
import pytest

from pipeline.config import load_settings
from pipeline.judge import VERDICTS, Relevance, build_static_prefix, judge_job
from pipeline.models import Job
from pipeline.profile import Profile

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y"})

PROFILE = Profile(
    resume={
        "summary": "Full-stack engineer with three years building production systems.",
        "skills": {"languages": ["Python", "TypeScript"], "cloud": ["AWS", "Docker"]},
        "experience": [
            {"company": "CloudBase", "title": "Software Engineer", "bullets": ["Shipped X."]}
        ],
        "education": [{"school": "Brown", "degree": "BS Computer Engineering"}],
    },
    competency_bullets=[{"label": "Full-Stack Development", "text": "Built things."}],
    identity={},
)


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
        description="Build full-stack features in Python and React.",
        apply_url="https://example.com/1",
        posted_at=None,
    )
    base.update(overrides)
    return Job(**base)


def test_verdict_vocabulary_is_closed():
    assert VERDICTS == frozenset({"strong", "possible", "weak"})


def test_static_prefix_contains_the_resume_and_no_job_content():
    """The prefix must be byte-identical across calls or nothing caches.

    Any per-job text here silently invalidates the cache on every request —
    the exact failure spec section 11 warns about.
    """
    prefix = build_static_prefix(PROFILE)
    assert "Full-stack engineer" in prefix
    assert "Brown" in prefix
    assert "Acme" not in prefix
    assert "Senior Software Engineer" not in prefix


def test_static_prefix_is_deterministic():
    assert build_static_prefix(PROFILE) == build_static_prefix(PROFILE)


async def test_judge_returns_a_parsed_verdict(db, monkeypatch):
    async def fake_call(conn, **kwargs):
        return Relevance(verdict="strong", score=0.9, rationale="Direct stack match."), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    result = await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert result.verdict == "strong"
    assert result.rationale == "Direct stack match."


async def test_an_out_of_vocabulary_verdict_is_coerced_not_stored_raw(db, monkeypatch):
    # A free-text verdict would break every downstream filter that groups by it.
    async def fake_call(conn, **kwargs):
        return Relevance(verdict="AMAZING FIT!!", score=0.9, rationale="…"), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    result = await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert result.verdict in VERDICTS


async def test_a_score_outside_zero_to_one_is_clamped(db, monkeypatch):
    async def fake_call(conn, **kwargs):
        return Relevance(verdict="strong", score=4.2, rationale="…"), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    result = await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert 0.0 <= result.score <= 1.0


async def test_the_spend_cap_stops_judging_without_raising(db, monkeypatch):
    """A reached cap is a normal end state, not an error.

    Raising here would abort the whole run and lose the jobs already scored.
    """
    from pipeline.llm import SpendCapExceeded

    async def fake_call(conn, **kwargs):
        raise SpendCapExceeded("cap")

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    assert await judge_job(db, make_job(), PROFILE, SETTINGS) is None


async def test_an_api_failure_returns_none_rather_than_a_fake_verdict(db, monkeypatch):
    async def fake_call(conn, **kwargs):
        raise RuntimeError("500")

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    assert await judge_job(db, make_job(), PROFILE, SETTINGS) is None


async def test_the_static_prefix_is_marked_for_caching(db, monkeypatch):
    seen = {}

    async def fake_call(conn, **kwargs):
        seen.update(kwargs)
        return Relevance(verdict="strong", score=0.9, rationale="…"), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert any("cache_control" in block for block in seen["system"])


async def test_effort_is_never_sent_to_haiku(db, monkeypatch):
    """Haiku 4.5 errors on output_config. Sending it breaks every judge call."""
    seen = {}

    async def fake_call(conn, **kwargs):
        seen.update(kwargs)
        return Relevance(verdict="strong", score=0.9, rationale="…"), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert "output_config" not in seen
    assert "thinking" not in seen


class _Usage:
    input_tokens = 1000
    output_tokens = 100
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_judge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.judge'`

- [ ] **Step 3: Implement `pipeline/judge.py`**

```python
"""Gate 4: the Haiku relevance judgement (spec section 8).

Runs on the top JOBHUNT_DAILY_JUDGE_LIMIT jobs by combined score, never on
everything -- judging all ~800 daily jobs would cost about $4/day against a
budget of roughly $5/month.

**The rationale is the product, not the score.** A number adds nothing the
embedding did not already give us; two sentences explaining why this job fits
is what makes the queue reviewable over a morning coffee.

Thinking is off (classification, not reasoning) and `output_config` is never
sent -- Haiku 4.5 errors on it.
"""

from __future__ import annotations

import json
import logging

from psycopg import AsyncConnection
from pydantic import BaseModel

from pipeline.config import Settings
from pipeline.llm import SpendCapExceeded, call_structured
from pipeline.models import Job
from pipeline.profile import Profile

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
VERDICTS = frozenset({"strong", "possible", "weak"})

_INSTRUCTIONS = """You assess how well a job posting fits one specific candidate, \
whose full background is given above.

Return a verdict of exactly "strong", "possible", or "weak", a score from 0.0 to \
1.0, and a rationale of at most two sentences.

The rationale is the important part. Write it for the candidate reading their \
morning queue: name the specific overlap or the specific gap. "Matches your \
Python and AWS experience, but wants 8+ years" is useful. "Good fit for your \
skills" is not.

Judge fit, not desirability. A well-matched role at a boring company is a strong \
fit. Do not comment on salary, location, or the company's reputation — those are \
scored separately."""


class Relevance(BaseModel):
    verdict: str
    score: float
    rationale: str


def build_static_prefix(profile: Profile) -> str:
    """The cacheable block. Must be byte-identical across every call.

    Any per-job content here would invalidate the cache on every request --
    exactly the silent failure spec section 11 warns about. Nothing that varies
    per job may enter this string.
    """
    resume = json.dumps(profile.resume, indent=2, sort_keys=True)
    bullets = "\n".join(f"- {b.get('label')}: {b.get('text')}" for b in profile.competency_bullets)
    return (
        "You are assessing job fit for the following candidate.\n\n"
        f"## Résumé\n\n{resume}\n\n"
        f"## Competency summary\n\n{bullets}\n\n"
        f"## Task\n\n{_INSTRUCTIONS}"
    )


async def judge_job(
    conn: AsyncConnection, job: Job, profile: Profile, settings: Settings
) -> Relevance | None:
    """One judgement, or None if the cap is reached or the call fails."""
    system = [
        {
            "type": "text",
            "text": build_static_prefix(profile),
            # Measured: see Task 5 step 8. If the prefix is under Haiku's
            # 4096-token minimum this marker is accepted and does nothing.
            "cache_control": {"type": "ephemeral"},
        }
    ]
    user = (
        f"Title: {job.title}\n"
        f"Company: {job.company_name}\n"
        f"Location: {job.location or 'unspecified'} ({job.remote_type})\n\n"
        f"Description:\n{job.description[:6000]}"
    )

    try:
        parsed, _usage = await call_structured(
            conn,
            model=MODEL,
            purpose="judge",
            system=system,
            user=user,
            output_format=Relevance,
            settings=settings,
            max_tokens=400,
        )
    except SpendCapExceeded:
        # A normal end state, not an error: raising would abort the run and
        # lose the jobs already scored this pass.
        log.info("judge: daily cap reached; stopping")
        return None
    except Exception as exc:
        log.warning("judge: call failed for %s: %s", job.title, exc)
        return None

    verdict = parsed.verdict if parsed.verdict in VERDICTS else "possible"
    return Relevance(
        verdict=verdict,
        score=min(1.0, max(0.0, parsed.score)),
        rationale=parsed.rationale.strip(),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_judge.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Judge ten real jobs and read every rationale**

The tests prove the plumbing. Whether the output is *useful* is a judgement
only a human can make. Create `scripts/judge_report.py`:

```python
"""Judge the ten highest-scoring jobs and print every rationale. Costs ~$0.03."""

import asyncio

from pipeline.config import load_settings
from pipeline.db import connection
from pipeline.judge import judge_job
from pipeline.llm import spend_today
from pipeline.profile import load_profile
from pipeline.store import top_unjudged


async def main() -> None:
    settings = load_settings()
    profile = load_profile(settings)

    async with connection() as conn:
        before = await spend_today(conn)
        for _job_id, job in await top_unjudged(conn, 10):
            relevance = await judge_job(conn, job, profile, settings)
            if relevance is None:
                print("stopped: cap reached or call failed")
                break
            print(
                f"[{relevance.verdict:8s} {relevance.score:.2f}] {job.title} — {job.company_name}"
            )
            print(f"    {relevance.rationale}\n")

        # Non-zero cached reads from call 2 onward is the caching proof.
        cur = await conn.execute(
            "SELECT sum(cached_input_tokens) AS cached, sum(cache_write_tokens) AS written"
            " FROM llm_spend WHERE purpose = 'judge' AND day = current_date"
        )
        row = await cur.fetchone()
        print(f"spend this run: ${await spend_today(conn) - before}")
        print(f"cache written: {row['written']}  cache read: {row['cached']}")
        if not row["cached"]:
            print("WARNING: nothing read from cache — prefix is likely under 4096 tokens")


if __name__ == "__main__":
    asyncio.run(main())
```

```bash
ANTHROPIC_API_KEY=... DATABASE_URL="postgresql://jobhunt@localhost:5433/jobhunt_dev" \
  JOBHUNT_PROFILE_DIR=profile ./venv/bin/python scripts/judge_report.py
```

Check that:

- Each rationale names something **specific** — a technology, a years-of-experience
  gap, a domain. Generic praise means the prompt needs the specificity instruction
  strengthened, not the model changed.
- The verdict spread is not all "strong". If everything is strong, the judge is
  agreeing with the embedding gate and adding nothing but cost.
- `cache_read_input_tokens` is non-zero on calls 2–10. If it is zero, the prefix
  is under 4096 and Task 5 step 8 was not resolved.

- [ ] **Step 6: Commit**

```bash
git add pipeline/judge.py tests/test_judge.py
git commit -m "feat: Haiku relevance judge with cached résumé prefix"
```

---

### Task 8: Scoring orchestration

**Files:**
- Create: `pipeline/run_score.py`
- Modify: `pipeline/store.py`, `api/index.py`, `vercel.json`
- Test: `tests/test_run_score.py`

**Interfaces:**
- Consumes: everything above
- Produces:
  - `pipeline.run_score.ScoreStats` — frozen dataclass: `scored`, `judged`, `screened`, `skipped`, `errors`, `spend_usd`, `budget_hit`, `duration_ms`
  - `await pipeline.run_score.run(conn, settings, *, budget_seconds=None, now=None) -> ScoreStats`
  - `await pipeline.store.upsert_score(conn, job_id, components, *, relevance=None) -> None`
  - `await pipeline.store.jobs_needing_scores(conn, limit) -> list[Job]`
  - `await pipeline.store.top_unjudged(conn, limit) -> list[tuple[Job, int]]`

Same shape as `run_discover`: a wall-clock budget, resumable, idempotent, one `run_log` row.

**Scoring and judging are separate passes over separate candidate sets.** Everything unscored gets embedded — it is free. Only the top N of what is already scored gets judged. Merging them into one loop would judge in arrival order rather than in rank order, which is exactly the wrong N jobs.

- [ ] **Step 1: Write the failing test**

`tests/test_run_score.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from pipeline.config import load_settings
from pipeline.profile import Profile
from pipeline.run_score import ScoreStats, run

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

PROFILE = Profile(
    resume={"summary": "Full-stack engineer. Python, React, AWS."},
    competency_bullets=[],
    identity={},
)


def settings(**env):
    return load_settings(env={"DATABASE_URL": "postgresql://x/y", **env})


async def _seed(db, n: int, *, filtered: bool = False) -> list[int]:
    await db.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    ids = []
    for i in range(n):
        cur = await db.execute(
            "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
            " description, apply_url, first_seen_at, last_seen_at, posted_at,"
            " filtered_out, filter_reason)"
            " VALUES (%s, 1, 'greenhouse', %s, %s, 'Python React AWS engineer.',"
            " 'https://x', now(), now(), %s, %s, %s) RETURNING job_id",
            (
                f"{i:064d}",
                str(i),
                f"Backend Engineer, Team {i}",
                NOW - timedelta(hours=i),
                filtered,
                "title_not_target" if filtered else None,
            ),
        )
        ids.append((await cur.fetchone())["job_id"])
    return ids


async def test_scores_every_unscored_live_job(db, monkeypatch):
    monkeypatch.setattr("pipeline.run_score.load_profile", lambda s: PROFILE)
    await _seed(db, 5)
    stats = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    assert stats.scored == 5
    cur = await db.execute("SELECT count(*) AS n FROM scores")
    assert (await cur.fetchone())["n"] == 5


async def test_filtered_out_jobs_are_never_scored(db, monkeypatch):
    """Embedding is free but not free enough to spend on rejects.

    More importantly, a filtered job in the scores table would surface in the
    queue, which is the one thing the pre-filter exists to prevent.
    """
    monkeypatch.setattr("pipeline.run_score.load_profile", lambda s: PROFILE)
    await _seed(db, 3, filtered=True)
    stats = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    assert stats.scored == 0


async def test_running_twice_does_not_rescore(db, monkeypatch):
    monkeypatch.setattr("pipeline.run_score.load_profile", lambda s: PROFILE)
    await _seed(db, 4)
    first = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    second = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    assert first.scored == 4
    assert second.scored == 0


async def test_judging_is_capped_at_the_daily_limit(db, monkeypatch):
    """Judging all ~800 daily jobs costs about $4/day against a ~$5/month budget."""
    monkeypatch.setattr("pipeline.run_score.load_profile", lambda s: PROFILE)
    calls = []

    async def fake_judge(conn, job, profile, s):
        from pipeline.judge import Relevance

        calls.append(job.job_id if hasattr(job, "job_id") else job.source_job_id)
        return Relevance(verdict="strong", score=0.9, rationale="Match.")

    monkeypatch.setattr("pipeline.run_score.judge_job", fake_judge)
    await _seed(db, 10)
    stats = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="3"), now=NOW)
    assert stats.judged == 3
    assert len(calls) == 3


async def test_judging_takes_the_highest_scoring_jobs_not_the_first_seen(db, monkeypatch):
    """Judging in arrival order would spend the budget on the wrong N jobs."""
    monkeypatch.setattr("pipeline.run_score.load_profile", lambda s: PROFILE)
    judged_titles = []

    async def fake_judge(conn, job, profile, s):
        from pipeline.judge import Relevance

        judged_titles.append(job.title)
        return Relevance(verdict="strong", score=0.9, rationale="Match.")

    monkeypatch.setattr("pipeline.run_score.judge_job", fake_judge)
    await _seed(db, 6)
    await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="2"), now=NOW)

    cur = await db.execute(
        "SELECT j.title FROM scores s JOIN jobs j USING (job_id)"
        " ORDER BY s.total_score DESC LIMIT 2"
    )
    expected = [r["title"] for r in await cur.fetchall()]
    assert set(judged_titles) == set(expected)


async def test_a_judged_job_is_not_judged_again(db, monkeypatch):
    monkeypatch.setattr("pipeline.run_score.load_profile", lambda s: PROFILE)

    async def fake_judge(conn, job, profile, s):
        from pipeline.judge import Relevance

        return Relevance(verdict="strong", score=0.9, rationale="Match.")

    monkeypatch.setattr("pipeline.run_score.judge_job", fake_judge)
    await _seed(db, 3)
    await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="3"), now=NOW)
    second = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="3"), now=NOW)
    assert second.judged == 0


async def test_an_expired_budget_returns_cleanly(db, monkeypatch):
    monkeypatch.setattr("pipeline.run_score.load_profile", lambda s: PROFILE)
    await _seed(db, 20)
    stats = await run(db, settings(), budget_seconds=0.0, now=NOW)
    assert stats.budget_hit is True
    assert stats.scored == 0


async def test_a_missing_profile_fails_the_run_loudly(db, monkeypatch):
    """Scoring against an empty résumé would rank every job identically —
    a failure that surfaces weeks later as "the ranking is useless"."""
    from pipeline.profile import ProfileUnavailableError

    def boom(s):
        raise ProfileUnavailableError("no profile")

    monkeypatch.setattr("pipeline.run_score.load_profile", boom)
    await _seed(db, 3)
    with pytest.raises(ProfileUnavailableError):
        await run(db, settings(), now=NOW)


async def test_a_run_log_row_is_written(db, monkeypatch):
    monkeypatch.setattr("pipeline.run_score.load_profile", lambda s: PROFILE)
    await _seed(db, 2)
    await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    cur = await db.execute("SELECT count(*) AS n FROM run_log WHERE finished_at IS NOT NULL")
    assert (await cur.fetchone())["n"] == 1


def test_stats_defaults_are_zero():
    stats = ScoreStats()
    assert (stats.scored, stats.judged, stats.errors) == (0, 0, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_run_score.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.run_score'`

- [ ] **Step 3: Add the store helpers**

Append to `pipeline/store.py`:

```python
async def jobs_needing_scores(conn: AsyncConnection, limit: int) -> list[tuple[int, Job]]:
    """Live, unscored jobs. Filtered-out rows are never scored.

    A filtered job with a score would surface in the queue, which is the one
    thing the pre-filter exists to prevent.
    """
    rows = await _fetch_all(
        conn,
        "SELECT j.* FROM jobs j LEFT JOIN scores s USING (job_id)"
        " WHERE j.filtered_out = false AND s.job_id IS NULL"
        " ORDER BY j.last_seen_at DESC LIMIT %s",
        (limit,),
    )
    return [(r["job_id"], _row_to_job(r)) for r in rows]


async def top_unjudged(conn: AsyncConnection, limit: int) -> list[tuple[int, Job]]:
    """Highest-scoring jobs that have not been judged.

    Ordered by total_score, not by arrival: judging in arrival order would
    spend a capped budget on the wrong N jobs.
    """
    rows = await _fetch_all(
        conn,
        "SELECT j.* FROM scores s JOIN jobs j USING (job_id)"
        " WHERE s.judged_at IS NULL AND j.filtered_out = false"
        " ORDER BY s.total_score DESC LIMIT %s",
        (limit,),
    )
    return [(r["job_id"], _row_to_job(r)) for r in rows]


async def upsert_score(
    conn: AsyncConnection,
    job_id: int,
    components: Any,
    *,
    relevance: Any | None = None,
    model: str | None = None,
) -> None:
    """Write or refresh a score. Idempotent, like every other write here."""
    await conn.execute(
        """
        INSERT INTO scores (job_id, embed_similarity, rule_score, freshness_score,
                            total_score, is_stretch, relevance_verdict, rationale,
                            model, judged_at, scored_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (job_id) DO UPDATE SET
            embed_similarity  = EXCLUDED.embed_similarity,
            rule_score        = EXCLUDED.rule_score,
            freshness_score   = EXCLUDED.freshness_score,
            total_score       = EXCLUDED.total_score,
            is_stretch        = EXCLUDED.is_stretch,
            -- A judgement already paid for is never overwritten with NULL by a
            -- later re-score.
            relevance_verdict = COALESCE(EXCLUDED.relevance_verdict, scores.relevance_verdict),
            rationale         = COALESCE(EXCLUDED.rationale, scores.rationale),
            model             = COALESCE(EXCLUDED.model, scores.model),
            judged_at         = COALESCE(EXCLUDED.judged_at, scores.judged_at),
            scored_at         = now()
        """,
        (
            job_id,
            components.embed_similarity,
            components.rule_score,
            components.freshness_score,
            components.total_score,
            components.is_stretch,
            relevance.verdict if relevance else None,
            relevance.rationale if relevance else None,
            model if relevance else None,
            "now()" if relevance else None,
        ),
    )
```

Three helpers the above depends on, added alongside the existing `_fetch_one`:

```python
async def _fetch_all(conn: AsyncConnection, sql: str, params: tuple) -> list[dict]:
    """Pins the row factory rather than inheriting the caller's.

    Same reason as _fetch_one: reading by name off a connection whose factory
    we did not set produced `tuple indices must be integers` twice in Phase 1.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


def _row_to_job(row: dict) -> Job:
    """Build a Job from a `jobs` row. Field names match upsert_job's columns."""
    return Job(
        job_id=row["job_id"],
        company_id=row["company_id"],
        fingerprint=row["fingerprint"],
        source=row["source"],
        source_job_id=row["source_job_id"],
        company_name=row.get("company_name") or "",
        normalized_company=row.get("normalized_company") or "",
        title=row["title"],
        location=row.get("location"),
        remote_type=row.get("remote_type") or "onsite",
        salary_min=row.get("salary_min"),
        salary_max=row.get("salary_max"),
        salary_source=row.get("salary_source") or "none",
        description=row.get("description") or "",
        apply_url=row["apply_url"],
        posted_at=row.get("posted_at"),
    )


async def score_components_for(conn: AsyncConnection, job_id: int) -> Any:
    """Re-read stored components so a judged upsert does not clobber them."""
    from pipeline.score import ScoreComponents

    row = await _fetch_one(
        conn,
        "SELECT embed_similarity, rule_score, freshness_score, total_score, is_stretch"
        " FROM scores WHERE job_id = %s",
        (job_id,),
    )
    return ScoreComponents(
        embed_similarity=row["embed_similarity"],
        rule_score=row["rule_score"],
        freshness_score=row["freshness_score"],
        total_score=row["total_score"],
        is_stretch=row["is_stretch"],
    )
```

`jobs_needing_scores` and `top_unjudged` select `j.*`, which does not include
`companies.name`. Join it in so `_row_to_job` has a company name for the judge
prompt — change both queries' `SELECT j.*` to
`SELECT j.*, c.name AS company_name, c.normalized_name AS normalized_company`
and add `JOIN companies c USING (company_id)`.

In `run_score.py`, `_components_for` is `store.score_components_for`.

- [ ] **Step 4: Implement `pipeline/run_score.py`**

```python
"""Scoring orchestration. Callable from the CLI and from the cron route.

Two passes over two different candidate sets, deliberately not merged:

- **Score everything unscored.** Embedding is free, so there is no reason to
  be selective.
- **Judge only the top N of what is already scored.** Merging the passes would
  judge in arrival order rather than rank order, spending a capped budget on
  the wrong N jobs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from psycopg import AsyncConnection

from pipeline.config import Settings
from pipeline.filters.sharia import screen_company
from pipeline.judge import MODEL as JUDGE_MODEL
from pipeline.judge import judge_job
from pipeline.llm import spend_today
from pipeline.profile import load_profile
from pipeline.score import score_jobs
from pipeline.store import (
    finish_run,
    jobs_needing_scores,
    start_run,
    top_unjudged,
    upsert_score,
)

log = logging.getLogger(__name__)

_SCORE_BATCH = 500


@dataclass(frozen=True)
class ScoreStats:
    scored: int = 0
    judged: int = 0
    screened: int = 0
    errors: int = 0
    spend_usd: Decimal = Decimal("0")
    budget_hit: bool = False
    duration_ms: int = 0


async def run(
    conn: AsyncConnection,
    settings: Settings,
    *,
    budget_seconds: float | None = None,
    now: datetime | None = None,
) -> ScoreStats:
    started = time.monotonic()
    budget = budget_seconds if budget_seconds is not None else settings.run_budget_seconds
    deadline = started + budget
    clock = now or datetime.now(UTC)

    # Raises rather than scoring against an empty résumé. That failure would
    # rank every job identically and only surface weeks later as "the ranking
    # is useless".
    profile = load_profile(settings)

    run_id = await start_run(conn)
    scored = judged = screened = errors = 0
    budget_hit = False

    # Pass 1: embed and score. Free, so it runs on everything.
    while time.monotonic() < deadline:
        batch = await jobs_needing_scores(conn, _SCORE_BATCH)
        if not batch:
            break
        job_ids = [jid for jid, _ in batch]
        jobs = [job for _, job in batch]
        for job_id, components in zip(
            job_ids, score_jobs(jobs, profile, settings, now=clock), strict=True
        ):
            try:
                await upsert_score(conn, job_id, components)
                scored += 1
            except Exception as exc:
                log.warning("scoring failed for job %s: %s", job_id, exc)
                errors += 1
        if len(batch) < _SCORE_BATCH:
            break
    else:
        budget_hit = True

    # Pass 2: judge the top N, in rank order.
    if settings.daily_judge_limit > 0 and time.monotonic() < deadline:
        for job_id, job in await top_unjudged(conn, settings.daily_judge_limit):
            if time.monotonic() >= deadline:
                budget_hit = True
                break

            try:
                verdict = await screen_company(
                    conn, job.company_id, job.company_name, job.description, settings
                )
                screened += 1
                if verdict == "excluded":
                    continue
            except Exception as exc:
                log.warning("sharia screen failed for %s: %s", job.company_name, exc)
                errors += 1

            relevance = await judge_job(conn, job, profile, settings)
            if relevance is None:
                # Cap reached or the call failed. Both mean stop judging; the
                # scores already written stay.
                break
            components = await _components_for(conn, job_id)
            await upsert_score(conn, job_id, components, relevance=relevance, model=JUDGE_MODEL)
            judged += 1

    stats = ScoreStats(
        scored=scored,
        judged=judged,
        screened=screened,
        errors=errors,
        spend_usd=await spend_today(conn),
        budget_hit=budget_hit,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    await finish_run(
        conn,
        run_id,
        jobs_seen=scored,
        jobs_new=judged,
        jobs_filtered=0,
        errors=errors,
        duration_ms=stats.duration_ms,
        budget_hit=stats.budget_hit,
        notes=f"scored={scored} judged={judged} screened={screened} spend=${stats.spend_usd}",
    )
    log.info("score: %s", stats)
    return stats
```

`_components_for` re-reads the stored components so the judged upsert does not
clobber them; it is a three-line `SELECT` returning a `ScoreComponents`.

- [ ] **Step 5: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_run_score.py -v`
Expected: PASS (10 tests)

- [ ] **Step 6: Add the cron route and schedule**

In `api/index.py`, add a `/api/cron/score` route mirroring `/api/cron/discover`
— same `_require_cron_auth` guard, same 200-on-partial-run behaviour.

In `vercel.json`, add:

```json
{ "path": "/api/cron/score", "schedule": "*/15 * * * *" }
```

Fifteen minutes rather than ten: scoring only has work when discovery has
produced some, and a spread avoids both crons contending for the same
connection pool.

- [ ] **Step 7: Commit**

```bash
git add pipeline/run_score.py pipeline/store.py api/index.py vercel.json tests/test_run_score.py
git commit -m "feat: scoring orchestration and cron route"
```

---

### Task 9: Ship it to Vercel

**Files:**
- Create: `scripts/vendor_model.py`
- Modify: `pyproject.toml`, `vercel.json`
- Test: manual, against the deployed function

The embedding model must be **in the bundle**, not downloaded at runtime — `/tmp` is ephemeral, so a runtime download would fetch 65 MB on every cold start.

- [ ] **Step 1: Write the vendoring script**

`scripts/vendor_model.py`:

```python
"""Download the embedding model into the build.

Runs as Vercel's build command. The alternative -- letting fastembed fetch the
model at runtime -- would download 65 MB into an ephemeral /tmp on every cold
start, which is both slow and repeated.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pipeline.embed import MODEL_NAME

CACHE = Path(__file__).resolve().parents[1] / "model_cache"


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    from fastembed import TextEmbedding

    TextEmbedding(MODEL_NAME, cache_dir=str(CACHE))
    size = sum(f.stat().st_size for f in CACHE.rglob("*") if f.is_file())
    print(f"vendored {MODEL_NAME} into {CACHE} ({size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Wire it as the build command**

In `pyproject.toml`:

```toml
[tool.vercel.scripts]
build = "python scripts/vendor_model.py"
```

In `vercel.json`, remove `model_cache` from `excludeFiles` if a glob would
catch it — the model must ship. Add `.gitignore` entry for `model_cache/`:
it is a build artifact, regenerated on every deploy.

- [ ] **Step 3: Verify the bundle still fits**

```bash
vercel build --prod
du -sh .vercel/output/functions/*.func
```

Expected: under 500 MB. Measured estimate is ~292 MB. If it exceeds the limit,
set `VERCEL_SUPPORT_LARGE_FUNCTIONS=1` for the 5 GB ceiling rather than
dropping the model.

- [ ] **Step 4: Push the profile and deploy**

`PROFILE_JSON` follows the pattern `JOBHUNT_TARGETS_JSON` already proved: the
profile directory never ships, so its contents reach production as an
environment variable. Create `scripts/sync_profile.py`:

```python
"""Print the local profile as one JSON line, for piping into `vercel env add`.

Never commit the output. This is the same pattern JOBHUNT_TARGETS_JSON uses:
profile/ is gitignored and refused on Vercel, so its contents travel as an
environment variable instead.
"""

import json
import os
from pathlib import Path

import yaml


def main() -> None:
    directory = Path(os.environ.get("JOBHUNT_PROFILE_DIR", "profile"))
    payload = {"resume": json.loads((directory / "resume.json").read_text())}

    for key, filename in (
        ("competency_bullets", "competency_bullets.yaml"),
        ("identity", "identity.yaml"),
    ):
        path = directory / filename
        if path.exists():
            payload[key] = yaml.safe_load(path.read_text())

    print(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    main()
```

```bash
# Both scopes: production reads the Neon production branch, preview reads staging.
./venv/bin/python scripts/sync_profile.py | vercel env add PROFILE_JSON production
./venv/bin/python scripts/sync_profile.py | vercel env add PROFILE_JSON preview

vercel deploy --prebuilt --prod
vercel crons run /api/cron/score
```

- [ ] **Step 5: Verify against the production database**

```sql
SELECT count(*) FROM scores;
SELECT count(*) FROM scores WHERE judged_at IS NOT NULL;
SELECT sum(cost_usd) FROM llm_spend WHERE day = current_date;
SELECT j.title, s.total_score, s.relevance_verdict, s.rationale
  FROM scores s JOIN jobs j USING (job_id)
  WHERE s.judged_at IS NOT NULL
  ORDER BY s.total_score DESC LIMIT 10;
```

Three things decide whether Phase 2 worked, and only the last one is a
judgement call:

- **Cost.** Daily spend should sit near $0.15. If it is near $4, the judge
  limit is not being applied.
- **Caching.** `SELECT sum(cached_input_tokens) FROM llm_spend WHERE purpose='judge'`
  must be non-zero after the first few calls. Zero means the prefix is under
  4096 tokens and Task 5 step 8 was not resolved.
- **The top ten.** Read them. If they are not jobs you would apply to, the
  weights in `score.py` need adjusting — the model is doing what it was asked.

- [ ] **Step 6: Commit**

```bash
git add scripts/vendor_model.py pyproject.toml vercel.json .gitignore
git commit -m "feat: vendor the embedding model into the Vercel build"
```

---

## Done criteria

Phase 2 is done when, against production data:

- Every live job has a row in `scores` within 15 minutes of ingestion.
- The top 50 by `total_score` carry a `relevance_verdict` and a rationale that
  names something specific.
- Daily `llm_spend` is under $0.20.
- `cached_input_tokens` is non-zero — caching is actually engaged, not
  silently no-opping.
- Every company has a `sharia_verdict`, and every `excluded` one has been read
  by a human at least once.

The number that decides Phase 3's shape:

```sql
SELECT count(*) FROM scores WHERE relevance_verdict = 'strong';
```

If that is 5–10 per day, the UI is a queue. If it is 60, the ranking needs work
before a UI is worth building — a queue of sixty is not a queue.
