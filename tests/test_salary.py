import pytest

from pipeline.salary import parse_salary


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$150,000 - $200,000", (150_000, 200_000)),
        ("$150,000-$200,000 per year", (150_000, 200_000)),
        ("Salary range: $130k to $170k", (130_000, 170_000)),
        # Currency marker on the first figure only — the common ATS phrasing.
        ("USD 145,000 — 185,000 annually", (145_000, 185_000)),
        ("The base pay range is $128,000—$164,000.", (128_000, 164_000)),
        ("Compensation: $180,000", (180_000, 180_000)),
        ("$95/hour", (197_600, 197_600)),  # 95 * 40 * 52
        ("$75 - $95 per hour", (156_000, 197_600)),
        # Trailing currency marker.
        ("Base salary of 210000 USD", (210_000, 210_000)),
        ("We offer competitive compensation.", (None, None)),
        ("", (None, None)),
        # No currency anchor: a customer count must never read as pay.
        ("Founded in 2011, we serve 150,000 customers", (None, None)),
        ("401k matching up to 5%", (None, None)),
        ("€120,000 - €150,000", (None, None)),  # non-USD: refuse
    ],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_min_never_exceeds_max():
    lo, hi = parse_salary("$200,000 - $150,000")
    assert lo is not None and hi is not None and lo <= hi


def test_implausible_values_rejected():
    assert parse_salary("$12 - $19") == (None, None)  # too low even hourly
    assert parse_salary("$5,000,000 - $9,000,000") == (None, None)


def test_equity_and_bonus_language_does_not_become_salary():
    # "$150,000" here is an equity value, not pay. We cannot tell the difference,
    # so matching the first currency figure is accepted behaviour — but a bare
    # percentage or share count must never produce a number.
    assert parse_salary("Equity: 0.05% - 0.15%") == (None, None)
    assert parse_salary("10,000 stock options") == (None, None)


def test_refuses_rather_than_half_parsing_a_range():
    # If either end of a range is implausible, the whole range is untrustworthy.
    # Returning the good half would silently misrepresent the posting.
    assert parse_salary("$150,000 - $9,000,000") == (None, None)


@pytest.mark.parametrize(
    "text,expected",
    [
        # Trailing ".00" must not truncate the range to its first figure.
        ("Pay Range: $120,000.00 - $180,000.00", (120_000, 180_000)),
        # Currency marker trailing the whole range rather than leading it.
        ("Salary: 150k-200k USD", (150_000, 200_000)),
        # "between X and Y" is a range, not a single figure.
        ("This role pays between $140,000 and $185,000 annually", (140_000, 185_000)),
        ("The base salary range for this position is $145,000 — $190,000 USD.", (145_000, 190_000)),
        ("$130K – $175K + equity", (130_000, 175_000)),
        ("Compensation ranges from USD 135,000 to USD 165,000", (135_000, 165_000)),
        ("Estimated pay: $58.65 - $88.00 per hour", (121_992, 183_040)),
    ],
)
def test_real_world_phrasings(text, expected):
    """Cases drawn from how ATS postings actually word compensation."""
    assert parse_salary(text) == expected


def test_and_requires_currency_on_both_sides():
    # "and" is weak evidence of a range. Without a marker on the second figure
    # this is a salary followed by a bonus percentage, and must read as the
    # single salary rather than a bogus $140,000-$15 range.
    assert parse_salary("$140,000 and 15% bonus") == (140_000, 140_000)


def test_company_metrics_are_never_salary():
    assert parse_salary("Series B, raised $50,000,000") == (None, None)
    assert parse_salary("We have 250,000 users and $10M ARR") == (None, None)
    assert parse_salary("$1,200 signing bonus") == (None, None)
