import json
from pathlib import Path

import pytest

from pipeline.config import load_settings
from pipeline.http import PoliteSession
from pipeline.sources.base import SourceConfig
from pipeline.sources.hn_algolia import HNAlgoliaSource, parse_hn_comment

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hn_hiring_comments.json"
BY_DATE = (
    "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=20"
)
STORY_ID = "48747976"


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture
def cfg():
    return SourceConfig(
        session=PoliteSession("ua/1.0", sleep=_noop_sleep),
        targets={},
        settings=load_settings(env={"DATABASE_URL": "postgresql://x/y"}),
    )


def _comments_url(story_id=STORY_ID, page=0):
    return (
        f"https://hn.algolia.com/api/v1/search?tags=comment,story_{story_id}"
        f"&hitsPerPage=1000&page={page}"
    )


def _thread(*titles):
    return {
        "hits": [
            {"objectID": f"{9000 + i}", "title": t, "num_comments": 100}
            for i, t in enumerate(titles)
        ]
    }


def _comment(text, object_id="1", story_id=STORY_ID, parent_id=None):
    return {
        "objectID": object_id,
        "comment_text": text,
        "story_id": story_id,
        "parent_id": parent_id if parent_id is not None else story_id,
        "created_at": "2026-07-01T16:00:00Z",
    }


async def _collect(cfg):
    return [job async for job in HNAlgoliaSource().fetch(cfg)]


# --- parse_hn_comment: a pure function, so most cases need no network --------


def test_parses_the_standard_header():
    job = parse_hn_comment(
        _comment("Portless | AI Engineer | Remote (North America) | $180k-$230k | Full-time")
    )
    assert job.company_name == "Portless"
    assert job.title == "AI Engineer"
    assert job.location == "Remote (North America)"
    assert job.apply_url == "https://news.ycombinator.com/item?id=1"


def test_company_is_taken_by_position_not_by_content():
    """Detecting the company by content misfires on AI-suffixed names.

    "TypeSafe AI" and "Cora AI" both read as role text, which pushed a working
    prototype from 79% yield down to 69% and silently dropped real employers.
    """
    job = parse_hn_comment(
        _comment("TypeSafe AI | AI research & engineering | San Francisco, CA | ONSITE | Full-time")
    )
    assert job.company_name == "TypeSafe AI"
    assert job.title == "AI research & engineering"


def test_strips_a_url_parenthetical_from_the_company():
    job = parse_hn_comment(
        _comment("Chronograph ( https://chronograph.pe ) | Platform Engineer | Remote (US)")
    )
    assert job.company_name == "Chronograph"


def test_strips_markdown_emphasis():
    job = parse_hn_comment(_comment("*OneChronos | Technical Lead | NYC (HQ) | Full-Time*"))
    assert job.company_name == "OneChronos"


def test_skips_a_post_with_no_role_in_the_header():
    assert parse_hn_comment(_comment("Marketron | REMOTE (US) | Full-time | 70k - 90k")) is None


def test_skips_a_post_with_no_company():
    # Here the first segment is the role, so no employer was named. The Sharia
    # screen is per-company (spec section 9), so this can never clear it.
    assert (
        parse_hn_comment(_comment("Engineering Manager | Remote (North America) | $180k")) is None
    )


def test_skips_non_pipe_formats_rather_than_guessing():
    assert (
        parse_hn_comment(_comment("Proton:Senior Foundation Engineer (Drive):Geneva, ONSITE"))
        is None
    )
    assert parse_hn_comment(_comment("This book was one of the most useful C++ resources")) is None


def test_employment_type_alone_is_not_a_title():
    assert parse_hn_comment(_comment("Acme | Full-time | Senior Roles | India")) is None


def test_reads_only_the_first_paragraph_as_the_header():
    job = parse_hn_comment(
        _comment("Acme | Backend Engineer | Remote<p>We are a great place | with pipes | in prose")
    )
    assert job.title == "Backend Engineer"


def test_unescapes_entities_in_the_description():
    job = parse_hn_comment(
        _comment("Acme | Backend Engineer | Remote<p>Stack is React&#x2F;Node &amp; Python")
    )
    assert "React/Node & Python" in job.description
    assert "&#x2F;" not in job.description


def test_arrangement_hint_comes_from_the_location_segment():
    assert parse_hn_comment(_comment("Acme | Engineer | ONSITE")).remote_type_hint == "ONSITE"
    assert parse_hn_comment(_comment("Acme | Engineer | Remote (US)")).remote_type_hint == (
        "Remote (US)"
    )


def test_rejects_an_absurdly_long_company_segment():
    long_name = "x" * 80
    assert parse_hn_comment(_comment(f"{long_name} | Backend Engineer | Remote")) is None


def test_parses_every_top_level_comment_in_the_captured_fixture():
    payload = json.loads(FIXTURE.read_text())
    top = [h for h in payload["hits"] if h.get("parent_id") == h.get("story_id")]
    parsed = [parse_hn_comment(h) for h in top]
    # Whatever parses must be well-formed; unparseable posts return None.
    for job in filter(None, parsed):
        assert job.company_name and job.title and job.apply_url.startswith("http")


# --- fetch: thread selection and reply filtering -----------------------------


async def test_picks_the_hiring_thread_not_the_job_seekers_thread(httpx_mock, cfg):
    """whoishiring posts both on the same day.

    "Who wants to be hired?" is candidates advertising themselves. Ingesting it
    fills the queue with people rather than jobs.
    """
    httpx_mock.add_response(
        url=BY_DATE,
        json={
            "hits": [
                {"objectID": "111", "title": "Ask HN: Who wants to be hired? (July 2026)"},
                {"objectID": STORY_ID, "title": "Ask HN: Who is hiring? (July 2026)"},
            ]
        },
    )
    httpx_mock.add_response(
        url=_comments_url(),
        json={"hits": [_comment("Acme | Backend Engineer | Remote")], "nbPages": 1},
    )
    jobs = await _collect(cfg)
    assert [j.company_name for j in jobs] == ["Acme"]


async def test_skips_the_freelancer_thread(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BY_DATE,
        json=_thread("Ask HN: Freelancer? Seeking freelancer? (July 2026)"),
    )
    assert await _collect(cfg) == []


async def test_replies_are_not_treated_as_job_posts(httpx_mock, cfg):
    """Exactly half the comments on the measured thread were replies."""
    httpx_mock.add_response(
        url=BY_DATE, json={"hits": [{"objectID": STORY_ID, "title": "Ask HN: Who is hiring?"}]}
    )
    httpx_mock.add_response(
        url=_comments_url(),
        json={
            "hits": [
                _comment("Acme | Backend Engineer | Remote", object_id="1"),
                _comment("Globex | Frontend Engineer | Remote", object_id="2", parent_id=99999),
            ],
            "nbPages": 1,
        },
    )
    jobs = await _collect(cfg)
    assert [j.source_job_id for j in jobs] == ["1"]


async def test_no_hiring_thread_found_yields_nothing(httpx_mock, cfg):
    httpx_mock.add_response(url=BY_DATE, json={"hits": []})
    assert await _collect(cfg) == []


async def test_paginates_until_the_last_page(httpx_mock, cfg):
    httpx_mock.add_response(
        url=BY_DATE, json={"hits": [{"objectID": STORY_ID, "title": "Ask HN: Who is hiring?"}]}
    )
    httpx_mock.add_response(
        url=_comments_url(page=0),
        json={"hits": [_comment("A | Backend Engineer | Remote", object_id="1")], "nbPages": 2},
    )
    httpx_mock.add_response(
        url=_comments_url(page=1),
        json={"hits": [_comment("B | Frontend Engineer | Remote", object_id="2")], "nbPages": 2},
    )
    jobs = await _collect(cfg)
    assert [j.source_job_id for j in jobs] == ["1", "2"]


async def test_blocked_host_stops_the_source(httpx_mock, cfg):
    httpx_mock.add_response(url=BY_DATE, status_code=403)
    assert await _collect(cfg) == []


def test_strips_domain_parentheticals_with_any_tld():
    # A hardcoded TLD list left "Chronograph (chronograph.pe)" unstripped in a
    # live run; country-code TLDs are too common to enumerate.
    for header in (
        "Chronograph (chronograph.pe) | Platform Engineer | Remote (US)",
        "Chronograph ( https://chronograph.pe ) | Platform Engineer | Remote (US)",
        "Chronograph (chronograph.co.uk/careers) | Platform Engineer | Remote",
    ):
        assert parse_hn_comment(_comment(header)).company_name == "Chronograph"


def test_does_not_strip_a_meaningful_parenthetical_from_the_company():
    job = parse_hn_comment(_comment("Acme (YC W21) | Backend Engineer | Remote"))
    assert job.company_name == "Acme (YC W21)"
