from datetime import UTC, datetime

from pipeline.models import RawJob
from pipeline.normalize import (
    classify_remote,
    compute_fingerprint,
    normalize_arrangement,
    normalize_city,
    normalize_company,
    normalize_title,
    to_job,
)


def test_normalize_company_strips_suffixes_and_case():
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_company("ACME Inc") == "acme"
    assert normalize_company("  Acme   Corporation ") == "acme"
    assert normalize_company("Acme LLC") == "acme"


def test_normalize_company_keeps_distinct_names_distinct():
    assert normalize_company("Acme Health") != normalize_company("Acme")


def test_normalize_company_never_returns_empty():
    # A name made entirely of legal suffixes must not normalize away to "",
    # which would collide with every other such company under one key.
    assert normalize_company("Inc.") == "inc"
    assert normalize_company("Co") == "co"


def test_normalize_company_does_not_strip_suffixes_inside_words():
    assert normalize_company("Coinbase") == "coinbase"
    assert normalize_company("Incredible Machines") == "incredible machines"


def test_normalize_title_strips_noise():
    assert normalize_title("Senior Software Engineer (Remote)") == "senior software engineer"
    assert normalize_title("Software Engineer II - Backend") == "software engineer ii backend"
    assert normalize_title("  Full-Stack   Engineer  ") == "full stack engineer"


def test_normalize_title_keeps_meaningful_parentheticals():
    # Real, separately-open Anthropic roles. Stripping every parenthetical
    # collapses them into one job and the other silently never reaches the queue.
    a = normalize_title("Research Engineer, Machine Learning (Reinforcement Learning)")
    b = normalize_title("Research Engineer, Machine Learning (RL Velocity)")
    assert a != b
    assert "reinforcement learning" in a

    assert normalize_title("Product Manager, Safeguards (Child Safety)") != normalize_title(
        "Product Manager, Safeguards (Verticals)"
    )
    assert normalize_title("Staff Software Engineer (CI/CD)") != normalize_title(
        "Staff Software Engineer (Developer Tools)"
    )


def test_normalize_title_still_drops_work_arrangement_parentheticals():
    assert normalize_title("Senior Engineer (Remote)") == "senior engineer"
    assert normalize_title("Senior Engineer (Remote - US)") == "senior engineer"
    assert normalize_title("Senior Engineer (Hybrid)") == "senior engineer"
    assert normalize_title("Senior Engineer (On-site)") == "senior engineer"


def test_genuinely_distinct_roles_keep_distinct_fingerprints():
    a = compute_fingerprint("Anthropic", "Research Engineer, ML (Reinforcement Learning)", "London")
    b = compute_fingerprint("Anthropic", "Research Engineer, ML (RL Velocity)", "London")
    assert a != b


def test_normalize_city_extracts_first_component():
    assert normalize_city("San Francisco, CA, USA") == "san francisco"
    assert normalize_city("Remote - US") == "remote"
    assert normalize_city(None) == ""


def test_fingerprint_collapses_trivial_variants():
    a = compute_fingerprint(
        "Acme, Inc.", "Senior Software Engineer (Remote)", "San Francisco, CA, USA"
    )
    b = compute_fingerprint("ACME Inc", "Senior Software Engineer", "San Francisco, CA")
    assert a == b


def test_fingerprint_separates_real_differences():
    base = compute_fingerprint("Acme", "Software Engineer", "San Francisco, CA")
    assert base != compute_fingerprint("Acme", "Staff Software Engineer", "San Francisco, CA")
    assert base != compute_fingerprint("Acme", "Software Engineer", "Austin, TX")
    assert base != compute_fingerprint("Globex", "Software Engineer", "San Francisco, CA")


def test_fingerprint_is_stable_and_hex():
    fp = compute_fingerprint("Acme", "Engineer", "SF")
    assert fp == compute_fingerprint("Acme", "Engineer", "SF")
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_field_separator_cannot_be_forged():
    # Concatenating fields without an unambiguous separator lets one job's
    # company bleed into another's title and collide by accident.
    assert compute_fingerprint("acme x", "engineer", "sf") != compute_fingerprint(
        "acme", "x engineer", "sf"
    )


def test_classify_remote():
    assert classify_remote("Engineer", "Remote - US", "", None) == "remote"
    assert classify_remote("Engineer", "San Francisco, CA", "", "Remote") == "remote"
    assert (
        classify_remote(
            "Engineer", "San Francisco, CA", "This is a hybrid role, 3 days in office", None
        )
        == "hybrid"
    )
    assert classify_remote("Engineer", "San Francisco, CA", "Onsite position", None) == "onsite"
    assert classify_remote("Engineer", None, "", None) == "onsite"


def test_classify_remote_reads_the_title():
    # "(Remote)" in the title is one of the most common ways a posting signals
    # this, and the location field often still names an office.
    assert classify_remote("Senior Engineer (Remote)", "San Francisco, CA", "", None) == "remote"


def test_classify_remote_respects_negation():
    # "This role is not remote" must not classify as remote.
    assert classify_remote("Engineer", "Austin, TX", "This position is not remote.", None) == (
        "onsite"
    )
    assert classify_remote("Engineer", "Austin, TX", "No remote work available.", None) == "onsite"


def test_to_job_parses_salary_from_description_when_absent():
    raw = RawJob(
        source="greenhouse",
        source_job_id="1",
        company_name="Acme, Inc.",
        title="Senior Software Engineer (Remote)",
        location="San Francisco, CA",
        description="The base pay range is $150,000 - $200,000.",
        apply_url="https://example.com/1",
        posted_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    job = to_job(raw)
    assert job.salary_min == 150_000
    assert job.salary_max == 200_000
    assert job.salary_source == "parsed"
    assert job.normalized_company == "acme"
    assert job.remote_type == "remote"
    assert len(job.fingerprint) == 64


def test_to_job_preserves_structured_salary():
    raw = RawJob(
        source="ashby",
        source_job_id="2",
        company_name="Globex",
        title="Engineer",
        location="Remote",
        description="No numbers here.",
        apply_url="https://example.com/2",
        posted_at=None,
        salary_min=160_000,
        salary_max=190_000,
        salary_source="structured",
    )
    job = to_job(raw)
    assert (job.salary_min, job.salary_max) == (160_000, 190_000)
    assert job.salary_source == "structured"


def test_normalize_company_strips_international_and_pbc_suffixes():
    assert normalize_company("Anthropic PBC") == "anthropic"
    assert normalize_company("Acme Pte Ltd") == "acme"
    assert normalize_company("The Walt Disney Company") == "the walt disney"


def test_normalize_company_keeps_group_and_holdings():
    # These routinely distinguish separate legal entities, so collapsing them
    # would merge genuinely different employers.
    assert normalize_company("Acme Group") != normalize_company("Acme")
    assert normalize_company("Acme Holdings") != normalize_company("Acme")


def test_normalize_city_handles_code_prefixed_locations():
    # Workday-style. Taking the first component yields "us" for every posting,
    # which collapses same-titled jobs in different cities onto one fingerprint.
    assert normalize_city("US-CA-San Francisco") == "san francisco"
    assert normalize_city("USA-TX-Austin") == "austin"
    # A long first component is a real city and must still win.
    assert normalize_city("San Francisco, CA") == "san francisco"


def test_code_prefixed_locations_do_not_collide_across_cities():
    a = compute_fingerprint("Acme", "Software Engineer", "US-CA-San Francisco")
    b = compute_fingerprint("Acme", "Software Engineer", "US-TX-Austin")
    assert a != b


def test_boilerplate_hybrid_policy_does_not_override_a_remote_location():
    """Anthropic restates a hybrid office policy in every job description.

    Reading the description before the location classified all 400 of their
    open roles as hybrid — including ones explicitly posted as remote.
    """
    boilerplate = (
        "Location-based hybrid policy: currently, we expect all staff to be in "
        "one of our offices at least 25% of the time."
    )
    assert classify_remote("Engineer", "Remote - US", boilerplate, None) == "remote"
    assert classify_remote("Engineer (Remote)", "San Francisco, CA", boilerplate, None) == "remote"
    # With no competing signal the boilerplate still stands.
    assert classify_remote("Engineer", "San Francisco, CA", boilerplate, None) == "hybrid"


def test_classify_remote_respects_negation_in_either_direction():
    assert (
        classify_remote("Engineer", "Bengaluru, India", "Remote work is not available.", None)
        == "onsite"
    )
    assert (
        classify_remote("Engineer", "Austin, TX", "This position is not remote.", None) == "onsite"
    )


def test_to_job_reports_none_when_salary_is_absent_everywhere():
    raw = RawJob(
        source="greenhouse",
        source_job_id="3",
        company_name="Acme",
        title="Engineer",
        location="Austin, TX",
        description="We offer competitive compensation.",
        apply_url="https://example.com/3",
        posted_at=None,
    )
    job = to_job(raw)
    assert (job.salary_min, job.salary_max) == (None, None)
    # "none" rather than "parsed": the prefilter must be able to tell a real
    # figure from an absent one, since unknown salary passes the gate.
    assert job.salary_source == "none"


def test_a_stated_arrangement_beats_every_textual_signal():
    """Lever and Ashby publish workplaceType. The employer saying it outright
    outranks anything inferred from a title, a location, or boilerplate."""
    boilerplate = "Location-based hybrid policy: staff are in office 25% of the time."
    assert classify_remote("Engineer (Remote)", "Remote - US", boilerplate, "Hybrid") == "hybrid"
    assert classify_remote("Engineer", "New York, NY", boilerplate, "Remote") == "remote"
    assert classify_remote("Engineer (Remote)", "Remote", "", "On-site") == "onsite"


def test_unrecognised_arrangement_falls_through_to_inference():
    # A source inventing a new value must not silently become "onsite".
    assert classify_remote("Engineer", "Remote - US", "", "Flexible") == "remote"
    assert classify_remote("Engineer", "Austin, TX", "", "") == "onsite"


def test_normalize_arrangement_vocabulary():
    assert normalize_arrangement("Hybrid") == "hybrid"
    assert normalize_arrangement("on-site") == "onsite"
    assert normalize_arrangement("ONSITE") == "onsite"
    assert normalize_arrangement("Fully Remote") == "remote"
    assert normalize_arrangement(None) is None
    assert normalize_arrangement("Flexible") is None
