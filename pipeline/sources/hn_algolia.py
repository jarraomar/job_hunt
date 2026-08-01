"""Hacker News "Ask HN: Who is hiring?" via the Algolia search API.

Structurally unlike the ATS sources, which is why it earns a place in Phase 1:
if the Source protocol can carry this, it is not accidentally ATS-shaped. Jobs
here are free-text comments written by humans to a loose convention, so parsing
is heuristic and must refuse rather than guess.

Three traps, all found by reading a live thread:

1. `whoishiring` posts "Who is hiring?" and "Who wants to be hired?" on the same
   day. The second is job *seekers*. Ingesting it fills the queue with
   candidates advertising themselves.
2. Half the comments on the thread are replies, not postings. Only top-level
   comments -- `parent_id == story_id` -- are jobs.
3. The header convention is `Company | Role | Location | Type | Salary`, but
   field order varies and both company and role are sometimes missing entirely.

Measured on the July 2026 thread: 49 top-level comments, 39 parsed (79%). Every
skip was a post with no company named, no role in the header, a non-pipe format,
or -- in one case -- a comment that was not a job at all.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime

from pipeline.http import HostBlockedError
from pipeline.models import RawJob
from pipeline.sources.base import SourceConfig
from pipeline.text import strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"

# Algolia caps a page at 1000; threads run to several hundred comments.
_PAGE_SIZE = 1000
_MAX_PAGES = 3

# "Who is hiring?" only. The sibling threads are job seekers and freelancers.
_HIRING_TITLE_RE = re.compile(r"who\s+is\s+hiring", re.IGNORECASE)
_NOT_HIRING_TITLE_RE = re.compile(r"wants\s+to\s+be\s+hired|freelancer", re.IGNORECASE)

_ROLE_RE = re.compile(
    r"\b(engineer(ing)?|developer|scientist|architect|sre|devops|programmer|analyst"
    r"|designer|manager|lead|founder|cto|full[\s-]?stack|backend|frontend"
    r"|infrastructure|security|researcher)\b",
    re.IGNORECASE,
)
_ARRANGEMENT_RE = re.compile(r"\b(remote|onsite|on-site|hybrid)\b", re.IGNORECASE)
_EMPLOYMENT_TYPE_RE = re.compile(
    r"^\s*(full[\s-]?time|part[\s-]?time|contract|intern(ship)?|freelance|flexible|permanent)\s*$",
    re.IGNORECASE,
)
# "Acme ( https://acme.com )" and "Acme (acme.io)" -> "Acme".
# Matches any domain-shaped parenthetical rather than a TLD allow-list: a list
# left "Chronograph (chronograph.pe)" unstripped, and country-code TLDs are
# common enough in company URLs that enumerating them is a losing game.
_URL_PAREN_RE = re.compile(
    r"\(\s*(?:https?://)?[\w.-]+\.[a-z]{2,}(?:/[^)\s]*)?\s*\)",
    re.IGNORECASE,
)

_MAX_COMPANY_LEN = 60


def _header_line(comment_html: str | None) -> str:
    """The first paragraph of a comment, which is where the header lives."""
    head = re.split(r"<p>", comment_html or "", maxsplit=1)[0]
    return strip_html(head)


def parse_hn_comment(comment: dict) -> RawJob | None:
    """Turn one top-level thread comment into a RawJob, or None if unparseable.

    Returns None generously. A malformed row costs review time on every future
    digest, whereas a skipped post costs one job we never knew about.
    """
    header = _header_line(comment.get("comment_text"))
    segments = [s.strip(" *_·-") for s in header.split("|")]
    segments = [s for s in segments if s]
    if len(segments) < 2:
        return None

    # By convention the company comes first. Detecting it by content instead
    # misfires badly: "TypeSafe AI" and "Cora AI" read as role text.
    company = _URL_PAREN_RE.sub("", segments[0]).strip(" -–—,")
    if not company or len(company) > _MAX_COMPANY_LEN:
        return None

    title = next(
        (s for s in segments[1:] if _ROLE_RE.search(s) and not _EMPLOYMENT_TYPE_RE.match(s)),
        None,
    )
    if not title:
        # Either the post names no role, or the company slot actually held the
        # role and no employer was given. Both are unusable: the Sharia screen
        # (spec section 9) is per-company, so a job with no employer cannot
        # clear it, and a job with no role cannot be scored.
        return None

    location = next(
        (s for s in segments[1:] if s != title and _ARRANGEMENT_RE.search(s)),
        None,
    )

    object_id = str(comment["objectID"])
    return RawJob(
        source=HNAlgoliaSource.name,
        source_job_id=object_id,
        company_name=company,
        title=title,
        location=location,
        description=strip_html(comment.get("comment_text")),
        apply_url=ITEM_URL.format(id=object_id),
        posted_at=_parse_ts(comment.get("created_at")),
        remote_type_hint=location,
    )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class HNAlgoliaSource:
    name = "hn_algolia"
    min_interval_seconds = 0.0

    async def _latest_thread_id(self, cfg: SourceConfig) -> str | None:
        payload = await cfg.session.get_json(
            SEARCH_BY_DATE_URL,
            params={"tags": "story,author_whoishiring", "hitsPerPage": "20"},
        )
        if not payload:
            return None
        for hit in payload.get("hits", []):
            title = hit.get("title") or ""
            if _HIRING_TITLE_RE.search(title) and not _NOT_HIRING_TITLE_RE.search(title):
                log.info("hn: using thread %s (%s)", hit.get("objectID"), title)
                return str(hit["objectID"])
        log.warning("hn: no 'Who is hiring' thread found")
        return None

    async def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]:
        try:
            story_id = await self._latest_thread_id(cfg)
            if story_id is None:
                return

            for page in range(_MAX_PAGES):
                payload = await cfg.session.get_json(
                    SEARCH_URL,
                    params={
                        "tags": f"comment,story_{story_id}",
                        "hitsPerPage": str(_PAGE_SIZE),
                        "page": str(page),
                    },
                )
                if not payload:
                    return

                hits = payload.get("hits", [])
                for hit in hits:
                    # Replies are discussion, not postings. On the thread we
                    # measured, exactly half the comments were replies.
                    if hit.get("parent_id") != hit.get("story_id"):
                        continue
                    job = parse_hn_comment(hit)
                    if job is not None:
                        yield job

                if page + 1 >= payload.get("nbPages", 1):
                    return
        except HostBlockedError:
            log.warning("hn host blocked; stopping source")
            return
        except Exception as exc:
            log.warning("hn fetch failed: %s", exc)
            cfg.errors.append("hn_algolia")
            return
