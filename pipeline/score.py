"""Gate 2 and the ranking blend (spec section 8).

Three components, combined into one total:

- **embed_similarity** -- cosine of the job description against the resume.
  Free, local, and the component that actually separates roles by fit.
- **rule_score** -- deterministic preferences: work arrangement, proximity to
  San Leandro, salary band.
- **freshness_score** -- a step function, not a decay curve. Application timing
  is one of the strongest predictors of a response, and the gap between 6 and
  40 hours matters far more than the gap between 20 and 25 days; an exponential
  blurs exactly the distinction worth keeping.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from pipeline.config import Settings
from pipeline.embed import cosine, get_embedder
from pipeline.models import Job
from pipeline.profile import Profile, resume_text

# Similarity dominates: fit is what we are ranking on. Freshness breaks ties
# among comparable jobs rather than promoting a poor match posted this morning.
WEIGHT_SIMILARITY = 0.55
WEIGHT_RULES = 0.25
WEIGHT_FRESHNESS = 0.20

# Raw cosine has to be rescaled before those weights mean anything.
#
# Measured over 813 real postings: every one of them landed between 0.60 and
# 0.85, with 53% inside a single 0.05-wide bucket. That is not the model
# failing -- everything reaching this point already passed the pre-filter, so
# they are all engineering roles written in the same register, and bge-small
# is reporting exactly that.
#
# Left raw, similarity swings 0.17 and contributes 0.55 * 0.17 = 0.09 to the
# total, while rule_score swings ~0.7 (0.175 weighted) and freshness 0.85
# (0.17 weighted). The weights would claim similarity dominates while the
# arithmetic let the other two outweigh it two to one.
#
# A fixed affine map rather than a per-batch rescale: batch-relative scores are
# not comparable across runs, and the queue shows jobs scored in different
# batches side by side. Re-derive these two constants if the corpus or the
# embedding model changes -- scripts/score_report.py prints the histogram.
SIMILARITY_FLOOR = 0.60
SIMILARITY_CEILING = 0.85

# Spec section 8: a small random slice of above-level roles and adjacent
# pivots is let through, so the ranking cannot quietly become a filter bubble.
STRETCH_RATE = 0.07

# Bay Area cities within a commute of San Leandro. Onsite roles outside this
# set are not disqualified -- the pre-filter already passed them -- but they
# rank below ones that are.
_NEARBY = frozenset(
    {
        "san leandro",
        "oakland",
        "san francisco",
        "berkeley",
        "emeryville",
        "alameda",
        "hayward",
        "fremont",
        "san mateo",
        "palo alto",
        "mountain view",
        "sunnyvale",
        "santa clara",
        "san jose",
        "redwood city",
        "menlo park",
        "burlingame",
        "south san francisco",
        "walnut creek",
        "richmond",
    }
)

_ARRANGEMENT_SCORE = {"remote": 1.0, "hybrid": 0.7, "onsite": 0.4}

# Unknown salary sits between the floor and the target rather than at zero:
# two thirds of intake has no parseable figure, and scoring absence as failure
# would rank every Greenhouse posting below every Ashby one on disclosure
# alone rather than on fit.
_UNKNOWN_SALARY_SCORE = 0.55

# The title carries most of the signal and the description carries the detail.
# Truncating keeps one enormous posting from dominating its own vector.
_DESCRIPTION_CHARS = 4000


@dataclass(frozen=True)
class ScoreComponents:
    embed_similarity: float
    rule_score: float
    freshness_score: float
    total_score: float
    is_stretch: bool


def freshness_score(job: Job, *, now: datetime) -> float:
    if job.posted_at is None:
        # Neither rewarded nor punished. HN and parts of Lever omit this.
        return 0.5

    age_hours = (now - job.posted_at).total_seconds() / 3600.0
    if age_hours <= 48:
        # Includes negative ages: a board a couple of hours ahead of our clock
        # is fresh, not invalid.
        return 1.0
    if age_hours <= 7 * 24:
        return 0.7
    if age_hours <= 30 * 24:
        return 0.4
    return 0.15


def rule_score(job: Job, settings: Settings) -> float:
    arrangement = _ARRANGEMENT_SCORE.get(job.remote_type, 0.4)

    proximity = 1.0
    if job.remote_type == "onsite":
        from pipeline.normalize import normalize_city

        proximity = 1.0 if normalize_city(job.location) in _NEARBY else 0.3

    if job.salary_source == "none":
        salary = _UNKNOWN_SALARY_SCORE
    else:
        ceiling = job.salary_max if job.salary_max is not None else job.salary_min
        if ceiling is None:
            salary = _UNKNOWN_SALARY_SCORE
        else:
            floor, target = settings.salary_floor, settings.salary_floor * 1.2
            salary = min(1.0, max(0.0, (ceiling - floor) / (target - floor)))

    return round(0.45 * arrangement + 0.20 * proximity + 0.35 * salary, 6)


def rescale_similarity(cosine_value: float) -> float:
    """Map the observed cosine band onto the full 0-1 range, clamped.

    Without this the similarity weight is nominal rather than real -- see the
    note on SIMILARITY_FLOOR.
    """
    span = SIMILARITY_CEILING - SIMILARITY_FLOOR
    return min(1.0, max(0.0, (cosine_value - SIMILARITY_FLOOR) / span))


def blend(embed_similarity: float, rule: float, freshness: float) -> float:
    total = (
        WEIGHT_SIMILARITY * embed_similarity + WEIGHT_RULES * rule + WEIGHT_FRESHNESS * freshness
    )
    return round(min(1.0, max(0.0, total)), 6)


def score_jobs(
    jobs: list[Job],
    profile: Profile,
    settings: Settings,
    *,
    now: datetime,
    rng: random.Random | None = None,
) -> list[ScoreComponents]:
    """Score a batch. Embedding is batched because per-call overhead dominates."""
    if not jobs:
        # Returns before touching get_embedder(): an empty pass must not pay
        # the model load.
        return []

    chooser = rng or random.Random()
    embedder = get_embedder()

    resume_vec = embedder.embed_one(resume_text(profile))
    job_vecs = embedder.embed([f"{j.title}\n{j.description[:_DESCRIPTION_CHARS]}" for j in jobs])

    results: list[ScoreComponents] = []
    for job, vec in zip(jobs, job_vecs, strict=True):
        similarity = rescale_similarity(cosine(resume_vec, vec))
        rules = rule_score(job, settings)
        fresh = freshness_score(job, now=now)
        results.append(
            ScoreComponents(
                embed_similarity=round(similarity, 6),
                rule_score=rules,
                freshness_score=fresh,
                total_score=blend(similarity, rules, fresh),
                is_stretch=chooser.random() < STRETCH_RATE,
            )
        )
    return results
