import dataclasses
import json
from pathlib import Path

import pytest

from pipeline.config import INVOCATION_CEILING_SECONDS, load_settings

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
    """The budget and vercel.json's maxDuration are coupled.

    Vercel hard-kills at maxDuration and records nothing for a killed
    invocation, so a budget at or above the ceiling can never fire — every
    over-long run would vanish instead of reporting budget_hit.
    """
    s = load_settings(env={"DATABASE_URL": DSN})
    assert s.run_budget_seconds < INVOCATION_CEILING_SECONDS
    assert s.run_budget_seconds <= INVOCATION_CEILING_SECONDS * 0.9


def test_the_declared_ceiling_matches_vercel_json():
    """A drift here is silent: the code would plan for headroom it lacks."""
    config = json.loads((Path(__file__).resolve().parents[1] / "vercel.json").read_text())
    declared = config["functions"]["api/index.py"]["maxDuration"]
    assert declared == INVOCATION_CEILING_SECONDS


def test_user_agent_identifies_a_contact():
    # Politeness (spec section 7): an operator who wants us to stop must be able
    # to reach a human instead of silently blocking the IP.
    s = load_settings(env={"DATABASE_URL": DSN})
    assert "@" in s.user_agent


def test_settings_is_frozen():
    s = load_settings(env={"DATABASE_URL": DSN})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.salary_floor = 1  # type: ignore[misc]
