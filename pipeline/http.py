"""The single outbound HTTP path for the whole pipeline.

Everything in spec section 7 lives here: per-host rate limiting with jitter,
conditional requests via stored ETags, exponential backoff on 429/503, and a
hard stop on 403 so we never retry into a block.

A bare httpx call anywhere else in the pipeline is a defect. Politeness is only
a guarantee if there is exactly one way out.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from psycopg import AsyncConnection
from psycopg.rows import dict_row

DEFAULT_DELAY = 1.0
WORKDAY_DELAY = 2.0
MAX_RETRIES = 3
JITTER = 0.3

# Beyond this we are being told to go away for longer than a run is worth.
MAX_RETRY_AFTER = 120.0

_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class HostBlockedError(Exception):
    """Raised when a host returns 403. We stop rather than retry into a block."""

    def __init__(self, host: str) -> None:
        super().__init__(f"host refused requests (403): {host}")
        self.host = host


class PoliteSession:
    """Rate-limited, conditional-request HTTP client.

    `sleep` is injected so tests can assert on delays without waiting, and so a
    run can be given a budget-aware sleeper later.
    """

    def __init__(
        self,
        user_agent: str,
        conn: AsyncConnection | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        default_delay: float = DEFAULT_DELAY,
        host_delays: dict[str, float] | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=True,
        )
        self._conn = conn
        self._sleep = sleep
        self._default_delay = default_delay
        self._host_delays = host_delays or {}
        self._last_hit: dict[str, float] = {}
        # One lock per host. This is what makes the rate limit survive
        # concurrency: without it, coroutines racing through the gate all read
        # _last_hit before any writes it and none of them waits.
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.blocked_hosts: set[str] = set()

    async def __aenter__(self) -> PoliteSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def delay_for(self, host: str) -> float:
        if host in self._host_delays:
            return self._host_delays[host]
        # Not configurable away: Workday rate-limits by source IP across every
        # tenant, so one misconfigured run poisons all of them (spec section 7).
        if ".myworkdayjobs.com" in host:
            return WORKDAY_DELAY
        return self._default_delay

    async def _wait_turn(self, host: str) -> None:
        """Hold this host's turnstile until the inter-request delay has elapsed.

        The lock is released once the wait is done rather than held across the
        request: the constraint is request *rate*, not concurrency, so there is
        no reason to make a slow response block the next scheduled one.
        """
        async with self._locks[host]:
            base = self.delay_for(host)
            elapsed = time.monotonic() - self._last_hit.get(host, -float("inf"))
            wait = base - elapsed
            if wait > 0:
                await self._sleep(wait * (1 + random.uniform(-JITTER, JITTER)))
            self._last_hit[host] = time.monotonic()

    async def _cached_validators(self, url: str) -> dict[str, str]:
        if self._conn is None:
            return {}
        # Pinned, not inherited: the CLI and the pooled app build connections
        # differently, and reading by name off a tuple-returning one raises
        # only on the path the fixtures do not cover.
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT etag, last_modified FROM http_cache WHERE url = %s", (url,))
            row = await cur.fetchone()
        if row is None:
            return {}
        headers: dict[str, str] = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    async def _store_validators(self, url: str, response: httpx.Response) -> None:
        if self._conn is None:
            return
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if not etag and not last_modified:
            return
        await self._conn.execute(
            "INSERT INTO http_cache (url, etag, last_modified, fetched_at)"
            " VALUES (%s, %s, %s, now())"
            " ON CONFLICT (url) DO UPDATE SET etag = excluded.etag,"
            " last_modified = excluded.last_modified, fetched_at = excluded.fetched_at",
            (url, etag, last_modified),
        )

    def _backoff_for(self, response: httpx.Response, attempt: int) -> float:
        """Prefer the server's own Retry-After over our guess.

        Guessing a shorter wait than the server asked for is the fastest way to
        turn a soft rate-limit into a hard block.
        """
        header = response.headers.get("Retry-After")
        if header:
            try:
                requested = float(header)
            except ValueError:
                requested = 0.0  # HTTP-date form; fall back to our own schedule
            if requested > 0:
                return min(requested, MAX_RETRY_AFTER)
        return (2**attempt) * self._default_delay

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any | None:
        host = urlsplit(url).netloc
        if host in self.blocked_hosts:
            raise HostBlockedError(host)

        headers = dict(kwargs.pop("headers", None) or {})
        if method == "GET":
            headers.update(await self._cached_validators(url))

        for attempt in range(MAX_RETRIES):
            await self._wait_turn(host)
            response = await self._client.request(method, url, headers=headers, **kwargs)

            if response.status_code == 403:
                self.blocked_hosts.add(host)
                raise HostBlockedError(host)
            if response.status_code == 304:
                return None
            if response.status_code in _RETRYABLE:
                if attempt == MAX_RETRIES - 1:
                    response.raise_for_status()
                backoff = self._backoff_for(response, attempt)
                await self._sleep(backoff * (1 + random.uniform(0, JITTER)))
                continue

            response.raise_for_status()
            if method == "GET":
                await self._store_validators(url, response)
            return response.json()

        return None

    async def get_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> Any | None:
        """GET and parse JSON. Returns None on 304 (content unchanged)."""
        return await self._request("GET", url, params=params, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()
