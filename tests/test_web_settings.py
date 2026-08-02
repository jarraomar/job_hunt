from pipeline.config import load_settings
from pipeline.filters.sharia import screen_company

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y", "ANTHROPIC_API_KEY": "sk-test"})


async def _company_of(db, job_id: int) -> int:
    cur = await db.execute("SELECT company_id FROM jobs WHERE job_id = %s", (job_id,))
    return (await cur.fetchone())["company_id"]


async def test_settings_renders(web_db, client):
    assert (await client.get("/settings")).status_code == 200


async def test_an_answer_can_be_added(web_db, client):
    response = await client.post(
        "/settings/answer",
        data={
            "question": "Work authorization?",
            "answer": "US-born citizen",
            "category": "eligibility",
        },
    )
    assert response.status_code in (200, 303)
    cur = await web_db.execute(
        "SELECT answer FROM answer_bank WHERE question = %s", ("Work authorization?",)
    )
    assert (await cur.fetchone())["answer"] == "US-born citizen"


async def test_re_posting_a_question_updates_rather_than_erroring(web_db, client):
    # question is UNIQUE; a plain INSERT would 500 on the second edit.
    for answer in ("First", "Second"):
        await client.post("/settings/answer", data={"question": "Q?", "answer": answer})
    cur = await web_db.execute("SELECT answer FROM answer_bank WHERE question = 'Q?'")
    assert (await cur.fetchone())["answer"] == "Second"


async def test_an_empty_answer_is_rejected(web_db, client):
    # An empty stored answer would be pasted into a real application form.
    response = await client.post("/settings/answer", data={"question": "Q?", "answer": "   "})
    assert response.status_code == 400
    cur = await web_db.execute("SELECT count(*) AS n FROM answer_bank")
    assert (await cur.fetchone())["n"] == 0


async def test_answering_a_question_clears_it_from_unmapped(web_db, client):
    await web_db.execute(
        "INSERT INTO unmapped_questions (question, seen_count) VALUES ('Start date?', 4)"
    )
    await client.post("/settings/answer", data={"question": "Start date?", "answer": "Two weeks"})
    cur = await web_db.execute("SELECT count(*) AS n FROM unmapped_questions")
    assert (await cur.fetchone())["n"] == 0


async def test_a_sharia_override_is_marked_as_user_sourced(web_db, client, seed_job):
    """A user ruling is permanent — never re-judged, never re-billed.

    This is how an LLM verdict stays correctable.
    """
    job_id = await seed_job(title="Fintech Job", company="Ambig", sharia="flagged")
    company_id = await _company_of(web_db, job_id)

    await client.post("/settings/sharia", data={"company_id": company_id, "verdict": "allowed"})

    cur = await web_db.execute(
        "SELECT sharia_verdict, sharia_source FROM companies WHERE company_id = %s",
        (company_id,),
    )
    row = await cur.fetchone()
    assert row["sharia_verdict"] == "allowed"
    assert row["sharia_source"] == "user"


async def test_an_override_survives_a_later_screen(web_db, client, seed_job):
    job_id = await seed_job(title="Casino Job", company="Betco", sharia="flagged")
    company_id = await _company_of(web_db, job_id)

    await client.post("/settings/sharia", data={"company_id": company_id, "verdict": "allowed"})

    async def never(conn, name, description, s):
        raise AssertionError("a user verdict must never be re-judged")

    verdict = await screen_company(
        web_db, company_id, "Casino Job", "Online betting.", SETTINGS, judge=never
    )
    assert verdict == "allowed"


async def test_an_unknown_verdict_is_rejected(web_db, client, seed_job):
    job_id = await seed_job()
    company_id = await _company_of(web_db, job_id)
    response = await client.post(
        "/settings/sharia", data={"company_id": company_id, "verdict": "halal-ish"}
    )
    assert response.status_code == 400


async def test_an_overridden_company_stays_listed_for_review(web_db, client, seed_job):
    """A permanent ruling you cannot see is a ruling you cannot change."""
    job_id = await seed_job(title="Fintech Job", company="Ambig", sharia="flagged")
    company_id = await _company_of(web_db, job_id)
    await client.post("/settings/sharia", data={"company_id": company_id, "verdict": "allowed"})
    body = (await client.get("/settings")).text
    # Listed once, in the group its verdict puts it in, marked as yours. A
    # separate "your rulings" section showed every override twice.
    assert body.count("Ambig") == 1
    assert "yours" in body


async def test_the_run_log_shows_duration_and_budget_hit(web_db, client):
    await web_db.execute(
        "INSERT INTO run_log (started_at, finished_at, jobs_seen, duration_ms, budget_hit)"
        " VALUES (now(), now(), 2614, 41000, true)"
    )
    body = (await client.get("/settings")).text
    assert "2614" in body
    assert "budget" in body.lower()


async def test_unmapped_questions_are_listed(web_db, client):
    await web_db.execute(
        "INSERT INTO unmapped_questions (question, seen_count) VALUES ('Desired start date?', 4)"
    )
    assert "Desired start date?" in (await client.get("/settings")).text


async def test_todays_spend_is_shown(web_db, client):
    await web_db.execute(
        "INSERT INTO llm_spend (model, purpose, input_tokens, output_tokens, cost_usd)"
        " VALUES ('claude-haiku-4-5', 'judge', 1000, 200, 0.0263)"
    )
    body = (await client.get("/settings")).text
    assert "0.0263" in body


async def test_settings_mutations_reject_get(web_db, client):
    response = await client.get("/settings/answer?question=Q&answer=A")
    assert response.status_code == 405


# --- visibility into what the screens did ------------------------------------


async def test_the_intake_breakdown_names_every_rejection_reason(web_db, client, db, seed_job):
    """Every gate rejects silently. This section is the only place that says so.

    Printing the raw reason codes would explain nothing to the person deciding
    whether a filter is too aggressive, so each is given a plain-English label.
    """
    await seed_job(title="Kept Role")
    await db.execute(
        "UPDATE jobs SET filtered_out = true, filter_reason = 'location_outside_area'"
        " WHERE title = %s",
        ("Kept Role",),
    )
    body = (await client.get("/settings")).text
    assert "Office out of range" in body
    assert "location_outside_area" not in body.split("What the filters did")[1][:2000]


async def test_the_location_grid_shows_the_pair_not_the_class_alone(web_db, client, db, seed_job):
    """`us` passes remote and is rejected hybrid.

    A flat count per class cannot show that, so the report is a grid.
    """
    job_id = await seed_job(title="Faraway Hybrid")
    await db.execute(
        "UPDATE jobs SET location_class = 'us', remote_type = 'hybrid',"
        " filtered_out = true, filter_reason = 'location_outside_area' WHERE job_id = %s",
        (job_id,),
    )
    body = (await client.get("/settings")).text
    assert "US, outside the Bay" in body
    assert "Where the jobs are" in body


async def test_an_excluded_company_reports_how_many_roles_it_hides(web_db, client, seed_job):
    """Excluding a company with 400 open roles and one with two are the same
    row in the table and very different decisions."""
    await seed_job(title="Casino Job", company="Betco", sharia="excluded")
    body = (await client.get("/settings")).text
    assert "Betco" in body
    assert "open roles" in body


async def test_allowed_companies_are_listed_too(web_db, client, seed_job):
    """A wrong `allow` is as invisible as a wrong exclusion, and only one of
    the two was previously shown anywhere."""
    await seed_job(title="Normal Job", company="Acme", sharia="allowed")
    body = (await client.get("/settings")).text
    assert "Allowed (1)" in body
    assert "Acme" in body


async def test_the_location_grid_counts_only_its_own_rejections(web_db, client, db, seed_job):
    """A job thrown out by the title screen is not the geography screen's doing.

    Counting `filtered_out` reported 673 of 822 local hybrid roles as rejected,
    which read as the geography rule discarding Bay Area jobs.
    """
    from pipeline.store import location_breakdown

    job_id = await seed_job(title="Wrong Title Role")
    await db.execute(
        "UPDATE jobs SET location_class = 'local', remote_type = 'hybrid',"
        " filtered_out = true, filter_reason = 'title_not_target' WHERE job_id = %s",
        (job_id,),
    )
    rows = await location_breakdown(db)
    local = next(r for r in rows if r["location_class"] == "local")
    assert local["n"] == 1
    assert local["rejected"] == 0
