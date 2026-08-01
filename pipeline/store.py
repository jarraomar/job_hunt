"""Persistence. Every write here reconciles rather than accumulates.

Vercel delivers cron duplicates and can skip a scheduled run entirely (spec
section 4.2), so "run it twice, get the same rows" is a hard requirement rather
than a nicety. Nothing in this module increments or appends.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from pipeline.models import Job
from pipeline.normalize import normalize_company

log = logging.getLogger(__name__)


async def _fetch_one(conn: AsyncConnection, sql: str, params: tuple) -> dict | None:
    """Run a statement and read one row by column name.

    Pins the row factory rather than inheriting the caller's. Reading by name
    off a connection that returns tuples raises `TypeError: tuple indices must
    be integers`, and it does so only on the code path whose connection was
    built differently -- which is exactly the path tests do not cover.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


# Fields refreshed on every sighting. first_seen_at is deliberately absent: it
# records when we first saw the posting and must survive a repost.
_JOB_REFRESH = """
    company_id    = EXCLUDED.company_id,
    source        = EXCLUDED.source,
    source_job_id = EXCLUDED.source_job_id,
    title         = EXCLUDED.title,
    location      = EXCLUDED.location,
    remote_type   = EXCLUDED.remote_type,
    salary_min    = EXCLUDED.salary_min,
    salary_max    = EXCLUDED.salary_max,
    salary_source = EXCLUDED.salary_source,
    description   = EXCLUDED.description,
    apply_url     = EXCLUDED.apply_url,
    posted_at     = EXCLUDED.posted_at,
    filtered_out  = EXCLUDED.filtered_out,
    filter_reason = EXCLUDED.filter_reason,
    last_seen_at  = now()
"""

_UPSERT_JOB = f"""
INSERT INTO jobs (
    fingerprint, company_id, source, source_job_id, title, location, remote_type,
    salary_min, salary_max, salary_source, description, apply_url, posted_at,
    first_seen_at, last_seen_at, filtered_out, filter_reason
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s)
ON CONFLICT (fingerprint) DO UPDATE SET {_JOB_REFRESH}
-- xmax is 0 only on a genuine insert; on the update path it holds the
-- transaction id that locked the row. This is how one statement reports
-- whether it created or matched.
RETURNING job_id, (xmax = 0) AS is_new
"""


async def upsert_company(
    conn: AsyncConnection,
    name: str,
    *,
    ats_type: str | None = None,
    board_token: str | None = None,
) -> int:
    """Insert or match a company by its normalized name, returning company_id.

    COALESCE rather than assignment on ats_type and board_token: an employer
    seen first on Greenhouse and later on HN (which carries no ATS) must not
    have its board token erased by the second sighting.

    The sharia_* columns are never touched here. Spec section 9 makes a user
    verdict permanent, and re-billing a decision we already have is exactly what
    the cache exists to prevent.
    """
    row = await _fetch_one(
        conn,
        """
        INSERT INTO companies (name, normalized_name, ats_type, board_token)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (normalized_name) DO UPDATE SET
            name        = EXCLUDED.name,
            ats_type    = COALESCE(companies.ats_type, EXCLUDED.ats_type),
            board_token = COALESCE(companies.board_token, EXCLUDED.board_token)
        RETURNING company_id
        """,
        (name, normalize_company(name), ats_type, board_token),
    )
    return row["company_id"]


async def upsert_job(
    conn: AsyncConnection,
    job: Job,
    company_id: int,
    *,
    filter_reason: str | None,
) -> tuple[int, bool]:
    """Insert or match a job by content fingerprint. Returns (job_id, is_new).

    Matching on fingerprint rather than source id is what makes a repost
    collapse onto the existing row (spec section 5).

    `filter_reason` carries the pre-filter verdict so `filtered_out` and its
    reason are written in the same statement. The CHECK constraint requires them
    to agree, and a second write could not satisfy it atomically.
    """
    params = (
        job.fingerprint,
        company_id,
        job.source,
        job.source_job_id,
        job.title,
        job.location,
        job.remote_type,
        job.salary_min,
        job.salary_max,
        job.salary_source,
        job.description,
        job.apply_url,
        job.posted_at,
        filter_reason is not None,
        filter_reason,
    )

    try:
        # A savepoint, not a bare statement: if this raises inside an outer
        # transaction, the whole transaction is poisoned and every later
        # statement fails too. The savepoint confines the damage.
        async with conn.transaction():
            row = await _fetch_one(conn, _UPSERT_JOB, params)
            return row["job_id"], row["is_new"]
    except psycopg.errors.UniqueViolation:
        # `jobs` has two unique keys -- fingerprint, and (source, source_job_id)
        # -- and ON CONFLICT can only target one. The escape hatch is a posting
        # whose content changed to match a *different* existing row: it
        # conflicts on fingerprint with that row and on the source id with its
        # own. Identical content is the same job by definition, so resolve onto
        # the fingerprint match and leave the source ids alone.
        log.info(
            "job %s/%s collides on both unique keys; resolving by fingerprint",
            job.source,
            job.source_job_id,
        )
        row = await _fetch_one(
            conn,
            "UPDATE jobs SET last_seen_at = now(), filtered_out = %s, filter_reason = %s"
            " WHERE fingerprint = %s RETURNING job_id",
            (filter_reason is not None, filter_reason, job.fingerprint),
        )
        return row["job_id"], False


async def start_run(conn: AsyncConnection) -> int:
    row = await _fetch_one(
        conn, "INSERT INTO run_log (started_at) VALUES (now()) RETURNING run_id", ()
    )
    return row["run_id"]


async def finish_run(
    conn: AsyncConnection,
    run_id: int,
    *,
    jobs_seen: int,
    jobs_new: int,
    jobs_filtered: int,
    errors: int,
    duration_ms: int | None = None,
    budget_hit: bool = False,
    notes: str | None = None,
) -> None:
    """Close out a run.

    A run with no row here was either killed mid-invocation or never delivered.
    Vercel writes no log at all for an undelivered cron, so the absence of this
    row is the only evidence that a run went missing (spec section 13.5).
    """
    await conn.execute(
        """
        UPDATE run_log SET
            finished_at   = now(),
            jobs_seen     = %s,
            jobs_new      = %s,
            jobs_filtered = %s,
            errors        = %s,
            duration_ms   = %s,
            budget_hit    = %s,
            notes         = %s
        WHERE run_id = %s
        """,
        (jobs_seen, jobs_new, jobs_filtered, errors, duration_ms, budget_hit, notes, run_id),
    )


async def claim_source(conn: AsyncConnection, source: str, min_interval_seconds: float) -> bool:
    """Mark a source as being fetched now, or return False if it is not due yet.

    One statement, so two overlapping cron invocations cannot both claim the
    same source. The WHERE clause on the DO UPDATE is what makes it a claim
    rather than a read-then-write race: when the interval has not elapsed the
    update matches nothing and RETURNING yields no row.

    Remotive is why this exists — its API response asks for at most four calls
    a day, and the discover cron ticks every ten minutes (spec section 4.3).
    """
    row = await _fetch_one(
        conn,
        """
        INSERT INTO source_state (source, last_fetch_started_at)
        VALUES (%s, now())
        ON CONFLICT (source) DO UPDATE SET last_fetch_started_at = now()
        WHERE source_state.last_fetch_started_at IS NULL
           OR source_state.last_fetch_started_at < now() - make_interval(secs => %s)
        RETURNING source
        """,
        (source, min_interval_seconds),
    )
    return row is not None


async def record_source_result(conn: AsyncConnection, source: str, *, ok: bool) -> None:
    """Track consecutive failures so a persistently broken source is visible."""
    if ok:
        await conn.execute(
            "UPDATE source_state SET last_fetch_ok_at = now(), consecutive_errors = 0"
            " WHERE source = %s",
            (source,),
        )
    else:
        await conn.execute(
            "UPDATE source_state SET consecutive_errors = consecutive_errors + 1 WHERE source = %s",
            (source,),
        )
