"""Remotive aggregator. Remote-only board, no per-company target list.

Two things about this API are load-bearing, both read off a live response:

Its filter parameters do not work. `?category=software-development`,
`?category=devops` and `?search=engineer` all return byte-identical results to
the unfiltered endpoint. Passing them would give false confidence that sales and
design postings had been excluded, so this fetches everything once and lets the
deterministic pre-filter do the work it already does for every other source.

Its response carries a legal notice with a rate expectation: "there is
absolutely no need to request Remotive job data too frequently... we advise max.
4 times a day... excessive requests will be blocked." That is a stricter budget
than PoliteSession's per-request delay expresses, so it is declared here as
MIN_INTERVAL_SECONDS for the orchestrator to honour.

The same notice requires attribution and a link back. We store Remotive's own
`url` as the apply link and tag every row with source="remotive", so the UI
attributes it. Nothing is ever republished anywhere.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig
from pipeline.text import strip_html

log = logging.getLogger(__name__)

JOBS_URL = "https://remotive.com/api/remote-jobs"

# Their published guidance is a maximum of four calls per day.
MIN_INTERVAL_SECONDS = 6 * 60 * 60


def _parse_ts(value: str | None) -> datetime | None:
    """Remotive publishes naive timestamps; they are UTC.

    Leaving them naive would put a tz-aware column and a tz-naive value in the
    same comparison and raise at insert time.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


class RemotiveSource:
    name = "remotive"
    min_interval_seconds = MIN_INTERVAL_SECONDS

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        try:
            payload = await cfg.session.get_json(JOBS_URL)
        except HostBlockedError:
            log.warning("remotive host blocked; stopping source")
            return
        except Exception as exc:
            log.warning("remotive fetch failed: %s", exc)
            cfg.errors.append("remotive")
            return

        if payload is None:  # 304 Not Modified
            return

        for item in payload.get("jobs", []):
            yield RawJob(
                source=self.name,
                source_job_id=str(item["id"]),
                company_name=item.get("company_name") or "unknown",
                title=item["title"],
                # Not a city: this is the geography a candidate may sit in.
                location=item.get("candidate_required_location"),
                description=strip_html(item.get("description")),
                apply_url=item["url"],
                posted_at=_parse_ts(item.get("publication_date")),
                # Remotive lists remote work exclusively.
                remote_type_hint="remote",
            )
