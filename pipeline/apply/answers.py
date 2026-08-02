"""Turning a form question into the answer to give it.

Three sources, checked in this order:

1. **The answer bank** -- something Jarra wrote. Always wins. He is allowed to
   disagree with the profile and the disagreement must stick.
2. **The catalog template** -- derived from the profile and the settings.
3. **Nothing** -- the question is recorded as unmapped and surfaced in
   /settings, where answering it once moves it to source 1 forever.

The reason answers are *templates* rather than strings is the start-date
question. "When can you start?" has no fixed answer: the right one is a date
about two weeks after whenever the application is actually submitted. Storing
"August 15th" is wrong by the following Monday, and storing "in two weeks" puts
the arithmetic on whoever reads it. `{{start_date}}` is resolved at the moment
the answer is asked for, so an agent preparing an application on a Saturday in
August gets a real weekday in the middle of the month.

Every variable is listed in VARIABLE_HELP and shown in the settings UI, because
a template language nobody can see the vocabulary of is a trap.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

from psycopg import AsyncConnection

from pipeline.apply.questions import QUESTIONS, Question, match_question
from pipeline.config import Settings
from pipeline.profile import Profile

log = logging.getLogger(__name__)

BANK = "bank"

# The category for a bank answer to a question the catalog does not know.
OWN = "your answers"
PROFILE = "profile"
MISSING = "missing"

# Two weeks' notice, then the following Monday. A date landing on a weekend
# reads as careless on an application, and "two weeks from Saturday" is a
# Saturday.
NOTICE_DAYS = 14

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

VARIABLE_HELP: tuple[tuple[str, str], ...] = (
    ("full_name", "Your name, from the profile"),
    ("email", "Your email address"),
    ("phone", "Your phone number"),
    ("city", "Your home city"),
    ("state", "Your home state"),
    ("work_authorization", "e.g. US-born citizen"),
    ("start_date", "Two weeks from today, rolled to the next Monday"),
    ("today", "Today's date"),
    ("salary_target", "Your target salary"),
    ("salary_floor", "The lowest salary worth answering"),
    ("years_experience", "Years since your first professional role"),
    ("linkedin", "LinkedIn URL, from the profile"),
    ("github", "GitHub URL, from the profile"),
    ("website", "Personal site, from the profile"),
)


@dataclass(frozen=True)
class ResolvedAnswer:
    key: str
    question: str
    category: str
    answer: str
    source: str

    @property
    def needs_review(self) -> bool:
        return self.source == MISSING


def start_date(today: date, *, notice_days: int = NOTICE_DAYS) -> date:
    """The first Monday on or after `today + notice_days`."""
    target = today + timedelta(days=notice_days)
    return target + timedelta(days=(7 - target.weekday()) % 7)


def _years_experience(profile: Profile, today: date) -> str:
    """Years since the earliest dated role on the résumé.

    Derived rather than stored so it cannot go stale, and returns "" when no
    role carries a parseable start -- an unfillable variable leaves the
    question unanswered, which is correct, rather than claiming zero years.
    """
    starts = []
    for role in profile.resume.get("experience") or []:
        raw = str(role.get("start") or "")
        if match := re.match(r"(\d{4})(?:-(\d{2}))?", raw):
            year, month = int(match.group(1)), int(match.group(2) or 1)
            starts.append(date(year, month, 1))
    if not starts:
        return ""
    years = (today - min(starts)).days / 365.25
    return str(max(1, round(years)))


def variables(profile: Profile, settings: Settings, *, today: date) -> dict[str, str]:
    """Everything a template may reference. Missing values are empty strings."""
    identity = profile.identity or {}
    target = int(settings.salary_floor * 1.2)
    return {
        "full_name": str(identity.get("name") or ""),
        "email": str(identity.get("email") or ""),
        "phone": str(identity.get("phone") or ""),
        "city": str(identity.get("city") or settings.home_city),
        "state": str(identity.get("state") or settings.home_state),
        "work_authorization": str(identity.get("work_authorization") or ""),
        "start_date": start_date(today).strftime("%A, %B %-d, %Y"),
        "today": today.strftime("%B %-d, %Y"),
        "salary_target": f"${target:,}",
        "salary_floor": f"${settings.salary_floor:,}",
        "years_experience": _years_experience(profile, today),
        "linkedin": str(identity.get("linkedin") or ""),
        "github": str(identity.get("github") or ""),
        "website": str(identity.get("website") or ""),
    }


def render(template: str, values: dict[str, str]) -> str:
    """Substitute `{{name}}`. An unfillable template renders as empty.

    "Empty" rather than "partially filled" on purpose: half an answer --
    "I can start on , about two weeks from today" -- looks filled in and would
    be pasted into a real application. Nothing is safer than something wrong
    here, because the failure is silent at exactly the moment it matters.
    """
    if not template:
        return ""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        value = values.get(match.group(1), "")
        if not value:
            missing.append(match.group(1))
        return value

    rendered = _TEMPLATE_RE.sub(replace, template)
    return "" if missing else rendered.strip()


async def resolve_all(
    conn: AsyncConnection,
    profile: Profile,
    settings: Settings,
    *,
    today: date,
    record_gaps: bool = True,
) -> list[ResolvedAnswer]:
    """Every catalog question, answered as well as it can be.

    Answers a question rather than a form, so the result is the same regardless
    of which employer is asking. `record_gaps` writes the unanswered ones to
    `unmapped_questions`, which is what makes the settings page a to-do list
    that shrinks instead of a static wish list.
    """
    from pipeline.store import answer_bank_all, record_unmapped

    values = variables(profile, settings, today=today)
    bank = {row["question_key"] or row["question"]: row for row in await answer_bank_all(conn)}

    resolved: list[ResolvedAnswer] = []
    gaps: list[tuple[str, str]] = []

    for question in QUESTIONS:
        stored = bank.get(question.key)
        if stored:
            # A stored answer is a template too, so Jarra can write "I can
            # start on {{start_date}}" once and have it stay correct.
            answer = render(stored["answer"], values) or stored["answer"]
            resolved.append(_answer(question, answer, BANK))
            continue

        rendered = render(question.template, values)
        if rendered:
            resolved.append(_answer(question, rendered, PROFILE))
            continue

        resolved.append(_answer(question, "", MISSING))
        gaps.append((question.key, question.canonical))

    # Anything Jarra stored that the catalog does not know about. Without this
    # an answer he typed himself is accepted, saved, and then never shown on any
    # job page -- the catalog would silently decide which of his own answers
    # were worth displaying.
    known = {q.key for q in QUESTIONS}
    for stored in bank.values():
        if stored["question_key"] in known:
            continue
        resolved.append(
            ResolvedAnswer(
                key=stored["question_key"] or stored["question"],
                question=stored["question"],
                category=stored["category"] or OWN,
                answer=render(stored["answer"], values) or stored["answer"],
                source=BANK,
            )
        )

    if record_gaps and gaps:
        for key, canonical in gaps:
            await record_unmapped(conn, canonical, question_key=key)

    return resolved


def _answer(question: Question, answer: str, source: str) -> ResolvedAnswer:
    return ResolvedAnswer(
        key=question.key,
        question=question.canonical,
        category=question.category,
        answer=answer,
        source=source,
    )


def resolve_one(
    text: str, profile: Profile, settings: Settings, *, today: date
) -> ResolvedAnswer | None:
    """Answer one free-text question, without touching the database.

    The entry point for an agent holding a question it has just read off a form
    and wanting the profile's answer to it. Returns None when the question
    matches nothing in the catalog -- the caller decides whether that is worth
    recording.
    """
    question = match_question(text)
    if question is None:
        return None
    rendered = render(question.template, variables(profile, settings, today=today))
    return _answer(question, rendered, PROFILE if rendered else MISSING)
