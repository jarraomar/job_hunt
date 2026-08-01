from datetime import UTC, datetime, timedelta

import pytest

from pipeline.models import Job
from pipeline.store import finish_run, start_run, upsert_company, upsert_job


def make_job(**overrides) -> Job:
    base = dict(
        fingerprint="a" * 64,
        source="greenhouse",
        source_job_id="1",
        company_name="Acme, Inc.",
        normalized_company="acme",
        title="Senior Software Engineer",
        location="San Francisco, CA",
        remote_type="hybrid",
        salary_min=150_000,
        salary_max=200_000,
        salary_source="parsed",
        description="Python, React, AWS.",
        apply_url="https://example.com/1",
        posted_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    base.update(overrides)
    return Job(**base)


# --- companies ---------------------------------------------------------------


async def test_upsert_company_inserts_and_returns_id(db):
    company_id = await upsert_company(db, "Acme, Inc.", ats_type="greenhouse", board_token="acme")
    assert isinstance(company_id, int)
    cur = await db.execute("SELECT name, normalized_name, ats_type FROM companies")
    row = await cur.fetchone()
    assert row["name"] == "Acme, Inc."
    assert row["normalized_name"] == "acme"
    assert row["ats_type"] == "greenhouse"


async def test_upsert_company_is_idempotent(db):
    first = await upsert_company(db, "Acme, Inc.")
    second = await upsert_company(db, "ACME Inc")
    assert first == second
    cur = await db.execute("SELECT count(*) AS n FROM companies")
    assert (await cur.fetchone())["n"] == 1


async def test_upsert_company_never_downgrades_a_known_ats(db):
    # HN gives no ATS; an earlier Greenhouse sighting must not be erased by a
    # later HN one for the same employer.
    await upsert_company(db, "Acme", ats_type="greenhouse", board_token="acme")
    await upsert_company(db, "Acme", ats_type=None, board_token=None)
    cur = await db.execute("SELECT ats_type, board_token FROM companies")
    row = await cur.fetchone()
    assert row["ats_type"] == "greenhouse"
    assert row["board_token"] == "acme"


async def test_upsert_company_preserves_the_sharia_verdict(db):
    # Spec section 9: a user verdict is permanent and must never be re-billed.
    company_id = await upsert_company(db, "Acme")
    await db.execute(
        "UPDATE companies SET sharia_verdict = 'excluded', sharia_source = 'user'"
        " WHERE company_id = %s",
        (company_id,),
    )
    await upsert_company(db, "Acme", ats_type="lever")
    cur = await db.execute("SELECT sharia_verdict, sharia_source FROM companies")
    row = await cur.fetchone()
    assert (row["sharia_verdict"], row["sharia_source"]) == ("excluded", "user")


# --- jobs --------------------------------------------------------------------


async def test_upsert_job_inserts_as_new(db):
    company_id = await upsert_company(db, "Acme")
    job_id, is_new = await upsert_job(db, make_job(), company_id, filter_reason=None)
    assert isinstance(job_id, int)
    assert is_new is True


async def test_second_sighting_is_not_new_and_reuses_the_row(db):
    company_id = await upsert_company(db, "Acme")
    first_id, first_new = await upsert_job(db, make_job(), company_id, filter_reason=None)
    second_id, second_new = await upsert_job(db, make_job(), company_id, filter_reason=None)
    assert (second_id, second_new) == (first_id, False)
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 1


async def test_second_sighting_advances_last_seen_but_not_first_seen(db):
    company_id = await upsert_company(db, "Acme")
    job_id, _ = await upsert_job(db, make_job(), company_id, filter_reason=None)
    await db.execute(
        "UPDATE jobs SET first_seen_at = %s, last_seen_at = %s WHERE job_id = %s",
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC), job_id),
    )
    await upsert_job(db, make_job(), company_id, filter_reason=None)

    cur = await db.execute(
        "SELECT first_seen_at, last_seen_at FROM jobs WHERE job_id = %s", (job_id,)
    )
    row = await cur.fetchone()
    assert row["first_seen_at"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert row["last_seen_at"] > row["first_seen_at"]


async def test_a_repost_under_a_new_source_id_collapses_onto_the_same_row(db):
    """Spec section 5: reposts must not appear as new jobs.

    The employer relists the identical role with a fresh ATS id. Content-derived
    fingerprinting is what makes that one row instead of two.
    """
    company_id = await upsert_company(db, "Acme")
    first_id, _ = await upsert_job(db, make_job(source_job_id="1"), company_id, filter_reason=None)
    second_id, is_new = await upsert_job(
        db, make_job(source_job_id="99"), company_id, filter_reason=None
    )
    assert second_id == first_id
    assert is_new is False

    cur = await db.execute("SELECT count(*) AS n, max(source_job_id) AS sid FROM jobs")
    row = await cur.fetchone()
    assert row["n"] == 1
    assert row["sid"] == "99", "the row should track the live posting id"


async def test_mutable_fields_refresh_on_a_second_sighting(db):
    # A repost frequently carries an updated salary band.
    company_id = await upsert_company(db, "Acme")
    await upsert_job(
        db, make_job(salary_min=150_000, salary_max=200_000), company_id, filter_reason=None
    )
    await upsert_job(
        db,
        make_job(
            source_job_id="2", salary_min=170_000, salary_max=220_000, salary_source="structured"
        ),
        company_id,
        filter_reason=None,
    )
    cur = await db.execute("SELECT salary_min, salary_max, salary_source FROM jobs")
    row = await cur.fetchone()
    assert (row["salary_min"], row["salary_max"]) == (170_000, 220_000)
    assert row["salary_source"] == "structured"


async def test_filter_result_is_written_atomically(db):
    company_id = await upsert_company(db, "Acme")
    await upsert_job(db, make_job(), company_id, filter_reason="title_not_target")
    cur = await db.execute("SELECT filtered_out, filter_reason FROM jobs")
    row = await cur.fetchone()
    assert row["filtered_out"] is True
    assert row["filter_reason"] == "title_not_target"


async def test_a_job_can_stop_being_filtered(db):
    # Widening the pre-filter must un-filter previously rejected rows rather
    # than leaving them invisible forever.
    company_id = await upsert_company(db, "Acme")
    await upsert_job(db, make_job(), company_id, filter_reason="title_not_target")
    await upsert_job(db, make_job(), company_id, filter_reason=None)
    cur = await db.execute("SELECT filtered_out, filter_reason FROM jobs")
    row = await cur.fetchone()
    assert row["filtered_out"] is False
    assert row["filter_reason"] is None


async def test_distinct_jobs_stay_distinct(db):
    company_id = await upsert_company(db, "Acme")
    await upsert_job(
        db, make_job(fingerprint="a" * 64, source_job_id="1"), company_id, filter_reason=None
    )
    await upsert_job(
        db, make_job(fingerprint="b" * 64, source_job_id="2"), company_id, filter_reason=None
    )
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 2


async def test_two_unique_constraints_can_collide_without_raising(db):
    """`jobs` has two unique keys and ON CONFLICT can only target one.

    Set up the awkward case: job A holds source_job_id 1, job B holds
    fingerprint Y. Now A's content changes to match B, so the incoming row
    conflicts with B on fingerprint *and* with A on (source, source_job_id).
    Naively this raises a UniqueViolation mid-run and loses the whole batch.
    """
    company_id = await upsert_company(db, "Acme")
    a_id, _ = await upsert_job(
        db, make_job(fingerprint="a" * 64, source_job_id="1"), company_id, filter_reason=None
    )
    b_id, _ = await upsert_job(
        db, make_job(fingerprint="b" * 64, source_job_id="2"), company_id, filter_reason=None
    )

    job_id, is_new = await upsert_job(
        db, make_job(fingerprint="b" * 64, source_job_id="1"), company_id, filter_reason=None
    )
    # Identical content is the same job, so it resolves onto B.
    assert job_id == b_id
    assert is_new is False
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 2


async def test_upsert_survives_inside_an_outer_transaction(db):
    # The collision fallback must use a savepoint: a plain UniqueViolation
    # inside a transaction poisons it and every later statement fails.
    company_id = await upsert_company(db, "Acme")
    await upsert_job(
        db, make_job(fingerprint="a" * 64, source_job_id="1"), company_id, filter_reason=None
    )
    await upsert_job(
        db, make_job(fingerprint="b" * 64, source_job_id="2"), company_id, filter_reason=None
    )

    async with db.transaction():
        await upsert_job(
            db, make_job(fingerprint="b" * 64, source_job_id="1"), company_id, filter_reason=None
        )
        # The connection must still be usable after the swallowed conflict.
        cur = await db.execute("SELECT count(*) AS n FROM jobs")
        assert (await cur.fetchone())["n"] == 2


# --- idempotency: the Vercel duplicate-cron requirement ----------------------


async def test_running_the_same_batch_twice_converges(db):
    """Spec section 4.2: Vercel delivers cron duplicates, so every write must
    reconcile rather than accumulate."""
    jobs = [make_job(fingerprint=f"{i:064d}", source_job_id=str(i)) for i in range(25)]

    async def run():
        company_id = await upsert_company(db, "Acme")
        return [await upsert_job(db, j, company_id, filter_reason=None) for j in jobs]

    first = await run()
    second = await run()

    assert all(is_new for _, is_new in first)
    assert not any(is_new for _, is_new in second)
    assert [i for i, _ in first] == [i for i, _ in second]

    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 25
    cur = await db.execute("SELECT count(*) AS n FROM companies")
    assert (await cur.fetchone())["n"] == 1


# --- run_log -----------------------------------------------------------------


async def test_start_run_returns_an_id_and_records_a_start(db):
    run_id = await start_run(db)
    cur = await db.execute(
        "SELECT started_at, finished_at FROM run_log WHERE run_id = %s", (run_id,)
    )
    row = await cur.fetchone()
    assert row["started_at"] is not None
    assert row["finished_at"] is None


async def test_finish_run_records_the_totals(db):
    run_id = await start_run(db)
    await finish_run(
        db,
        run_id,
        jobs_seen=100,
        jobs_new=12,
        jobs_filtered=80,
        errors=1,
        duration_ms=4321,
        budget_hit=True,
        notes="hit the wall-clock budget",
    )
    cur = await db.execute("SELECT * FROM run_log WHERE run_id = %s", (run_id,))
    row = await cur.fetchone()
    assert row["finished_at"] is not None
    assert (row["jobs_seen"], row["jobs_new"], row["jobs_filtered"]) == (100, 12, 80)
    assert row["errors"] == 1
    assert row["duration_ms"] == 4321
    assert row["budget_hit"] is True
    assert row["notes"] == "hit the wall-clock budget"


async def test_finish_run_defaults_budget_hit_to_false(db):
    run_id = await start_run(db)
    await finish_run(db, run_id, jobs_seen=1, jobs_new=1, jobs_filtered=0, errors=0)
    cur = await db.execute("SELECT budget_hit FROM run_log WHERE run_id = %s", (run_id,))
    assert (await cur.fetchone())["budget_hit"] is False


async def test_runs_are_independent(db):
    a = await start_run(db)
    b = await start_run(db)
    assert a != b
    await finish_run(db, a, jobs_seen=1, jobs_new=1, jobs_filtered=0, errors=0)
    cur = await db.execute("SELECT finished_at FROM run_log WHERE run_id = %s", (b,))
    assert (await cur.fetchone())["finished_at"] is None


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=1)])
async def test_last_seen_is_timezone_aware(db, delta):
    company_id = await upsert_company(db, "Acme")
    job_id, _ = await upsert_job(db, make_job(), company_id, filter_reason=None)
    cur = await db.execute("SELECT last_seen_at FROM jobs WHERE job_id = %s", (job_id,))
    assert (await cur.fetchone())["last_seen_at"].tzinfo is not None


async def test_store_works_on_a_connection_without_dict_rows(migrated_db):
    """The CLI and the pooled app build connections differently.

    Reading rows by name off a tuple-returning connection raises `TypeError:
    tuple indices must be integers`, and only on the path whose connection was
    built the other way — which is precisely the path the test fixtures miss.
    Caught by running the CLI, not by the suite.
    """
    import psycopg

    conn = await psycopg.AsyncConnection.connect(migrated_db, autocommit=True)
    try:
        await conn.execute(
            "TRUNCATE companies, jobs, run_log, source_state RESTART IDENTITY CASCADE"
        )
        company_id = await upsert_company(conn, "Acme")
        job_id, is_new = await upsert_job(conn, make_job(), company_id, filter_reason=None)
        run_id = await start_run(conn)
        await finish_run(conn, run_id, jobs_seen=1, jobs_new=1, jobs_filtered=0, errors=0)
        assert isinstance(company_id, int) and isinstance(job_id, int) and is_new is True
        assert isinstance(run_id, int)
    finally:
        await conn.close()
