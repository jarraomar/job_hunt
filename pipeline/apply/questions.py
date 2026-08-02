"""The questions application forms actually ask, and how to recognise them.

A catalog rather than a scraper. Nothing in this system opens an application
form -- spec section 3 forbids authenticating anywhere -- so the question set
cannot be discovered by reading one. It is instead the set Greenhouse, Lever,
Ashby and Workable ask across employers, which is small, stable, and almost
entirely the same everywhere.

Each entry has a `key`. The key is what an answer is stored against, so
answering "When can you start?" once also answers "What is your earliest start
date?" and "Notice period?" -- three phrasings of one question that would
otherwise sit in the answer bank as three rows drifting apart.

`self_identification` questions carry no default and never will. Race, gender,
veteran and disability status are Jarra's to answer or decline, and a system
that filled them in from a profile would be answering for him.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Categories, in the order a form asks them.
CONTACT = "contact"
ELIGIBILITY = "eligibility"
LOGISTICS = "logistics"
LINKS = "links"
SELF_ID = "self_identification"

CATEGORY_ORDER = (CONTACT, ELIGIBILITY, LOGISTICS, LINKS, SELF_ID)


@dataclass(frozen=True)
class Question:
    key: str
    canonical: str
    category: str
    # The answer, as a template. Empty means there is no safe default and the
    # question must be answered by Jarra once, in the answer bank.
    template: str = ""
    patterns: tuple[str, ...] = ()
    _compiled: list[re.Pattern[str]] = field(default_factory=list, compare=False, repr=False)

    def matches(self, text: str) -> bool:
        if not self._compiled:
            self._compiled.extend(re.compile(p, re.IGNORECASE) for p in self.patterns)
        return any(p.search(text) for p in self._compiled)


QUESTIONS: tuple[Question, ...] = (
    Question(
        key="full_name",
        canonical="Full name",
        category=CONTACT,
        template="{{full_name}}",
        patterns=(r"\b(full|legal)?\s*name\b",),
    ),
    Question(
        key="email",
        canonical="Email address",
        category=CONTACT,
        template="{{email}}",
        patterns=(r"\be-?mail\b",),
    ),
    Question(
        key="phone",
        canonical="Phone number",
        category=CONTACT,
        template="{{phone}}",
        patterns=(r"\b(phone|mobile|cell)\b",),
    ),
    Question(
        key="location",
        canonical="Where are you located?",
        category=CONTACT,
        template="{{city}}, {{state}}",
        patterns=(r"\b(current\s+)?(location|city|where are you (based|located))\b",),
    ),
    Question(
        key="work_authorization",
        canonical="Are you authorized to work in the United States?",
        category=ELIGIBILITY,
        # Spec 15.11. This is an eligibility MATCH: clearance and "US
        # Citizenship required" postings qualify rather than disqualify.
        template="Yes — {{work_authorization}}.",
        patterns=(
            r"\bauthoriz(ed|ation) to work\b",
            r"\blegally (authorized|entitled) to work\b",
            r"\bwork authorization\b",
            r"\beligible to work\b",
        ),
    ),
    Question(
        key="sponsorship",
        canonical="Will you now or in the future require visa sponsorship?",
        category=ELIGIBILITY,
        template="No.",
        patterns=(r"\bsponsor(ship)?\b", r"\bvisa\b", r"\bh-?1b\b"),
    ),
    Question(
        key="clearance",
        canonical="Are you able to obtain a US security clearance?",
        category=ELIGIBILITY,
        template="Yes — {{work_authorization}}, eligible to obtain a clearance.",
        patterns=(r"\bsecurity clearance\b", r"\bclearance\b", r"\bitar\b"),
    ),
    Question(
        key="start_date",
        canonical="When can you start?",
        category=LOGISTICS,
        # The date is computed when the answer is resolved, not when it is
        # written. An answer bank holding a literal date is wrong the week
        # after it is stored.
        template="I can start on {{start_date}}, about two weeks from today.",
        patterns=(
            r"\b(when|earliest|available|availability).{0,20}\bstart\b",
            r"\bstart date\b",
            r"\bnotice period\b",
            r"\bhow (soon|much notice)\b",
        ),
    ),
    Question(
        key="salary_expectation",
        canonical="What are your salary expectations?",
        category=LOGISTICS,
        template="{{salary_target}}, with flexibility depending on the whole package.",
        patterns=(
            r"\b(salary|compensation|pay)\s+(expectation|requirement|range)",
            r"\bdesired (salary|compensation)\b",
            r"\bexpected (salary|compensation)\b",
        ),
    ),
    Question(
        key="years_experience",
        canonical="How many years of experience do you have?",
        category=LOGISTICS,
        template="{{years_experience}} years.",
        patterns=(r"\byears? of (professional |relevant |software )?experience\b",),
    ),
    Question(
        key="relocation",
        canonical="Are you willing to relocate?",
        category=LOGISTICS,
        template="",  # A real decision, not a fact about the profile.
        patterns=(r"\brelocat(e|ion)\b",),
    ),
    Question(
        key="referral",
        canonical="How did you hear about this role?",
        category=LOGISTICS,
        template="",
        patterns=(r"\bhow did you (hear|find out)\b", r"\breferr?(al|ed by)\b"),
    ),
    Question(
        key="linkedin",
        canonical="LinkedIn profile",
        category=LINKS,
        template="{{linkedin}}",
        patterns=(r"\blinkedin\b",),
    ),
    Question(
        key="github",
        canonical="GitHub profile",
        category=LINKS,
        template="{{github}}",
        patterns=(r"\bgithub\b",),
    ),
    Question(
        key="portfolio",
        canonical="Website or portfolio",
        category=LINKS,
        template="{{website}}",
        patterns=(r"\b(portfolio|personal (web)?site|website)\b",),
    ),
    # No templates below this line, by design. See the module docstring.
    Question(
        key="gender",
        canonical="Gender",
        category=SELF_ID,
        patterns=(r"\bgender\b",),
    ),
    Question(
        key="race",
        canonical="Race or ethnicity",
        category=SELF_ID,
        patterns=(r"\b(race|ethnicity|hispanic|latino)\b",),
    ),
    Question(
        key="veteran_status",
        canonical="Veteran status",
        category=SELF_ID,
        patterns=(r"\bveteran\b", r"\bprotected veteran\b"),
    ),
    Question(
        key="disability_status",
        canonical="Disability status",
        category=SELF_ID,
        patterns=(r"\bdisability\b",),
    ),
)

BY_KEY = {q.key: q for q in QUESTIONS}


def match_question(text: str) -> Question | None:
    """Find the catalog entry a form question corresponds to.

    Checked in catalog order, and the order is load-bearing: "Are you
    authorized to work in the US without sponsorship?" matches both
    `work_authorization` and `sponsorship`, and the authorization reading is
    the one that produces the right answer.
    """
    text = text.strip()
    if not text:
        return None
    for question in QUESTIONS:
        if question.matches(text):
            return question
    return None
