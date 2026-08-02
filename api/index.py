"""Vercel entrypoint. The whole deployment is this one FastAPI app.

Vercel bundles a FastAPI application into exactly one function, so the cron
routes and (from Phase 3) the web UI are the same artifact — sharing models and
database code by direct import rather than over a wire.

Phase 0 scope: enough to prove the deploy chain end to end. A health check that
actually touches the database, and the discovery cron. The UI arrives in
Phase 3.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

from pipeline.config import load_settings
from pipeline.db import close_pool, connection
from pipeline.http import PoliteSession
from pipeline.run_discover import run
from pipeline.run_score import run as score_run
from pipeline.sources.base import SourceConfig
from pipeline.sources.registry import SOURCES, load_targets
from web import register

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jobhunt.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Vercel allows ~500ms after SIGTERM. Closing the pool is well inside that
    # and stops Neon holding connections open for an instance that is gone.
    await close_pool()


app = FastAPI(title="job_hunt", lifespan=lifespan, docs_url=None, redoc_url=None)


def _require_cron_auth(authorization: str | None) -> None:
    """Reject anything that is not Vercel's scheduler.

    Vercel sends `Authorization: Bearer $CRON_SECRET` on every cron invocation.
    Without this check, anyone who guesses the path can trigger a crawl — and
    since these routes are the only writers, that is also a way to burn our
    rate-limit budget against third-party APIs.

    compare_digest rather than `==`: the comparison is against a secret, and a
    short-circuiting equality leaks its prefix through timing.
    """
    expected = os.environ.get("CRON_SECRET")
    if not expected:
        # Failing closed. A missing secret in production would otherwise leave
        # the endpoint wide open, which is worse than the endpoint being down.
        raise HTTPException(status_code=503, detail="CRON_SECRET is not configured")
    header = authorization or ""
    if not header.startswith("Bearer "):
        # removeprefix() alone is a no-op when the prefix is absent, which
        # silently accepts a bare secret too. Vercel always sends the Bearer
        # form, so anything else is not the scheduler.
        raise HTTPException(status_code=401, detail="unauthorized")
    if not secrets.compare_digest(header.removeprefix("Bearer "), expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/api/health")
async def health() -> JSONResponse:
    """Liveness plus a real database round-trip.

    A health check that does not touch Postgres would pass while DATABASE_URL
    pointed somewhere useless, which is the failure this is meant to catch.
    """
    try:
        async with connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT count(*) AS n FROM jobs WHERE filtered_out = false")
            live = (await cur.fetchone())["n"]
            await cur.execute(
                "SELECT started_at, jobs_seen, budget_hit FROM run_log ORDER BY run_id DESC LIMIT 1"
            )
            last = await cur.fetchone()
    except Exception as exc:
        log.exception("health check failed")
        return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})

    return JSONResponse(
        {
            "ok": True,
            "live_jobs": live,
            "last_run": {
                "started_at": last["started_at"].isoformat() if last else None,
                "jobs_seen": last["jobs_seen"] if last else None,
                "budget_hit": last["budget_hit"] if last else None,
            },
        }
    )


@app.get("/api/cron/discover")
async def cron_discover(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """One discovery pass. Vercel's scheduler issues GET, not POST.

    Returns 200 with the stats even on a partial run: the work is durable and
    the next tick resumes, so a non-200 would make Vercel's logs report a
    failure for what is normal, expected behaviour.
    """
    _require_cron_auth(authorization)

    settings = load_settings()
    targets = load_targets(settings.profile_dir / "targets.yaml")
    if not targets:
        # profile/ is gitignored and absent from the build until PROFILE_JSON
        # lands in Phase 2. The aggregators need no tokens, so a run is still
        # useful — say so rather than looking like a silent no-op.
        log.warning("no targets.yaml; only tokenless sources will run")

    async with connection() as conn, PoliteSession(settings.user_agent, conn=conn) as session:
        cfg = SourceConfig(session=session, targets=targets, settings=settings)
        stats = await run(conn, cfg, SOURCES)

    log.info("discover: %s", stats)
    return {
        "jobs_seen": stats.jobs_seen,
        "jobs_new": stats.jobs_new,
        "jobs_filtered": stats.jobs_filtered,
        "errors": stats.errors,
        "sources_skipped": stats.sources_skipped,
        "budget_hit": stats.budget_hit,
        "duration_ms": stats.duration_ms,
    }


@app.get("/api/cron/score")
async def cron_score(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """One scoring pass: embed everything unscored, judge the top N.

    Returns 200 with the stats even on a partial run, for the same reason as
    discovery: the work is durable and the next tick resumes, so a non-200
    would report a failure for what is normal behaviour.
    """
    _require_cron_auth(authorization)

    settings = load_settings()
    async with connection() as conn:
        stats = await score_run(conn, settings)

    log.info("score: %s", stats)
    return {
        "scored": stats.scored,
        "judged": stats.judged,
        "screened": stats.screened,
        "errors": stats.errors,
        "spend_usd": str(stats.spend_usd),
        "budget_hit": stats.budget_hit,
        "duration_ms": stats.duration_ms,
    }


# Mounts /static and the four UI routes. Last, so the API routes above are
# registered first and "/" resolves to the queue rather than a JSON stub.
register(app)
