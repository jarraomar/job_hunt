"""Which application platform a job uses, and what can be prepared for it.

Two tiers, and the line between them is about *predictability*, not about
trust:

- ``structured`` -- Greenhouse, Lever, Ashby. One page, a stable field set, and
  the same handful of custom questions across every employer using them. The
  full answer set can be prepared before the form is ever opened.
- ``manual`` -- Workday, iCIMS, Taleo, SuccessFactors, BrassRing. Every tenant
  configures its own multi-step wizard, most require creating an account first,
  and the question set is not knowable in advance. Answers are still prepared,
  but as a reference list rather than a field mapping.

Nothing here is ever submitted. Spec section 3: no automated process
authenticates as Jarra on any job platform, so there is no submit path, no
credential handling, and no browser dependency. `prefill` describes what can be
worked out ahead of time; it never describes anything being typed by a machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

STRUCTURED = "structured"
MANUAL = "manual"


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    prefill: str
    note: str
    hosts: tuple[str, ...] = ()

    @property
    def is_structured(self) -> bool:
        return self.prefill == STRUCTURED


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        key="greenhouse",
        name="Greenhouse",
        prefill=STRUCTURED,
        note="One page. Résumé upload plus a short, consistent custom-question set.",
        hosts=("greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io"),
    ),
    Platform(
        key="lever",
        name="Lever",
        prefill=STRUCTURED,
        note="One page. Asks for links (LinkedIn, GitHub, portfolio) more often than most.",
        hosts=("lever.co", "jobs.lever.co"),
    ),
    Platform(
        key="ashby",
        name="Ashby",
        prefill=STRUCTURED,
        note="One page, well-structured. Usually asks work authorization explicitly.",
        hosts=("ashbyhq.com", "jobs.ashbyhq.com"),
    ),
    Platform(
        key="workable",
        name="Workable",
        prefill=STRUCTURED,
        note="One page. Question set varies more by employer than Greenhouse.",
        hosts=("workable.com", "apply.workable.com"),
    ),
    Platform(
        key="workday",
        name="Workday",
        prefill=MANUAL,
        note="Account required, multi-step wizard, per-tenant questions. Budget 20 minutes.",
        hosts=("myworkdayjobs.com", "wd1.myworkdayjobs.com", "wd5.myworkdayjobs.com"),
    ),
    Platform(
        key="icims",
        name="iCIMS",
        prefill=MANUAL,
        note="Account required. Form structure differs per employer.",
        hosts=("icims.com",),
    ),
    Platform(
        key="taleo",
        name="Taleo",
        prefill=MANUAL,
        note="Account required. Long form, frequent session timeouts.",
        hosts=("taleo.net", "tbe.taleo.net"),
    ),
    Platform(
        key="successfactors",
        name="SAP SuccessFactors",
        prefill=MANUAL,
        note="Account required. Per-tenant question set.",
        hosts=("successfactors.com", "successfactors.eu"),
    ),
    Platform(
        key="smartrecruiters",
        name="SmartRecruiters",
        prefill=STRUCTURED,
        note="One page, predictable fields.",
        hosts=("smartrecruiters.com", "jobs.smartrecruiters.com"),
    ),
    Platform(
        key="breezy",
        name="Breezy HR",
        prefill=STRUCTURED,
        note="One page.",
        hosts=("breezy.hr",),
    ),
    Platform(
        key="rippling",
        name="Rippling",
        prefill=STRUCTURED,
        note="One page.",
        hosts=("rippling.com", "ats.rippling.com"),
    ),
)

# The fallback. Deliberately `manual`: an unrecognised host is an unknown form,
# and treating unknown as structured would present a prepared field mapping for
# a form nobody has looked at.
UNKNOWN = Platform(
    key="unknown",
    name="Unknown platform",
    prefill=MANUAL,
    note="Not a platform we recognise. Read the form before assuming anything.",
)

_BY_HOST = {host: platform for platform in PLATFORMS for host in platform.hosts}


def detect_platform(apply_url: str | None) -> Platform:
    """Identify the platform from the apply link.

    Matched on the registrable host and its subdomains rather than by substring:
    a substring test on "lever.co" also matches "notlever.co.evil.com", and the
    result decides what the UI tells Jarra to expect from the form.
    """
    if not apply_url:
        return UNKNOWN
    host = (urlparse(apply_url).hostname or "").lower().removeprefix("www.")
    if not host:
        return UNKNOWN

    # Walk up the labels so "acme.jobs.lever.co" resolves to Lever while
    # "lever.co.example.com" does not.
    labels = host.split(".")
    for i in range(len(labels) - 1):
        candidate = ".".join(labels[i:])
        if candidate in _BY_HOST:
            return _BY_HOST[candidate]
    return UNKNOWN
