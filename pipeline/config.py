"""Runtime settings, loaded from the environment with spec-derived defaults."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# Identifies us and offers a way to ask us to stop. An operator who can reach a
# human is less likely to reach for an IP block (spec section 7).
USER_AGENT = "jobhunt/0.1 (personal job search; contact: developer@cloudbaseservices.com)"

# Vercel Pro terminates a function at 800s. Stopping at 600 leaves room to
# finish bookkeeping and return a response; a killed invocation records nothing
# at all, so the run would be invisible rather than merely incomplete.
DEFAULT_RUN_BUDGET_SECONDS = 600.0


@dataclass(frozen=True)
class Settings:
    database_url: str
    migrations_dir: Path
    profile_dir: Path
    salary_floor: int
    home_city: str
    home_state: str
    user_agent: str
    run_budget_seconds: float


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env

    database_url = e.get("DATABASE_URL", "")
    if not database_url:
        # Deliberately no localhost fallback. A misconfigured deploy should fail
        # loudly at startup rather than write to a database nobody is reading.
        raise ValueError("DATABASE_URL is required but not set")

    return Settings(
        database_url=database_url,
        migrations_dir=Path(e.get("JOBHUNT_MIGRATIONS_DIR", _ROOT / "migrations")),
        profile_dir=Path(e.get("JOBHUNT_PROFILE_DIR", _ROOT / "profile")),
        salary_floor=int(e.get("JOBHUNT_SALARY_FLOOR", "125000")),
        home_city=e.get("JOBHUNT_HOME_CITY", "San Leandro"),
        home_state=e.get("JOBHUNT_HOME_STATE", "CA"),
        user_agent=e.get("JOBHUNT_USER_AGENT", USER_AGENT),
        run_budget_seconds=float(e.get("JOBHUNT_RUN_BUDGET_SECONDS", DEFAULT_RUN_BUDGET_SECONDS)),
    )
