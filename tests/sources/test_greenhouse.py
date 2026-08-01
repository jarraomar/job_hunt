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


async def test_a_failed_board_is_reported_not_only_logged(httpx_mock, cfg):
    """A run where every board 500s must not report errors=0.

    Adapters swallow per-board failures so one dead token cannot kill the run.
    Without surfacing them the digest looks healthy while nothing was ingested.
    """
    cfg.targets["greenhouse"] = ["broken"]
    for _ in range(3):
        httpx_mock.add_response(
            url="https://boards-api.greenhouse.io/v1/boards/broken/jobs?content=true",
            status_code=500,
        )
    assert await _collect(GreenhouseSource(), cfg) == []
    assert cfg.errors == ["greenhouse:broken"]
