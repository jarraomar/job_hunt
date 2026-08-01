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
