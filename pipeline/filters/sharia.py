"""The Sharia business-activity screen (spec section 9).

**Business activity only.** The DJIM/AAOIFI financial-ratio screens -- debt to
market cap and similar -- exist for equity investing and do not transfer to
employment; applying them would wrongly exclude most leveraged companies.
IDEA.md conflated the two. There is deliberately no ratio logic in this file,
and a test asserts its absence.

Three tiers:

1. Static blocklist -- free, deterministic, catches the clear cases.
2. Haiku classification -- cached per company forever, so the cost trends to
   zero after the first couple of weeks.
3. Gray zone -- flagged, never dropped, with the model's reasoning surfaced in
   the UI so the decision stays Jarra's.

`sharia_source='user'` always wins and is never re-billed or re-evaluated. An
LLM must never silently make a religious ruling that cannot be corrected.

The errors here are asymmetric, in the opposite direction from the pre-filter.
A false exclusion silently deletes an employer and nobody finds out; a false
`flagged` costs one glance at the settings page. Everything below leans toward
asking rather than deciding.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from pipeline.config import Settings
from pipeline.llm import SpendCapExceeded, call_structured

log = logging.getLogger(__name__)

VERDICTS = frozenset({"allowed", "excluded", "flagged", "unknown"})

MODEL = "claude-haiku-4-5"

_BLOCKLIST_PATH = Path(__file__).with_name("blocklist.yaml")
_blocklist = yaml.safe_load(_BLOCKLIST_PATH.read_text())


def _flatten(section: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (sector, term)
        for sector, terms in (_blocklist.get(section) or {}).items()
        for term in terms or []
    )


_OVERRIDES = tuple(_blocklist.get("allowed_overrides") or [])
_NAME_TERMS = _flatten("name_terms")
_DESCRIPTION_TERMS = _flatten("description_terms")

_SYSTEM = """You classify an employer's primary business activity for a Sharia \
business-activity screen used to decide whether to apply for a job there.

Apply ONLY the business-activity screen. Do not consider financial ratios, debt \
levels, or market capitalisation -- those apply to equity investing, not \
employment.

Return "excluded" only when the company's PRIMARY revenue comes from: \
interest-based finance, alcohol, gambling, adult content, weapons manufacture, \
or pork.

Return "allowed" for ordinary businesses, including ones that merely serve \
excluded industries as customers. A developer-tools company whose customers are \
banks is a software company, not a bank.

Return "flagged" when the primary activity is genuinely ambiguous -- a payments \
company with a lending arm, a conglomerate with a mixed portfolio. Explain what \
makes it ambiguous; a human will decide.

Prefer "flagged" over "excluded" when uncertain. A wrong exclusion silently \
removes an employer from consideration and nobody reviews it; a wrong flag costs \
one glance.

Give the reason FIRST, then the sector, then the verdict. Reason your way to the \
verdict rather than announcing it and justifying it afterwards, and make sure the \
verdict you give follows from the reason you wrote.

Be concise. One or two sentences of reason."""


class Verdict(BaseModel):
    """Field order is load-bearing and must not be "tidied".

    Structured output is generated in declaration order, so a field decided
    before `reason` is decided before the model has reasoned at all. With
    `verdict` first, one run returned excluded for GitLab with the reason
    "this is clearly a permitted technology/software sector with no excluded
    primary revenue streams" -- the model guessed, then argued itself to the
    opposite conclusion, and the guess is what got stored.

    Reasoning first gives it somewhere to think before committing. Thinking is
    off for this call, so these fields are the only place that can happen.
    """

    reason: str
    sector: str
    verdict: str


def screen_blocklist(name: str, description: str) -> tuple[str, str, str] | None:
    """Tier 1. Returns (verdict, sector, reason) or None if unresolved.

    The name and the description are held to different standards on purpose.
    A company called "First National Bank" is a bank -- the name is evidence.
    A job description is about the ROLE, and matching business-activity terms
    against it excluded Anthropic, Ramp, Robinhood and Brex on one run, because
    benefits boilerplate says "medical, dental and vision insurance". Only
    phrases that cannot appear in perks text are matched against description.
    """
    name_hay = name.lower()

    # Overrides first: "blood bank" contains "bank" but is not a bank, and
    # without this an entire category of medical employers disappears.
    for phrase in _OVERRIDES:
        if phrase in name_hay:
            return None

    for sector, term in _NAME_TERMS:
        # Word-boundary matched so "betterment" is not "bet" and "embanking"
        # is not "banking".
        if re.search(rf"\b{re.escape(term)}\b", name_hay):
            return ("excluded", sector, f"Company name contains {term!r} ({sector}).")

    description_hay = description.lower()
    for sector, term in _DESCRIPTION_TERMS:
        if term in description_hay:
            return ("excluded", sector, f"Description states {term!r} ({sector}).")

    return None


async def judge_company(
    conn: AsyncConnection, name: str, description: str, settings: Settings
) -> tuple[str, str, str]:
    """Tier 2. One cached-forever Haiku classification."""
    parsed, _usage = await call_structured(
        conn,
        model=MODEL,
        purpose="sharia",
        system=[{"type": "text", "text": _SYSTEM}],
        user=f"Company: {name}\n\nSelf-description:\n{description[:2000]}",
        output_format=Verdict,
        settings=settings,
        max_tokens=300,
    )
    # No coercion here on purpose -- screen_company validates whatever any
    # judge returns, so a batch implementation or a different model cannot
    # route around it.
    return parsed.verdict, parsed.sector, parsed.reason


async def screen_company(
    conn: AsyncConnection,
    company_id: int,
    name: str,
    description: str,
    settings: Settings,
    *,
    judge: Callable[..., Awaitable[tuple[str, str, str]]] = judge_company,
) -> str:
    """Resolve a company's verdict, using the cheapest tier that can decide."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sharia_verdict, sharia_source FROM companies WHERE company_id = %s",
            (company_id,),
        )
        row = await cur.fetchone()

    if row:
        # A user verdict is permanent and is never re-billed or re-evaluated.
        if row["sharia_source"] == "user":
            return row["sharia_verdict"]
        # Any other decided verdict stands: this is what makes the cost trend
        # to near zero after the first couple of weeks. `unknown` is explicitly
        # not a decision -- an API outage must not permanently mark every
        # company seen during it.
        if row["sharia_source"] and row["sharia_verdict"] != "unknown":
            return row["sharia_verdict"]

    if resolved := screen_blocklist(name, description):
        verdict, sector, reason = resolved
        await _store(conn, company_id, verdict, sector, reason, "blocklist")
        return verdict

    try:
        verdict, sector, reason = await judge(conn, name, description, settings)
    except SpendCapExceeded:
        log.warning("sharia: daily cap reached; %s left unknown", name)
        return "unknown"
    except Exception as exc:
        # Leave it unknown rather than excluded. Failing closed would silently
        # delete employers during an API outage.
        log.warning("sharia: classification failed for %s: %s", name, exc)
        return "unknown"

    if verdict not in VERDICTS:
        # Validated here rather than inside judge_company so that no judge
        # implementation can route around it. Coerced toward the verdict that
        # asks a human, never toward a silent "allowed".
        log.warning("sharia: %s returned verdict %r for %s; flagging", MODEL, verdict, name)
        verdict = "flagged"

    await _store(conn, company_id, verdict, sector, reason, "llm")
    return verdict


async def _store(
    conn: AsyncConnection, company_id: int, verdict: str, sector: str, reason: str, source: str
) -> None:
    await conn.execute(
        "UPDATE companies SET sharia_verdict = %s, sharia_sector = %s,"
        " sharia_reason = %s, sharia_source = %s, sharia_decided_at = now()"
        " WHERE company_id = %s AND COALESCE(sharia_source, '') <> 'user'",
        (verdict, sector, reason, source, company_id),
    )
