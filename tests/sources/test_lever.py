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
