"""Score every live job and print the ranking. Reads only; writes nothing.

Safe to point at production: it opens one connection, selects, and scores in
memory. Nothing is persisted, so this can be run against the real corpus to
tune weights before any scores exist.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime

from psycopg.rows import dict_row

from pipeline.config import load_settings
from pipeline.db import connection
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
LIMIT %s
"""


async def main(limit: int = 3000) -> None:
    settings = load_settings()
    profile = load_profile(settings)

    async with connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(SQL, (limit,))
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

    started = datetime.now(UTC)
    results = score_jobs(jobs, profile, settings, now=started)
    elapsed = (datetime.now(UTC) - started).total_seconds()

    ranked = sorted(zip(jobs, results, strict=True), key=lambda p: -p[1].total_score)
    print(f"{len(ranked)} jobs scored in {elapsed:.1f}s ({len(ranked) / elapsed:.0f}/s)\n")

    print("TOP 25")
    for job, s in ranked[:25]:
        print(
            f"{s.total_score:.3f} sim={s.embed_similarity:.2f} rule={s.rule_score:.2f}"
            f" fr={s.freshness_score:.2f}  {job.title[:48]:50s} {job.company_name[:22]}"
        )

    print("\nBOTTOM 10 (sanity: these should look clearly wrong for the profile)")
    for job, s in ranked[-10:]:
        print(
            f"{s.total_score:.3f} sim={s.embed_similarity:.2f}"
            f"  {job.title[:48]:50s} {job.company_name[:22]}"
        )

    print("\nSIMILARITY HISTOGRAM")
    buckets = Counter(int(s.embed_similarity * 20) for _, s in ranked)
    for bucket in sorted(buckets):
        low = bucket / 20
        bar = "#" * max(1, buckets[bucket] * 60 // max(buckets.values()))
        print(f"  {low:.2f}-{low + 0.05:.2f} {buckets[bucket]:5d} {bar}")

    print("\nTOTAL-SCORE HISTOGRAM")
    buckets = Counter(int(s.total_score * 20) for _, s in ranked)
    for bucket in sorted(buckets):
        low = bucket / 20
        bar = "#" * max(1, buckets[bucket] * 60 // max(buckets.values()))
        print(f"  {low:.2f}-{low + 0.05:.2f} {buckets[bucket]:5d} {bar}")


if __name__ == "__main__":
    asyncio.run(main())
