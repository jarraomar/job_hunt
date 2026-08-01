"""Gate 1 of the scoring pipeline: free, deterministic, runs on everything.

Spec section 8 expects this to reject roughly 70% of intake so the paid stages
see very few jobs.

Two rules here are load-bearing and both cut against intuition:

- **Unknown salary passes.** Measured across five live sources, a third of all
  postings carried no parseable figure. Rejecting on unknown discards most of
  the real pipeline.
- **Clearance and citizenship language passes.** Jarra is a US-born citizen
  (spec section 15.11), so "US Citizenship required" and "must be able to obtain
  a security clearance" are eligibility matches, not disqualifiers. There is
  deliberately no rule keyed on those words -- this docstring and the regression
  tests exist so nobody adds one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.config import Settings
from pipeline.models import Job

TARGET_TITLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(software|full[\s-]?stack|back[\s-]?end|front[\s-]?end|web|product)\s+"
        r"(engineer|developer)\b",
        r"\b(cloud|infrastructure|platform|devops|site\s+reliability|sre|systems?)\s+engineer\b",
        r"\b(machine\s+learning|ml|ai|applied\s+scientist)\s+engineer\b",
        r"\b(embedded|firmware|systems)\s+(software\s+)?engineer\b",
        r"\bcomputer\s+engineer\b",
        r"\b(software|application)\s+developer\b",
        r"\bengineer,?\s+(software|platform|infrastructure|back[\s-]?end|front[\s-]?end)\b",
        # Language- and stack-named roles: "Senior Python Engineer".
        r"\b(python|javascript|typescript|react|node|go(?:lang)?|rust|java|c\+\+)\s+"
        r"(engineer|developer)\b",
        # Adjacent families that appear constantly on real boards. Included
        # because the errors here are asymmetric -- see the note below.
        r"\b(founding|research|data|security)\s+engineer\b",
    )
]

# This gate errs toward passing, deliberately. A false accept costs a little
# scoring compute and gets caught by the embedding and LLM stages downstream. A
# false reject is silent, permanent, and unrecoverable -- the job simply never
# existed as far as the rest of the system is concerned. When a family is
# genuinely arguable, it belongs here and Phase 2 can rank it down.

# Management is a different function, not the same function at a higher level.
# Kept separate from seniority so the run_log breakdown can distinguish
# "wrong job" from "right job, wrong level".
MANAGEMENT_RE = re.compile(
    r"\b(manager|director|head\s+of|vp|vice\s+president|chief|cto|ceo)\b",
    re.IGNORECASE,
)

# Individual-contributor levels well above ~3 years of experience. The stretch
# allowance in scoring (spec section 8) reintroduces a small random slice later.
SENIORITY_EXCLUDE_RE = re.compile(
    r"\b(principal|distinguished|staff|fellow|architect)\b",
    re.IGNORECASE,
)

NON_ENGINEERING_RE = re.compile(
    r"\b(nurse|sales|account\s+executive|warehouse|driver|recruiter|recruiting|"
    r"marketing|paralegal|teacher|intern|internship|designer|"
    r"business\s+development|customer\s+success|support\s+specialist)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str | None = None


def _role_head(title: str) -> str:
    """The part of a title that names the role.

    Everything after the first comma or dash is a team, product, or location:
    "Software Engineer, Ads Manager" is an IC engineering job on the ads
    product, not a management job. Matching the whole string rejected it.
    """
    return re.split(r"[,–—]|\s+-\s+", title, maxsplit=1)[0]


def prefilter(job: Job, settings: Settings) -> FilterResult:
    """Cheap, deterministic accept/reject. Never calls out to anything."""
    title = job.title
    head = _role_head(title)

    # Title checks run before salary so a rejected job records *why* it was
    # wrong rather than the first rule that happened to fire.
    if NON_ENGINEERING_RE.search(head):
        return FilterResult(False, "title_not_target")

    if MANAGEMENT_RE.search(head):
        return FilterResult(False, "management_role")

    if SENIORITY_EXCLUDE_RE.search(head):
        return FilterResult(False, "seniority_mismatch")

    if not any(p.search(title) for p in TARGET_TITLE_PATTERNS):
        return FilterResult(False, "title_not_target")

    # Unknown salary passes: most ATSes do not publish it, and rejecting on
    # unknown would discard the majority of real intake.
    if job.salary_source != "none":
        ceiling = job.salary_max if job.salary_max is not None else job.salary_min
        if ceiling is not None and ceiling < settings.salary_floor:
            return FilterResult(False, "salary_below_floor")

    return FilterResult(True, None)
