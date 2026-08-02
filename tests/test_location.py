"""The location screen.

Every reject here permanently removes a job from the queue, so the tests lean
hard on the ambiguous cases: the eight city names shared between the Bay Area
and somewhere else, and the many shapes "this is a US-remote role" takes.
"""

import pytest

from pipeline.filters.location import (
    ELSEWHERE,
    LOCAL,
    UNKNOWN,
    US,
    US_WIDE,
    classify_location,
    location_verdict,
)


def cls(location, description=""):
    return classify_location(location=location, description=description)


# --- the Bay Area ------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "San Francisco",
        "San Francisco, CA",
        "San Francisco, California",
        "San Francisco, California, United States",
        "Oakland, CA",
        "Mountain View, California",
        "Palo Alto",
        "South San Francisco, CA",
        "SF Bay Area",
        "Silicon Valley",
        "East Bay",
    ],
)
def test_bay_area_places_are_local(location):
    assert cls(location) == LOCAL


def test_a_california_state_code_does_not_defeat_a_bay_area_city():
    # "ca" is a US-state signal and San Francisco is a Bay Area city. Resolving
    # the state first would classify every Bay Area posting as merely US and
    # reject every hybrid role in the target market.
    assert cls("San Francisco, CA") == LOCAL


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        # These eight city names exist both in the Bay Area and elsewhere. The
        # qualifier has to win, or "Newark, NJ" reads as a 20-minute commute.
        ("Newark, CA", LOCAL),
        ("Newark, NJ", US),
        ("Newark, New Jersey", US),
        ("Richmond, CA", LOCAL),
        ("Richmond, VA", US),
        ("Dublin, CA", LOCAL),
        ("Dublin, Ireland", ELSEWHERE),
        ("Brisbane, CA", LOCAL),
        ("Brisbane, Australia", ELSEWHERE),
        ("Cambridge, MA", US),
        ("Cambridge, UK", ELSEWHERE),
        ("San Jose, CA", LOCAL),
        ("San José, Costa Rica", ELSEWHERE),
    ],
)
def test_an_ambiguous_city_is_resolved_by_its_qualifier(location, expected):
    assert cls(location) == expected


# --- multi-location postings -------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "San Francisco, CA | New York City, NY",
        "San Francisco, CA • New York, NY • United States",
        "Mountain View, California; San Francisco, California",
        "New York, NY | San Francisco, CA | Seattle, WA",
        "SF or Remote",
        "London, UK / San Francisco, CA",
    ],
)
def test_any_listed_office_in_the_bay_makes_it_local(location):
    """A posting open to several offices is open to the local one.

    Reading only the first component would reject every "New York | San
    Francisco" role -- and those are among the best-paying in the corpus.
    """
    assert cls(location) == LOCAL


def test_a_multi_country_remote_scope_keeps_the_us_half():
    # Real string from HN. Taking the strictest component would drop it.
    assert cls("REMOTE (US + Canada)") == US_WIDE


# --- the United States -------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "Remote US",
        "Remote (US)",
        "Remote - US",
        "Remote U.S.",
        "United States",
        "Remote (North America)",
        "Remote, USA",
        "Anywhere in the US",
        "Nationwide",
        "Distributed",
    ],
)
def test_a_us_scope_with_no_place_named_is_us_wide(location):
    """Distinct from `us`, and the distinction decides 111 live jobs.

    Databricks, Cloudflare, ClickHouse, Stripe and Brex publish onsite roles
    whose location field says only "United States" or "Distributed". That is
    the employer declining to say where, not a statement that the office is
    outside the Bay Area.
    """
    assert cls(location) == US_WIDE


@pytest.mark.parametrize(
    "location",
    ["Anywhere", "Worldwide", "Remote (Global)", "Fully Distributed"],
)
def test_an_unrestricted_scope_counts_as_reachable(location):
    """ "Worldwide" includes the United States.

    Treating it as foreign would reject the most permissive postings there are.
    """
    assert cls(location) == US_WIDE


@pytest.mark.parametrize(
    "location",
    [
        "Seattle",
        "Seattle, Washington, United States",
        "New York, NY (HQ)",
        "New York, New York",
        "Austin, TX",
        "Boston",
        "Los Angeles, CA",
        "Sacramento, California",
        "Washington, DC",
    ],
)
def test_us_places_outside_the_bay_are_us_not_local(location):
    assert cls(location) == US


# --- outside the country -----------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "Remote Canada",
        "Remote - Canada",
        "Remote Spain",
        "Remote Poland",
        "Bangalore, India",
        "Bengaluru, India",
        "London, UK",
        "EMEA",
        "REMOTE (EU)",
        "Tokyo",
        "Belgrade, Serbia",
        "São Paulo, São Paulo, Brazil",
        "Vancouver, British Columbia, Canada",
        "Remote (LATAM)",
    ],
)
def test_foreign_places_are_elsewhere(location):
    assert cls(location) == ELSEWHERE


def test_latin_america_is_not_read_as_america():
    # "america" is a US-wide marker and a substring of the region name. The
    # longer, more specific match has to win.
    assert cls("Remote - Latin America") == ELSEWHERE


# --- no information ----------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [None, "", "   ", "N/A", "Hybrid", "In-Office", "ONSITE", "Remote", "-"],
)
def test_a_location_with_no_geography_is_unknown(location):
    """Unknown is not a rejection.

    Roughly 50 live postings say only "N/A" or "Onsite". Guessing on them
    would delete real jobs to tidy the queue; they pass and are badged instead.
    """
    assert cls(location) == UNKNOWN


def test_us_is_not_matched_inside_an_ordinary_word():
    assert cls("Business Operations Hub") == UNKNOWN


# --- the description fallback ------------------------------------------------


def test_a_pipe_delimited_hn_header_supplies_a_missing_location():
    """HN postings put the location in the title line, not in a field.

    "New York City Public Interest Technology" carries location=None and is a
    New York job. Without this the whole HN source is unfilterable.
    """
    header = "NYC PIT Crew | Senior Product Engineer | Full-time | NYC"
    assert cls(None, description=header + " We believe in tech for good.") == US


def test_the_description_is_only_consulted_when_the_field_says_nothing():
    """An office mentioned in boilerplate must not override a stated location.

    Anthropic restates its San Francisco office policy in every posting,
    including the roles based in London.
    """
    body = "London, UK | Engineer | Full-time. We also have a San Francisco office."
    assert cls("London, UK", description=body) == ELSEWHERE


def test_only_the_header_of_a_description_is_read():
    # Prose far into a posting is about the company, not about this role.
    body = "Engineer | Full-time | Remote " + ("x " * 400) + "Bangalore, India"
    assert cls(None, description=body) != ELSEWHERE


# --- the verdict -------------------------------------------------------------


@pytest.mark.parametrize("arrangement", ["remote", "hybrid", "onsite"])
def test_a_local_job_passes_in_every_arrangement(arrangement):
    assert location_verdict(LOCAL, arrangement) is None


def test_a_us_remote_job_passes():
    assert location_verdict(US, "remote") is None


@pytest.mark.parametrize("arrangement", ["remote", "hybrid", "onsite"])
def test_an_unspecified_us_scope_passes_in_every_arrangement(arrangement):
    """The 111-job decision, pinned.

    An onsite role listed as "United States" has an undisclosed office, not a
    disclosed one somewhere else. Rejecting it deleted every Databricks and
    Cloudflare posting in the corpus.
    """
    assert location_verdict(US_WIDE, arrangement) is None


@pytest.mark.parametrize("arrangement", ["hybrid", "onsite"])
def test_a_us_job_requiring_attendance_is_rejected(arrangement):
    """The whole point of the screen: a hybrid role in New York means going to
    an office in New York."""
    assert location_verdict(US, arrangement) == "location_outside_area"


def test_a_foreign_remote_job_is_rejected_with_its_own_reason():
    # Distinct from location_outside_area so the settings breakdown can tell
    # "wrong metro" from "wrong country".
    assert location_verdict(ELSEWHERE, "remote") == "remote_outside_us"


@pytest.mark.parametrize("arrangement", ["hybrid", "onsite"])
def test_a_foreign_onsite_job_is_rejected(arrangement):
    assert location_verdict(ELSEWHERE, arrangement) == "location_outside_area"


@pytest.mark.parametrize("arrangement", ["remote", "hybrid", "onsite"])
def test_an_unknown_location_never_rejects(arrangement):
    assert location_verdict(UNKNOWN, arrangement) is None


# --- the data file -----------------------------------------------------------


def test_no_bay_area_term_is_also_a_foreign_term():
    """A term in both lists resolves by list order rather than by geography,
    which is how a silent mass-exclusion starts."""
    from pipeline.filters.location import BAY_AREA, NON_US

    assert not (BAY_AREA & NON_US)


def test_no_bay_area_term_is_also_a_distant_us_city():
    from pipeline.filters.location import BAY_AREA, US_CITIES

    assert not (BAY_AREA & US_CITIES)


# --- regressions from the live corpus ----------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "San Francisco, Seattle",
        "NYC, SF",
        "SF, NYC, SEA, CHI",
        "DC, SF, NYC",
        "Seattle, San Francisco, or New York",
        "Chicago, San Francisco, Seattle, New York City",
        "US-San Francisco, US-Chicago, US-New York",
        "San Francisco, New York, Seattle, Chicago, Atlanta, Toronto, Remote in the US",
    ],
)
def test_a_comma_separated_office_list_keeps_its_bay_area_entry(location):
    """25 live jobs, all at companies with a San Francisco office.

    A comma joins a city to its state and separates one city from the next.
    Reading every comma as a join let the most distant city in the string
    decide, and these were rejected as though the SF office did not exist.
    """
    assert cls(location) == LOCAL


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("San Francisco, CA", LOCAL),
        ("Brooklyn, New York", US),
        ("Seattle, Washington", US),
        ("Bellevue, WA", US),
        ("San Francisco, California, United States", LOCAL),
    ],
)
def test_a_trailing_state_still_qualifies_rather_than_splits(location, expected):
    assert cls(location) == expected


@pytest.mark.parametrize("location", ["Dublin, London", "London, Dublin"])
def test_a_bare_ambiguous_city_does_not_claim_the_bay_area(location):
    """Bare "Dublin" means Ireland far more often than Alameda County.

    Claiming it as local put three Dublin-and-London roles in the queue as
    though they were a twenty-minute drive.
    """
    assert cls(location) == ELSEWHERE


def test_an_ambiguous_city_is_local_once_california_is_stated():
    assert cls("Dublin, CA") == LOCAL


def test_in_office_is_not_read_as_indiana():
    """`^in` matched the leading "In" of "In-Office".

    Sixteen live Stripe-style postings were rejected as though they were in
    Indianapolis. State codes are only trusted after a comma.
    """
    assert cls("In-Office") == UNKNOWN


def test_a_foreign_city_does_not_qualify_the_city_before_it():
    """ "SF, London, Metro DC" is three offices, one of them local.

    Countries qualify ("San José, Costa Rica"); cities do not. Treating London
    as a qualifier merged it onto SF and rejected the role.
    """
    assert cls("SF, London, Metro DC (On-site)") == LOCAL


def test_an_airport_code_names_the_city():
    # "SFO, SEA, NYC, ATX" is how Hacker News writes an office list.
    assert cls("SFO, SEA, NYC, ATX (Onsite)") == LOCAL


def test_country_and_city_vocabularies_do_not_overlap():
    """Only countries qualify. A term in both lists would restore the bug
    where a foreign city swallowed the place before it."""
    from pipeline.filters.location import NON_US_CITIES, NON_US_COUNTRIES

    assert not (NON_US_COUNTRIES & NON_US_CITIES)
