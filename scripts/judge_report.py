"""Judge the highest-scoring jobs and print every rationale. Costs a few cents.

The tests prove the plumbing. Whether the output is USEFUL is a judgement only
a human can make, so this prints the rationales for reading rather than
asserting on them.

Also the proof that prompt caching is actually engaged: cached reads must be
non-zero from the second call onward. Zero means the static prefix slipped back
under the model's minimum and every call is paying full price silently.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

from psycopg.rows import dict_row

from pipeline.config import load_settings
from pipeline.db import connection
from pipeline.judge import judge_job
from pipeline.llm import spend_today
from pipeline.models import Job
from pipeline.profile import load_profile
from pipeline.score import score_jobs

SQL = """
SELECT j.title, j.location, j.remote_type, j.salary_min, j.salary_max,
       j.salary_source, j.description, j.posted_at, j.source,
       j.fingerprint, j.source_job_id, j.apply_url,
       c.name AS company_name, c.normalized_name AS normalized_company
FROM jobs j
JOIN companies c USING (company_id)
WHERE j.filtered_out = false AND j.closed_at IS NULL
ORDER BY j.last_seen_at DESC
LIMIT 2000
"""


async def main(limit: int = 10) -> None:
    settings = load_settings()
    profile = load_profile(settings)

    async with connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(SQL)
            rows = await cur.fetchall()

        jobs = [
            Job(
                fingerprint=r["fingerprint"],
                source=r["source"],
                source_job_id=r["source_job_id"],
                company_name=r["company_name"],
                normalized_company=r["normalized_company"],
                title=r["title"],
                location=r["location"],
                remote_type=r["remote_type"] or "onsite",
                salary_min=r["salary_min"],
                salary_max=r["salary_max"],
                salary_source=r["salary_source"],
                description=r["description"],
                apply_url=r["apply_url"],
                posted_at=r["posted_at"],
            )
            for r in rows
        ]

        scored = sorted(
            zip(jobs, score_jobs(jobs, profile, settings, now=datetime.now(UTC)), strict=True),
            key=lambda p: -p[1].total_score,
        )

        # One per company, so ten calls do not become ten variations of the
        # same OpenAI posting.
        picked: list[Job] = []
        seen: set[str] = set()
        for job, _ in scored:
            if job.normalized_company in seen:
                continue
            seen.add(job.normalized_company)
            picked.append(job)
            if len(picked) == limit:
                break

        before = await spend_today(conn)
        for job in picked:
            relevance = await judge_job(conn, job, profile, settings)
            if relevance is None:
                print("stopped: cap reached or call failed")
                break
            print(
                f"[{relevance.verdict:8s} {relevance.score:.2f}] "
                f"{job.title[:52]} — {job.company_name}"
            )
            print(f"    {relevance.rationale}\n")

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT count(*) AS calls, sum(cached_input_tokens) AS cached,"
                " sum(cache_write_tokens) AS written, sum(input_tokens) AS fresh"
                " FROM llm_spend WHERE purpose = 'judge' AND day = current_date"
            )
            row = await cur.fetchone()

        spent = await spend_today(conn) - before
        print(f"calls: {row['calls']}   spend this run: ${spent}")
        print(f"cache written: {row['written']:,}   cache read: {row['cached']:,}")
        print(f"uncached input: {row['fresh']:,}")

        if not row["cached"]:
            print("\nWARNING: nothing read from cache — the prefix is under the")
            print("model's minimum and every call is paying full price.")
        else:
            print(f"\ncaching engaged; per-judgement cost ${spent / max(1, row['calls']):.5f}")


if __name__ == "__main__":
    os.environ.setdefault("JOBHUNT_PROFILE_DIR", "profile")
    asyncio.run(main())
