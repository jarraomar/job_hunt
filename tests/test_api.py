import pytest
from httpx import ASGITransport, AsyncClient

from api.index import app
from pipeline.db import close_pool

SECRET = "test-cron-secret-value-0123456789"


@pytest.fixture
async def client(db, migrated_db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("CRON_SECRET", SECRET)
    await close_pool()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await close_pool()


async def test_root_serves_the_queue(client):
    """`/` is the queue from Phase 3 onward, not the JSON service stub.

    web.register() is mounted last so the API routes above it keep their paths
    and only "/" changes hands.
    """
    response = await client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "job_hunt" in response.text


async def test_the_api_routes_still_answer_after_the_ui_is_mounted(client):
    # Mount order is load-bearing: a catch-all would shadow these.
    assert (await client.get("/api/health")).status_code in (200, 503)
    assert (await client.get("/api/cron/discover")).status_code == 401


async def test_health_reports_live_job_count(client, db):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["live_jobs"] == 0
    assert body["last_run"]["started_at"] is None


async def test_health_fails_closed_when_the_database_is_unreachable(client, monkeypatch):
    """A health check that does not touch Postgres would pass while
    DATABASE_URL pointed somewhere useless — the exact failure it exists for."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@localhost:1/nope")
    await close_pool()
    response = await client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["ok"] is False


async def test_cron_rejects_a_missing_authorization_header(client):
    assert (await client.get("/api/cron/discover")).status_code == 401


async def test_cron_rejects_a_wrong_secret(client):
    response = await client.get(
        "/api/cron/discover", headers={"Authorization": "Bearer not-the-secret"}
    )
    assert response.status_code == 401


async def test_cron_rejects_a_bare_secret_without_the_bearer_prefix(client):
    # Vercel always sends "Bearer <secret>". Accepting the bare form would
    # widen what counts as valid for no reason.
    response = await client.get("/api/cron/discover", headers={"Authorization": SECRET})
    assert response.status_code == 401


async def test_cron_fails_closed_when_no_secret_is_configured(client, monkeypatch):
    """A missing CRON_SECRET must not mean "no auth required".

    Reading it as absent-therefore-open would leave the only write endpoint in
    the system publicly triggerable.
    """
    monkeypatch.delenv("CRON_SECRET", raising=False)
    response = await client.get("/api/cron/discover", headers={"Authorization": f"Bearer {SECRET}"})
    assert response.status_code == 503


async def test_cron_runs_with_a_valid_secret(client, db, monkeypatch, tmp_path):
    # No targets.yaml: the tokenless aggregators still run, which is what makes
    # a Phase 0 deploy testable before PROFILE_JSON exists.
    monkeypatch.setenv("JOBHUNT_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("JOBHUNT_RUN_BUDGET_SECONDS", "0")

    response = await client.get("/api/cron/discover", headers={"Authorization": f"Bearer {SECRET}"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "jobs_seen",
        "jobs_new",
        "jobs_filtered",
        "errors",
        "sources_skipped",
        "budget_hit",
        "duration_ms",
    }
    # Budget of 0 means it returns immediately without touching the network.
    assert body["budget_hit"] is True
    assert body["jobs_seen"] == 0


async def test_a_partial_run_still_returns_200(client, db, monkeypatch, tmp_path):
    """Vercel logs a non-200 as a failed cron.

    A run that stops on its wall-clock budget is normal and its work is
    durable, so reporting failure would make the expected case look broken.
    """
    monkeypatch.setenv("JOBHUNT_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("JOBHUNT_RUN_BUDGET_SECONDS", "0")
    response = await client.get("/api/cron/discover", headers={"Authorization": f"Bearer {SECRET}"})
    assert response.status_code == 200
    assert response.json()["budget_hit"] is True


async def test_the_run_is_recorded_even_when_it_does_nothing(client, db, monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUNT_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("JOBHUNT_RUN_BUDGET_SECONDS", "0")
    await client.get("/api/cron/discover", headers={"Authorization": f"Bearer {SECRET}"})

    cur = await db.execute("SELECT count(*) AS n FROM run_log WHERE finished_at IS NOT NULL")
    assert (await cur.fetchone())["n"] == 1
