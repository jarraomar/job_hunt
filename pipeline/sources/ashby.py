"""Ashby job board API. The best structured-compensation source we have.

Spec section 6 calls this the source with reliable structured salary. Measured
against ten live boards on 2026-08-01, that is true per-employer rather than
per-source: publishing compensation is an opt-in Ashby setting. Ramp exposes it
on 95% of postings, OpenAI 80%, Perplexity 82%, Vanta 62% -- while Linear,
Notion, Cursor, ClickHouse and PostHog expose it on none at all.

So this adapter sets salary_source="structured" when the numbers are there and
otherwise leaves salary unknown, which lets pipeline.normalize fall back to
parsing the description like every other source.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig
from pipeline.text import strip_html

log = logging.getLogger(__name__)

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"

# Multipliers onto an annual figure, keyed by Ashby's `interval` value.
_INTERVAL_FACTORS = {
    "1 YEAR": 1,
    "1 MONTH": 12,
    "1 WEEK": 52,
    "1 DAY": 260,
    "1 HOUR": 40 * 52,
}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _structured_salary(item: dict) -> tuple[int | None, int | None]:
    """Pull an annualized USD range out of the compensation object.

    Two traps here, both live in the captured fixture:

    - A tier's components are not ordered, and an EquityPercentage component
      with null values frequently comes first. Taking components[0] reads
      equity as salary. Filter on compensationType == "Salary".
    - The interval is not always yearly, so raw minValue/maxValue cannot be
      used as annual figures.

    Non-USD is refused rather than converted, matching pipeline.salary.
    """
    compensation = item.get("compensation") or {}
    if not item.get("shouldDisplayCompensationOnJobPostings", True):
        # The employer chose not to publish a range; anything present is
        # internal. Fall through to "unknown", which passes the salary gate.
        return (None, None)

    for tier in compensation.get("compensationTiers") or []:
        for component in tier.get("components") or []:
            if component.get("compensationType") != "Salary":
                continue
            if (component.get("currencyCode") or "USD") != "USD":
                continue
            factor = _INTERVAL_FACTORS.get(component.get("interval") or "1 YEAR")
            if factor is None:
                continue
            low, high = component.get("minValue"), component.get("maxValue")
            if low is None and high is None:
                continue
            low = int(low * factor) if low is not None else None
            high = int(high * factor) if high is not None else None
            if low is not None and high is not None:
                return (min(low, high), max(low, high))
            value = low if low is not None else high
            return (value, value)

    return (None, None)


class AshbySource:
    name = "ashby"
    min_interval_seconds = 0.0

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        for token in cfg.targets.get(self.name, []):
            try:
                payload = await cfg.session.get_json(
                    BOARD_URL.format(token=token), params={"includeCompensation": "true"}
                )
            except HostBlockedError:
                log.warning("ashby host blocked; stopping source")
                return
            except Exception as exc:  # one bad board must not kill the run
                log.warning("ashby board %s failed: %s", token, exc)
                cfg.errors.append(f"ashby:{token}")
                continue

            if payload is None:  # 304 Not Modified
                continue

            jobs = payload.get("jobs", [])
            if not jobs:
                # Like Lever, a retired board answers 200 with nothing rather
                # than 404, so a dead token would contribute silently forever.
                log.warning("ashby board %s returned no jobs; token may be retired", token)
                continue

            for item in jobs:
                # isListed=False is a posting Ashby is still serving but no
                # longer showing. Applying to one is wasted effort.
                if item.get("isListed") is False:
                    continue

                low, high = _structured_salary(item)
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    company_name=token,
                    title=item["title"],
                    location=item.get("location"),
                    description=item.get("descriptionPlain")
                    or strip_html(item.get("descriptionHtml")),
                    apply_url=item.get("jobUrl") or item["applyUrl"],
                    posted_at=_parse_ts(item.get("publishedAt")),
                    remote_type_hint=item.get("workplaceType"),
                    salary_min=low,
                    salary_max=high,
                    salary_source="structured" if low is not None else "none",
                )
