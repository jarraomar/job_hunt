"""Application preparation.

The invariant these tests exist to protect: nothing here ever submits anything.
Spec section 3 forbids authenticating as Jarra on any job platform, so "safe for
prefill" means "safe to work out the answers in advance" and never "safe to
drive". test_no_submission_path_exists pins that.
"""

from datetime import date, timedelta

import pytest

from pipeline.apply.answers import (
    BANK,
    MISSING,
    PROFILE,
    render,
    resolve_all,
    resolve_one,
    start_date,
    variables,
)
from pipeline.apply.platforms import MANUAL, STRUCTURED, UNKNOWN, detect_platform
from pipeline.apply.questions import BY_KEY, match_question
from pipeline.config import load_settings
from pipeline.profile import Profile
from pipeline.store import answer_bank_all, unmapped_questions, upsert_answer

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y"})

PROFILE_FIXTURE = Profile(
    resume={
        "summary": "Full stack engineer.",
        "experience": [
            {"company": "CloudBase", "title": "Full Stack Engineer", "start": "2023-06"},
            {"company": "Earlier", "title": "Junior Developer", "start": "2021-01"},
        ],
    },
    competency_bullets=[],
    identity={
        "name": "Jarra Omar",
        "email": "jarra@example.com",
        # Deliberately a reserved-for-fiction number. Test fixtures get read,
        # copied, and committed; the real one belongs only in the profile
        # directory, which lives outside this repository.
        "phone": "(555) 010-0100",
        "city": "San Leandro",
        "state": "California",
        "work_authorization": "US-born citizen",
    },
)

SATURDAY = date(2026, 8, 1)


# --- platforms ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://jobs.lever.co/acme/abc-123", "lever"),
        ("https://jobs.ashbyhq.com/acme/abc", "ashby"),
        ("https://acme.wd1.myworkdayjobs.com/careers/job/123", "workday"),
        ("https://careers.icims.com/jobs/123", "icims"),
    ],
)
def test_a_platform_is_recognised_from_its_apply_link(url, key):
    assert detect_platform(url).key == key


def test_a_lookalike_host_is_not_matched():
    """A substring test on "lever.co" also matches "lever.co.evil.example".

    The result decides what the UI tells Jarra to expect from the form, so it
    is matched on label boundaries.
    """
    assert detect_platform("https://lever.co.evil.example/apply") is UNKNOWN


@pytest.mark.parametrize("url", [None, "", "not a url", "https://acme.com/careers"])
def test_an_unrecognised_link_falls_back_to_unknown(url):
    assert detect_platform(url) is UNKNOWN


def test_an_unknown_platform_is_treated_as_manual():
    """Unknown means nobody has looked at the form.

    Defaulting it to `structured` would present a confident field mapping for
    a form whose fields have never been seen.
    """
    assert UNKNOWN.prefill == MANUAL


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/acme/jobs/1",
        "https://jobs.lever.co/acme/1",
        "https://jobs.ashbyhq.com/acme/1",
    ],
)
def test_the_single_page_platforms_are_structured(url):
    assert detect_platform(url).prefill == STRUCTURED


def test_workday_is_never_structured():
    # Per-tenant wizard, account required, question set unknowable in advance.
    assert detect_platform("https://acme.wd5.myworkdayjobs.com/x").prefill == MANUAL


def test_no_submission_path_exists():
    """Spec section 3: no automated process authenticates as Jarra anywhere.

    A `submit`, `apply`, or `login` callable appearing in this package is the
    single change that would break that guarantee, so its absence is asserted
    rather than trusted.
    """
    import pipeline.apply.answers as answers_mod
    import pipeline.apply.platforms as platforms_mod
    import pipeline.apply.questions as questions_mod

    forbidden = {"submit", "apply", "login", "authenticate", "fill_form", "post_application"}
    for module in (answers_mod, platforms_mod, questions_mod):
        assert not forbidden & set(vars(module)), module.__name__


# --- question matching -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("When can you start a new role?", "start_date"),
        ("What is your earliest available start date?", "start_date"),
        ("What is your notice period?", "start_date"),
        ("Are you legally authorized to work in the United States?", "work_authorization"),
        ("Will you now or in the future require visa sponsorship?", "sponsorship"),
        ("What are your salary expectations?", "salary_expectation"),
        ("Desired compensation", "salary_expectation"),
        ("LinkedIn Profile URL", "linkedin"),
        ("Are you willing to relocate?", "relocation"),
        ("How did you hear about this role?", "referral"),
    ],
)
def test_a_form_question_maps_to_its_canonical_key(text, key):
    """Three phrasings of "when can you start" are one question.

    Keyed by text alone they become three answer bank rows that drift apart,
    and the first form to phrase it a fourth way gets nothing.
    """
    assert match_question(text).key == key


def test_authorization_wins_over_sponsorship_when_both_are_asked():
    """ "Authorized to work without sponsorship?" matches both patterns.

    Catalog order decides it, and the authorization reading is the one that
    produces the right answer.
    """
    matched = match_question("Are you authorized to work in the US without sponsorship?")
    assert matched.key == "work_authorization"


def test_an_unrecognised_question_matches_nothing():
    assert match_question("What is your favourite Postgres index type?") is None
    assert match_question("") is None


# --- the start date ----------------------------------------------------------


def test_the_start_date_is_about_two_weeks_out():
    """Jarra's rule: within two weeks of the application being submitted."""
    assert (start_date(SATURDAY) - SATURDAY).days >= 14


@pytest.mark.parametrize("offset", range(7))
def test_the_start_date_is_never_a_weekend(offset):
    """Two weeks from a Saturday is a Saturday.

    Offering to start on a weekend reads as careless on an application, so the
    date rolls to the following Monday.
    """
    assert start_date(date(2026, 8, 1) + timedelta(days=offset)).weekday() == 0


def test_the_start_date_moves_with_the_day_it_is_asked():
    """The whole reason answers are templates.

    A literal date stored in the answer bank is wrong the following week.
    """
    assert start_date(date(2026, 8, 1)) != start_date(date(2026, 9, 1))


# --- templates ---------------------------------------------------------------


def test_a_template_is_filled_from_the_profile():
    values = variables(PROFILE_FIXTURE, SETTINGS, today=SATURDAY)
    assert render("Reach me at {{email}}.", values) == "Reach me at jarra@example.com."


def test_a_template_with_a_missing_variable_renders_empty():
    """Half an answer looks filled in and gets pasted into a real application.

    "I can start on , about two weeks from today" is worse than no answer,
    because nothing about it says it is broken.
    """
    values = variables(PROFILE_FIXTURE, SETTINGS, today=SATURDAY)
    assert render("Find me at {{github}}.", values) == ""


def test_an_unknown_variable_name_also_renders_empty():
    assert render("{{not_a_variable}}", {}) == ""


def test_years_of_experience_is_derived_from_the_resume():
    values = variables(PROFILE_FIXTURE, SETTINGS, today=date(2026, 8, 1))
    # Earliest dated role starts 2021-01.
    assert values["years_experience"] == "6"


def test_years_of_experience_is_empty_when_no_role_is_dated():
    bare = Profile(resume={"experience": [{"company": "X"}]}, competency_bullets=[], identity={})
    assert variables(bare, SETTINGS, today=SATURDAY)["years_experience"] == ""


# --- resolution --------------------------------------------------------------


async def test_the_profile_answers_what_it_can(db):
    resolved = {r.key: r for r in await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY)}
    assert resolved["email"].answer == "jarra@example.com"
    assert resolved["email"].source == PROFILE
    assert "US-born citizen" in resolved["work_authorization"].answer


async def test_the_start_date_answer_names_a_real_day(db):
    resolved = {r.key: r for r in await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY)}
    # Two weeks after Saturday 1 August 2026 is the 15th; the Monday after is
    # the 17th.
    assert "Monday, August 17, 2026" in resolved["start_date"].answer


async def test_self_identification_questions_are_never_answered_for_him(db):
    """Race, gender, veteran and disability status are Jarra's to answer or
    decline. A system that filled them from a profile would answer for him."""
    resolved = {r.key: r for r in await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY)}
    for key in ("gender", "race", "veteran_status", "disability_status"):
        assert resolved[key].answer == ""
        assert resolved[key].needs_review


async def test_an_unanswerable_question_becomes_a_gap_to_fill(db):
    """Jarra's requirement: a question needing review lands in the answer bank
    so a later agent knows where to get the answer from."""
    await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY)
    gaps = {row["question_key"] for row in await unmapped_questions(db)}
    assert "relocation" in gaps
    assert "referral" in gaps


async def test_answering_a_gap_removes_it_and_wins_from_then_on(db):
    await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY)
    await upsert_answer(db, "Are you willing to relocate?", "No.", question_key="relocation")

    resolved = {r.key: r for r in await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY)}
    assert resolved["relocation"].answer == "No."
    assert resolved["relocation"].source == BANK
    assert "relocation" not in {r["question_key"] for r in await unmapped_questions(db)}


async def test_a_stored_answer_beats_the_profile(db):
    """He is allowed to disagree with his own profile, and it has to stick."""
    await upsert_answer(db, "When can you start?", "Immediately.", question_key="start_date")
    resolved = {r.key: r for r in await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY)}
    assert resolved["start_date"].answer == "Immediately."


async def test_a_stored_answer_can_itself_be_a_template(db):
    """The point of Jarra's start-date example.

    He writes the rule once; the date is computed whenever an application is
    actually prepared.
    """
    await upsert_answer(
        db, "When can you start?", "Available from {{start_date}}.", question_key="start_date"
    )
    resolved = {r.key: r for r in await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY)}
    assert resolved["start_date"].answer == "Available from Monday, August 17, 2026."


async def test_the_same_question_asked_differently_hits_one_row(db):
    """ "Notice period?" and "When can you start?" must not become two answers
    that drift apart."""
    await upsert_answer(db, "When can you start?", "Two weeks.", question_key="start_date")
    await upsert_answer(db, "What is your notice period?", "Ten days.", question_key="start_date")
    rows = [r for r in await answer_bank_all(db) if r["question_key"] == "start_date"]
    assert len(rows) == 1
    assert rows[0]["answer"] == "Ten days."


async def test_recording_a_gap_counts_sightings_rather_than_duplicating(db):
    for _ in range(3):
        await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY)
    rows = {r["question_key"]: r for r in await unmapped_questions(db)}
    assert rows["relocation"]["seen_count"] == 3


async def test_gaps_can_be_resolved_without_recording_them(db):
    await resolve_all(db, PROFILE_FIXTURE, SETTINGS, today=SATURDAY, record_gaps=False)
    assert await unmapped_questions(db) == []


# --- the single-question entry point -----------------------------------------


def test_one_question_can_be_answered_without_a_database():
    """The entry point for an agent holding a question it just read off a form."""
    answer = resolve_one(
        "When can you start a new role?", PROFILE_FIXTURE, SETTINGS, today=SATURDAY
    )
    assert "Monday, August 17, 2026" in answer.answer


def test_an_unrecognised_question_resolves_to_nothing():
    assert resolve_one("Favourite index type?", PROFILE_FIXTURE, SETTINGS, today=SATURDAY) is None


def test_a_recognised_question_with_no_default_reports_that_it_needs_review():
    answer = resolve_one("Are you willing to relocate?", PROFILE_FIXTURE, SETTINGS, today=SATURDAY)
    assert answer.source == MISSING
    assert answer.needs_review


def test_every_catalog_key_is_unique():
    assert len(BY_KEY) == len({q.key for q in BY_KEY.values()})
