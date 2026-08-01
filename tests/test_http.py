import asyncio
import time

import httpx
import pytest

from pipeline.http import MAX_RETRIES, HostBlockedError, PoliteSession


@pytest.fixture
def recorder():
    """An async sleep that records durations instead of waiting."""
    calls: list[float] = []

    async def sleeper(duration: float) -> None:
        calls.append(duration)

    return calls, sleeper


async def test_rate_limits_per_host(httpx_mock, recorder):
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/1", json={"ok": 1})
    httpx_mock.add_response(url="https://a.test/2", json={"ok": 2})
    async with PoliteSession("ua/1.0", sleep=sleeper, default_delay=1.0) as s:
        await s.get_json("https://a.test/1")
        await s.get_json("https://a.test/2")
    assert any(d > 0 for d in slept), "second same-host request should be delayed"


async def test_does_not_delay_across_different_hosts(httpx_mock, recorder):
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/1", json={})
    httpx_mock.add_response(url="https://b.test/1", json={})
    async with PoliteSession("ua/1.0", sleep=sleeper, default_delay=1.0) as s:
        await s.get_json("https://a.test/1")
        await s.get_json("https://b.test/1")
    assert not [d for d in slept if d > 0]


async def test_concurrent_same_host_requests_are_serialized(httpx_mock, monkeypatch):
    """The guarantee that async breaks if the gate is not locked.

    Without a per-host lock, coroutines 2 and 3 both compute their wait from the
    same stale _last_hit, sleep concurrently, and fire together — so three
    requests cost one delay instead of two and the rate limit silently halves.

    This has to use real sleeps and real elapsed time. A fake sleeper that never
    yields to the event loop prevents the interleaving entirely, so the test
    would pass with or without the lock. Jitter is pinned to 0 so the locked and
    unlocked timings cannot overlap.
    """
    monkeypatch.setattr("pipeline.http.JITTER", 0.0)
    delay = 0.05
    for i in range(3):
        httpx_mock.add_response(url=f"https://a.test/{i}", json={"i": i})

    started = time.monotonic()
    async with PoliteSession("ua/1.0", default_delay=delay) as s:
        await asyncio.gather(*(s.get_json(f"https://a.test/{i}") for i in range(3)))
    elapsed = time.monotonic() - started

    # Serialized: two full gaps. Unlocked would land near one.
    assert elapsed >= delay * 1.8, f"rate limit collapsed under concurrency ({elapsed:.3f}s)"


async def test_concurrent_different_hosts_are_not_serialized(httpx_mock, monkeypatch):
    monkeypatch.setattr("pipeline.http.JITTER", 0.0)
    delay = 0.05
    for host in ("a", "b", "c"):
        httpx_mock.add_response(url=f"https://{host}.test/x", json={})

    started = time.monotonic()
    async with PoliteSession("ua/1.0", default_delay=delay) as s:
        await asyncio.gather(*(s.get_json(f"https://{h}.test/x") for h in ("a", "b", "c")))
    elapsed = time.monotonic() - started

    # Distinct hosts must not wait on each other, or the crawl loses the
    # parallelism that makes it fit inside one bounded invocation.
    assert elapsed < delay, f"different hosts blocked each other ({elapsed:.3f}s)"


def test_workday_uses_slower_lane():
    s = PoliteSession("ua/1.0", host_delays={"x.wd1.myworkdayjobs.com": 2.0})
    assert s.delay_for("x.wd1.myworkdayjobs.com") == 2.0
    assert s.delay_for("boards-api.greenhouse.io") == 1.0


def test_workday_slow_lane_applies_without_explicit_config():
    # Spec section 7: Workday rate-limits by source IP across all tenants, so
    # the slow lane must not depend on someone remembering to configure it.
    s = PoliteSession("ua/1.0")
    assert s.delay_for("acme.wd5.myworkdayjobs.com") == 2.0


async def test_403_raises_and_marks_host_blocked(httpx_mock):
    httpx_mock.add_response(url="https://blocked.test/x", status_code=403)
    async with PoliteSession("ua/1.0") as s:
        with pytest.raises(HostBlockedError):
            await s.get_json("https://blocked.test/x")
        assert "blocked.test" in s.blocked_hosts


async def test_blocked_host_is_not_retried(httpx_mock):
    httpx_mock.add_response(url="https://blocked.test/x", status_code=403)
    async with PoliteSession("ua/1.0") as s:
        with pytest.raises(HostBlockedError):
            await s.get_json("https://blocked.test/x")
        with pytest.raises(HostBlockedError):
            await s.get_json("https://blocked.test/y")
    # Only the first request ever left the process. Retrying into a block is
    # what turns a soft refusal into a durable one.
    assert len(httpx_mock.get_requests()) == 1


async def test_429_backs_off_then_succeeds(httpx_mock, recorder):
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/x", status_code=429)
    httpx_mock.add_response(url="https://a.test/x", json={"ok": True})
    async with PoliteSession("ua/1.0", sleep=sleeper) as s:
        assert await s.get_json("https://a.test/x") == {"ok": True}
    assert len(httpx_mock.get_requests()) == 2


async def test_429_honours_retry_after(httpx_mock, recorder):
    # When a server states how long to wait, guessing a shorter backoff is how
    # a soft rate-limit escalates into a block.
    slept, sleeper = recorder
    httpx_mock.add_response(url="https://a.test/x", status_code=429, headers={"Retry-After": "7"})
    httpx_mock.add_response(url="https://a.test/x", json={"ok": True})
    async with PoliteSession("ua/1.0", sleep=sleeper, default_delay=1.0) as s:
        await s.get_json("https://a.test/x")
    assert any(d >= 7 for d in slept)


async def test_gives_up_after_max_retries(httpx_mock, recorder):
    _, sleeper = recorder
    for _ in range(MAX_RETRIES):
        httpx_mock.add_response(url="https://a.test/x", status_code=503)
    async with PoliteSession("ua/1.0", sleep=sleeper) as s:
        with pytest.raises(httpx.HTTPStatusError):
            await s.get_json("https://a.test/x")
    # Exactly MAX_RETRIES attempts: a persistent 503 must not become an
    # unbounded hammer on a struggling host.
    assert len(httpx_mock.get_requests()) == MAX_RETRIES


async def test_sends_user_agent(httpx_mock):
    httpx_mock.add_response(url="https://a.test/x", json={})
    async with PoliteSession("jobhunt/0.1 (contact)") as s:
        await s.get_json("https://a.test/x")
    assert httpx_mock.get_requests()[0].headers["user-agent"] == "jobhunt/0.1 (contact)"


async def test_sends_etag_on_second_request_and_returns_none_on_304(httpx_mock, db):
    httpx_mock.add_response(url="https://a.test/x", json={"v": 1}, headers={"ETag": 'W/"abc"'})
    httpx_mock.add_response(url="https://a.test/x", status_code=304)

    async with PoliteSession("ua/1.0", conn=db) as s:
        assert await s.get_json("https://a.test/x") == {"v": 1}
        assert await s.get_json("https://a.test/x") is None

    assert httpx_mock.get_requests()[1].headers["if-none-match"] == 'W/"abc"'


async def test_sends_if_modified_since_when_only_last_modified_is_offered(httpx_mock, db):
    stamp = "Wed, 30 Jul 2026 12:00:00 GMT"
    httpx_mock.add_response(url="https://a.test/y", json={"v": 1}, headers={"Last-Modified": stamp})
    httpx_mock.add_response(url="https://a.test/y", status_code=304)

    async with PoliteSession("ua/1.0", conn=db) as s:
        await s.get_json("https://a.test/y")
        await s.get_json("https://a.test/y")

    assert httpx_mock.get_requests()[1].headers["if-modified-since"] == stamp


async def test_validators_are_upserted_not_duplicated(httpx_mock, db):
    httpx_mock.add_response(url="https://a.test/z", json={"v": 1}, headers={"ETag": '"one"'})
    httpx_mock.add_response(url="https://a.test/z", json={"v": 2}, headers={"ETag": '"two"'})

    async with PoliteSession("ua/1.0", conn=db) as s:
        await s.get_json("https://a.test/z")
        await s.get_json("https://a.test/z")

    cur = await db.execute("SELECT etag FROM http_cache WHERE url = 'https://a.test/z'")
    rows = await cur.fetchall()
    assert len(rows) == 1 and rows[0]["etag"] == '"two"'


async def test_works_without_a_connection(httpx_mock):
    # Fixture capture and ad-hoc probing run with no database at all.
    httpx_mock.add_response(url="https://a.test/x", json={"ok": True})
    async with PoliteSession("ua/1.0") as s:
        assert await s.get_json("https://a.test/x") == {"ok": True}
