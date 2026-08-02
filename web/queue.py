"""GET / — the ranked queue (spec section 12).

Every filter and sort lives in the query string. That makes each view a URL
Jarra can bookmark or open in a second tab, and it means the back button does
what it looks like it does -- which no amount of client-side state gives you
for free.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from pipeline.db import connection
from pipeline.filters.location import CLASS_LABELS
from pipeline.store import QueueFilters, queue_facets, queue_page
from web import templates_env

router = APIRouter()

PAGE_SIZE = 50


def _int(raw: str, default: int | None = None) -> int | None:
    """Parse a numeric query parameter, tolerating the empty string.

    Every numeric filter is typed as `str` rather than `int | None` for this
    reason. A `<select>` whose "any" option carries `value=""` submits an empty
    string, and FastAPI answers an unparseable int with a 422 -- so choosing
    "any salary" and pressing Apply returned a JSON validation error instead of
    the queue.

    Junk is treated the same way as empty. These URLs are hand-edited and
    shared between tabs; a stale one should render the default page, exactly as
    an unknown `sort` key does.

    Catches ValueError alone rather than (TypeError, ValueError): FastAPI binds
    these parameters as `str`, so None never arrives, and ruff's py314 target
    rewrites a parenthesised tuple into PEP 758 syntax that only parses on
    3.14. Nothing else in the function bundle needs 3.14, and this is not worth
    being the reason a deploy will not import.
    """
    try:
        return int(raw)
    except ValueError:
        return default


def _float(raw: str, default: float = 0.0) -> float:
    try:
        return float(raw)
    except ValueError:
        return default


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    sort: str = "score",
    dir: str = "desc",
    # Repeatable, so several arrangements can be selected at once:
    # ?remote=remote&remote=hybrid
    remote: Annotated[list[str] | None, Query()] = None,
    location: Annotated[list[str] | None, Query()] = None,
    min_score: str = "",
    min_salary: str = "",
    posted_within: str = "",
    company: str = "",
    judged_only: bool = False,
    per_company: str = "3",
    offset: str = "0",
) -> HTMLResponse:
    filters = QueueFilters(
        sort=sort,
        descending=dir != "asc",
        min_score=_float(min_score),
        remote_types=tuple(remote or ()),
        location_classes=tuple(location or ()),
        min_salary=_int(min_salary),
        posted_within_days=_int(posted_within),
        company=company.strip(),
        judged_only=judged_only,
        per_company_cap=_int(per_company, 3) or 3,
    ).normalized()
    offset = max(0, _int(offset, 0) or 0)

    async with connection() as conn:
        # One row more than the page, so "next" can be offered without a
        # count(*) over the whole eligible set on every page load.
        jobs = await queue_page(conn, filters, limit=PAGE_SIZE + 1, offset=offset)
        facets = await queue_facets(conn)

    return templates_env.TemplateResponse(
        request=request,
        name="queue.html",
        context={
            "jobs": jobs[:PAGE_SIZE],
            "filters": filters,
            "facets": facets,
            "offset": offset,
            "page_size": PAGE_SIZE,
            "has_next": len(jobs) > PAGE_SIZE,
            "location_labels": CLASS_LABELS,
            "paging_query": _query_without(request, "offset"),
        },
    )


def _query_without(request: Request, *drop: str) -> str:
    """The current query string minus some keys, for building paging links.

    Rebuilt from the raw params rather than from the parsed filters so repeated
    keys (?remote=remote&remote=hybrid) survive the round trip. Collapsing them
    to one value is how a multi-select filter quietly loses its second choice
    on page two.
    """
    kept = [(k, v) for k, v in request.query_params.multi_items() if k not in drop]
    return urlencode(kept)
