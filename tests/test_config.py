import dataclasses

import pytest

from pipeline.config import load_settings

DSN = "postgresql://x/y"


def test_defaults_match_spec():
    s = load_settings(env={"DATABASE_URL": DSN})
    assert s.salary_floor == 125_000
    assert s.home_city == "San Leandro"
    assert s.home_state == "CA"
    assert "jobhunt" in s.user_agent.lower()


def test_database_url_is_required():
    # No localhost fallback: a misconfigured deploy must fail loudly rather than
    # quietly write to a database nobody reads.
    with pytest.raises(ValueError, match="DATABASE_URL"):
        load_settings(env={})


def test_env_overrides_defaults():
    s = load_settings(
        env={
            "DATABASE_URL": DSN,
            "JOBHUNT_SALARY_FLOOR": "140000",
            "JOBHUNT_RUN_BUDGET_SECONDS": "60",
        }
    )
    assert s.salary_floor == 140_000
    assert s.run_budget_seconds == 60.0


def test_run_budget_leaves_headroom_below_the_invocation_ceiling():
    # Vercel Pro hard-kills at 800s. The budget must leave room to write run_log
    # and return a response — a killed invocation records nothing at all.
    s = load_settings(env={"DATABASE_URL": DSN})
    assert s.run_budget_seconds <= 700


def test_user_agent_identifies_a_contact():
    # Politeness (spec section 7): an operator who wants us to stop must be able
    # to reach a human instead of silently blocking the IP.
    s = load_settings(env={"DATABASE_URL": DSN})
    assert "@" in s.user_agent


def test_settings_is_frozen():
    s = load_settings(env={"DATABASE_URL": DSN})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.salary_floor = 1  # type: ignore[misc]
