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
