import json

import pytest

from pipeline.config import load_settings
from pipeline.profile import (
    ProfileUnavailableError,
    load_profile,
    resume_text,
)

MINIMAL = {
    "resume": {
        "summary": "Engineer.",
        "skills": {"languages": ["Python"]},
        "experience": [{"company": "Acme", "title": "SWE", "bullets": ["Shipped X."]}],
        "education": [],
    },
    "competency_bullets": [{"label": "Full-Stack Development", "text": "Built things."}],
    "identity": {"email": "someone@example.com"},
}


def settings(**env):
    return load_settings(env={"DATABASE_URL": "postgresql://x/y", **env})


def test_loads_from_env_var():
    profile = load_profile(settings(PROFILE_JSON=json.dumps(MINIMAL)))
    assert profile.resume["summary"] == "Engineer."
    assert profile.competency_bullets[0]["label"] == "Full-Stack Development"


def test_env_var_wins_over_disk(tmp_path):
    (tmp_path / "resume.json").write_text(json.dumps({"summary": "from disk"}))
    profile = load_profile(
        settings(PROFILE_JSON=json.dumps(MINIMAL), JOBHUNT_PROFILE_DIR=str(tmp_path))
    )
    assert profile.resume["summary"] == "Engineer."


def test_disk_is_never_read_on_vercel(tmp_path, monkeypatch):
    """profile/ ships with a Vercel build and excludeFiles does not stop it.

    Confirmed in production during Phase 0. This directory holds a home
    address, phone number, work-authorization answers and a salary floor, so
    the deployed code must refuse to read it even when it is present.
    """
    (tmp_path / "resume.json").write_text(json.dumps({"summary": "secret"}))
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(ProfileUnavailableError, match="PROFILE_JSON"):
        load_profile(settings(JOBHUNT_PROFILE_DIR=str(tmp_path)))


def test_reads_from_disk_locally(tmp_path, monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    (tmp_path / "resume.json").write_text(json.dumps(MINIMAL["resume"]))
    (tmp_path / "competency_bullets.yaml").write_text(
        "- label: Full-Stack Development\n  text: Built things.\n"
    )
    profile = load_profile(settings(JOBHUNT_PROFILE_DIR=str(tmp_path)))
    assert profile.resume["summary"] == "Engineer."
    assert profile.competency_bullets[0]["label"] == "Full-Stack Development"


def test_missing_profile_raises_rather_than_defaulting(tmp_path, monkeypatch):
    # A silent empty profile would embed the empty string and score every job
    # identically — a failure that looks like "scoring is broken" much later.
    monkeypatch.delenv("VERCEL", raising=False)
    with pytest.raises(ProfileUnavailableError):
        load_profile(settings(JOBHUNT_PROFILE_DIR=str(tmp_path)))


def test_malformed_profile_json_raises():
    with pytest.raises(ProfileUnavailableError, match="not valid JSON"):
        load_profile(settings(PROFILE_JSON="{not json"))


def test_resume_text_includes_summary_skills_and_bullets():
    profile = load_profile(settings(PROFILE_JSON=json.dumps(MINIMAL)))
    text = resume_text(profile)
    assert "Engineer." in text
    assert "Python" in text
    assert "Shipped X." in text


def test_resume_text_is_deterministic():
    # The embedding is cached against this string; a set-ordering wobble would
    # silently invalidate it on every process start.
    profile = load_profile(settings(PROFILE_JSON=json.dumps(MINIMAL)))
    assert resume_text(profile) == resume_text(profile)


def test_the_default_profile_directory_is_outside_the_repository():
    """Everything under the project directory is uploaded to Vercel on deploy.

    Both documented exclusion mechanisms were measured not to prevent it:
    vercel.json's excludeFiles is ignored for this bundling path, and a
    .vercelignore left the upload manifest unchanged. Phase 0 confirmed the
    consequence in production. Relocation is the only control that works, so
    the default must never point back inside the repo.
    """
    from pipeline.config import _ROOT, load_settings

    profile_dir = load_settings(env={"DATABASE_URL": "postgresql://x/y"}).profile_dir
    assert _ROOT not in profile_dir.parents
    assert profile_dir != _ROOT
