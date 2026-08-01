"""Lever postings API. Unauthenticated; whole board in one JSON array.

Field names were read off a captured live response. Two things differ from the
obvious guess: the payload is a bare array rather than an object, and createdAt
is epoch milliseconds rather than an ISO string.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig
from pipeline.text import join_sections, strip_html

log = logging.getLogger(__name__)

POSTINGS_URL = "https://api.lever.co/v0/postings/{token}"

# Milliseconds since epoch; anything below this is a seconds-based value that
# would place the posting in 1970 and make every job look impossibly stale.
_MIN_PLAUSIBLE_MS = 1_000_000_000_000


def _parse_epoch_ms(value: object) -> datetime | None:
    if not isinstance(value, int | float) or value < _MIN_PLAUSIBLE_MS:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _description(item: dict) -> str:
    """Assemble the full posting text.

    Lever exposes pre-rendered plain-text variants, so no HTML stripping is
    needed for those. The `lists` blocks are HTML-only and routinely hold the
    compensation range, so skipping them loses salary on US postings.
    """
    lists = item.get("lists") or []
    sections = [strip_html(f"{blk.get('text', '')} {blk.get('content', '')}") for blk in lists]
    return join_sections(
        item.get("descriptionPlain") or strip_html(item.get("description")),
        *sections,
        item.get("additionalPlain") or strip_html(item.get("additional")),
    )


class LeverSource:
    name = "lever"
    min_interval_seconds = 0.0

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        for token in cfg.targets.get(self.name, []):
            try:
                payload = await cfg.session.get_json(
                    POSTINGS_URL.format(token=token), params={"mode": "json"}
                )
            except HostBlockedError:
                log.warning("lever host blocked; stopping source")
                return
            except Exception as exc:  # one bad board must not kill the run
                log.warning("lever board %s failed: %s", token, exc)
                cfg.errors.append(f"lever:{token}")
                continue

            if payload is None:  # 304 Not Modified
                continue
            if not isinstance(payload, list):
                log.warning("lever board %s returned %s, expected a list", token, type(payload))
                continue
            if not payload:
                # A retired board answers 200 with [], which is indistinguishable
                # from a real board that happens to have nothing open. Say so,
                # or a dead token silently contributes nothing forever.
                log.warning("lever board %s returned no postings; token may be retired", token)
                continue

            for item in payload:
                categories = item.get("categories") or {}
                yield RawJob(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    # Lever does not carry a display name; the token is all we get.
                    company_name=token,
                    title=item["text"],
                    location=categories.get("location"),
                    description=_description(item),
                    apply_url=item.get("hostedUrl") or item["applyUrl"],
                    posted_at=_parse_epoch_ms(item.get("createdAt")),
                    remote_type_hint=item.get("workplaceType"),
                )
