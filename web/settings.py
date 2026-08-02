"""GET /settings and its mutations (spec section 12).

Answer bank, Sharia overrides, unmapped questions, the run log, and today's
model spend.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pipeline.apply.answers import VARIABLE_HELP
from pipeline.apply.platforms import PLATFORMS
from pipeline.db import connection
from pipeline.filters.location import CLASS_LABELS
from pipeline.store import (
    answer_bank_all,
    company_screen_all,
    intake_breakdown,
    llm_spend_summary,
    location_breakdown,
    recent_runs,
    set_sharia_override,
    unmapped_questions,
    upsert_answer,
)
from web import templates_env

router = APIRouter()

# What each pre-filter reason means, in Jarra's terms rather than the code's.
# A settings page that prints `title_not_target` explains nothing to the person
# deciding whether the filter is too aggressive.
FILTER_REASONS = {
    "passes": ("In the queue", "Scored and waiting for you."),
    "title_not_target": ("Not an engineering role", "Sales, design, recruiting, support."),
    "management_role": ("Management", "A different job, not a more senior one."),
    "seniority_mismatch": ("Staff and above", "Principal, distinguished, architect, fellow."),
    "salary_below_floor": ("Below your floor", "Published a range topping out under $125k."),
    "location_outside_area": (
        "Office out of range",
        "Hybrid or onsite somewhere you cannot commute to.",
    ),
    "remote_outside_us": ("Remote, wrong country", "Scoped to Canada, the EU, or elsewhere."),
}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    async with connection() as conn:
        companies = await company_screen_all(conn)
        context = {
            "answers": await answer_bank_all(conn),
            "unmapped": await unmapped_questions(conn),
            "companies": companies,
            "flagged": [c for c in companies if c["sharia_verdict"] == "flagged"],
            "excluded": [c for c in companies if c["sharia_verdict"] == "excluded"],
            # Grouped by verdict, not by who decided it. A company Jarra ruled
            # on appears once, in the group its verdict puts it in, badged
            # "yours" -- listing his rulings separately as well showed every
            # override twice.
            "allowed": [c for c in companies if c["sharia_verdict"] == "allowed"],
            "intake": await intake_breakdown(conn),
            "locations": await location_breakdown(conn),
            "runs": await recent_runs(conn, limit=20),
            "spend": await llm_spend_summary(conn),
            "filter_reasons": FILTER_REASONS,
            "location_labels": CLASS_LABELS,
            "variable_help": VARIABLE_HELP,
            "platforms": PLATFORMS,
        }
    return templates_env.TemplateResponse(request=request, name="settings.html", context=context)


@router.post("/settings/answer")
async def save_answer(
    question: Annotated[str, Form()],
    answer: Annotated[str, Form()],
    category: Annotated[str, Form()] = "",
    question_key: Annotated[str, Form()] = "",
) -> RedirectResponse:
    async with connection() as conn:
        try:
            await upsert_answer(conn, question, answer, category, question_key or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 303 so a refresh does not re-post the form.
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/sharia")
async def save_override(
    company_id: Annotated[int, Form()], verdict: Annotated[str, Form()]
) -> RedirectResponse:
    async with connection() as conn:
        try:
            await set_sharia_override(conn, company_id, verdict)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/settings", status_code=303)
