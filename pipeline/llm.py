"""The only module that talks to Anthropic.

Every call goes through here so the daily spend ceiling cannot be bypassed by
adding a call site somewhere else.

Three model-specific rules, taken from the API documentation rather than
memory, each of which fails quietly if got wrong:

1. Haiku 4.5's minimum cacheable prefix is 4096 tokens (Sonnet 5's is 1024).
   Below it, caching does nothing at all -- no error, no warning,
   cache_creation_input_tokens simply returns 0.
2. `effort` / `output_config` must not be sent to Haiku 4.5; it errors.
3. Thinking stays off for classification. On Haiku 4.5 that means omitting the
   parameter entirely -- `{"type": "adaptive"}` is a 4.6-and-later shape.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import anthropic
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from pipeline.config import Settings

log = logging.getLogger(__name__)


# USD per million tokens: (input, output). Verified 2026-08-01.
# Sonnet 5 carries an introductory rate of $2/$10 through 2026-08-31; the
# standard rate is used here so the ceiling never under-counts.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
}

# Minimum prefix length for prompt caching to engage at all. Not monotonic
# across generations -- Haiku 4.5 needs four times what Sonnet 5 does.
MIN_CACHEABLE_TOKENS = {
    "claude-haiku-4-5": 4096,
    "claude-sonnet-5": 1024,
}

_CACHE_READ_MULTIPLIER = Decimal("0.1")
_CACHE_WRITE_MULTIPLIER = Decimal("1.25")
_MILLION = Decimal("1000000")


class SpendCapExceeded(Exception):
    """The daily ceiling is reached. Raised rather than proceeding."""


class ApiKeyMissing(Exception):
    """No ANTHROPIC_API_KEY. Raised at the call site, not at settings load."""


def cache_prefix_is_effective(model: str, prefix_tokens: int) -> bool:
    """Whether a prefix of this size will actually be cached on this model.

    An unknown model is reported as uncacheable: claiming a saving that is not
    happening is worse than skipping the optimisation.
    """
    minimum = MIN_CACHEABLE_TOKENS.get(model)
    return minimum is not None and prefix_tokens >= minimum


def cost_of(model: str, usage: Any) -> Decimal:
    """Exact cost for one call. Decimal throughout -- never float for money."""
    input_rate, output_rate = PRICING.get(model, (Decimal("0"), Decimal("0")))

    plain = Decimal(getattr(usage, "input_tokens", 0) or 0)
    cached_read = Decimal(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = Decimal(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    output = Decimal(getattr(usage, "output_tokens", 0) or 0)

    total = (
        plain * input_rate
        + cached_read * input_rate * _CACHE_READ_MULTIPLIER
        + cache_write * input_rate * _CACHE_WRITE_MULTIPLIER
        + output * output_rate
    ) / _MILLION
    return total.quantize(Decimal("0.000001"))


async def spend_today(conn: AsyncConnection) -> Decimal:
    """Today's spend, in UTC, read from the table.

    Deliberately not an in-process counter: a serverless instance is recreated
    constantly, and a tally that resets on every cold start is no ceiling.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT COALESCE(sum(cost_usd), 0) AS total FROM llm_spend"
            " WHERE day = (now() AT TIME ZONE 'UTC')::date"
        )
        return (await cur.fetchone())["total"]


async def record_spend(conn: AsyncConnection, *, model: str, purpose: str, usage: Any) -> Decimal:
    cost = cost_of(model, usage)
    await conn.execute(
        "INSERT INTO llm_spend (model, purpose, input_tokens, cached_input_tokens,"
        " cache_write_tokens, output_tokens, cost_usd)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            model,
            purpose,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            cost,
        ),
    )
    return cost


def _warn_if_caching_is_not_working(
    usage: Any, *, model: str, purpose: str, system: list[dict[str, Any]]
) -> None:
    """Warn only when caching is genuinely doing nothing.

    A successful cache READ reports cache_creation_input_tokens = 0 -- only the
    write is non-zero. Checking creation alone warned on nine calls out of ten
    while caching was working perfectly, which is how a warning becomes noise
    that gets ignored. Both counters must be zero for caching to have failed.
    """
    if not any("cache_control" in block for block in system):
        return

    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    if written or read:
        return

    log.warning(
        "%s: cache_control was set but nothing was written to or read from cache — "
        "the prefix is probably below %s's %s-token minimum",
        purpose,
        model,
        MIN_CACHEABLE_TOKENS.get(model, "?"),
    )


async def assert_under_cap(conn: AsyncConnection, settings: Settings) -> None:
    spent = await spend_today(conn)
    if spent >= settings.daily_llm_cap_usd:
        raise SpendCapExceeded(f"daily LLM cap reached: ${spent} >= ${settings.daily_llm_cap_usd}")


async def call_structured[T: BaseModel](
    conn: AsyncConnection,
    *,
    model: str,
    purpose: str,
    system: list[dict[str, Any]],
    user: str,
    output_format: type[T],
    settings: Settings,
    max_tokens: int = 1024,
) -> tuple[T, Any]:
    """One structured call, billed and recorded.

    `system` is a list of content blocks so the caller can mark the static
    prefix with `cache_control`. No `thinking` and no `output_config` are sent:
    this is classification, and Haiku 4.5 rejects `output_config` outright.
    """
    if not settings.anthropic_api_key:
        raise ApiKeyMissing(
            f"{purpose}: ANTHROPIC_API_KEY is not set. Discovery runs without it; "
            "scoring and judging do not."
        )

    await assert_under_cap(conn, settings)

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=output_format,
    )

    await record_spend(conn, model=model, purpose=purpose, usage=response.usage)

    _warn_if_caching_is_not_working(response.usage, model=model, purpose=purpose, system=system)

    return response.parsed_output, response.usage
