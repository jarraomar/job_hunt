"""Print the local profile as one JSON line, for piping into `vercel env add`.

Never redirect this to a file inside the repository. The profile directory
lives outside the project tree precisely because everything under it is
uploaded to Vercel on deploy -- see the note on _DEFAULT_PROFILE_DIR in
pipeline/config.py.

This is the same pattern JOBHUNT_TARGETS_JSON already uses: the files stay on
the machine, their contents travel as an environment variable.
"""

from __future__ import annotations

import json
import sys

import yaml

from pipeline.config import load_settings


def main() -> int:
    settings = load_settings(env={"DATABASE_URL": "postgresql://placeholder/x"})
    directory = settings.profile_dir

    resume_path = directory / "resume.json"
    if not resume_path.exists():
        print(f"no resume at {resume_path}", file=sys.stderr)
        return 1

    payload: dict[str, object] = {"resume": json.loads(resume_path.read_text())}

    for key, filename in (
        ("competency_bullets", "competency_bullets.yaml"),
        ("identity", "identity.yaml"),
    ):
        path = directory / filename
        if path.exists():
            payload[key] = yaml.safe_load(path.read_text())

    print(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
