"""Personal profile: the resume, the competency-bullet pool, and identity.

Loading follows the rule Phase 1 established for targets.yaml, for the same
reason and with higher stakes. `PROFILE_JSON` wins, and **disk is never read
when VERCEL is set** -- profile/ ships with a Vercel build (excludeFiles governs
the function bundle; the source tree uploads separately), and this directory
holds a home address, phone number, work-authorization answers and a salary
floor.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import yaml

from pipeline.config import Settings

log = logging.getLogger(__name__)


class ProfileUnavailableError(Exception):
    """No usable profile. Never fall back to an empty one."""


@dataclass(frozen=True)
class Profile:
    resume: dict[str, Any]
    competency_bullets: list[dict[str, str]]
    identity: dict[str, Any]


def load_profile(settings: Settings) -> Profile:
    if settings.profile_json:
        try:
            data = json.loads(settings.profile_json)
        except json.JSONDecodeError as exc:
            raise ProfileUnavailableError(f"PROFILE_JSON is not valid JSON: {exc}") from exc
        return _from_dict(data)

    if os.environ.get("VERCEL"):
        raise ProfileUnavailableError(
            "running on Vercel with no PROFILE_JSON; refusing to read the profile "
            "directory so personal data cannot leak via the build directory"
        )

    return _from_disk(settings)


def _from_dict(data: dict[str, Any]) -> Profile:
    resume = data.get("resume") or {}
    if not resume:
        raise ProfileUnavailableError("profile has no resume")
    return Profile(
        resume=resume,
        competency_bullets=list(data.get("competency_bullets") or []),
        identity=dict(data.get("identity") or {}),
    )


def _from_disk(settings: Settings) -> Profile:
    resume_path = settings.profile_dir / "resume.json"
    if not resume_path.exists():
        raise ProfileUnavailableError(
            f"no PROFILE_JSON and no {resume_path}. Copy profile.example/ into "
            f"{settings.profile_dir} and fill it in."
        )
    resume = json.loads(resume_path.read_text())

    bullets_path = settings.profile_dir / "competency_bullets.yaml"
    bullets = yaml.safe_load(bullets_path.read_text()) if bullets_path.exists() else []

    identity_path = settings.profile_dir / "identity.yaml"
    identity = yaml.safe_load(identity_path.read_text()) if identity_path.exists() else {}

    return Profile(resume=resume, competency_bullets=list(bullets or []), identity=identity or {})


def resume_text(profile: Profile) -> str:
    """Flatten the resume into the string that gets embedded.

    Deterministic by construction: dict iteration order is insertion order in
    Python 3.7+, and nothing here iterates a set. The embedding is compared
    against this exact string, so a reordering would change every score.
    """
    resume = profile.resume
    parts: list[str] = []

    if summary := resume.get("summary"):
        parts.append(str(summary))

    for group, items in (resume.get("skills") or {}).items():
        parts.append(f"{group}: {', '.join(items)}")

    for role in resume.get("experience") or []:
        header = " ".join(str(role.get(k, "")) for k in ("title", "company")).strip()
        parts.append(header)
        parts.extend(str(b) for b in role.get("bullets") or [])

    for school in resume.get("education") or []:
        parts.append(" ".join(str(school.get(k, "")) for k in ("degree", "school")).strip())

    return "\n".join(p for p in parts if p)
