"""Discovery orchestration.

Callable two ways and identical in both: from the CLI during development, and
from the cron route once deployed. Nothing here knows which one invoked it.

Two properties shape the design:

**Sources run concurrently, writes run serially.** Different sources are
different hosts, so fetching them in parallel is what lets the crawl finish
inside a bounded invocation -- PoliteSession still rate-limits each host
independently. But a psycopg connection cannot serve concurrent operations, so
producers push onto a queue and a single consumer does every write.

**A run stops on a wall-clock budget rather than trying to finish.** Vercel
terminates a function at 800s and records nothing for a killed invocation, so
returning early with `budget_hit=True` is strictly better than being cut off:
the work is durable, the next tick resumes, and we find out the budget was hit.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass

from psycopg import AsyncConnection

from pipeline.config import Settings, load_settings
from pipeline.filters.prefilter import prefilter
from pipeline.models import RawJob
from pipeline.normalize import to_job
from pipeline.sources.base import Source, SourceConfig
from pipeline.store import (
    claim_source,
    finish_run,
    record_source_result,
    start_run,
    upsert_company,
    upsert_job,
)

log = logging.getLogger(__name__)

# Bounded so a fast source cannot pull an entire board into memory while the
# consumer is still writing the previous one.
_QUEUE_SIZE = 200

_DONE = object()


@dataclass(frozen=True)
class RunStats:
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_filtered: int = 0
    errors: int = 0
    budget_hit: bool = False
    sources_skipped: int = 0
    duration_ms: int = 0


async def _produce(
    source: Source,
    cfg: SourceConfig,
    queue: asyncio.Queue,
    deadline: float,
    errors: list[str],
    counters: dict[str, int],
) -> None:
    """Fetch one source onto the queue, stopping at the deadline.

    The producer flags the budget as well as the consumer. A run whose budget
    expires before anything is dequeued would otherwise report budget_hit=False
    while having silently done nothing -- the exact case that makes a missed
    run indistinguishable from an empty one.
    """
    try:
        async for raw in source.fetch(cfg):
            if time.monotonic() >= deadline:
                log.info("%s: stopping on time budget", source.name)
                counters["budget_hit"] = 1
                return
            await queue.put(raw)
    except Exception as exc:  # a broken source must not take down the run
        log.warning("%s: fetch failed: %s", source.name, exc)
        errors.append(source.name)


async def _consume(
    conn: AsyncConnection,
    queue: asyncio.Queue,
    settings: Settings,
    deadline: float,
    counters: dict[str, int],
) -> None:
    """Normalize, filter, and persist. The only writer."""
    while True:
        item = await queue.get()
        try:
            if item is _DONE:
                return
            if time.monotonic() >= deadline:
                counters["budget_hit"] = 1
                continue  # drain without writing; the next run refetches

            raw: RawJob = item
            job = to_job(raw)
            verdict = prefilter(job, settings)

            company_id = await upsert_company(
                conn,
                job.company_name,
                ats_type=raw.source if raw.source in _ATS_SOURCES else None,
            )
            _, is_new = await upsert_job(conn, job, company_id, filter_reason=verdict.reason)

            counters["seen"] += 1
            counters["new"] += int(is_new)
            counters["filtered"] += int(not verdict.passed)
        except Exception as exc:
            log.warning("failed to store a job: %s", exc)
            counters["errors"] += 1
        finally:
            queue.task_done()


# Sources whose name is also the employer's ATS. Aggregators are not.
_ATS_SOURCES = frozenset({"greenhouse", "lever", "ashby"})


async def run(
    conn: AsyncConnection,
    cfg: SourceConfig,
    sources: dict[str, Source],
    *,
    budget_seconds: float | None = None,
) -> RunStats:
    """Fetch every due source, persist what they yield, and record the run."""
    started = time.monotonic()
    budget = budget_seconds if budget_seconds is not None else cfg.settings.run_budget_seconds
    deadline = started + budget

    run_id = await start_run(conn)

    due: list[Source] = []
    skipped = 0
    for source in sources.values():
        if await claim_source(conn, source.name, source.min_interval_seconds):
            due.append(source)
        else:
            skipped += 1
            log.info("%s: not due yet, skipping", source.name)

    counters = {"seen": 0, "new": 0, "filtered": 0, "errors": 0, "budget_hit": 0}
    errors: list[str] = []

    if due:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        consumer = asyncio.create_task(_consume(conn, queue, cfg.settings, deadline, counters))
        await asyncio.gather(*(_produce(s, cfg, queue, deadline, errors, counters) for s in due))
        await queue.put(_DONE)
        await consumer

        for source in due:
            await record_source_result(conn, source.name, ok=source.name not in errors)

    stats = RunStats(
        jobs_seen=counters["seen"],
        jobs_new=counters["new"],
        jobs_filtered=counters["filtered"],
        errors=counters["errors"] + len(errors) + len(cfg.errors),
        budget_hit=bool(counters["budget_hit"]),
        sources_skipped=skipped,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    await finish_run(
        conn,
        run_id,
        jobs_seen=stats.jobs_seen,
        jobs_new=stats.jobs_new,
        jobs_filtered=stats.jobs_filtered,
        errors=stats.errors,
        duration_ms=stats.duration_ms,
        budget_hit=stats.budget_hit,
        notes=f"sources={[s.name for s in due]} skipped={skipped}" if due or skipped else None,
    )
    return stats


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one discovery pass.")
    parser.add_argument("--sources", help="comma-separated subset, e.g. greenhouse,lever")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run everything, then roll back — nothing is persisted",
    )
    parser.add_argument("--budget", type=float, help="wall-clock seconds before stopping")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Imported here so the module can be imported without a profile present.
    from pipeline.http import PoliteSession
    from pipeline.sources.registry import SOURCES, load_targets

    settings = load_settings()
    selected = SOURCES
    if args.sources:
        names = [n.strip() for n in args.sources.split(",") if n.strip()]
        unknown = set(names) - SOURCES.keys()
        if unknown:
            parser.error(f"unknown sources: {', '.join(sorted(unknown))}")
        selected = {n: SOURCES[n] for n in names}

    targets = load_targets(settings.profile_dir / "targets.yaml")
    if not targets:
        log.warning("no targets.yaml found in %s; ATS sources will be empty", settings.profile_dir)

    conn = await AsyncConnection.connect(settings.database_url, autocommit=True)
    try:
        async with PoliteSession(settings.user_agent, conn=conn) as session:
            cfg = SourceConfig(session=session, targets=targets, settings=settings)
            # force_rollback undoes everything at exit, so a dry run exercises
            # the real write path instead of a parallel one that could drift.
            async with conn.transaction(force_rollback=args.dry_run):
                stats = await run(conn, cfg, selected, budget_seconds=args.budget)
    finally:
        await conn.close()

    print(
        f"seen={stats.jobs_seen} new={stats.jobs_new} filtered={stats.jobs_filtered} "
        f"errors={stats.errors} skipped_sources={stats.sources_skipped} "
        f"budget_hit={stats.budget_hit} duration={stats.duration_ms}ms"
        + (" (DRY RUN — rolled back)" if args.dry_run else "")
    )
    return 1 if stats.errors else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
