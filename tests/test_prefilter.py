import pytest

from pipeline.config import load_settings
from pipeline.filters.prefilter import prefilter
from pipeline.models import Job

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y"})


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
        posted_at=None,
    )
    base.update(overrides)
    return Job(**base)


def test_relevant_role_passes():
    assert prefilter(make_job(), SETTINGS).passed


def test_reason_is_none_when_passed():
    assert prefilter(make_job(), SETTINGS).reason is None


# --- salary ------------------------------------------------------------------


def test_salary_below_floor_is_rejected():
    result = prefilter(
        make_job(salary_min=90_000, salary_max=110_000, salary_source="structured"), SETTINGS
    )
    assert not result.passed
    assert result.reason == "salary_below_floor"


def test_salary_at_floor_passes():
    assert prefilter(
        make_job(salary_min=125_000, salary_max=140_000, salary_source="structured"), SETTINGS
    ).passed


def test_unknown_salary_passes():
    """The single most consequential rule in this gate.

    Measured across five live sources, 355 of 1001 postings carried no
    parseable salary at all — including 149 from Greenhouse and 117 from HN.
    Rejecting on unknown would discard a third of real intake.
    """
    assert prefilter(make_job(salary_source="none"), SETTINGS).passed


def test_max_above_floor_passes_even_if_min_below():
    # A $115k-$160k band is worth seeing; the floor is about the ceiling.
    assert prefilter(
        make_job(salary_min=115_000, salary_max=160_000, salary_source="parsed"), SETTINGS
    ).passed


def test_single_figure_below_floor_is_rejected():
    assert (
        prefilter(
            make_job(salary_min=95_000, salary_max=95_000, salary_source="parsed"), SETTINGS
        ).reason
        == "salary_below_floor"
    )


# --- titles ------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Full Stack Developer",
        "Full-Stack Engineer",
        "Backend Engineer, Platform",
        "Frontend Engineer",
        "Cloud Infrastructure Engineer",
        "DevOps Engineer",
        "Machine Learning Engineer",
        "AI Engineer",
        "Embedded Software Engineer",
        "Firmware Engineer",
        "Computer Engineer",
        "Site Reliability Engineer",
        "Platform Engineer",
        "Software Engineer II",
        "Engineer, Backend",
        "Senior Python Engineer",
    ],
)
def test_target_titles_pass(title):
    assert prefilter(make_job(title=title), SETTINGS).passed, title


@pytest.mark.parametrize(
    "title",
    [
        "Registered Nurse",
        "Account Executive",
        "Warehouse Associate",
        "Senior Graphic Designer",
        "Business Development Representative",
        "Technical Recruiter",
    ],
)
def test_off_target_titles_are_rejected(title):
    result = prefilter(make_job(title=title), SETTINGS)
    assert not result.passed
    assert result.reason == "title_not_target"


@pytest.mark.parametrize(
    "title",
    ["Engineering Manager", "VP of Engineering", "Director of Product", "Head of Platform"],
)
def test_management_roles_are_rejected_as_off_target(title):
    # Not a seniority mismatch: management is a different job, not the same job
    # at a higher level. Keeping the reasons distinct is what makes the run_log
    # breakdown answer "wrong level" vs "wrong function".
    result = prefilter(make_job(title=title), SETTINGS)
    assert not result.passed
    assert result.reason == "management_role"


@pytest.mark.parametrize(
    "title", ["Senior Staff Engineer", "Principal Engineer", "Distinguished Engineer"]
)
def test_far_above_level_titles_are_rejected(title):
    result = prefilter(make_job(title=title), SETTINGS)
    assert not result.passed
    assert result.reason == "seniority_mismatch"


def test_internship_is_rejected():
    assert prefilter(make_job(title="Software Engineering Intern"), SETTINGS).reason == (
        "title_not_target"
    )


def test_senior_is_not_treated_as_over_level():
    # "Senior" is the target band, not above it.
    assert prefilter(make_job(title="Senior Backend Engineer"), SETTINGS).passed


# --- clearance and citizenship (spec section 15.11) --------------------------


@pytest.mark.parametrize(
    "text",
    [
        "US Citizenship required for this position.",
        "Must be able to obtain a security clearance.",
        "This role is subject to ITAR regulations.",
        "Candidates must be U.S. Persons under ITAR.",
        "Ability to obtain and maintain a TS/SCI clearance.",
    ],
)
def test_clearance_and_citizenship_language_passes(text):
    """Jarra is a US-born citizen, so these are eligibility *matches*.

    "Clearance" reads like an exclusion term and is exactly the rule a future
    edit would add by mistake. The defense and aerospace embedded roles that
    carry this language also face a structurally smaller applicant pool.
    """
    result = prefilter(make_job(description=text), SETTINGS)
    assert result.passed, f"{text!r} was rejected as {result.reason}"


def test_clearance_in_the_title_also_passes():
    assert prefilter(
        make_job(title="Embedded Software Engineer - TS/SCI Required"), SETTINGS
    ).passed


# --- ordering ----------------------------------------------------------------


def test_title_is_checked_before_salary():
    # A nurse posting with a great salary is still a nurse posting, and the
    # reason recorded should say so.
    result = prefilter(
        make_job(
            title="Registered Nurse",
            salary_min=200_000,
            salary_max=250_000,
            salary_source="structured",
        ),
        SETTINGS,
    )
    assert result.reason == "title_not_target"


# --- the role head names the job; what follows a comma names the team --------


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer, Ads Manager",
        "Backend Engineer, Director Tools",
        "Full Stack Engineer - Principal Product Line",
        "Software Engineer, Staff Scheduling",
    ],
)
def test_management_or_seniority_words_after_a_comma_do_not_reject(title):
    # These are IC engineering roles whose *product* is named after a manager,
    # director, or staffing concept. Matching the whole title rejected them.
    assert prefilter(make_job(title=title), SETTINGS).passed, title


@pytest.mark.parametrize(
    "title",
    ["Manager, Forward Deployed Engineer", "Product Manager, Developer Productivity"],
)
def test_management_in_the_head_still_rejects(title):
    assert prefilter(make_job(title=title), SETTINGS).reason == "management_role"


@pytest.mark.parametrize(
    "title", ["Founding Engineer", "Research Engineer", "Data Engineer", "Security Engineer"]
)
def test_adjacent_engineering_families_pass(title):
    # This gate errs toward passing: a false accept is corrected by scoring,
    # a false reject is silent and permanent.
    assert prefilter(make_job(title=title), SETTINGS).passed, title


@pytest.mark.parametrize(
    "title",
    [
        "Solutions Engineer, Pre-Sales",
        "Field Service Engineer (Automotive)",
        "Product Support Engineer",
        "Technical Recruiter",
    ],
)
def test_customer_facing_engineering_titles_are_still_rejected(title):
    # "Engineer" in the title does not make it an engineering job.
    assert not prefilter(make_job(title=title), SETTINGS).passed, title
