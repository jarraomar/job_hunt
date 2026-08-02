import subprocess
from pathlib import Path

import pytest

from web import ago, money, pct


async def test_static_css_is_served(web_db, client):
    response = await client.get("/static/app.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


async def test_htmx_is_served(web_db, client):
    assert (await client.get("/static/htmx.min.js")).status_code == 200


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
    assert "cdn.tailwindcss.com" not in Path("web/templates/base.html").read_text()


def test_committed_css_is_current():
    """A stale stylesheet fails silently — the page just loses its styling,
    weeks after the template change that caused it."""
    css = Path("web/static/app.css")
    if not css.exists():
        pytest.fail("run ./scripts/build_css.sh")
    before = css.read_bytes()
    subprocess.run(["./scripts/build_css.sh"], check=True, capture_output=True)
    assert css.read_bytes() == before, (
        "web/static/app.css is stale — run ./scripts/build_css.sh and commit"
    )


@pytest.mark.parametrize(
    "hours,expected",
    [(0.5, "just now"), (3, "3h ago"), (30, "1d ago"), (24 * 9, "9d ago")],
)
def test_ago_filter(hours, expected):
    from datetime import UTC, datetime, timedelta

    assert ago(datetime.now(UTC) - timedelta(hours=hours)) == expected


def test_ago_handles_none():
    # HN and parts of Lever omit posted_at; the template must not crash on it.
    assert ago(None) == "unknown"


def test_ago_handles_clock_skew():
    from datetime import UTC, datetime, timedelta

    # A board a couple of hours ahead of us reads as "now", not as the future.
    assert ago(datetime.now(UTC) + timedelta(hours=2)) == "just now"


@pytest.mark.parametrize(
    "low,high,expected",
    [
        (150_000, 180_000, "$150k–$180k"),
        (150_000, None, "$150k+"),
        (None, 180_000, "up to $180k"),
        (150_000, 150_000, "$150k"),
        (None, None, "not listed"),
    ],
)
def test_money_filter(low, high, expected):
    assert money(low, high) == expected


def test_pct_handles_none():
    assert pct(None) == "—"
    assert pct(0.842) == "84%"
