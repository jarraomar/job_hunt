"""Greenhouse job board API. Unauthenticated; whole board in one response.

Field names here were read off a captured live response, not assumed. Two of
them differ from the obvious guess and both matter -- see posted_at and
company_name below.
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

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GreenhouseSource:
    name = "greenhouse"
    min_interval_seconds = 0.0

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        for token in cfg.targets.get(self.name, []):
            try:
                payload = await cfg.session.get_json(
                    BOARD_URL.format(token=token), params={"content": "true"}
                )
            except HostBlockedError:
                # Every board shares one host, so continuing would be retrying
                # into a block. Stop the source, not just this board.
                log.warning("greenhouse host blocked; stopping source")
                return
            except Exception as exc:  # one bad board must not kill the run
                log.warning("greenhouse board %s failed: %s", token, exc)
                cfg.errors.append(f"greenhouse:{token}")
                continue

            if payload is None:  # 304 Not Modified
                continue

            jobs = payload.get("jobs", [])
            total = (payload.get("meta") or {}).get("total")
            if total is not None and total != len(jobs):
                # Verified against two live boards that the full board arrives
                # in one response. If that changes, quietly taking the first
                # page would look like a shrinking job market, not a bug.
                log.warning(
                    "greenhouse board %s looks truncated: got %d of %d",
                    token,
                    len(jobs),
                    total,
                )

            for item in jobs:
                location = (item.get("location") or {}).get("name")
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    # The payload carries a properly-cased display name; the
                    # token is a slug. Fall back only when it is absent.
                    company_name=item.get("company_name") or token,
                    title=item["title"],
                    location=location,
                    description=strip_html(item.get("content", "")),
                    apply_url=item["absolute_url"],
                    # first_published is when the job was posted; updated_at
                    # moves on any edit. Freshness scoring (spec section 8)
                    # needs the former, or a typo fix makes an old job look new.
                    posted_at=_parse_ts(item.get("first_published"))
                    or _parse_ts(item.get("updated_at")),
                )
