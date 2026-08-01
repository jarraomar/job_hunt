# job_hunt

Automated job discovery and application preparation. Finds relevant engineering
roles daily, filters them against hard criteria, and (from Phase 2) drafts a
tailored résumé and cover letter for the best matches — which a human reviews
and submits by hand.

**It is a discovery-and-preparation engine, not an auto-submit bot.** No process
here ever authenticates as you on any job platform. That constraint is enforced
structurally: there is no browser in the dependency tree, and function instances
have no writable disk on which a session could persist. See spec §3.

- Design: [`docs/superpowers/specs/2026-07-31-job-hunt-system-design.md`](docs/superpowers/specs/2026-07-31-job-hunt-system-design.md)
- Phase 1 plan: [`docs/superpowers/plans/2026-07-31-phase-1-discovery-pipeline.md`](docs/superpowers/plans/2026-07-31-phase-1-discovery-pipeline.md)

## Status

Phase 1 (discovery) is complete: five sources, deduplication, deterministic
pre-filtering, and persistence, with a wall-clock-bounded runner. No LLM calls,
no document generation, no web UI yet.

## Setup

Requires Python 3.14 and PostgreSQL 16.

```bash
brew install postgresql@16
./scripts/dev_db.sh up                  # local cluster on port 5433

python3 -m venv venv
./venv/bin/pip install -e ".[dev]"

mkdir -p profile
cp profile.example/targets.yaml profile/targets.yaml   # then edit
```

Configuration comes from the real environment — nothing loads a dotenv file.
Only `DATABASE_URL` is required to run Phase 1:

```bash
export DATABASE_URL="postgresql://jobhunt@localhost:5433/jobhunt_dev"
```

[`docs/environment.md`](docs/environment.md) is the full reference: every
variable, where its value comes from, and the `vercel env add` / `gh secret set`
commands that provision the deployed stores without touching a dashboard.

`profile/` is gitignored. It holds personal data — address, phone, work
authorization, salary expectations — and must never be committed.

## Run

```bash
./venv/bin/python scripts/migrate.py

# everything
./venv/bin/python -m pipeline.run_discover

# a subset, verbosely, without writing anything
./venv/bin/python -m pipeline.run_discover --sources greenhouse,lever --dry-run -v

# stop after N seconds, as the cron invocation will
./venv/bin/python -m pipeline.run_discover --budget 60
```

`--dry-run` executes the real write path inside a transaction that is always
rolled back, so it exercises the same code as a live run rather than a parallel
one that could drift.

## Test

```bash
./scripts/dev_db.sh up
./venv/bin/pytest
./venv/bin/ruff check . && ./venv/bin/ruff format --check .
```

Tests run against a real PostgreSQL rather than a stub: `FOR UPDATE SKIP
LOCKED`, partial unique indexes, `ON CONFLICT`, and `CHECK` constraints have no
equivalent in a fake. No test touches the network — source adapters run against
fixtures captured from live endpoints by `scripts/capture_fixture.py`.

**Fixtures are captured, never hand-written.** A hand-written fixture encodes a
guess about an API's shape and the tests then verify the guess rather than
reality. When a captured fixture disagrees with an adapter, the fixture wins.

## Sources

| Source | Tokens needed | Notes |
|---|---|---|
| Greenhouse | yes | Whole board in one response; `first_published` is the posting date |
| Lever | yes | Bare JSON array; `createdAt` is epoch **milliseconds** |
| Ashby | yes | Structured compensation, but per-employer opt-in (Ramp 95%, Notion 0%) |
| Remotive | no | Filter parameters are non-functional; capped at 4 fetches/day by their terms |
| HN "Who is hiring?" | no | Free-text comments; ~79% parse, refuses rather than guessing |

Adding a source is one module plus one registry entry.

## Checking the result

The number that decides whether Phase 2 is worth building:

```sql
SELECT count(*) FROM jobs WHERE filtered_out = false;
```

Against ten real boards this yields ~800 live roles from ~5,200 ingested — an
80% pre-filter kill rate, in line with spec §8's ~70% target.

`run_log` records every pass, including `budget_hit` when a run returned on its
wall-clock budget with work outstanding. A missing `run_log` row is the only
evidence that a scheduled run was never delivered.
