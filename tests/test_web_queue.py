import pytest


async def body_of(client, url="/") -> str:
    response = await client.get(url)
    assert response.status_code == 200
    return response.text


async def test_queue_renders(web_db, client, seed_job):
    await seed_job()
    assert "Backend Engineer" in await body_of(client)


async def test_jobs_are_ordered_by_score_descending(web_db, client, seed_job):
    await seed_job(title="Low Match", score=0.2)
    await seed_job(title="High Match", score=0.9)
    body = await body_of(client)
    assert body.index("High Match") < body.index("Low Match")


async def test_the_rationale_is_shown_on_the_card(web_db, client, seed_job):
    """The rationale is what makes the queue reviewable; the number is not."""
    await seed_job()
    assert "Matches your Python and AWS work." in await body_of(client)


async def test_salary_is_formatted_not_raw(web_db, client, seed_job):
    await seed_job()
    body = await body_of(client)
    assert "$150k–$180k" in body
    # Scoped to the cards. The filter bar's "pays at least" options carry raw
    # figures in their value attributes, which is what a form value is for --
    # what must never leak is a raw number rendered as text a human reads.
    cards = body.split('<div class="space-y-3">', 1)[1]
    assert "150000" not in cards


async def test_excluded_companies_are_absent(web_db, client, seed_job):
    await seed_job(title="Casino Job", company="Betco", sharia="excluded")
    assert "Casino Job" not in await body_of(client)


async def test_flagged_companies_appear_with_a_badge(web_db, client, seed_job):
    """An excluded verdict is a decision; a flagged one is a question for Jarra.

    Collapsing the two either hides jobs that need a human or shows ones that
    do not.
    """
    await seed_job(title="Fintech Job", company="Ambig", sharia="flagged")
    body = await body_of(client)
    assert "Fintech Job" in body
    assert "flagged" in body.lower()


async def test_filtered_out_jobs_never_appear(web_db, client, seed_job):
    job_id = await seed_job(title="Sales Role")
    await web_db.execute(
        "UPDATE jobs SET filtered_out = true, filter_reason = 'title_not_target' WHERE job_id = %s",
        (job_id,),
    )
    assert "Sales Role" not in await body_of(client)


@pytest.mark.parametrize("status", ["applied", "dismissed", "rejected"])
async def test_jobs_already_acted_on_leave_the_queue(web_db, client, seed_job, status):
    # The queue is what is left to review, not an archive.
    await seed_job(title="Already Handled", status=status)
    assert "Already Handled" not in await body_of(client)


async def test_an_unjudged_job_still_renders(web_db, client, seed_job):
    """Only the top N get judged. The rest must still be visible, not blank."""
    await seed_job(title="Unjudged Role", judged=False)
    assert "Unjudged Role" in await body_of(client)


async def test_an_empty_queue_says_so_rather_than_rendering_nothing(web_db, client, seed_job):
    body = (await body_of(client)).lower()
    assert "nothing" in body or "no jobs" in body


async def test_a_missing_posted_at_does_not_break_the_card(web_db, client, seed_job):
    job_id = await seed_job(title="No Date")
    await web_db.execute("UPDATE jobs SET posted_at = NULL WHERE job_id = %s", (job_id,))
    assert "unknown" in await body_of(client)


async def test_one_company_cannot_dominate_the_queue(web_db, client, seed_job):
    """One employer supplied 23% of live postings and 17 of the top 25 by score.

    Without a cap the queue reads as though nobody else is hiring.
    """
    for i in range(8):
        await seed_job(title=f"BigCo Role {i}", company="BigCo", score=0.9 - i * 0.01)
    await seed_job(title="SmallCo Role", company="SmallCo", score=0.5)

    body = await body_of(client, "/?per_company=3")
    assert body.count("BigCo Role") <= 3
    assert "SmallCo Role" in body


# --- sorting and filtering ---------------------------------------------------


async def test_sorting_by_pay_beats_the_score_ordering(web_db, client, db, seed_job):
    low = await seed_job(title="Well Matched Cheap", score=0.9)
    high = await seed_job(title="Poorly Matched Rich", score=0.1)
    await db.execute("UPDATE jobs SET salary_max = 120000 WHERE job_id = %s", (low,))
    await db.execute("UPDATE jobs SET salary_max = 300000 WHERE job_id = %s", (high,))

    body = await body_of(client, "/?sort=salary")
    assert body.index("Poorly Matched Rich") < body.index("Well Matched Cheap")


async def test_sorting_ascending_reverses_it(web_db, client, db, seed_job):
    low = await seed_job(title="Cheap Role", score=0.9)
    high = await seed_job(title="Rich Role", score=0.1)
    await db.execute("UPDATE jobs SET salary_max = 120000 WHERE job_id = %s", (low,))
    await db.execute("UPDATE jobs SET salary_max = 300000 WHERE job_id = %s", (high,))

    body = await body_of(client, "/?sort=salary&dir=asc")
    assert body.index("Cheap Role") < body.index("Rich Role")


async def test_sorting_by_company_is_alphabetical(web_db, client, seed_job):
    await seed_job(title="Zebra Role", company="Zylo")
    await seed_job(title="Apple Role", company="Acme")
    body = await body_of(client, "/?sort=company&dir=asc")
    assert body.index("Apple Role") < body.index("Zebra Role")


async def test_an_unknown_sort_key_falls_back_instead_of_failing(web_db, client, seed_job):
    """Query strings are hand-edited and bookmarks go stale.

    A junk sort key must render the default page; anything else turns a typo
    into a 500 and an ORDER BY built from user text into an injection.
    """
    await seed_job()
    body = await body_of(client, "/?sort=total_score;DROP+TABLE+jobs")
    assert "Backend Engineer" in body


async def test_a_sort_key_cannot_inject_sql(web_db, client, db, seed_job):
    await seed_job()
    await body_of(client, "/?sort=score,(SELECT+1)")
    cur = await db.execute("SELECT count(*) AS n FROM jobs")
    assert (await cur.fetchone())["n"] == 1


async def test_filtering_by_arrangement(web_db, client, db, seed_job):
    onsite = await seed_job(title="Onsite Role")
    await seed_job(title="Remote Role")
    await db.execute("UPDATE jobs SET remote_type = 'onsite' WHERE job_id = %s", (onsite,))

    body = await body_of(client, "/?remote=remote")
    assert "Remote Role" in body
    assert "Onsite Role" not in body


async def test_several_arrangements_can_be_selected_at_once(web_db, client, db, seed_job):
    onsite = await seed_job(title="Onsite Role")
    hybrid = await seed_job(title="Hybrid Role")
    await seed_job(title="Remote Role")
    await db.execute("UPDATE jobs SET remote_type = 'onsite' WHERE job_id = %s", (onsite,))
    await db.execute("UPDATE jobs SET remote_type = 'hybrid' WHERE job_id = %s", (hybrid,))

    body = await body_of(client, "/?remote=remote&remote=hybrid")
    assert "Remote Role" in body
    assert "Hybrid Role" in body
    assert "Onsite Role" not in body


async def test_filtering_by_company(web_db, client, seed_job):
    await seed_job(title="Wanted Role", company="Stripe")
    await seed_job(title="Other Role", company="Databricks")
    body = await body_of(client, "/?company=strip")
    assert "Wanted Role" in body
    assert "Other Role" not in body


async def test_filtering_by_location_class(web_db, client, db, seed_job):
    local = await seed_job(title="Local Role")
    await seed_job(title="Faraway Role")
    await db.execute("UPDATE jobs SET location_class = 'local' WHERE job_id = %s", (local,))
    body = await body_of(client, "/?location=local")
    assert "Local Role" in body
    assert "Faraway Role" not in body


async def test_a_pay_floor_keeps_jobs_that_publish_no_salary(web_db, client, db, seed_job):
    """Two thirds of postings publish no figure.

    Dropping them would turn "pays at least $175k" into "published a number",
    which is a different and far smaller question.
    """
    silent = await seed_job(title="Undisclosed Role")
    await db.execute(
        "UPDATE jobs SET salary_min = NULL, salary_max = NULL, salary_source = 'none'"
        " WHERE job_id = %s",
        (silent,),
    )
    assert "Undisclosed Role" in await body_of(client, "/?min_salary=175000")


async def test_a_pay_floor_still_drops_jobs_that_publish_a_lower_one(web_db, client, seed_job):
    await seed_job(title="Underpaid Role")  # 150k-180k from the fixture
    assert "Underpaid Role" not in await body_of(client, "/?min_salary=200000")


async def test_a_recency_filter_keeps_jobs_with_no_posted_date(web_db, client, db, seed_job):
    # HN and parts of Lever never publish one. They are not therefore old.
    undated = await seed_job(title="Undated Role")
    await db.execute("UPDATE jobs SET posted_at = NULL WHERE job_id = %s", (undated,))
    assert "Undated Role" in await body_of(client, "/?posted_within=1")


async def test_a_recency_filter_drops_an_old_posting(web_db, client, db, seed_job):
    old = await seed_job(title="Stale Role")
    await db.execute(
        "UPDATE jobs SET posted_at = now() - interval '40 days' WHERE job_id = %s", (old,)
    )
    assert "Stale Role" not in await body_of(client, "/?posted_within=7")


async def test_judged_only_hides_jobs_with_no_rationale(web_db, client, seed_job):
    await seed_job(title="Judged Role", judged=True)
    await seed_job(title="Unjudged Role", judged=False)
    body = await body_of(client, "/?judged_only=true")
    assert "Judged Role" in body
    assert "Unjudged Role" not in body


async def test_the_per_company_cap_applies_after_filtering(web_db, client, db, seed_job):
    """Ranking within a company before filtering wastes its slots.

    With a cap of 1, an onsite job scoring highest would take Acme's only slot
    and then be filtered out, leaving the remote job invisible even though the
    filter was meant to find it.
    """
    onsite = await seed_job(title="Onsite Winner", company="Acme", score=0.99)
    await seed_job(title="Remote Runner Up", company="Acme", score=0.5)
    await db.execute("UPDATE jobs SET remote_type = 'onsite' WHERE job_id = %s", (onsite,))

    assert "Remote Runner Up" in await body_of(client, "/?remote=remote&per_company=1")


async def test_filters_survive_the_next_page_link(web_db, client, seed_job):
    """A "next" link that dropped the filters would silently widen the results
    halfway through reading them."""
    for n in range(55):
        await seed_job(title=f"Role {n}", company=f"Co {n}")
    body = await body_of(client, "/?remote=remote&sort=company&dir=asc")
    assert "remote=remote" in body
    assert "sort=company" in body
    assert "offset=50" in body


async def test_paging_does_not_repeat_a_job_across_pages(web_db, client, seed_job):
    """Every ordering ends in job_id for this reason.

    Without a tiebreaker, rows sharing a sort key have no defined order between
    queries and LIMIT/OFFSET quietly repeats some while skipping others.
    """
    for n in range(60):
        await seed_job(title=f"Role {n:02d}", company=f"Co {n}", score=0.5)

    first = await body_of(client, "/?sort=score")
    second = await body_of(client, "/?sort=score&offset=50")
    on_first = {n for n in range(60) if f"Role {n:02d}<" in first}
    on_second = {n for n in range(60) if f"Role {n:02d}<" in second}
    assert not (on_first & on_second)
    assert len(on_first | on_second) == 60


async def test_an_empty_filtered_queue_says_the_filters_did_it(web_db, client, seed_job):
    await seed_job(title="Backend Engineer")
    body = await body_of(client, "/?company=nonexistent")
    assert "Nothing matches these filters" in body


async def test_an_empty_unfiltered_queue_says_something_else(web_db, client):
    assert "Nothing to review" in await body_of(client)


# --- query strings are hand-edited and forms submit blanks --------------------


async def test_choosing_any_on_every_dropdown_still_renders(web_db, client, seed_job):
    """The filter form's "any" options carry value="".

    Typed as `int | None`, FastAPI answered those with a 422, so pressing Apply
    without narrowing anything returned a JSON validation error instead of the
    queue. This is the exact URL the form submits.
    """
    await seed_job()
    body = await body_of(
        client,
        "/?sort=score&dir=desc&company=&min_salary=&posted_within="
        "&min_score=0&per_company=3&remote=remote",
    )
    assert "Backend Engineer" in body


@pytest.mark.parametrize(
    "query",
    [
        "min_salary=abc",
        "posted_within=",
        "min_score=lots",
        "per_company=",
        "per_company=0",
        "offset=-40",
        "offset=banana",
    ],
)
async def test_a_junk_numeric_parameter_falls_back_instead_of_erroring(
    web_db, client, seed_job, query
):
    await seed_job()
    assert "Backend Engineer" in await body_of(client, f"/?{query}")
