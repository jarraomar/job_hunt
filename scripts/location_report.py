#!/usr/bin/env python
"""What the location screen does to the live corpus.

Run this before changing geography.yaml and read the rejected list. Every line
under "would be rejected" is a job that disappears from the queue, and the
Sharia screen already demonstrated once that a vocabulary change can quietly
delete fourteen good employers while every unit test still passes.

    ./venv/bin/python scripts/location_report.py
    ./venv/bin/python scripts/location_report.py --show elsewhere
    ./venv/bin/python scripts/location_report.py --sample newark
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.db import connection  # noqa: E402
from pipeline.filters.location import (  # noqa: E402
    CLASSES,
    classify_location,
    location_verdict,
)

_QUERY = """
SELECT j.job_id, j.title, j.location, j.remote_type, j.description, c.name AS company
FROM jobs j JOIN companies c USING (company_id)
WHERE j.closed_at IS NULL
ORDER BY j.job_id
"""


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", choices=[*CLASSES, "rejected"], help="list every job in a class")
    parser.add_argument("--sample", help="list jobs whose location contains this substring")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)

    async with connection() as conn:
        cur = await conn.execute(_QUERY)
        rows = await cur.fetchall()

    by_class: collections.Counter[str] = collections.Counter()
    by_reason: collections.Counter[str] = collections.Counter()
    pairs: collections.Counter[tuple[str, str]] = collections.Counter()
    shown = 0

    for row in rows:
        verdict_class = classify_location(
            location=row["location"], description=row["description"] or ""
        )
        reason = location_verdict(verdict_class, row["remote_type"])
        by_class[verdict_class] += 1
        by_reason[reason or "passes"] += 1
        pairs[(verdict_class, row["remote_type"])] += 1

        matches_show = args.show and (
            verdict_class == args.show or (args.show == "rejected" and reason)
        )
        matches_sample = args.sample and args.sample.lower() in (row["location"] or "").lower()
        if (matches_show or matches_sample) and shown < args.limit:
            shown += 1
            mark = "REJECT" if reason else "keep  "
            print(
                f"{mark} {verdict_class:<9} {row['remote_type']:<7}"
                f" {(row['location'] or '(none)')[:38]:<40}"
                f" {row['company'][:24]:<26} {row['title'][:40]}"
            )

    if shown:
        print()

    total = len(rows)
    print(f"{total} jobs\n")
    print("class            n     share")
    for name in CLASSES:
        n = by_class[name]
        print(f"  {name:<12} {n:>5}   {n / total:>6.1%}" if total else f"  {name:<12} {n:>5}")

    print("\nverdict                    n")
    for reason, n in by_reason.most_common():
        print(f"  {reason:<24} {n:>5}")

    print("\nclass x arrangement")
    for (name, arrangement), n in sorted(pairs.items()):
        gate = location_verdict(name, arrangement)
        print(f"  {name:<10} {arrangement:<8} {n:>5}  {'REJECTED: ' + gate if gate else 'kept'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
