"""Parse annualized USD salary ranges out of free-text job descriptions.

Spec section 6: Greenhouse and Workday almost never expose structured salary, so
most figures have to come from prose.

Biased toward refusing rather than guessing. A wrong number silently drops a good
job at the section 8 salary gate, whereas an unknown salary passes that gate --
so a false number is strictly worse than no number.
"""

from __future__ import annotations

import re

_HOURS_PER_YEAR = 40 * 52

# Plausibility bounds. Anything outside these is a misread, not a salary.
_MIN_ANNUAL = 30_000
_MAX_ANNUAL = 1_000_000
_MIN_HOURLY = 20
_MAX_HOURLY = 500

# Every figure must be anchored to a currency marker. Without this, "we serve
# 150,000 customers" parses as a salary.
_CUR = r"(?:USD|US\$|\$)"

# Ordered alternation, longest form first: a comma-grouped number must win over
# the bare-digit form, and "130k" must win over the "130" inside it. Getting
# this order wrong silently truncates 130k to 130, which then fails the
# plausibility check and reads as "no salary found".
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s?[kK]\b|\d+(?:\.\d+)?"

_SEP = r"\s*(?:-|–|—|\bto\b)\s*"

# "and" is handled separately from the dash forms because it is far weaker
# evidence: "$140,000 and 15% bonus" is not a range. Requiring a currency marker
# on the second figure is what separates "between $X and $Y" from that.
_SEP_AND = r"\s+and\s+"

# Three range shapes, tried in order. Each needs at least one currency anchor.
_RANGE_PATTERNS = [
    # "$150,000 - $200,000" and "USD 145,000 - 185,000" (marker on the first only)
    re.compile(rf"{_CUR}\s?({_NUM}){_SEP}(?:{_CUR}\s?)?({_NUM})", re.IGNORECASE),
    # "between $140,000 and $185,000"
    re.compile(rf"{_CUR}\s?({_NUM}){_SEP_AND}{_CUR}\s?({_NUM})", re.IGNORECASE),
    # "150k-200k USD" — marker trails the whole range
    re.compile(rf"({_NUM}){_SEP}({_NUM})\s?(?:USD|US\$)", re.IGNORECASE),
]

# Leading marker ("$180,000") or trailing marker ("210000 USD").
_SINGLE_RE = re.compile(
    rf"{_CUR}\s?({_NUM})|({_NUM})\s?(?:USD|US\$)",
    re.IGNORECASE,
)

_HOURLY_RE = re.compile(r"(?:per\s+hour|/\s?h(?:ou)?r\b|hourly)", re.IGNORECASE)
_NON_USD_RE = re.compile(r"[€£¥]")


def _to_number(token: str) -> float:
    token = token.replace(",", "").replace(" ", "").lower()
    if token.endswith("k"):
        return float(token[:-1]) * 1_000
    return float(token)


def _annualize(value: float, hourly: bool) -> int | None:
    """Convert to an annual figure, or None if implausible at either scale."""
    if hourly:
        if not (_MIN_HOURLY <= value <= _MAX_HOURLY):
            return None
        value *= _HOURS_PER_YEAR
    if not (_MIN_ANNUAL <= value <= _MAX_ANNUAL):
        return None
    return int(round(value))


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """Return (min, max) annualized USD, or (None, None) if nothing trustworthy.

    A single figure returns (n, n). If either end of a range is implausible the
    whole range is refused: returning the good half would misrepresent the post.
    """
    if not text or _NON_USD_RE.search(text):
        return (None, None)

    hourly = bool(_HOURLY_RE.search(text))

    for pattern in _RANGE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        lo = _annualize(_to_number(match.group(1)), hourly)
        hi = _annualize(_to_number(match.group(2)), hourly)
        if lo is None or hi is None:
            return (None, None)
        return (min(lo, hi), max(lo, hi))

    match = _SINGLE_RE.search(text)
    if match:
        # Exactly one of the two groups participates, depending on whether the
        # currency marker led or trailed.
        token = match.group(1) or match.group(2)
        value = _annualize(_to_number(token), hourly)
        if value is None:
            return (None, None)
        return (value, value)

    return (None, None)
