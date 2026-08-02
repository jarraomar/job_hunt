"""The location screen: is this job reachable from San Leandro?

Jarra commutes in the Bay Area and works remotely for US employers. Everything
else -- a hybrid role in New York, a remote role scoped to Canada or the EU --
is unreachable no matter how well it scores, and a queue full of them is a
queue nobody trusts.

Five classes, four of which pass:

- ``local``     a Bay Area place. Passes in every arrangement.
- ``us_wide``   the US with no place named ("United States", "Distributed").
                Passes in every arrangement -- see below.
- ``us``        a named US place outside the Bay. Passes only if remote.
- ``elsewhere`` outside the country. Never passes.
- ``unknown``   no geographic information at all. **Passes**, and is badged.

Two of those decisions are the whole design, and both were made by reading the
live corpus rather than by reasoning about it:

**`unknown` passes.** Around fifty live postings say only "N/A", "Onsite", or
nothing -- Stripe publishes no location field, and half of Hacker News puts it
in the title line. Guessing on those to tidy the queue would silently delete
real jobs. The pre-filter's asymmetry note applies here with more force than
anywhere else, because this screen rejects on geography *inferred from a
free-text field*.

**`us_wide` passes even for onsite roles.** Databricks, Cloudflare, ClickHouse,
Stripe and Brex list 111 live roles whose location is literally "United States"
or "Distributed" while `remote_type` says onsite. That string does not mean
"an office outside the Bay Area"; it means the employer did not say. Folding it
in with named metros rejected all 111, including roles at companies
headquartered fifteen miles away.

Three mechanisms keep the inference honest:

1. **A qualifier beats a bare city name.** "Newark, NJ" resolves as New Jersey
   because the state is a more specific signal than the city.
2. **A comma both joins and separates**, and the difference decides real jobs.
   "San Francisco, CA" is one place; "San Francisco, Seattle" is two. Treating
   every comma as a join rejected 25 roles with a Bay Area office because a
   more distant city appeared later in the same string. Comma parts are split,
   then a part that is *only* a state or country is re-attached to the place it
   qualifies.
3. **The description is only read when the field says nothing**, and only its
   header. Anthropic restates its San Francisco office policy in every posting,
   including roles based in London; reading prose would relocate all of them.

The vocabulary lives in geography.yaml so a wrong term can be fixed without
touching this logic, and scripts/location_report.py prints what the current
lists do to the live corpus before a change is committed.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

LOCAL = "local"
US_WIDE = "us_wide"
US = "us"
ELSEWHERE = "elsewhere"
UNKNOWN = "unknown"

CLASSES = (LOCAL, US_WIDE, US, ELSEWHERE, UNKNOWN)

# What each class means to the queue, in the order the settings page shows them.
CLASS_LABELS = {
    LOCAL: "Bay Area",
    US_WIDE: "US, unspecified",
    US: "US, outside the Bay",
    ELSEWHERE: "outside the US",
    UNKNOWN: "not stated",
}

_DATA = yaml.safe_load(Path(__file__).with_name("geography.yaml").read_text())


def _terms(section: str) -> frozenset[str]:
    return frozenset(t.lower() for t in (_DATA.get(section) or []))


BAY_AREA = _terms("bay_area")
BAY_AREA_AMBIGUOUS = _terms("bay_area_ambiguous")
US_WIDE_TERMS = _terms("us_wide")
ANYWHERE = _terms("anywhere")
US_STATES = _terms("us_states")
US_STATE_CODES = _terms("us_state_codes")
US_CITIES = _terms("us_cities")
NON_US_COUNTRIES = _terms("non_us_countries")
NON_US_CITIES = _terms("non_us_cities")
NON_US = NON_US_COUNTRIES | NON_US_CITIES

# California is a US state that is not, by itself, evidence of the Bay Area.
# It is excluded from the "some other state" test so that "San Francisco, CA"
# stays local -- otherwise the state code would outrank the city and every
# hybrid role in the target market would be rejected.
_LOCAL_STATE = frozenset({"california", "ca"})

_OTHER_STATES = US_STATES - _LOCAL_STATE
_OTHER_STATE_CODES = US_STATE_CODES - _LOCAL_STATE

# Terms that are also ordinary English words. Safe inside a location field,
# which is a list of places; unsafe in a description, where "join us" and
# "global scale" appear constantly. Excluded from the description fallback --
# the cost is a posting classified unknown, which passes.
_LOOSE = frozenset(
    {
        "us",
        "u.s",
        "u.s.",
        "america",
        "americas",
        "global",
        "globally",
        "anywhere",
        "distributed",
        "fully distributed",
        "eu",
        "in",
        "or",
    }
)

# Everything that separates one place from another in a real location string:
#   "San Francisco, CA | New York City, NY"
#   "San Francisco, CA • New York, NY • United States"
#   "Mountain View, California; San Francisco, California"
#   "REMOTE (US + Canada)"
#   "SF or Remote"
#
# The comma is deliberately absent here and handled by _comma_places instead:
# it separates places in "San Francisco, Seattle" but joins them in "San
# Francisco, CA", and splitting it naively strips every qualifier this module
# depends on. The hyphen is absent because it is never a separator in this data
# ("Remote - US", "Winston-Salem", "In-Office").
_SPLIT_RE = re.compile(r"[|•·;/+()\n]|\bor\b|\band\b")

_WS_RE = re.compile(r"\s+")

# How far into a description the header can run. HN's "Company | Role |
# Location | Salary" line is always inside this; company prose is not.
_HEADER_CHARS = 300


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text.lower()).strip()


def _compile(terms: frozenset[str]) -> re.Pattern[str] | None:
    """One alternation per vocabulary, longest term first.

    Longest-first is load-bearing: "america" and "latin america" both match the
    same string, and the shorter one would classify a LATAM-only posting as
    reachable. Python's alternation returns the first branch that matches at a
    position, not the longest, so the order here decides it.
    """
    if not terms:
        return None
    body = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    # Boundaries by lookaround rather than \b: several terms end in "." ("u.s."),
    # where \b would sit in the wrong place.
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])")


def _compile_codes(codes: frozenset[str]) -> re.Pattern[str] | None:
    """State codes, matched only after a comma or as the whole string.

    A bare two-letter token is not evidence of anything: "OR", "IN", "ME", "OK"
    and "HI" are ordinary words, and a free-standing "CA" is as likely to mean
    Canada as California. Requiring the "City, XX" position is what makes the
    signal trustworthy.

    Anchoring at the start of the string is NOT good enough, and the failure is
    instructive: "In-Office" begins with "in", so `^in` read every one of the
    sixteen Stripe-style "In-Office" postings as Indiana and rejected them. The
    only other accepted form is a code that is the entire place.
    """
    if not codes:
        return None
    body = "|".join(re.escape(c) for c in sorted(codes))
    return re.compile(rf",\s*(?:{body})(?![a-z0-9])|^(?:{body})$")


_BAY_RE = _compile(BAY_AREA)
_BAY_AMBIGUOUS_RE = _compile(BAY_AREA_AMBIGUOUS)
_WIDE_RE = _compile(US_WIDE_TERMS | ANYWHERE)
_OTHER_STATE_RE = _compile(_OTHER_STATES)
_OTHER_CODE_RE = _compile_codes(_OTHER_STATE_CODES)
_US_CITY_RE = _compile(US_CITIES)
_NON_US_RE = _compile(NON_US)

_CALIFORNIA_RE = re.compile(r"(?<![a-z0-9])california(?![a-z0-9])")
_CA_CODE_RE = re.compile(r",\s*ca(?![a-z0-9])|^ca$")

_BAY_LOOSE_RE = _compile(BAY_AREA - _LOOSE)
_WIDE_LOOSE_RE = _compile((US_WIDE_TERMS | ANYWHERE) - _LOOSE)
_NON_US_LOOSE_RE = _compile(NON_US - _LOOSE)

# Terms that qualify the place before them rather than naming a new one.
# "San Francisco, California" is one place; "San Francisco, Seattle" is two.
_QUALIFIER_RE = _compile(US_STATES | US_WIDE_TERMS | NON_US_COUNTRIES)
_QUALIFIER_CODE_RE = _compile_codes(US_STATE_CODES)
_US_QUALIFIER_RE = _compile(US_STATES | US_WIDE_TERMS)

# Most permissive first. A posting open to several offices is open to the best
# of them, so the resolution across places takes the strongest class found.
_PRECEDENCE = (LOCAL, US_WIDE, US, ELSEWHERE, UNKNOWN)


def _is_qualifier(part: str) -> bool:
    """True when a comma-separated part is *only* a state, country, or region.

    "CA" and "California" and "United Kingdom" qualify; "Seattle" names a new
    place. Anchored, so "New York" the state qualifies "Brooklyn, New York"
    but "New York, San Francisco" splits into two.
    """
    if _QUALIFIER_CODE_RE and _QUALIFIER_CODE_RE.fullmatch(part):
        return True
    return bool(_QUALIFIER_RE and _QUALIFIER_RE.fullmatch(part))


def _is_us_qualifier(part: str) -> bool:
    if _QUALIFIER_CODE_RE and _QUALIFIER_CODE_RE.fullmatch(part):
        return True
    return bool(_US_QUALIFIER_RE and _US_QUALIFIER_RE.fullmatch(part))


def _comma_places(place: str) -> list[str]:
    """Split on commas, re-attaching qualifiers to the place they qualify.

    One exception, and it decides real jobs: a US state never qualifies an
    unambiguous Bay Area city, because no Bay Area city sits in another state.
    "San Francisco, New York" is two offices, not one -- merging them let New
    York decide and rejected the role. A *foreign* qualifier still merges, so
    "San José, Costa Rica" stays one place and does not read as San Jose.
    """
    parts = [p.strip() for p in place.split(",") if p.strip()]
    out: list[str] = []
    for part in parts:
        if out and _is_qualifier(part):
            contradicts = _is_us_qualifier(part) and _BAY_RE and _BAY_RE.search(out[-1])
            if not contradicts:
                out[-1] = f"{out[-1]}, {part}"
                continue
        out.append(part)
    return out


def _place_class(place: str, *, loose: bool) -> str:
    """Classify one place string.

    Order is precedence, not convenience. A qualifier -- a country, a state, a
    distant metro -- is checked before the bare city name it qualifies, which
    is what separates Newark CA from Newark NJ.
    """
    non_us_re = _NON_US_RE if loose else _NON_US_LOOSE_RE
    bay_re = _BAY_RE if loose else _BAY_LOOSE_RE
    wide_re = _WIDE_RE if loose else _WIDE_LOOSE_RE

    if non_us_re and non_us_re.search(place):
        return ELSEWHERE
    # A state other than California places the job in the US and rules out any
    # Bay Area reading of the city name.
    if _OTHER_STATE_RE and _OTHER_STATE_RE.search(place):
        return US
    if _OTHER_CODE_RE and _OTHER_CODE_RE.search(place):
        return US
    if _US_CITY_RE and _US_CITY_RE.search(place):
        return US
    if bay_re and bay_re.search(place):
        return LOCAL

    in_california = bool(_CALIFORNIA_RE.search(place) or _CA_CODE_RE.search(place))
    if in_california and _BAY_AMBIGUOUS_RE and _BAY_AMBIGUOUS_RE.search(place):
        # "Dublin, CA" is local; bare "Dublin" is not claimed either way.
        return LOCAL
    if wide_re and wide_re.search(place):
        return US_WIDE
    # California with no recognised city: somewhere in the state, not here.
    # Reached only after the Bay Area check, so it means "a California place we
    # do not have a name for" -- Modesto, Chico, Redding.
    if in_california:
        return US
    return UNKNOWN


def _best(places: list[str], *, loose: bool) -> str:
    found: set[str] = set()
    for place in places:
        found.update(_place_class(p, loose=loose) for p in _comma_places(place))
    for candidate in _PRECEDENCE:
        if candidate in found:
            return candidate
    return UNKNOWN


def _split(text: str) -> list[str]:
    return [p.strip() for p in _SPLIT_RE.split(_normalize(text)) if p.strip()]


def classify_location(*, location: str | None, description: str = "") -> str:
    """Return one of LOCAL, US, ELSEWHERE, UNKNOWN."""
    if location:
        verdict = _best(_split(location), loose=True)
        if verdict != UNKNOWN:
            return verdict

    # Only now, and only the header. A stated location always wins: office
    # boilerplate in the body describes the company, not this role.
    if description:
        return _best(_split(description[:_HEADER_CHARS]), loose=False)

    return UNKNOWN


def location_verdict(location_class: str, remote_type: str) -> str | None:
    """The rejection reason, or None to let the job through.

    Two distinct reasons rather than one so the settings breakdown can separate
    "right country, wrong metro" from "wrong country" -- they are corrected in
    completely different ways.
    """
    if location_class == ELSEWHERE:
        return "remote_outside_us" if remote_type == "remote" else "location_outside_area"
    if location_class == US and remote_type != "remote":
        # A hybrid role in New York means going to an office in New York.
        return "location_outside_area"
    return None
