#!/usr/bin/env python
"""Re-run the pre-filter over every stored job.

The pre-filter normally runs once, at discovery. This applies it again to rows
already in the table, which is what makes a rule change recoverable: edit
geography.yaml or a title pattern, run this, and every job is re-judged under
the new rule -- including jobs the old rule wrongly rejected, because rejection
sets a flag rather than deleting a row.

That property is why the location screen was allowed to reject at all. Without
it, a wrong term in geography.yaml would silently and permanently delete jobs,
and the pre-filter's own docstring rules that out.

    ./venv/bin/python scripts/reclassify.py --dry-run   # report, change nothing
    ./venv/bin/python scripts/reclassify.py
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.config import load_settings  # noqa: E402
from pipeline.db import connection  # noqa: E402
from pipeline.filters.prefilter import prefilter  # noqa: E402
from pipeline.store import reclassify_jobs  # noqa: E402

log = logging.getLogger("reclassify")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--batch", type=int, default=1000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = load_settings()
    changes: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)

    async with connection() as conn:
        async for job, before, after in reclassify_jobs(conn, settings, batch=args.batch):
            verdict = prefilter(job.job, settings)
            was = before or "passes"
            now = verdict.reason or "passes"
            if was == now and after == verdict.location_class:
                continue
            key = f"{was} -> {now}"
            changes[key] += 1
            if len(examples[key]) < 4:
                examples[key].append(f"{job.job.company_name}: {job.job.title[:48]}")
            if not args.dry_run:
                await conn.execute(
                    "UPDATE jobs SET filtered_out = %s, filter_reason = %s, location_class = %s"
                    " WHERE job_id = %s",
                    (
                        verdict.reason is not None,
                        verdict.reason,
                        verdict.location_class,
                        job.job_id,
                    ),
                )

    if not changes:
        print("nothing changed")
        return 0

    print("dry run -- nothing written\n" if args.dry_run else "")
    for key, n in changes.most_common():
        print(f"{n:>6}  {key}")
        for example in examples[key]:
            print(f"          {example}")
    print(f"\n{sum(changes.values())} jobs re-judged")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
