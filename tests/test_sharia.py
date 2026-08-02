import pytest

from pipeline.config import load_settings
from pipeline.filters.sharia import (
    VERDICTS,
    screen_blocklist,
    screen_company,
)

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y", "ANTHROPIC_API_KEY": "sk-test"})


async def _company(db, name="Acme", **cols) -> int:
    keys = "".join(f", {k}" for k in cols)
    marks = "".join(", %s" for _ in cols)
    cur = await db.execute(
        f"INSERT INTO companies (name, normalized_name{keys})"
        f" VALUES (%s, %s{marks}) RETURNING company_id",
        (name, name.lower().replace(" ", "-"), *cols.values()),
    )
    return (await cur.fetchone())["company_id"]


async def _never_called(conn, name, description, settings):
    raise AssertionError("the judge should not have been called")


# --- tier 1: the blocklist ---------------------------------------------------


@pytest.mark.parametrize(
    "name,description",
    [
        ("First National Bank", "Retail banking and mortgages."),
        ("Acme Casino Group", "Online sportsbook and casino games."),
        ("Golden Distillery", "We make small-batch whiskey."),
        ("SafeGuard Insurance", "Auto and home insurance."),
    ],
)
def test_clear_exclusions_are_caught_for_free(name, description):
    result = screen_blocklist(name, description)
    assert result is not None
    assert result[0] == "excluded"


@pytest.mark.parametrize(
    "name,description",
    [
        ("Stripe", "Payments infrastructure for the internet."),
        ("Anthropic", "AI safety research company."),
        ("Figma", "Collaborative design tool."),
        ("Databricks", "Data and AI platform for analytics."),
    ],
)
def test_ordinary_companies_are_not_matched(name, description):
    assert screen_blocklist(name, description) is None


def test_allowed_overrides_beat_keyword_hits():
    """ "Blood bank" contains "bank"; it is not a bank.

    Without the override this drops an entire category of medical employers.
    """
    assert screen_blocklist("Regional Blood Bank", "We manage blood donation.") is None


def test_serving_an_excluded_industry_is_not_being_one():
    # A developer-tools company whose customers are banks is a software company.
    assert screen_blocklist("Ledger Systems", "Banking infrastructure for developers.") is None


def test_a_substring_alone_does_not_match():
    # "banking" must not fire on "embanking", nor "bet" on "better".
    assert screen_blocklist("Betterment Tools", "We make things better.") is None


@pytest.mark.parametrize(
    "name",
    ["Anthropic", "ramp", "Robinhood", "Brex", "ViyaMD", "Coalition Technologies"],
)
def test_benefits_boilerplate_never_excludes_a_company(name):
    """The regression that mattered most.

    Matching business-activity terms against job-description text excluded 17
    of 197 real companies, ~14 of them wrongly — Anthropic, Ramp, Robinhood and
    Brex among them — because job postings list "medical, dental and vision
    insurance" under benefits. A job description describes the ROLE, not the
    business, and is not evidence about business activity.
    """
    benefits = (
        "We offer competitive compensation, medical, dental and vision insurance, "
        "life insurance, disability insurance, a 401(k) with company match, and "
        "generous parental leave. We work with banking partners across the industry."
    )
    assert screen_blocklist(name, benefits) is None


def test_the_company_name_is_still_authoritative():
    # The flip side: a name hit remains reliable evidence.
    result = screen_blocklist("First National Bank", "We offer dental insurance.")
    assert result is not None and result[0] == "excluded"


def test_only_unambiguous_phrases_match_description_text():
    # "sports betting" cannot appear in a perks section; "insurance" can.
    result = screen_blocklist("Acme Interactive", "We operate sports betting markets.")
    assert result is not None and result[1] == "gambling"


def test_the_financial_ratio_screens_are_not_applied():
    """Spec section 9: business activity only.

    The DJIM/AAOIFI debt and market-cap screens exist for equity investing.
    Applying them to employment would exclude most leveraged companies for no
    defensible reason, and IDEA.md conflated the two.
    """
    from pathlib import Path

    from pipeline.filters import sharia

    source = Path(sharia.__file__).read_text()
    for term in ("debt_ratio", "market_cap", "debt_to_equity", "interest_income_ratio"):
        assert term not in source


def test_verdict_vocabulary_is_closed():
    assert VERDICTS == frozenset({"allowed", "excluded", "flagged", "unknown"})


# --- tier 2 and 3: caching and the user override ----------------------------


async def test_a_blocklist_hit_is_cached_without_an_llm_call(db):
    company_id = await _company(db, "Acme Casino")
    verdict = await screen_company(
        db, company_id, "Acme Casino", "Online betting.", SETTINGS, judge=_never_called
    )
    assert verdict == "excluded"
    cur = await db.execute("SELECT sharia_verdict, sharia_source FROM companies")
    row = await cur.fetchone()
    assert row["sharia_verdict"] == "excluded"
    assert row["sharia_source"] == "blocklist"


async def test_a_cached_verdict_is_not_re_billed(db):
    company_id = await _company(db, "Acme", sharia_verdict="allowed", sharia_source="llm")
    verdict = await screen_company(
        db, company_id, "Acme", "Software.", SETTINGS, judge=_never_called
    )
    assert verdict == "allowed"


async def test_a_user_verdict_always_wins_and_is_never_re_evaluated(db):
    """Spec section 9: an LLM must never silently make a religious ruling that
    cannot be corrected."""
    company_id = await _company(db, "Acme Casino", sharia_verdict="allowed", sharia_source="user")
    verdict = await screen_company(
        db, company_id, "Acme Casino", "Online betting.", SETTINGS, judge=_never_called
    )
    # The blocklist would say excluded. The user said allowed. The user wins.
    assert verdict == "allowed"


async def test_a_user_verdict_is_not_overwritten_by_a_later_store(db):
    company_id = await _company(db, "Acme", sharia_verdict="excluded", sharia_source="user")
    await screen_company(db, company_id, "Acme", "Software.", SETTINGS, judge=_never_called)
    cur = await db.execute("SELECT sharia_verdict, sharia_source FROM companies")
    row = await cur.fetchone()
    assert (row["sharia_verdict"], row["sharia_source"]) == ("excluded", "user")


async def test_an_unresolved_company_reaches_the_judge(db):
    company_id = await _company(db, "Novel Corp")
    calls = []

    async def judge(conn, name, description, settings):
        calls.append(name)
        return "allowed", "software", "General-purpose SaaS."

    verdict = await screen_company(
        db, company_id, "Novel Corp", "We do something new.", SETTINGS, judge=judge
    )
    assert verdict == "allowed"
    assert calls == ["Novel Corp"]

    cur = await db.execute("SELECT sharia_source, sharia_reason FROM companies")
    row = await cur.fetchone()
    assert row["sharia_source"] == "llm"
    assert "SaaS" in row["sharia_reason"]


async def test_a_gray_zone_verdict_is_flagged_not_dropped(db):
    company_id = await _company(db, "Ambiguous Inc")

    async def judge(conn, name, description, settings):
        return "flagged", "fintech", "Payments processor with a lending arm."

    verdict = await screen_company(
        db, company_id, "Ambiguous Inc", "Fintech.", SETTINGS, judge=judge
    )
    assert verdict == "flagged"
    cur = await db.execute("SELECT sharia_reason FROM companies")
    # The reason is surfaced in the UI so the decision is Jarra's, with the
    # model's argument visible rather than hidden.
    assert "lending" in (await cur.fetchone())["sharia_reason"]


async def test_a_judge_failure_leaves_the_company_unknown_not_excluded(db):
    # Failing closed would silently delete employers on an API outage.
    company_id = await _company(db, "Novel Corp")

    async def judge(conn, name, description, settings):
        raise RuntimeError("api down")

    verdict = await screen_company(
        db, company_id, "Novel Corp", "Something.", SETTINGS, judge=judge
    )
    assert verdict == "unknown"


async def test_a_spend_cap_leaves_the_company_unknown(db):
    from pipeline.llm import SpendCapExceeded

    company_id = await _company(db, "Novel Corp")

    async def judge(conn, name, description, settings):
        raise SpendCapExceeded("cap")

    verdict = await screen_company(
        db, company_id, "Novel Corp", "Something.", SETTINGS, judge=judge
    )
    assert verdict == "unknown"


async def test_an_unknown_company_is_retried_on_a_later_pass(db):
    """`unknown` must not be cached as if it were a decision.

    An API outage would otherwise permanently mark every company seen during
    it, and nothing would ever look at them again.
    """
    company_id = await _company(db, "Novel Corp")

    async def failing(conn, name, description, settings):
        raise RuntimeError("api down")

    await screen_company(db, company_id, "Novel Corp", "x", SETTINGS, judge=failing)

    async def working(conn, name, description, settings):
        return "allowed", "software", "Ordinary SaaS."

    assert (
        await screen_company(db, company_id, "Novel Corp", "x", SETTINGS, judge=working)
        == "allowed"
    )


async def test_an_out_of_vocabulary_verdict_is_coerced_to_flagged(db):
    company_id = await _company(db, "Novel Corp")

    async def judge(conn, name, description, settings):
        return "probably fine?", "software", "Unsure."

    verdict = await screen_company(db, company_id, "Novel Corp", "x", SETTINGS, judge=judge)
    # Coerced toward the verdict that asks a human, never toward allowed.
    assert verdict == "flagged"


def test_reasoning_is_generated_before_the_verdict():
    """Field order is load-bearing, not cosmetic.

    Structured output is generated in declaration order, so a verdict declared
    before the reason is committed before the model has reasoned. With the old
    ordering a real run returned `excluded` for GitLab with the reason "this is
    clearly a permitted technology/software sector with no excluded primary
    revenue streams" — and the guess, not the reasoning, is what got stored.
    """
    from pipeline.filters.sharia import Verdict

    fields = list(Verdict.model_fields)
    assert fields.index("reason") < fields.index("verdict")


def test_the_prompt_asks_for_the_reason_first():
    from pipeline.filters.sharia import _SYSTEM

    assert "reason FIRST" in _SYSTEM
