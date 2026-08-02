async def test_detail_renders_the_description(web_db, client, seed_job):
    job_id = await seed_job()
    response = await client.get(f"/job/{job_id}")
    assert response.status_code == 200
    assert "Build things with Python." in response.text


async def test_the_rationale_is_shown(web_db, client, seed_job):
    job_id = await seed_job()
    body = (await client.get(f"/job/{job_id}")).text
    assert "Matches your Python and AWS work." in body


async def test_the_apply_link_opens_in_a_new_tab(web_db, client, seed_job):
    """Spec section 3: no automated process ever authenticates as Jarra on a
    job platform. This is a plain link and stays one."""
    job_id = await seed_job()
    body = (await client.get(f"/job/{job_id}")).text
    assert 'href="https://example.com/1"' in body
    assert 'target="_blank"' in body
    assert 'rel="noopener' in body


async def test_a_missing_job_is_a_404(web_db, client, seed_job):
    assert (await client.get("/job/999999")).status_code == 404


async def test_marking_applied_sets_the_timestamp(web_db, client, seed_job):
    job_id = await seed_job()
    response = await client.post(f"/job/{job_id}/status", data={"status": "applied"})
    assert response.status_code == 200
    cur = await web_db.execute(
        "SELECT status, applied_at FROM applications WHERE job_id = %s", (job_id,)
    )
    row = await cur.fetchone()
    assert row["status"] == "applied"
    assert row["applied_at"] is not None


async def test_status_can_be_changed_twice(web_db, client, seed_job):
    # The first POST inserts, the second updates. A plain INSERT would 500.
    job_id = await seed_job()
    await client.post(f"/job/{job_id}/status", data={"status": "applied"})
    await client.post(f"/job/{job_id}/status", data={"status": "responded"})
    cur = await web_db.execute("SELECT status FROM applications WHERE job_id = %s", (job_id,))
    assert (await cur.fetchone())["status"] == "responded"


async def test_applied_at_is_preserved_across_later_transitions(web_db, client, seed_job):
    """Conversion stats measure from the application date. Overwriting it on
    every transition would silently reset every funnel interval."""
    job_id = await seed_job()
    await client.post(f"/job/{job_id}/status", data={"status": "applied"})
    cur = await web_db.execute("SELECT applied_at FROM applications WHERE job_id = %s", (job_id,))
    first = (await cur.fetchone())["applied_at"]

    await client.post(f"/job/{job_id}/status", data={"status": "interview"})
    cur = await web_db.execute("SELECT applied_at FROM applications WHERE job_id = %s", (job_id,))
    assert (await cur.fetchone())["applied_at"] == first


async def test_an_unknown_status_is_rejected_with_400_not_500(web_db, client, seed_job):
    job_id = await seed_job()
    response = await client.post(f"/job/{job_id}/status", data={"status": "wat"})
    assert response.status_code == 400


async def test_status_cannot_be_changed_by_a_get(web_db, client, seed_job):
    """A GET mutation gets fired by prefetchers, history restore, and crawlers."""
    job_id = await seed_job()
    assert (await client.get(f"/job/{job_id}/status?status=applied")).status_code == 405
    cur = await web_db.execute("SELECT count(*) AS n FROM applications")
    assert (await cur.fetchone())["n"] == 0


async def test_the_status_post_returns_a_fragment_not_a_full_page(web_db, client, seed_job):
    # HTMX swaps this into the button row; a full document would nest <html>.
    job_id = await seed_job()
    body = (await client.post(f"/job/{job_id}/status", data={"status": "applied"})).text
    assert "<html" not in body.lower()
    assert "status-buttons" in body


async def test_the_answer_bank_panel_is_rendered(web_db, client, seed_job):
    await web_db.execute(
        "INSERT INTO answer_bank (question, answer)"
        " VALUES ('Work authorization?', 'US-born citizen')"
    )
    job_id = await seed_job()
    body = (await client.get(f"/job/{job_id}")).text
    assert "Work authorization?" in body
    assert "US-born citizen" in body


async def test_a_flagged_company_shows_its_reason_on_the_detail_page(web_db, client, seed_job):
    job_id = await seed_job(title="Fintech Job", company="Ambig", sharia="flagged")
    body = (await client.get(f"/job/{job_id}")).text
    assert "stated reason" in body


async def test_an_excluded_job_is_still_reachable_by_direct_link(web_db, client, seed_job):
    """Excluded jobs leave the queue but must stay viewable.

    Otherwise a wrong exclusion is unreviewable — you cannot see what you were
    not shown.
    """
    job_id = await seed_job(title="Casino Job", company="Betco", sharia="excluded")
    response = await client.get(f"/job/{job_id}")
    assert response.status_code == 200
    assert "excluded" in response.text.lower()


# --- application prep --------------------------------------------------------


async def test_the_profile_answers_most_of_the_form_unprompted(web_db, client, seed_job):
    """The panel is useful on a job you have never touched, or it is not used."""
    job_id = await seed_job()
    body = (await client.get(f"/job/{job_id}")).text
    assert "Application prep" in body
    assert "test@example.com" in body
    assert "US-born citizen" in body


async def test_the_start_date_answer_is_a_real_upcoming_date(web_db, client, seed_job):
    from datetime import date

    from pipeline.apply.answers import start_date

    job_id = await seed_job()
    body = (await client.get(f"/job/{job_id}")).text
    assert start_date(date.today()).strftime("%A, %B %-d, %Y") in body


async def test_the_platform_is_named_and_its_form_described(web_db, client, seed_job):
    # The fixture's apply_url is example.com, so this is the unknown case.
    job_id = await seed_job()
    assert "Unknown platform" in (await client.get(f"/job/{job_id}")).text


async def test_a_greenhouse_link_is_recognised(web_db, client, db, seed_job):
    job_id = await seed_job()
    await db.execute(
        "UPDATE jobs SET apply_url = 'https://boards.greenhouse.io/acme/jobs/1' WHERE job_id = %s",
        (job_id,),
    )
    assert "Greenhouse" in (await client.get(f"/job/{job_id}")).text


async def test_self_identification_is_shown_as_yours_to_answer(web_db, client, seed_job):
    """Not as a gap to fill in. Race, gender, veteran and disability status are
    Jarra's to answer or decline, and the UI must not nag him toward a value."""
    job_id = await seed_job()
    body = (await client.get(f"/job/{job_id}")).text
    assert "yours to answer or decline" in body


async def test_an_answer_outside_the_catalog_is_still_shown(web_db, client, seed_job):
    """A question Jarra invents is accepted by the form, saved, and would
    otherwise never appear on any job page -- the catalog would silently decide
    which of his own answers were worth showing."""
    await web_db.execute(
        "INSERT INTO answer_bank (question, answer)"
        " VALUES ('Favourite database?', 'Postgres, obviously.')"
    )
    job_id = await seed_job()
    body = (await client.get(f"/job/{job_id}")).text
    assert "Favourite database?" in body
    assert "Postgres, obviously." in body


async def test_viewing_a_job_does_not_inflate_the_unanswered_counts(web_db, client, seed_job):
    """The settings to-do list ranks by how often a form asks, not by how often
    Jarra opened a job. Counting page views would sort it by his browsing."""
    job_id = await seed_job()
    for _ in range(3):
        await client.get(f"/job/{job_id}")
    cur = await web_db.execute("SELECT count(*) AS n FROM unmapped_questions")
    assert (await cur.fetchone())["n"] == 0


async def test_the_page_still_renders_without_a_profile(web_db, client, seed_job, monkeypatch):
    """The description, score and apply link do not depend on a profile.

    Failing the whole page because PROFILE_JSON is unset would make a
    misconfigured deploy look like a broken database.
    """
    monkeypatch.delenv("PROFILE_JSON", raising=False)
    job_id = await seed_job()
    response = await client.get(f"/job/{job_id}")
    assert response.status_code == 200
    assert "Backend Engineer" in response.text
    assert "Application prep" not in response.text
