"""Record a live API response as a test fixture.

Fixtures are captured, never hand-written: a hand-written fixture encodes a
guess about the API's shape, and the tests then verify the guess rather than
reality. When a captured fixture disagrees with an adapter, the fixture wins.

Goes through PoliteSession like everything else, so capturing fixtures obeys the
same rate limits as the crawl (spec section 7).

Usage:
    python scripts/capture_fixture.py greenhouse_board \\
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true"

    # Keep only the first N items under a key, so tests stay fast:
    python scripts/capture_fixture.py greenhouse_board "<url>" --trim jobs=5
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pipeline.config import USER_AGENT
from pipeline.http import PoliteSession

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def _trim(payload: Any, spec: str | None) -> Any:
    """Apply "key=n" (or bare "n" for a top-level list) to shrink the payload."""
    if not spec:
        return payload
    if "=" in spec:
        key, count = spec.split("=", 1)
        if isinstance(payload, dict) and isinstance(payload.get(key), list):
            payload[key] = payload[key][: int(count)]
        return payload
    if isinstance(payload, list):
        return payload[: int(spec)]
    return payload


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="fixture filename, without .json")
    parser.add_argument("url")
    parser.add_argument("--trim", help='e.g. "jobs=5" or "5" for a top-level list')
    args = parser.parse_args()

    async with PoliteSession(USER_AGENT) as session:
        payload = await session.get_json(args.url)

    if payload is None:
        print("no body returned (304?) — nothing captured")
        return 1

    payload = _trim(payload, args.trim)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{args.name}.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
