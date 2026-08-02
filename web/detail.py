"""GET /job/{id} and POST /job/{id}/status (spec section 12).

Mutations are POST only. A status change behind a GET gets fired by link
prefetchers, by history restore, and by anything that crawls the page.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from pipeline.apply.answers import resolve_all
from pipeline.apply.platforms import detect_platform
from pipeline.apply.questions import CATEGORY_ORDER
from pipeline.config import load_settings
from pipeline.db import connection
from pipeline.profile import ProfileUnavailableError, load_profile
from pipeline.store import job_detail, set_status
from web import templates_env

router = APIRouter()


@router.get("/job/{job_id}", response_class=HTMLResponse)
async def detail(request: Request, job_id: int) -> HTMLResponse:
    async with connection() as conn:
        job = await job_detail(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")

        settings = load_settings()
        try:
            profile = load_profile(settings)
        except ProfileUnavailableError:
            # The page is still worth serving without a profile -- the
            # description, the score and the apply link do not depend on one.
            answers = []
        else:
            # record_gaps is False here. This runs on every page view, and
            # counting a "sighting" per refresh would make the settings to-do
            # list rank questions by how often Jarra opened a job rather than
            # by how often a form asked.
            answers = await resolve_all(
                conn, profile, settings, today=date.today(), record_gaps=False
            )

    # Catalog categories in form order, then anything Jarra added himself.
    # Iterating CATEGORY_ORDER alone would silently drop his own answers.
    extra = [a.category for a in answers if a.category not in CATEGORY_ORDER]
    order = [*CATEGORY_ORDER, *dict.fromkeys(extra)]
    by_category = {name: [a for a in answers if a.category == name] for name in order}
    return templates_env.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "job": job,
            "answers": answers,
            "answers_by_category": by_category,
            "missing_count": sum(1 for a in answers if a.needs_review),
            "platform": detect_platform(job["apply_url"]),
        },
    )


@router.post("/job/{job_id}/status", response_class=HTMLResponse)
async def update_status(
    request: Request, job_id: int, status: Annotated[str, Form()]
) -> HTMLResponse:
    """Set the status and return just the button row for HTMX to swap in."""
    async with connection() as conn:
        if await job_detail(conn, job_id) is None:
            raise HTTPException(status_code=404, detail="no such job")
        try:
            await set_status(conn, job_id, status)
        except ValueError as exc:
            # 400, not a 500 surfacing from the CHECK constraint downstream.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return templates_env.TemplateResponse(
        request=request,
        name="_partials/status_buttons.html",
        context={"job": {"job_id": job_id, "status": status}},
    )
