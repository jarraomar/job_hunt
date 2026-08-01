"""Normalization and content-derived fingerprinting.

The fingerprint is what makes repost detection work (spec section 5): a job
relisted under a new source id collapses onto the existing row instead of
looking new. It must therefore ignore cosmetic variation while preserving every
difference that makes a posting a genuinely different job.
"""

from __future__ import annotations

import hashlib
import re

from pipeline.models import Job, RawJob
from pipeline.salary import parse_salary

# Stripped only when they stand as separate tokens, so "Coinbase" and
# "Incredible Machines" survive intact.
#
# Deliberately excluded: "group" and "holdings", which frequently distinguish
# genuinely different legal entities, and "spa"/"as"/"ab", which are ordinary
# words in some company names.
_LEGAL_SUFFIXES = {
    "inc",
    "llc",
    "llp",
    "lp",
    "ltd",
    "corp",
    "corporation",
    "company",
    "co",
    "pbc",
    "gmbh",
    "plc",
    "sa",
    "sas",
    "ag",
    "bv",
    "nv",
    "srl",
    "pte",
    "pvt",
}

_PAREN_RE = re.compile(r"\(([^)]*)\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# A parenthetical is dropped only when it is a work-arrangement marker.
# Dropping all of them collapses "Research Engineer, ML (Reinforcement
# Learning)" onto "(RL Velocity)" — two real, separately-open roles — and one
# of them then never reaches the queue.
_ARRANGEMENT_RE = re.compile(r"\b(?:remote|hybrid|on[\s-]?site|wfh)\b", re.IGNORECASE)
_MAX_ARRANGEMENT_WORDS = 4
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\b(?:fully\s+)?remote\b", re.IGNORECASE)

# Negation appears on both sides of the word in real postings:
#   "this role is not remote"        -> negation first
#   "remote work is not available"   -> negation second
# Matching only one direction leaves the other reading as remote.
_NOT_REMOTE_RE = re.compile(
    r"\b(?:not|non|no|isn't)[\s-]+(?:\w+\s+){0,2}?remote\b"
    r"|\bremote\b(?:\s+\w+){0,2}?\s+(?:is\s+)?(?:not|un)(?:\s+|available|supported)",
    re.IGNORECASE,
)

# A character that cannot survive _squash, so it cannot appear inside any field
# and cannot be forged to shift a boundary.
_FIELD_SEP = "|"


def _squash(text: str) -> str:
    """Lowercase, replace every run of non-alphanumerics with a single space."""
    return " ".join(_NON_ALNUM_RE.sub(" ", text.lower()).split())


def normalize_company(name: str) -> str:
    squashed = _squash(name)
    tokens = [t for t in squashed.split() if t not in _LEGAL_SUFFIXES]
    # A name made entirely of legal suffixes ("Inc") must not become "", which
    # would collide with every other such company under a single key.
    return " ".join(tokens) if tokens else squashed


def _drop_arrangement_parentheticals(title: str) -> str:
    """Remove "(Remote)" and friends; keep "(Child Safety)" and "(CI/CD)"."""

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if _ARRANGEMENT_RE.search(inner) and len(inner.split()) <= _MAX_ARRANGEMENT_WORDS:
            return " "
        return f" {inner} "

    return _PAREN_RE.sub(replace, title)


def normalize_title(title: str) -> str:
    return _squash(_drop_arrangement_parentheticals(title))


def normalize_city(location: str | None) -> str:
    """Extract the city from the many shapes a location field takes.

    Most sources put the city first ("San Francisco, CA, USA"). Workday-style
    strings invert it ("US-CA-San Francisco"), and taking the first component
    there yields "us" for every posting — which would collapse same-titled jobs
    in different cities onto one fingerprint.
    """
    if not location:
        return ""

    parts = [p for p in (_squash(p) for p in re.split(r"[,\-–—/]", location)) if p]
    if not parts:
        return ""
    # A short leading component is a country or state code, not a city name.
    # The threshold is 3 and must not be raised: at 4 this would rewrite
    # "Mesa, AZ" to "az". Region codes longer than that (EMEA, APAC) stay
    # misread, which is acceptable — those postings are outside the US scope.
    if len(parts) > 1 and len(parts[0]) <= 3:
        return parts[-1]
    return parts[0]


def compute_fingerprint(company: str, title: str, location: str | None) -> str:
    """sha256 over the normalized (company, title, city) triple."""
    payload = _FIELD_SEP.join(
        (normalize_company(company), normalize_title(title), normalize_city(location))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_ARRANGEMENT_ALIASES = {
    "remote": "remote",
    "fullyremote": "remote",
    "remotefirst": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "inoffice": "onsite",
    "office": "onsite",
}


def normalize_arrangement(value: str | None) -> str | None:
    """Map a source's structured work-arrangement string onto our vocabulary."""
    if not value:
        return None
    key = re.sub(r"[^a-z]", "", value.lower())
    return _ARRANGEMENT_ALIASES.get(key)


def classify_remote(
    title: str,
    location: str | None,
    description: str,
    remote_type_hint: str | None,
) -> str:
    """Return "remote", "hybrid", or "onsite".

    Signals are ranked by how specific they are to *this* posting, strongest
    first. Title and location describe the role; the description is largely
    company boilerplate. Anthropic, for one, restates a hybrid office policy in
    every job description — reading the description first classified all 400 of
    their open roles as hybrid, including ones whose location says Remote.

    Within each tier hybrid outranks remote: it is the more restrictive claim,
    and a posting asserting both is in practice hybrid.
    """
    # A source that states the arrangement outright is believed. Everything
    # below is inference from prose and cannot beat the employer saying it.
    stated = normalize_arrangement(remote_type_hint)
    if stated:
        return stated

    strong = f"{title} {location or ''}"

    if _HYBRID_RE.search(strong):
        return "hybrid"
    if _REMOTE_RE.search(strong):
        return "remote"
    if _HYBRID_RE.search(description):
        return "hybrid"
    # The weakest signal, and the one that needs the negation guard —
    # boilerplate like "this role is not remote" is common.
    if _REMOTE_RE.search(description) and not _NOT_REMOTE_RE.search(description):
        return "remote"
    return "onsite"


def to_job(raw: RawJob) -> Job:
    salary_min, salary_max, salary_source = (raw.salary_min, raw.salary_max, raw.salary_source)
    if salary_min is None and salary_max is None:
        salary_min, salary_max = parse_salary(raw.description)
        salary_source = "parsed" if salary_min is not None else "none"

    return Job(
        fingerprint=compute_fingerprint(raw.company_name, raw.title, raw.location),
        source=raw.source,
        source_job_id=raw.source_job_id,
        company_name=raw.company_name.strip(),
        normalized_company=normalize_company(raw.company_name),
        title=raw.title.strip(),
        location=raw.location,
        remote_type=classify_remote(raw.title, raw.location, raw.description, raw.remote_type_hint),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_source=salary_source,
        description=raw.description,
        apply_url=raw.apply_url,
        posted_at=raw.posted_at,
    )
