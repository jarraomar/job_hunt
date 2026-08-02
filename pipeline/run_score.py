"""Scoring orchestration. Callable from the CLI and from the cron route.

Two passes over two different candidate sets, deliberately not merged:

- **Score everything unscored.** Embedding is free, so there is no reason to
  be selective.
- **Judge only the top N of what is already scored.** Merging the passes would
  judge in arrival order rather than rank order, spending a capped budget on
  the wrong N jobs.

Both are resumable and idempotent, because Vercel's scheduler is best-effort in
both directions -- it may skip a tick or deliver one twice.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from psycopg import AsyncConnection

from pipeline.config import Settings
from pipeline.filters.sharia import screen_company
from pipeline.judge import MODEL as JUDGE_MODEL
from pipeline.judge import judge_job
from pipeline.llm import spend_today
from pipeline.profile import load_profile
from pipeline.score import score_jobs
from pipeline.store import (
    finish_run,
    jobs_needing_scores,
    score_components_for,
    start_run,
    top_unjudged,
    upsert_score,
)

log = logging.getLogger(__name__)

# Embedding real job descriptions runs at ~11/sec, so a batch of 500 is about
# 45 seconds -- large enough to amortise the model load, small enough that a
# budget check happens often enough to matter.
_SCORE_BATCH = 500


@dataclass(frozen=True)
class ScoreStats:
    scored: int = 0
    judged: int = 0
    screened: int = 0
    errors: int = 0
    spend_usd: Decimal = Decimal("0")
    budget_hit: bool = False
    duration_ms: int = 0


async def run(
    conn: AsyncConnection,
    settings: Settings,
    *,
    budget_seconds: float | None = None,
    now: datetime | None = None,
) -> ScoreStats:
    started = time.monotonic()
    budget = budget_seconds if budget_seconds is not None else settings.run_budget_seconds
    deadline = started + budget
    clock = now or datetime.now(UTC)

    # Raises rather than scoring against an empty résumé. That failure would
    # rank every job identically and only surface weeks later as "the ranking
    # is useless".
    profile = load_profile(settings)

    run_id = await start_run(conn)
    scored = judged = screened = errors = 0
    budget_hit = False

    # Pass 1: embed and score. Free, so it runs on everything.
    while True:
        if time.monotonic() >= deadline:
            budget_hit = True
            break

        batch = await jobs_needing_scores(conn, _SCORE_BATCH)
        if not batch:
            break

        components = score_jobs([s.job for s in batch], profile, settings, now=clock)
        for scored_job, component in zip(batch, components, strict=True):
            try:
                await upsert_score(conn, scored_job.job_id, component)
                scored += 1
            except Exception as exc:
                log.warning("scoring failed for job %s: %s", scored_job.job_id, exc)
                errors += 1

        if len(batch) < _SCORE_BATCH:
            break

    # Pass 2: judge the top N, in rank order, capped per employer.
    if settings.daily_judge_limit > 0 and time.monotonic() < deadline:
        candidates = await top_unjudged(
            conn,
            settings.daily_judge_limit,
            per_company_cap=settings.judge_per_company_cap,
        )
        for candidate in candidates:
            if time.monotonic() >= deadline:
                budget_hit = True
                break

            try:
                verdict = await screen_company(
                    conn,
                    candidate.company_id,
                    candidate.job.company_name,
                    candidate.job.description,
                    settings,
                )
                screened += 1
                if verdict == "excluded":
                    continue
            except Exception as exc:
                log.warning("sharia screen failed for %s: %s", candidate.job.company_name, exc)
                errors += 1

            relevance = await judge_job(conn, candidate.job, profile, settings)
            if relevance is None:
                # Cap reached or the call failed. Both mean stop judging; the
                # scores already written stay.
                break

            component = await score_components_for(conn, candidate.job_id)
            if component is None:
                continue
            await upsert_score(
                conn,
                candidate.job_id,
                component,
                relevance=relevance,
                model=JUDGE_MODEL,
            )
            judged += 1

    stats = ScoreStats(
        scored=scored,
        judged=judged,
        screened=screened,
        errors=errors,
        spend_usd=await spend_today(conn),
        budget_hit=budget_hit,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    await finish_run(
        conn,
        run_id,
        jobs_seen=scored,
        jobs_new=judged,
        jobs_filtered=0,
        errors=errors,
        duration_ms=stats.duration_ms,
        budget_hit=stats.budget_hit,
        notes=f"scored={scored} judged={judged} screened={screened} spend=${stats.spend_usd}",
    )
    log.info("score: %s", stats)
    return stats
