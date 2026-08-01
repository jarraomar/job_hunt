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
