"""Web UI: routers, the Jinja environment, and template filters.

Server-rendered throughout. HTMX handles status mutations; there is no
client-side framework and no deploy-time JavaScript build, which is what keeps
the deployment one Python function rather than two runtimes (spec section 12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates_env = Jinja2Templates(directory=str(TEMPLATE_DIR))


def ago(when: datetime | None) -> str:
    """Human-readable age. Never raises on a missing timestamp."""
    if when is None:
        # HN and parts of Lever omit posted_at entirely.
        return "unknown"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    hours = (datetime.now(UTC) - when).total_seconds() / 3600
    if hours < 0:
        # Clock skew between a board and us reads as "now", not as the future.
        return "just now"
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


def money(low: int | None, high: int | None) -> str:
    """Salary range. Two thirds of postings have nothing to show here."""
    if low is None and high is None:
        return "not listed"
    if high is None:
        return f"${low // 1000:,}k+"
    if low is None:
        return f"up to ${high // 1000:,}k"
    if low == high:
        return f"${low // 1000:,}k"
    return f"${low // 1000:,}k–${high // 1000:,}k"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


templates_env.env.filters["ago"] = ago
templates_env.env.filters["money"] = money
templates_env.env.filters["pct"] = pct


def register(app: FastAPI) -> None:
    """Mount static files and every route module onto the FastAPI app."""
    from fastapi.staticfiles import StaticFiles

    from web import detail, queue, settings, tracker

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for module in (queue, detail, tracker, settings):
        app.include_router(module.router)
