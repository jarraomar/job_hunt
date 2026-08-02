import pytest

from pipeline.config import load_settings
from pipeline.judge import VERDICTS, Relevance, build_static_prefix, judge_job
from pipeline.models import Job
from pipeline.profile import Profile

SETTINGS = load_settings(env={"DATABASE_URL": "postgresql://x/y", "ANTHROPIC_API_KEY": "sk-test"})

PROFILE = Profile(
    resume={
        "summary": "Full-stack engineer with three years building production systems.",
        "skills": {"languages": ["Python", "TypeScript"], "cloud": ["AWS", "Docker"]},
        "experience": [
            {"company": "CloudBase", "title": "Software Engineer", "bullets": ["Shipped X."]}
        ],
        "education": [{"school": "Brown", "degree": "BS Computer Engineering"}],
    },
    competency_bullets=[{"label": "Full-Stack Development", "text": "Built things."}],
    identity={},
)


def make_job(**overrides) -> Job:
    base = dict(
        fingerprint="f" * 64,
        source="greenhouse",
        source_job_id="1",
        company_name="Acme",
        normalized_company="acme",
        title="Senior Software Engineer",
        location="Remote",
        remote_type="remote",
        salary_min=None,
        salary_max=None,
        salary_source="none",
        description="Build full-stack features in Python and React.",
        apply_url="https://example.com/1",
        posted_at=None,
    )
    base.update(overrides)
    return Job(**base)


class _Usage:
    input_tokens = 1000
    output_tokens = 100
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


def test_verdict_vocabulary_is_closed():
    assert VERDICTS == frozenset({"strong", "possible", "weak"})


def test_static_prefix_contains_the_resume_and_no_job_content():
    """The prefix must be byte-identical across calls or nothing caches.

    Any per-job text here silently invalidates the cache on every request —
    the exact failure spec section 11 warns about.

    Deliberately does NOT assert the absence of generic titles like "Senior
    Software Engineer": the worked examples in the prefix contain those by
    design. The invariant is that the prefix does not vary with the job under
    test, which the determinism and unique-title tests below cover.
    """
    prefix = build_static_prefix(PROFILE)
    assert "Full-stack engineer" in prefix
    assert "Brown" in prefix
    assert "Acme" not in prefix


def test_static_prefix_does_not_vary_with_the_job_being_judged():
    # The property that actually matters for caching.
    assert build_static_prefix(PROFILE) == build_static_prefix(PROFILE)


def test_the_prompt_template_clears_the_cache_minimum():
    """Below 4096 tokens on Haiku 4.5, cache_control is accepted and ignored.

    Guards the template rather than the assembled prefix: the résumé is user
    data of unknown length, so including it would make this pass or fail for
    reasons an editor does not control. Trimming the rubric or the worked
    examples is what would break caching, and that is what this catches.
    """
    from pipeline.judge import _EXAMPLES, _INSTRUCTIONS, MIN_TEMPLATE_CHARS

    assert len(_INSTRUCTIONS) + len(_EXAMPLES) >= MIN_TEMPLATE_CHARS


def test_the_rubric_defines_every_verdict():
    # A verdict the rubric does not describe gets applied by vibes.
    from pipeline.judge import _EXAMPLES

    for verdict in VERDICTS:
        assert f"**{verdict}" in _EXAMPLES


def test_the_worked_examples_cover_the_whole_verdict_range():
    """Without weak examples Haiku calls almost everything strong.

    Everything reaching this gate is already a software engineering role, so
    the model reads the question as "is this plausible?" unless shown otherwise.
    """
    from pipeline.judge import _EXAMPLES

    for verdict in VERDICTS:
        assert f"verdict: {verdict}" in _EXAMPLES


def test_static_prefix_is_deterministic():
    assert build_static_prefix(PROFILE) == build_static_prefix(PROFILE)


async def test_judge_returns_a_parsed_verdict(db, monkeypatch):
    async def fake_call(conn, **kwargs):
        return Relevance(verdict="strong", score=0.9, rationale="Direct stack match."), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    result = await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert result.verdict == "strong"
    assert result.rationale == "Direct stack match."


async def test_an_out_of_vocabulary_verdict_is_coerced_not_stored_raw(db, monkeypatch):
    # A free-text verdict would break every downstream filter that groups by it.
    async def fake_call(conn, **kwargs):
        return Relevance(verdict="AMAZING FIT!!", score=0.9, rationale="..."), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    result = await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert result.verdict in VERDICTS


async def test_a_score_outside_zero_to_one_is_clamped(db, monkeypatch):
    async def fake_call(conn, **kwargs):
        return Relevance(verdict="strong", score=4.2, rationale="..."), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    result = await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert 0.0 <= result.score <= 1.0


async def test_the_spend_cap_stops_judging_without_raising(db, monkeypatch):
    """A reached cap is a normal end state, not an error.

    Raising here would abort the whole run and lose the jobs already scored.
    """
    from pipeline.llm import SpendCapExceeded

    async def fake_call(conn, **kwargs):
        raise SpendCapExceeded("cap")

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    assert await judge_job(db, make_job(), PROFILE, SETTINGS) is None


async def test_an_api_failure_returns_none_rather_than_a_fake_verdict(db, monkeypatch):
    async def fake_call(conn, **kwargs):
        raise RuntimeError("500")

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    assert await judge_job(db, make_job(), PROFILE, SETTINGS) is None


async def test_the_static_prefix_is_marked_for_caching(db, monkeypatch):
    seen = {}

    async def fake_call(conn, **kwargs):
        seen.update(kwargs)
        return Relevance(verdict="strong", score=0.9, rationale="..."), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert any("cache_control" in block for block in seen["system"])


async def test_effort_and_thinking_are_never_sent_to_haiku(db, monkeypatch):
    """Haiku 4.5 errors on output_config, and thinking is off for classification."""
    seen = {}

    async def fake_call(conn, **kwargs):
        seen.update(kwargs)
        return Relevance(verdict="strong", score=0.9, rationale="..."), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    await judge_job(db, make_job(), PROFILE, SETTINGS)
    assert "output_config" not in seen
    assert "thinking" not in seen
    assert "effort" not in seen


async def test_the_job_content_reaches_the_user_turn_not_the_prefix(db, monkeypatch):
    seen = {}

    async def fake_call(conn, **kwargs):
        seen.update(kwargs)
        return Relevance(verdict="strong", score=0.9, rationale="..."), _Usage()

    monkeypatch.setattr("pipeline.judge.call_structured", fake_call)
    await judge_job(db, make_job(title="Unique Title Xyz"), PROFILE, SETTINGS)
    assert "Unique Title Xyz" in seen["user"]
    assert all("Unique Title Xyz" not in block["text"] for block in seen["system"])


@pytest.mark.parametrize("model_attr", ["MODEL"])
def test_the_model_id_has_no_date_suffix(model_attr):
    from pipeline import judge

    assert getattr(judge, model_attr) == "claude-haiku-4-5"


def test_reasoning_is_generated_before_the_verdict():
    """Same ordering hazard that mis-classified GitLab in the Sharia screen.

    Structured output follows declaration order; a verdict declared first is
    chosen before any reasoning exists to support it.
    """
    fields = list(Relevance.model_fields)
    assert fields.index("rationale") < fields.index("verdict")
    assert fields.index("verdict") < fields.index("score")


def test_the_prompt_asks_for_the_rationale_first():
    from pipeline.judge import _INSTRUCTIONS

    assert "rationale FIRST" in _INSTRUCTIONS
