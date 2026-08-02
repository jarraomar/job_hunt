from decimal import Decimal

import psycopg
import pytest


async def _job(db) -> int:
    await db.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    cur = await db.execute(
        "INSERT INTO jobs (fingerprint, company_id, source, source_job_id, title,"
        " description, apply_url, first_seen_at, last_seen_at)"
        " VALUES ('fp', 1, 'greenhouse', '1', 'Engineer', 'd', 'https://x', now(), now())"
        " RETURNING job_id"
    )
    return (await cur.fetchone())["job_id"]


async def test_tables_exist(db):
    cur = await db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    names = {r["tablename"] for r in await cur.fetchall()}
    assert {"scores", "llm_spend"} <= names


async def test_scores_row_requires_only_a_total(db):
    job_id = await _job(db)
    await db.execute("INSERT INTO scores (job_id, total_score) VALUES (%s, 0.5)", (job_id,))
    cur = await db.execute("SELECT total_score, judged_at FROM scores")
    row = await cur.fetchone()
    assert row["total_score"] == 0.5
    assert row["judged_at"] is None


async def test_verdict_without_model_is_rejected(db):
    """A verdict we cannot attribute to a model is unusable for re-billing."""
    job_id = await _job(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(
            "INSERT INTO scores (job_id, total_score, relevance_verdict, model)"
            " VALUES (%s, 0.5, 'strong', NULL)",
            (job_id,),
        )


async def test_model_without_verdict_is_rejected(db):
    # A paid-for call whose result was dropped.
    job_id = await _job(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(
            "INSERT INTO scores (job_id, total_score, relevance_verdict, model)"
            " VALUES (%s, 0.5, NULL, 'claude-haiku-4-5')",
            (job_id,),
        )


async def test_scores_cascade_when_a_job_is_deleted(db):
    job_id = await _job(db)
    await db.execute("INSERT INTO scores (job_id, total_score) VALUES (%s, 0.5)", (job_id,))
    await db.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
    cur = await db.execute("SELECT count(*) AS n FROM scores")
    assert (await cur.fetchone())["n"] == 0


async def test_llm_spend_uses_exact_numeric_for_money(db):
    await db.execute(
        "INSERT INTO llm_spend (model, purpose, input_tokens, output_tokens, cost_usd)"
        " VALUES ('claude-haiku-4-5', 'judge', 1000, 200, 0.001234)"
    )
    cur = await db.execute("SELECT cost_usd FROM llm_spend")
    # NUMERIC, not float: a daily ceiling compared against accumulated float
    # error is a ceiling that drifts.
    assert (await cur.fetchone())["cost_usd"] == Decimal("0.001234")
