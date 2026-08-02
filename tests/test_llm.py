from decimal import Decimal

import pytest

from pipeline import llm
from pipeline.config import load_settings


class FakeUsage:
    def __init__(self, input_tokens=1000, output_tokens=200, cache_read=0, cache_write=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write


def settings(**env):
    return load_settings(env={"DATABASE_URL": "postgresql://x/y", **env})


# --- pricing and the ceiling -------------------------------------------------


def test_pricing_covers_every_model_we_use():
    assert "claude-haiku-4-5" in llm.PRICING
    assert "claude-sonnet-5" in llm.PRICING


def test_cost_uses_exact_arithmetic_not_float():
    cost = llm.cost_of("claude-haiku-4-5", FakeUsage(input_tokens=1_000_000, output_tokens=0))
    # $1.00 per MTok input on Haiku 4.5.
    assert cost == Decimal("1.000000")
    assert isinstance(cost, Decimal)


def test_cached_reads_are_billed_at_a_tenth():
    full = llm.cost_of("claude-haiku-4-5", FakeUsage(input_tokens=1_000_000, output_tokens=0))
    cached = llm.cost_of(
        "claude-haiku-4-5", FakeUsage(input_tokens=0, output_tokens=0, cache_read=1_000_000)
    )
    assert cached == full / 10


def test_cache_writes_cost_more_than_plain_input():
    # 1.25x. Caching only pays off from the second request onward.
    plain = llm.cost_of("claude-haiku-4-5", FakeUsage(input_tokens=1_000_000, output_tokens=0))
    write = llm.cost_of(
        "claude-haiku-4-5", FakeUsage(input_tokens=0, output_tokens=0, cache_write=1_000_000)
    )
    assert write > plain


def test_an_unknown_model_costs_nothing_rather_than_crashing_the_run():
    # A new model id should not take down a scoring pass; the spend row just
    # under-counts, and the model name in it makes that visible.
    assert llm.cost_of("claude-something-new", FakeUsage()) == Decimal("0.000000")


async def test_spend_today_starts_at_zero(db):
    assert await llm.spend_today(db) == Decimal("0")


async def test_record_spend_accumulates(db):
    await llm.record_spend(db, model="claude-haiku-4-5", purpose="judge", usage=FakeUsage())
    await llm.record_spend(db, model="claude-haiku-4-5", purpose="judge", usage=FakeUsage())
    cur = await db.execute("SELECT count(*) AS n FROM llm_spend")
    assert (await cur.fetchone())["n"] == 2
    assert await llm.spend_today(db) > 0


async def test_the_ceiling_reads_from_the_database(db):
    """An in-process counter resets on every cold start, which is no ceiling.

    A serverless instance is recreated constantly; the only durable record of
    what has been spent today is the table.
    """
    for _ in range(5):
        await llm.record_spend(
            db,
            model="claude-haiku-4-5",
            purpose="judge",
            usage=FakeUsage(input_tokens=100_000_000, output_tokens=0),
        )
    with pytest.raises(llm.SpendCapExceeded):
        await llm.assert_under_cap(db, settings(DAILY_LLM_CAP_USD="1.50"))


async def test_spend_stays_allowed_below_the_cap(db):
    await llm.record_spend(db, model="claude-haiku-4-5", purpose="judge", usage=FakeUsage())
    await llm.assert_under_cap(db, settings(DAILY_LLM_CAP_USD="1.50"))


async def test_spend_from_a_previous_day_does_not_count(db):
    await db.execute(
        "INSERT INTO llm_spend (day, model, purpose, input_tokens, output_tokens, cost_usd)"
        " VALUES (current_date - 1, 'claude-haiku-4-5', 'judge', 1, 1, 99.0)"
    )
    assert await llm.spend_today(db) == Decimal("0")


# --- the caching trap --------------------------------------------------------


def test_haiku_minimum_cacheable_prefix_is_4096():
    """The single most consequential number in this module.

    Below the minimum, caching silently does nothing: no error, no warning,
    cache_creation_input_tokens simply comes back 0.
    """
    assert llm.MIN_CACHEABLE_TOKENS["claude-haiku-4-5"] == 4096
    assert llm.MIN_CACHEABLE_TOKENS["claude-sonnet-5"] == 1024


def test_a_short_prefix_is_reported_as_ineffective():
    assert llm.cache_prefix_is_effective("claude-haiku-4-5", 2900) is False
    assert llm.cache_prefix_is_effective("claude-haiku-4-5", 4200) is True


def test_an_unknown_model_is_treated_as_uncacheable():
    # Better to skip caching than to claim a saving that is not happening.
    assert llm.cache_prefix_is_effective("claude-something-new", 100_000) is False


_CACHED_SYSTEM = [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]


def test_a_cache_read_does_not_warn(caplog):
    """A successful read reports cache_creation = 0; only the write is non-zero.

    Checking creation alone warned on nine calls in ten while caching was
    working perfectly — which is how a warning becomes noise that gets ignored.
    """
    llm._warn_if_caching_is_not_working(
        FakeUsage(cache_read=4000), model="claude-haiku-4-5", purpose="judge", system=_CACHED_SYSTEM
    )
    assert not caplog.records


def test_a_cache_write_does_not_warn(caplog):
    llm._warn_if_caching_is_not_working(
        FakeUsage(cache_write=4000),
        model="claude-haiku-4-5",
        purpose="judge",
        system=_CACHED_SYSTEM,
    )
    assert not caplog.records


def test_neither_read_nor_write_does_warn(caplog):
    llm._warn_if_caching_is_not_working(
        FakeUsage(), model="claude-haiku-4-5", purpose="judge", system=_CACHED_SYSTEM
    )
    assert any("below" in r.message for r in caplog.records)


def test_no_warning_when_caching_was_never_requested(caplog):
    llm._warn_if_caching_is_not_working(
        FakeUsage(),
        model="claude-haiku-4-5",
        purpose="sharia",
        system=[{"type": "text", "text": "x"}],
    )
    assert not caplog.records


# --- key handling ------------------------------------------------------------


async def test_a_missing_api_key_raises_where_the_call_is_made(db):
    """Unlike DATABASE_URL this is optional at load time.

    Discovery is most of the system's work and needs no model access, so a
    missing key must not stop a crawl — it fails here, naming the caller.
    """
    from pydantic import BaseModel

    class Out(BaseModel):
        x: str

    with pytest.raises(llm.ApiKeyMissing, match="judge"):
        await llm.call_structured(
            db,
            model="claude-haiku-4-5",
            purpose="judge",
            system=[{"type": "text", "text": "s"}],
            user="u",
            output_format=Out,
            settings=settings(),
        )
