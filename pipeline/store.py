"""Persistence. Every write here reconciles rather than accumulates.

Vercel delivers cron duplicates and can skip a scheduled run entirely (spec
section 4.2), so "run it twice, get the same rows" is a hard requirement rather
than a nicety. Nothing in this module increments or appends.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
    location_class = EXCLUDED.location_class,
    last_seen_at  = now()
"""

_UPSERT_JOB = f"""
INSERT INTO jobs (
    fingerprint, company_id, source, source_job_id, title, location, remote_type,
    salary_min, salary_max, salary_source, description, apply_url, posted_at,
    first_seen_at, last_seen_at, filtered_out, filter_reason, location_class
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s, %s)
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
    location_class: str = "unknown",
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
        location_class,
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
            "UPDATE jobs SET last_seen_at = now(), filtered_out = %s, filter_reason = %s,"
            " location_class = %s WHERE fingerprint = %s RETURNING job_id",
            (filter_reason is not None, filter_reason, location_class, job.fingerprint),
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


# --- Phase 2: scoring ------------------------------------------------------


async def _fetch_all(conn: AsyncConnection, sql: str, params: tuple) -> list[dict]:
    """Run a statement and read every row by column name.

    Pins the row factory for the same reason as _fetch_one.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


@dataclass(frozen=True)
class ScoredJob:
    """A job plus the identifiers Job itself does not carry.

    `Job` is the shape sources emit and store writes; it has no job_id or
    company_id because neither exists until a row is written. Scoring needs
    both -- one to write the score against, one to screen the employer -- so
    they travel alongside rather than being bolted onto Job and made optional
    everywhere upstream.
    """

    job_id: int
    company_id: int
    job: Job


_JOB_SELECT = """
SELECT j.job_id, j.company_id, j.fingerprint, j.source, j.source_job_id,
       j.title, j.location, j.remote_type, j.salary_min, j.salary_max,
       j.salary_source, j.description, j.apply_url, j.posted_at,
       c.name AS company_name, c.normalized_name AS normalized_company
"""


def _row_to_scored_job(row: dict) -> ScoredJob:
    return ScoredJob(
        job_id=row["job_id"],
        company_id=row["company_id"],
        job=Job(
            fingerprint=row["fingerprint"],
            source=row["source"],
            source_job_id=row["source_job_id"],
            company_name=row["company_name"],
            normalized_company=row["normalized_company"],
            title=row["title"],
            location=row["location"],
            remote_type=row["remote_type"] or "onsite",
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            salary_source=row["salary_source"],
            description=row["description"],
            apply_url=row["apply_url"],
            posted_at=row["posted_at"],
        ),
    )


async def reclassify_jobs(conn: AsyncConnection, settings, *, batch: int = 1000):
    """Stream every stored job with its current verdict, for re-judging.

    Pages rather than returning a list: the corpus is already ~5,000 rows and
    grows every ten minutes, and holding all of their descriptions in memory to
    re-run a regex over them is the kind of thing that works locally and dies
    inside a 1 GB function.

    Each page is fully read before anything is yielded, so the caller may write
    on the same connection while iterating. Paging by OFFSET is safe here for
    the same reason: the caller's updates touch neither the ORDER BY key nor
    the WHERE clause, so no row shifts between pages.
    """
    offset = 0
    while True:
        rows = await _fetch_all(
            conn,
            _JOB_SELECT
            + """
            , j.filter_reason, j.location_class
            FROM jobs j
            JOIN companies c USING (company_id)
            WHERE j.closed_at IS NULL
            ORDER BY j.job_id
            LIMIT %s OFFSET %s
            """,
            (batch, offset),
        )
        if not rows:
            return
        for row in rows:
            yield _row_to_scored_job(row), row["filter_reason"], row["location_class"]
        offset += len(rows)


async def jobs_needing_scores(conn: AsyncConnection, limit: int) -> list[ScoredJob]:
    """Live, unscored jobs.

    Filtered-out rows are never scored. A filtered job carrying a score would
    surface in the queue, which is the one thing the pre-filter exists to
    prevent.
    """
    rows = await _fetch_all(
        conn,
        _JOB_SELECT
        + """
        FROM jobs j
        JOIN companies c USING (company_id)
        LEFT JOIN scores s USING (job_id)
        WHERE j.filtered_out = false AND j.closed_at IS NULL AND s.job_id IS NULL
        ORDER BY j.last_seen_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [_row_to_scored_job(r) for r in rows]


async def top_unjudged(
    conn: AsyncConnection, limit: int, *, per_company_cap: int = 3
) -> list[ScoredJob]:
    """Highest-scoring unjudged jobs, with at most `per_company_cap` per employer.

    Ordered by total_score, not arrival: judging in arrival order would spend a
    capped budget on the wrong N jobs.

    The per-company cap exists because one employer supplied 23% of live
    postings and 17 of the top 25 by score. Without it a 50-job budget is spent
    re-reading near-duplicate roles at a single company, and the queue reads as
    though nobody else is hiring.
    """
    rows = await _fetch_all(
        conn,
        """
        WITH ranked AS (
            SELECT j.job_id,
                   row_number() OVER (
                       PARTITION BY j.company_id ORDER BY s.total_score DESC
                   ) AS rank_in_company
            FROM scores s
            JOIN jobs j USING (job_id)
            WHERE s.judged_at IS NULL AND j.filtered_out = false AND j.closed_at IS NULL
        )
        """
        + _JOB_SELECT
        + """
        , s.total_score
        FROM ranked
        JOIN jobs j USING (job_id)
        JOIN companies c USING (company_id)
        JOIN scores s USING (job_id)
        WHERE ranked.rank_in_company <= %s
        ORDER BY s.total_score DESC
        LIMIT %s
        """,
        (per_company_cap, limit),
    )
    return [_row_to_scored_job(r) for r in rows]


async def score_components_for(conn: AsyncConnection, job_id: int):
    """Re-read stored components so a judged upsert does not clobber them."""
    from pipeline.score import ScoreComponents

    row = await _fetch_one(
        conn,
        "SELECT embed_similarity, rule_score, freshness_score, total_score, is_stretch"
        " FROM scores WHERE job_id = %s",
        (job_id,),
    )
    if row is None:
        return None
    return ScoreComponents(
        embed_similarity=row["embed_similarity"],
        rule_score=row["rule_score"],
        freshness_score=row["freshness_score"],
        total_score=row["total_score"],
        is_stretch=row["is_stretch"],
    )


async def upsert_score(
    conn: AsyncConnection,
    job_id: int,
    components,
    *,
    relevance=None,
    model: str | None = None,
) -> None:
    """Write or refresh a score. Idempotent, like every other write here."""
    await conn.execute(
        """
        INSERT INTO scores (job_id, embed_similarity, rule_score, freshness_score,
                            total_score, is_stretch, relevance_verdict, rationale,
                            model, judged_at, scored_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s::text IS NULL THEN NULL ELSE now() END, now())
        ON CONFLICT (job_id) DO UPDATE SET
            embed_similarity  = EXCLUDED.embed_similarity,
            rule_score        = EXCLUDED.rule_score,
            freshness_score   = EXCLUDED.freshness_score,
            total_score       = EXCLUDED.total_score,
            is_stretch        = EXCLUDED.is_stretch,
            -- A judgement already paid for is never overwritten with NULL by a
            -- later re-score.
            relevance_verdict = COALESCE(EXCLUDED.relevance_verdict, scores.relevance_verdict),
            rationale         = COALESCE(EXCLUDED.rationale, scores.rationale),
            model             = COALESCE(EXCLUDED.model, scores.model),
            judged_at         = COALESCE(EXCLUDED.judged_at, scores.judged_at),
            scored_at         = now()
        """,
        (
            job_id,
            components.embed_similarity,
            components.rule_score,
            components.freshness_score,
            components.total_score,
            components.is_stretch,
            relevance.verdict if relevance else None,
            relevance.rationale if relevance else None,
            model if relevance else None,
            relevance.verdict if relevance else None,
        ),
    )


# --- Phase 3: the queue ----------------------------------------------------


# Sortable columns, mapped to SQL. A whitelist rather than interpolation: this
# value arrives from a query string, and an ORDER BY clause cannot be
# parameterised, so the only safe way to accept one is to never use the caller's
# text at all.
#
# Every ordering ends in job_id. Without it, rows tied on the sort key have no
# defined order between queries, and LIMIT/OFFSET paging silently repeats and
# skips them -- hundreds of jobs share a salary band or a posting date.
_SORTS = {
    "score": "s.total_score {dir}, j.job_id DESC",
    "posted": "j.posted_at {dir} NULLS LAST, j.job_id DESC",
    # Sorts on the top of the band: "up to $200k" beats "$150k+" when you are
    # looking for the best-paying role, and the band's ceiling is what says so.
    "salary": "COALESCE(j.salary_max, j.salary_min) {dir} NULLS LAST, j.job_id DESC",
    "company": "lower(c.name) {dir}, s.total_score DESC, j.job_id DESC",
}

DEFAULT_SORT = "score"


@dataclass(frozen=True)
class QueueFilters:
    """What the queue is currently showing.

    A dataclass rather than eight keyword arguments because the route, the
    template, and the pagination links all need to pass the same set around
    intact -- and a filter dropped on the way to the "next page" link is the
    classic version of this bug.
    """

    sort: str = DEFAULT_SORT
    descending: bool = True
    min_score: float = 0.0
    remote_types: tuple[str, ...] = ()
    location_classes: tuple[str, ...] = ()
    min_salary: int | None = None
    posted_within_days: int | None = None
    company: str = ""
    judged_only: bool = False
    per_company_cap: int = 3

    def normalized(self) -> QueueFilters:
        """Coerce anything a query string can carry into something valid.

        Query strings are hand-editable and links go stale across deploys, so a
        junk `sort=` value must render the default page rather than a 500.
        """
        from dataclasses import replace

        return replace(self, sort=self.sort if self.sort in _SORTS else DEFAULT_SORT)

    @property
    def is_active(self) -> bool:
        """True when anything is narrowing the queue, so the UI can say so."""
        return bool(
            self.min_score
            or self.remote_types
            or self.location_classes
            or self.min_salary
            or self.posted_within_days
            or self.company
            or self.judged_only
        )


async def queue_page(
    conn: AsyncConnection,
    filters: QueueFilters | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """The ranked queue: scored, live, not yet acted on.

    `excluded` companies are absent; `flagged` ones are present and badged. An
    excluded verdict is a decision, a flagged one is a question for Jarra, and
    collapsing them either hides jobs needing a human or shows ones that do not.

    Capped per employer for the same reason judging is: one company supplied
    23% of live postings and 17 of the top 25 by score, and an uncapped queue
    reads as though nobody else is hiring.

    The per-company cap is applied to the *filtered* set, not before it. Ranking
    within a company first and filtering second would let a company's three
    slots be taken by jobs the filter then removes, so narrowing to "remote
    only" could empty an employer that has ten remote roles.
    """
    f = (filters or QueueFilters()).normalized()

    where = [
        "j.filtered_out = false",
        "j.closed_at IS NULL",
        "c.sharia_verdict <> 'excluded'",
        "s.total_score >= %(min_score)s",
        # The queue is what is left to review, not an archive: anything already
        # acted on has left it.
        "(a.status IS NULL OR a.status = 'queued')",
    ]
    params: dict[str, object] = {
        "min_score": f.min_score,
        "cap": f.per_company_cap,
        "limit": limit,
        "offset": offset,
    }

    if f.remote_types:
        where.append("j.remote_type = ANY(%(remote_types)s)")
        params["remote_types"] = list(f.remote_types)
    if f.location_classes:
        where.append("j.location_class = ANY(%(location_classes)s)")
        params["location_classes"] = list(f.location_classes)
    if f.min_salary:
        # A job with no published salary is kept. Two thirds of postings have
        # none, and dropping them would turn a "pays at least $150k" filter into
        # "published a number", which is a different and much smaller question.
        where.append(
            "(COALESCE(j.salary_max, j.salary_min) IS NULL"
            " OR COALESCE(j.salary_max, j.salary_min) >= %(min_salary)s)"
        )
        params["min_salary"] = f.min_salary
    if f.posted_within_days:
        # NULL posted_at is kept for the same reason: HN and parts of Lever
        # never publish one, and they are not therefore old.
        where.append(
            "(j.posted_at IS NULL OR j.posted_at >= now() - make_interval(days => %(days)s))"
        )
        params["days"] = f.posted_within_days
    if f.company:
        where.append("c.name ILIKE %(company)s")
        params["company"] = f"%{f.company}%"
    if f.judged_only:
        where.append("s.judged_at IS NOT NULL")

    order = _SORTS[f.sort].format(dir="DESC" if f.descending else "ASC")
    conditions = "\n              AND ".join(where)

    return await _fetch_all(
        conn,
        f"""
        WITH eligible AS (
            SELECT j.job_id,
                   row_number() OVER (
                       PARTITION BY j.company_id ORDER BY s.total_score DESC
                   ) AS rank_in_company
            FROM scores s
            JOIN jobs j USING (job_id)
            JOIN companies c USING (company_id)
            LEFT JOIN applications a USING (job_id)
            WHERE {conditions}
        )
        SELECT j.job_id, j.title, j.location, j.remote_type, j.location_class,
               j.salary_min, j.salary_max, j.apply_url, j.posted_at,
               c.name AS company_name, c.sharia_verdict, c.sharia_reason,
               s.total_score, s.embed_similarity, s.relevance_verdict, s.rationale,
               s.is_stretch
        FROM eligible
        JOIN jobs j USING (job_id)
        JOIN companies c USING (company_id)
        JOIN scores s USING (job_id)
        WHERE eligible.rank_in_company <= %(cap)s
        ORDER BY {order}
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )


async def queue_facets(conn: AsyncConnection) -> dict:
    """What the filter controls should offer, counted from live data.

    Built from the queue's own eligibility rules rather than from the whole
    jobs table, so a company with nothing left to review does not appear in the
    dropdown as though it did.
    """
    rows = await _fetch_all(
        conn,
        """
        SELECT c.name AS company, j.remote_type, j.location_class,
               s.judged_at IS NOT NULL AS judged
        FROM scores s
        JOIN jobs j USING (job_id)
        JOIN companies c USING (company_id)
        LEFT JOIN applications a USING (job_id)
        WHERE j.filtered_out = false AND j.closed_at IS NULL
          AND c.sharia_verdict <> 'excluded'
          AND (a.status IS NULL OR a.status = 'queued')
        """,
        (),
    )
    companies: dict[str, int] = {}
    remote_types: dict[str, int] = {}
    location_classes: dict[str, int] = {}
    for row in rows:
        companies[row["company"]] = companies.get(row["company"], 0) + 1
        remote_types[row["remote_type"]] = remote_types.get(row["remote_type"], 0) + 1
        cls = row["location_class"]
        location_classes[cls] = location_classes.get(cls, 0) + 1
    return {
        "total": len(rows),
        "judged": sum(1 for r in rows if r["judged"]),
        "companies": sorted(companies.items(), key=lambda kv: (-kv[1], kv[0])),
        "remote_types": remote_types,
        "location_classes": location_classes,
    }


VALID_STATUSES = frozenset({"queued", "applied", "responded", "interview", "rejected", "dismissed"})

FUNNEL_STAGES = ("queued", "applied", "responded", "interview", "rejected")


async def job_detail(conn: AsyncConnection, job_id: int) -> dict | None:
    return await _fetch_one(
        conn,
        """
        SELECT j.*, c.name AS company_name, c.company_id,
               c.sharia_verdict, c.sharia_reason, c.sharia_source,
               s.total_score, s.embed_similarity, s.rule_score, s.freshness_score,
               s.relevance_verdict, s.rationale, s.is_stretch,
               COALESCE(a.status, 'queued') AS status, a.applied_at, a.notes
        FROM jobs j
        JOIN companies c USING (company_id)
        LEFT JOIN scores s USING (job_id)
        LEFT JOIN applications a USING (job_id)
        WHERE j.job_id = %s
        """,
        (job_id,),
    )


async def set_status(conn: AsyncConnection, job_id: int, status: str) -> None:
    """Upsert the application row.

    applied_at is set on the first transition out of queued and never rewritten:
    conversion stats measure intervals from the application date, so overwriting
    it on a later transition would silently reset every one of them.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {status!r}")

    needs_timestamp = status not in ("queued", "dismissed")
    await conn.execute(
        """
        INSERT INTO applications (job_id, status, applied_at, updated_at)
        VALUES (%s, %s, CASE WHEN %s THEN now() ELSE NULL END, now())
        ON CONFLICT (job_id) DO UPDATE SET
            status     = EXCLUDED.status,
            applied_at = CASE
                WHEN %s THEN COALESCE(applications.applied_at, now())
                ELSE applications.applied_at
            END,
            updated_at = now()
        """,
        (job_id, status, needs_timestamp, needs_timestamp),
    )


async def answer_bank_all(conn: AsyncConnection) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT answer_id, question, answer, category, question_key FROM answer_bank"
        " ORDER BY category NULLS LAST, question",
        (),
    )


async def upsert_answer(
    conn: AsyncConnection,
    question: str,
    answer: str,
    category: str | None = None,
    question_key: str | None = None,
) -> None:
    """Store an answer, keyed by the canonical question where there is one.

    Two conflict targets, and both are needed: `question` catches re-editing the
    same wording, `question_key` catches answering the same question phrased
    differently. Postgres only allows ON CONFLICT to name one, so the key case
    is resolved by looking first.
    """
    if not answer.strip():
        # An empty stored answer gets pasted into a real application form.
        raise ValueError("answer cannot be empty")
    if not question.strip():
        raise ValueError("question cannot be empty")

    question, answer = question.strip(), answer.strip()
    category = (category or "").strip() or None
    key = (question_key or "").strip() or None

    if key:
        existing = await _fetch_one(
            conn, "SELECT answer_id FROM answer_bank WHERE question_key = %s", (key,)
        )
        if existing:
            await conn.execute(
                "UPDATE answer_bank SET question = %s, answer = %s, category = %s,"
                " updated_at = now() WHERE answer_id = %s",
                (question, answer, category, existing["answer_id"]),
            )
            await _clear_unmapped(conn, question, key)
            return

    await conn.execute(
        "INSERT INTO answer_bank (question, answer, category, question_key)"
        " VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (question) DO UPDATE SET answer = EXCLUDED.answer,"
        " category = EXCLUDED.category,"
        # COALESCE so re-editing by text does not erase a key set earlier.
        " question_key = COALESCE(EXCLUDED.question_key, answer_bank.question_key),"
        " updated_at = now()",
        (question, answer, category, key),
    )
    await _clear_unmapped(conn, question, key)


async def _clear_unmapped(conn: AsyncConnection, question: str, key: str | None) -> None:
    """The question is answered now, so every phrasing of it is no longer a gap."""
    await conn.execute(
        "DELETE FROM unmapped_questions WHERE question = %s"
        " OR (question_key IS NOT NULL AND question_key = %s)",
        (question, key),
    )


async def record_unmapped(
    conn: AsyncConnection, question: str, *, question_key: str | None = None
) -> None:
    """Note a question we could not answer.

    Counts sightings rather than inserting duplicates: the count is what sorts
    the settings to-do list, so the question blocking the most applications sits
    at the top rather than the one seen first.
    """
    await conn.execute(
        "INSERT INTO unmapped_questions (question, question_key) VALUES (%s, %s)"
        " ON CONFLICT (question) DO UPDATE SET seen_count = unmapped_questions.seen_count + 1,"
        " last_seen = now(), question_key = COALESCE(EXCLUDED.question_key,"
        " unmapped_questions.question_key)",
        (question.strip(), (question_key or "").strip() or None),
    )


async def set_sharia_override(conn: AsyncConnection, company_id: int, verdict: str) -> None:
    """Record a human ruling.

    sharia_source='user' is permanent: the screen returns it without re-billing
    or re-evaluating. This is the mechanism that keeps an LLM from silently
    making a religious ruling that stands uncorrected.
    """
    from pipeline.filters.sharia import VERDICTS

    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict: {verdict!r}")
    await conn.execute(
        "UPDATE companies SET sharia_verdict = %s, sharia_source = 'user',"
        " sharia_reason = 'Set manually.', sharia_decided_at = now()"
        " WHERE company_id = %s",
        (verdict, company_id),
    )


async def funnel_counts(conn: AsyncConnection) -> dict[str, int]:
    """Count per funnel stage. `dismissed` is excluded -- it is not a stage, and
    counting it would deflate every rate."""
    rows = await _fetch_all(
        conn,
        "SELECT status, count(*) AS n FROM applications"
        " WHERE status <> 'dismissed' GROUP BY status",
        (),
    )
    return {row["status"]: row["n"] for row in rows}


async def weekly_conversion(conn: AsyncConnection, weeks: int = 8) -> list[dict]:
    """Applications per week and what came of them.

    Bucketed by applied_at, NOT by responded_at. A response arrives one to three
    weeks after the application; bucketing by response date attributes it to a
    week whose application volume has nothing to do with it, which makes the
    rate meaningless exactly when it matters.
    """
    return await _fetch_all(
        conn,
        """
        SELECT date_trunc('week', applied_at) AS week,
               count(*) AS applied,
               count(*) FILTER (WHERE status IN ('responded', 'interview')) AS responded,
               count(*) FILTER (WHERE status = 'interview') AS interviewed,
               CASE WHEN count(*) = 0 THEN NULL
                    ELSE count(*) FILTER (WHERE status IN ('responded', 'interview'))::float
                         / count(*)
               END AS response_rate
        FROM applications
        WHERE applied_at IS NOT NULL
          AND applied_at >= date_trunc('week', now()) - (%s * interval '1 week')
        GROUP BY week
        ORDER BY week DESC
        """,
        (weeks,),
    )


async def recent_runs(conn: AsyncConnection, limit: int = 20) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT run_id, started_at, finished_at, jobs_seen, jobs_new, jobs_filtered,"
        " errors, duration_ms, budget_hit, notes"
        " FROM run_log ORDER BY run_id DESC LIMIT %s",
        (limit,),
    )


async def unmapped_questions(conn: AsyncConnection) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT question, question_key, seen_count, last_seen FROM unmapped_questions"
        " ORDER BY seen_count DESC, last_seen DESC",
        (),
    )


async def reviewable_companies(conn: AsyncConnection) -> list[dict]:
    """Companies whose verdict is a decision worth showing or revisiting."""
    return await _fetch_all(
        conn,
        "SELECT company_id, name, sharia_verdict, sharia_sector, sharia_reason, sharia_source"
        " FROM companies WHERE sharia_verdict IN ('flagged', 'excluded')"
        "    OR sharia_source = 'user'"
        " ORDER BY sharia_verdict, name",
        (),
    )


# --- what the filters actually did ------------------------------------------
#
# Every screen in this system rejects silently: a filtered job keeps its row,
# loses its place in the queue, and says nothing about it. That is the correct
# behaviour and it is also how the Sharia screen deleted fourteen good employers
# for a fortnight without anyone noticing. These queries are the antidote --
# they exist so the settings page can state, in numbers, what each gate threw
# away and on what grounds.


async def intake_breakdown(conn: AsyncConnection) -> list[dict]:
    """Live jobs by pre-filter verdict, worst offender first.

    `filter_reason IS NULL` is the pass row and is reported as one of the
    reasons rather than as a separate total, so the column sums to intake.
    """
    return await _fetch_all(
        conn,
        """
        SELECT COALESCE(filter_reason, 'passes') AS reason,
               count(*) AS n,
               count(*) FILTER (WHERE first_seen_at >= now() - interval '7 days') AS this_week
        FROM jobs
        WHERE closed_at IS NULL
        GROUP BY 1
        ORDER BY (COALESCE(filter_reason, 'passes') = 'passes') DESC, n DESC
        """,
        (),
    )


async def location_breakdown(conn: AsyncConnection) -> list[dict]:
    """Live jobs by geography class and arrangement.

    Reported as the grid rather than as two independent totals because the
    decision is made on the pair: `us` passes when remote and is rejected when
    hybrid, and a flat count of `us` cannot show that.

    `rejected` counts only the jobs *this* screen rejected, not every filtered
    job in the cell. Counting `filtered_out` reported 673 of 822 local hybrid
    roles as rejected -- all of them thrown out by the title screen -- which
    read as the geography rule discarding Bay Area jobs.
    """
    return await _fetch_all(
        conn,
        """
        SELECT location_class, remote_type, count(*) AS n,
               count(*) FILTER (
                   WHERE filter_reason IN ('location_outside_area', 'remote_outside_us')
               ) AS rejected
        FROM jobs
        WHERE closed_at IS NULL
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
        (),
    )


async def company_screen_all(conn: AsyncConnection) -> list[dict]:
    """Every company the Sharia screen has ruled on, with what it costs them.

    `live_jobs` is the point of this query. A verdict on a company with two
    open roles and a verdict on a company with 400 are the same row in the
    companies table and very different decisions, and the excluded list is only
    reviewable if it says which is which.
    """
    return await _fetch_all(
        conn,
        """
        SELECT c.company_id, c.name, c.sharia_verdict, c.sharia_sector,
               c.sharia_reason, c.sharia_source, c.sharia_decided_at,
               count(j.job_id) FILTER (
                   WHERE j.filtered_out = false AND j.closed_at IS NULL
               ) AS live_jobs
        FROM companies c
        LEFT JOIN jobs j USING (company_id)
        WHERE c.sharia_verdict IS NOT NULL AND c.sharia_verdict <> 'unknown'
        GROUP BY c.company_id
        ORDER BY
            -- Things needing a decision first, then things that cost the most.
            CASE c.sharia_verdict
                WHEN 'flagged' THEN 0 WHEN 'excluded' THEN 1 ELSE 2 END,
            live_jobs DESC, c.name
        """,
        (),
    )


async def llm_spend_summary(conn: AsyncConnection) -> dict | None:
    return await _fetch_one(
        conn,
        "SELECT COALESCE(sum(cost_usd), 0) AS today,"
        " count(*) AS calls,"
        " COALESCE(sum(cached_input_tokens), 0) AS cached"
        " FROM llm_spend WHERE day = (now() AT TIME ZONE 'UTC')::date",
        (),
    )
