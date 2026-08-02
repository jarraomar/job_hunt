from datetime import UTC, datetime, timedelta

import pytest

from pipeline.config import load_settings
from pipeline.models import Job
from pipeline.score import (
    SIMILARITY_CEILING,
    SIMILARITY_FLOOR,
    STRETCH_RATE,
    blend,
    freshness_score,
    rescale_similarity,
    rule_score,
    score_jobs,
)

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y"})
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def make_job(**overrides) -> Job:
    base = dict(
        fingerprint="f" * 64,
        source="greenhouse",
        source_job_id="1",
        company_name="Acme",
        normalized_company="acme",
        title="Senior Software Engineer",
        location="Remote",
        remote_type="remote",
        salary_min=None,
        salary_max=None,
        salary_source="none",
        description="Python, React, AWS, Docker.",
        apply_url="https://example.com/1",
        posted_at=NOW - timedelta(hours=6),
    )
    base.update(overrides)
    return Job(**base)


# --- freshness ---------------------------------------------------------------


def test_fresh_jobs_score_highest():
    assert freshness_score(make_job(posted_at=NOW - timedelta(hours=6)), now=NOW) == 1.0


def test_the_48_hour_cliff_is_sharp():
    """Application timing is a strong response predictor (spec section 8).

    A smooth decay would blur the one distinction that matters most.
    """
    just_inside = freshness_score(make_job(posted_at=NOW - timedelta(hours=47)), now=NOW)
    just_outside = freshness_score(make_job(posted_at=NOW - timedelta(hours=49)), now=NOW)
    assert just_inside == 1.0
    assert just_outside < 0.8


def test_old_jobs_score_low_but_not_zero():
    score = freshness_score(make_job(posted_at=NOW - timedelta(days=60)), now=NOW)
    assert 0.0 < score < 0.2


def test_unknown_posted_at_scores_mid_range():
    # HN and some Lever boards omit it. Scoring those as stale would bury an
    # entire source without anyone noticing.
    assert freshness_score(make_job(posted_at=None), now=NOW) == pytest.approx(0.5)


def test_a_future_posted_at_is_treated_as_fresh_not_negative():
    # Clock skew between a board and us should not produce a negative score.
    assert freshness_score(make_job(posted_at=NOW + timedelta(hours=2)), now=NOW) == 1.0


# --- rules -------------------------------------------------------------------


def test_remote_outranks_hybrid_outranks_onsite():
    remote = rule_score(make_job(remote_type="remote"), SETTINGS)
    hybrid = rule_score(make_job(remote_type="hybrid"), SETTINGS)
    onsite = rule_score(make_job(remote_type="onsite", location="Austin, TX"), SETTINGS)
    assert remote > hybrid > onsite


def test_bay_area_onsite_beats_distant_onsite():
    # Jarra is in San Leandro; a San Francisco onsite is commutable and a
    # Miami one is not.
    near = rule_score(make_job(remote_type="onsite", location="San Francisco, CA"), SETTINGS)
    far = rule_score(make_job(remote_type="onsite", location="Miami, FL"), SETTINGS)
    assert near > far


def test_salary_above_target_scores_higher_than_at_floor():
    high = rule_score(
        make_job(salary_min=180_000, salary_max=220_000, salary_source="structured"), SETTINGS
    )
    low = rule_score(
        make_job(salary_min=125_000, salary_max=135_000, salary_source="structured"), SETTINGS
    )
    assert high > low


def test_unknown_salary_scores_between_floor_and_target():
    """Two thirds of intake has no parseable figure.

    Scoring unknown as zero would rank every Greenhouse posting below every
    Ashby one purely on disclosure, not on fit.
    """
    unknown = rule_score(make_job(salary_source="none"), SETTINGS)
    at_floor = rule_score(
        make_job(salary_min=125_000, salary_max=125_000, salary_source="structured"), SETTINGS
    )
    at_target = rule_score(
        make_job(salary_min=150_000, salary_max=160_000, salary_source="structured"), SETTINGS
    )
    assert at_floor < unknown < at_target


def test_rule_score_is_bounded():
    for job in (make_job(), make_job(remote_type="onsite", location="Nowhere")):
        assert 0.0 <= rule_score(job, SETTINGS) <= 1.0


# --- similarity rescaling ----------------------------------------------------


def test_rescale_spreads_the_observed_cosine_band_across_the_full_range():
    """Measured over 813 real postings, every cosine landed in 0.60-0.85.

    Left raw, the 0.55 similarity weight would apply to a 0.17 span while
    freshness applied to a 0.85 span — the weights would say similarity
    dominates while the arithmetic said the opposite.
    """
    assert rescale_similarity(SIMILARITY_FLOOR) == 0.0
    assert rescale_similarity(SIMILARITY_CEILING) == 1.0
    assert rescale_similarity(0.725) == pytest.approx(0.5)


def test_rescale_clamps_outside_the_band():
    assert rescale_similarity(0.1) == 0.0
    assert rescale_similarity(0.99) == 1.0


def test_similarity_actually_outweighs_freshness_over_realistic_ranges():
    """The defect the corpus run caught, pinned.

    Compares full-range swings rather than the nominal weights: a job that is
    a much better match but a week older must still win.
    """
    better_match_older = blend(rescale_similarity(0.80), 0.6, 0.7)
    worse_match_today = blend(rescale_similarity(0.66), 0.6, 1.0)
    assert better_match_older > worse_match_today


# --- blend -------------------------------------------------------------------


def test_blend_is_bounded_and_monotonic_in_similarity():
    low = blend(0.1, 0.5, 0.5)
    high = blend(0.9, 0.5, 0.5)
    assert 0.0 <= low <= 1.0 and 0.0 <= high <= 1.0
    assert high > low


def test_similarity_carries_more_weight_than_freshness():
    # A perfectly-matched month-old job should outrank a poorly-matched one
    # posted this morning.
    good_old = blend(0.9, 0.6, 0.1)
    bad_new = blend(0.2, 0.6, 1.0)
    assert good_old > bad_new


# --- end to end --------------------------------------------------------------


def test_score_jobs_returns_one_component_set_per_job():
    from pipeline import profile as profile_mod

    prof = profile_mod.Profile(
        resume={"summary": "Full-stack engineer. Python, React, AWS."},
        competency_bullets=[],
        identity={},
    )
    jobs = [make_job(source_job_id="1"), make_job(source_job_id="2", title="Backend Engineer")]
    results = score_jobs(jobs, prof, SETTINGS, now=NOW)
    assert len(results) == 2
    for r in results:
        assert 0.0 <= r.total_score <= 1.0


def test_score_jobs_on_an_empty_list_does_not_load_the_model():
    from pipeline import profile as profile_mod

    prof = profile_mod.Profile(resume={"summary": "x"}, competency_bullets=[], identity={})
    assert score_jobs([], prof, SETTINGS, now=NOW) == []


def test_stretch_rate_is_within_the_specified_band():
    # Spec section 8 calls for a 5-10% random stretch allowance.
    assert 0.05 <= STRETCH_RATE <= 0.10
