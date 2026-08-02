from pipeline.store import funnel_counts, weekly_conversion


async def test_tracker_renders_with_no_data(web_db, client):
    response = await client.get("/tracker")
    assert response.status_code == 200
    # A zero-state that renders nothing looks like a crash.
    assert "0" in response.text


async def test_the_page_shows_every_funnel_stage(web_db, client):
    body = (await client.get("/tracker")).text.lower()
    for stage in ("queued", "applied", "responded", "interview", "rejected"):
        assert stage in body


async def test_funnel_counts_each_status(web_db, seed_job):
    for status in ("applied", "responded", "interview", "rejected"):
        job_id = await seed_job(title=f"Job {status}")
        await web_db.execute(
            "INSERT INTO applications (job_id, status, applied_at) VALUES (%s, %s, now())",
            (job_id, status),
        )
    counts = await funnel_counts(web_db)
    assert counts["applied"] == 1
    assert counts["interview"] == 1


async def test_dismissed_jobs_are_not_in_the_funnel(web_db, seed_job):
    """Dismissing is not a funnel stage — counting it would deflate every rate."""
    job_id = await seed_job(title="Dismissed")
    await web_db.execute(
        "INSERT INTO applications (job_id, status) VALUES (%s, 'dismissed')", (job_id,)
    )
    assert "dismissed" not in await funnel_counts(web_db)


async def test_conversion_buckets_by_application_date_not_response_date(web_db, seed_job):
    """A response arrives 1-3 weeks after the application.

    Bucketing by response date attributes it to a week whose application volume
    has nothing to do with it, which makes the rate meaningless exactly when it
    matters.
    """
    job_id = await seed_job(title="Slow Response")
    await web_db.execute(
        "INSERT INTO applications (job_id, status, applied_at, responded_at)"
        " VALUES (%s, 'responded', now() - interval '21 days', now())",
        (job_id,),
    )
    weeks = await weekly_conversion(web_db, weeks=8)
    with_applications = [w for w in weeks if w["applied"] == 1]
    assert len(with_applications) == 1
    assert with_applications[0]["responded"] == 1


async def test_a_response_rate_is_never_a_division_error(web_db, seed_job):
    job_id = await seed_job()
    await web_db.execute(
        "INSERT INTO applications (job_id, status, applied_at) VALUES (%s, 'applied', now())",
        (job_id,),
    )
    for week in await weekly_conversion(web_db, weeks=8):
        assert week["response_rate"] is None or 0.0 <= week["response_rate"] <= 1.0


async def test_an_applied_job_appears_in_the_rendered_table(web_db, client, seed_job):
    job_id = await seed_job()
    await web_db.execute(
        "INSERT INTO applications (job_id, status, applied_at) VALUES (%s, 'applied', now())",
        (job_id,),
    )
    body = (await client.get("/tracker")).text
    assert "Weekly conversion" in body
    assert "No applications sent yet" not in body
