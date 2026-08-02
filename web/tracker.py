"""GET /tracker — funnel and weekly conversion (spec section 12)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pipeline.db import connection
from pipeline.store import FUNNEL_STAGES, funnel_counts, weekly_conversion
from web import templates_env

router = APIRouter()


@router.get("/tracker", response_class=HTMLResponse)
async def tracker(request: Request) -> HTMLResponse:
    async with connection() as conn:
        counts = await funnel_counts(conn)
        weeks = await weekly_conversion(conn, weeks=8)

    return templates_env.TemplateResponse(
        request=request,
        name="tracker.html",
        context={
            # Every stage is present with an explicit zero: a stage that
            # disappears when empty makes the funnel look shorter than it is.
            "stages": [(stage, counts.get(stage, 0)) for stage in FUNNEL_STAGES],
            "weeks": weeks,
        },
    )
