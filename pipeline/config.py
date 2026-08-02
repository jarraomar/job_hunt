"""Runtime settings, loaded from the environment with spec-derived defaults."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# Deliberately OUTSIDE the repository, for the same reason the dev Postgres
# cluster lives outside it (scripts/dev_db.sh).
#
# Everything under the project directory is uploaded to Vercel on deploy. Both
# documented exclusion mechanisms were measured NOT to prevent it: vercel.json's
# `excludeFiles` is ignored for this bundling path (though `maxDuration` from
# the same block is honoured), and a `.vercelignore` left the upload manifest
# unchanged at 1,398 files. Phase 0 confirmed the consequence in production --
# a deployed run read board tokens that existed only in the local profile
# directory.
#
# This directory holds a home address, a phone number, work-authorization
# answers and a salary floor. Keeping it out of the project tree is the only
# mechanism that actually works; the VERCEL guard in pipeline/profile.py is
# defence in depth behind it, not the primary control.
_DEFAULT_PROFILE_DIR = Path.home() / ".local" / "share" / "jobhunt" / "profile"

# Identifies us and offers a way to ask us to stop. An operator who can reach a
# human is less likely to reach for an IP block (spec section 7).
USER_AGENT = "jobhunt/0.1 (personal job search; contact: developer@cloudbaseservices.com)"

# MUST stay below `maxDuration` in vercel.json. The two are coupled: Vercel
# hard-kills at maxDuration and records nothing for a killed invocation, so a
# budget above the ceiling can never fire and every over-long run becomes
# invisible rather than merely incomplete.
#
#   Hobby      maxDuration 300 (cap)      -> budget 240
#   Pro        maxDuration 800            -> budget 600
#
# Currently on Pro. Raise or lower both together, never one alone; a test
# asserts this constant still matches vercel.json.
INVOCATION_CEILING_SECONDS = 800.0
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
    profile_json: str | None
    anthropic_api_key: str | None
    daily_llm_cap_usd: Decimal
    daily_judge_limit: int
    judge_per_company_cap: int


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
        profile_dir=Path(e.get("JOBHUNT_PROFILE_DIR", _DEFAULT_PROFILE_DIR)),
        salary_floor=int(e.get("JOBHUNT_SALARY_FLOOR", "125000")),
        home_city=e.get("JOBHUNT_HOME_CITY", "San Leandro"),
        home_state=e.get("JOBHUNT_HOME_STATE", "CA"),
        user_agent=e.get("JOBHUNT_USER_AGENT", USER_AGENT),
        run_budget_seconds=float(e.get("JOBHUNT_RUN_BUDGET_SECONDS", DEFAULT_RUN_BUDGET_SECONDS)),
        # The whole profile as one JSON blob. profile/ is gitignored and refused
        # on Vercel, so in a deployment its contents arrive this way instead —
        # the same pattern JOBHUNT_TARGETS_JSON already uses.
        profile_json=e.get("PROFILE_JSON") or None,
        # Optional, unlike DATABASE_URL. Discovery is the majority of the
        # system's work and needs no model access at all, so a missing key must
        # not stop a crawl. pipeline/llm.py raises at the point of use instead,
        # where the message can say which call wanted it.
        anthropic_api_key=e.get("ANTHROPIC_API_KEY") or None,
        daily_llm_cap_usd=Decimal(e.get("DAILY_LLM_CAP_USD", "1.50")),
        daily_judge_limit=int(e.get("JOBHUNT_DAILY_JUDGE_LIMIT", "50")),
        # One company supplies 23% of live postings and 17 of the top 25 by
        # score. Without a cap the judge budget is spent re-reading near-
        # duplicate roles at a single employer.
        judge_per_company_cap=int(e.get("JOBHUNT_JUDGE_PER_COMPANY_CAP", "3")),
    )
