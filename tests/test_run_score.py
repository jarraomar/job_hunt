from datetime import UTC, datetime, timedelta

import pytest

from pipeline.config import load_settings
from pipeline.judge import Relevance
from pipeline.profile import Profile
from pipeline.run_score import ScoreStats, run

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

PROFILE = Profile(
    resume={"summary": "Full-stack engineer. Python, React, AWS."},
    competency_bullets=[],
    identity={},
)


def settings(**env):
    return load_settings(
        env={"DATABASE_URL": "postgresql://x/y", "ANTHROPIC_API_KEY": "sk-test", **env}
    )


async def _company(db, name: str) -> int:
    cur = await db.execute(
        "INSERT INTO companies (name, normalized_name) VALUES (%s, %s)"
        " ON CONFLICT (normalized_name) DO UPDATE SET name = EXCLUDED.name"
        " RETURNING company_id",
        (name, name.lower()),
    )
    return (await cur.fetchone())["company_id"]


async def _seed(db, n: int, *, company="acme", filtered=False, start=0) -> list[int]:
    company_id = await _company(db, company)
    ids = []
    for i in range(start, start + n):
        cur = await db.execute(
            "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
            " description, apply_url, first_seen_at, last_seen_at, posted_at,"
            " filtered_out, filter_reason)"
            " VALUES (%s, %s, 'greenhouse', %s, %s, 'Python React AWS engineer.',"
            " 'https://x', now(), now(), %s, %s, %s) RETURNING job_id",
            (
                f"{company}{i:056d}",
                company_id,
                f"{company}-{i}",
                f"Backend Engineer {i}",
                NOW - timedelta(hours=i),
                filtered,
                "title_not_target" if filtered else None,
            ),
        )
        ids.append((await cur.fetchone())["job_id"])
    return ids


def _fake_judge(record: list | None = None):
    async def judge(conn, job, profile, s):
        if record is not None:
            record.append(job.title)
        return Relevance(verdict="strong", score=0.9, rationale="Match.")

    return judge


@pytest.fixture(autouse=True)
def _profile(monkeypatch):
    monkeypatch.setattr("pipeline.run_score.load_profile", lambda s: PROFILE)


@pytest.fixture(autouse=True)
def _no_sharia_calls(monkeypatch):
    async def allow(conn, company_id, name, description, s, **kw):
        return "allowed"

    monkeypatch.setattr("pipeline.run_score.screen_company", allow)


# --- scoring pass ------------------------------------------------------------


async def test_scores_every_unscored_live_job(db):
    await _seed(db, 5)
    stats = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    assert stats.scored == 5
    cur = await db.execute("SELECT count(*) AS n FROM scores")
    assert (await cur.fetchone())["n"] == 5


async def test_filtered_out_jobs_are_never_scored(db):
    """A filtered job carrying a score would surface in the queue, which is the
    one thing the pre-filter exists to prevent."""
    await _seed(db, 3, filtered=True)
    stats = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    assert stats.scored == 0


async def test_running_twice_does_not_rescore(db):
    await _seed(db, 4)
    first = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    second = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    assert first.scored == 4
    assert second.scored == 0


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


# --- judging pass ------------------------------------------------------------


async def test_judging_is_capped_at_the_daily_limit(db, monkeypatch):
    """Judging all ~800 daily jobs costs about $4/day against a ~$5/month budget."""
    calls: list[str] = []
    monkeypatch.setattr("pipeline.run_score.judge_job", _fake_judge(calls))
    await _seed(db, 10)
    stats = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="3"), now=NOW)
    assert stats.judged == 3
    assert len(calls) == 3


async def test_judging_takes_the_highest_scoring_jobs_not_the_first_seen(db, monkeypatch):
    """Judging in arrival order would spend the budget on the wrong N jobs."""
    judged: list[str] = []
    monkeypatch.setattr("pipeline.run_score.judge_job", _fake_judge(judged))
    await _seed(db, 6)
    await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="2"), now=NOW)

    cur = await db.execute(
        "SELECT j.title FROM scores s JOIN jobs j USING (job_id)"
        " WHERE s.judged_at IS NOT NULL ORDER BY s.total_score DESC"
    )
    assert set(judged) == {r["title"] for r in await cur.fetchall()}


async def test_a_judged_job_is_not_judged_again(db, monkeypatch):
    monkeypatch.setattr("pipeline.run_score.judge_job", _fake_judge())
    await _seed(db, 3)
    await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="3"), now=NOW)
    second = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="3"), now=NOW)
    assert second.judged == 0


async def test_judging_is_capped_per_company(db, monkeypatch):
    """One employer supplied 23% of live postings and 17 of the top 25.

    Without a cap the judge budget is spent re-reading near-duplicate roles at
    a single company, and the queue reads as though nobody else is hiring.
    """
    judged: list[str] = []
    monkeypatch.setattr("pipeline.run_score.judge_job", _fake_judge(judged))
    await _seed(db, 8, company="bigco")
    await _seed(db, 2, company="smallco")

    await run(
        db,
        settings(JOBHUNT_DAILY_JUDGE_LIMIT="10", JOBHUNT_JUDGE_PER_COMPANY_CAP="2"),
        now=NOW,
    )
    cur = await db.execute(
        "SELECT c.normalized_name AS n, count(*) AS k FROM scores s"
        " JOIN jobs j USING (job_id) JOIN companies c USING (company_id)"
        " WHERE s.judged_at IS NOT NULL GROUP BY 1"
    )
    per_company = {r["n"]: r["k"] for r in await cur.fetchall()}
    assert per_company.get("bigco", 0) <= 2
    assert per_company.get("smallco", 0) <= 2


async def test_a_score_survives_being_judged(db, monkeypatch):
    """The judged upsert must not clobber the components already computed."""
    monkeypatch.setattr("pipeline.run_score.judge_job", _fake_judge())
    await _seed(db, 1)
    await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="1"), now=NOW)
    cur = await db.execute(
        "SELECT embed_similarity, rule_score, total_score, relevance_verdict, model FROM scores"
    )
    row = await cur.fetchone()
    assert row["embed_similarity"] is not None
    assert row["rule_score"] is not None
    assert row["total_score"] > 0
    assert row["relevance_verdict"] == "strong"
    assert row["model"] == "claude-haiku-4-5"


async def test_a_rescore_does_not_erase_a_paid_for_judgement(db, monkeypatch):
    monkeypatch.setattr("pipeline.run_score.judge_job", _fake_judge())
    job_ids = await _seed(db, 1)
    await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="1"), now=NOW)

    # Force a re-score of the same job.
    await db.execute("DELETE FROM scores WHERE false")
    from pipeline.score import ScoreComponents
    from pipeline.store import upsert_score

    await upsert_score(
        db,
        job_ids[0],
        ScoreComponents(
            embed_similarity=0.1,
            rule_score=0.1,
            freshness_score=0.1,
            total_score=0.1,
            is_stretch=False,
        ),
    )
    cur = await db.execute("SELECT relevance_verdict, judged_at FROM scores")
    row = await cur.fetchone()
    assert row["relevance_verdict"] == "strong"
    assert row["judged_at"] is not None


async def test_an_excluded_company_is_never_judged(db, monkeypatch):
    judged: list[str] = []
    monkeypatch.setattr("pipeline.run_score.judge_job", _fake_judge(judged))

    async def exclude(conn, company_id, name, description, s, **kw):
        return "excluded"

    monkeypatch.setattr("pipeline.run_score.screen_company", exclude)
    await _seed(db, 3)
    stats = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="3"), now=NOW)
    assert judged == []
    assert stats.judged == 0


async def test_the_spend_cap_stops_judging_without_losing_scores(db, monkeypatch):
    async def capped(conn, job, profile, s):
        return None

    monkeypatch.setattr("pipeline.run_score.judge_job", capped)
    await _seed(db, 5)
    stats = await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="5"), now=NOW)
    assert stats.judged == 0
    assert stats.scored == 5


# --- budget and bookkeeping --------------------------------------------------


async def test_an_expired_budget_returns_cleanly(db):
    await _seed(db, 20)
    stats = await run(db, settings(), budget_seconds=0.0, now=NOW)
    assert stats.budget_hit is True
    assert stats.scored == 0


async def test_a_run_log_row_is_written(db):
    await _seed(db, 2)
    await run(db, settings(JOBHUNT_DAILY_JUDGE_LIMIT="0"), now=NOW)
    cur = await db.execute("SELECT count(*) AS n FROM run_log WHERE finished_at IS NOT NULL")
    assert (await cur.fetchone())["n"] == 1


async def test_an_empty_database_is_not_an_error(db):
    stats = await run(db, settings(), now=NOW)
    assert stats == ScoreStats(scored=0, judged=0, screened=0, errors=0, **_zeros(stats))


def _zeros(stats):
    # duration varies; compare everything else exactly.
    return {
        "spend_usd": stats.spend_usd,
        "budget_hit": stats.budget_hit,
        "duration_ms": stats.duration_ms,
    }


def test_stats_defaults_are_zero():
    stats = ScoreStats()
    assert (stats.scored, stats.judged, stats.errors) == (0, 0, 0)
