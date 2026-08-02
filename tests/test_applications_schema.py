import psycopg
import pytest


async def _job(db, fingerprint="fp") -> int:
    await db.execute(
        "INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')"
        " ON CONFLICT DO NOTHING"
    )
    cur = await db.execute(
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at)"
        " VALUES (%s, 1, 'greenhouse', '1', 'Engineer', 'd', 'https://x', now(), now())"
        " RETURNING job_id",
        (fingerprint,),
    )
    return (await cur.fetchone())["job_id"]


async def test_tables_exist(db):
    cur = await db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    names = {r["tablename"] for r in await cur.fetchall()}
    assert {"applications", "answer_bank", "unmapped_questions"} <= names


async def test_a_new_application_defaults_to_queued(db):
    job_id = await _job(db)
    await db.execute("INSERT INTO applications (job_id) VALUES (%s)", (job_id,))
    cur = await db.execute("SELECT status, applied_at FROM applications")
    row = await cur.fetchone()
    assert row["status"] == "queued"
    assert row["applied_at"] is None


async def test_an_unknown_status_is_rejected(db):
    """A typo reaching the database breaks every funnel query silently."""
    job_id = await _job(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(
            "INSERT INTO applications (job_id, status, applied_at) VALUES (%s, 'aplied', now())",
            (job_id,),
        )


async def test_applied_without_a_timestamp_is_rejected(db):
    # Otherwise the conversion stats count an application that has no date.
    job_id = await _job(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(
            "INSERT INTO applications (job_id, status) VALUES (%s, 'applied')", (job_id,)
        )


async def test_dismissed_needs_no_timestamp(db):
    # Dismissing a job is not applying to it.
    job_id = await _job(db)
    await db.execute(
        "INSERT INTO applications (job_id, status) VALUES (%s, 'dismissed')", (job_id,)
    )


async def test_status_survives_the_job_being_re_upserted(db):
    """The reason this table exists.

    Discovery upserts jobs on fingerprint every pass. A status column on `jobs`
    would be erased by the next repost of the same posting.
    """
    job_id = await _job(db)
    await db.execute(
        "INSERT INTO applications (job_id, status, applied_at) VALUES (%s, 'applied', now())",
        (job_id,),
    )
    await db.execute(
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at)"
        " VALUES ('fp', 1, 'greenhouse', '1', 'Engineer (Reposted)', 'd2', 'https://x',"
        " now(), now())"
        " ON CONFLICT (fingerprint) DO UPDATE SET title = EXCLUDED.title,"
        " description = EXCLUDED.description, last_seen_at = now()"
    )
    cur = await db.execute("SELECT status FROM applications WHERE job_id = %s", (job_id,))
    assert (await cur.fetchone())["status"] == "applied"


async def test_deleting_a_job_removes_its_application(db):
    job_id = await _job(db)
    await db.execute("INSERT INTO applications (job_id) VALUES (%s)", (job_id,))
    await db.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
    cur = await db.execute("SELECT count(*) AS n FROM applications")
    assert (await cur.fetchone())["n"] == 0


async def test_answer_bank_questions_are_unique(db):
    await db.execute(
        "INSERT INTO answer_bank (question, answer) VALUES ('Work authorization?', 'US citizen')"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        await db.execute(
            "INSERT INTO answer_bank (question, answer) VALUES ('Work authorization?', 'different')"
        )
