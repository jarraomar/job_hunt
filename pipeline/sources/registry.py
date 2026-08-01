"""Name to source mapping, and the target-employer list loader."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import yaml

from pipeline.sources.ashby import AshbySource
from pipeline.sources.base import Source
from pipeline.sources.greenhouse import GreenhouseSource
from pipeline.sources.hn_algolia import HNAlgoliaSource
from pipeline.sources.lever import LeverSource
from pipeline.sources.remotive import RemotiveSource

SOURCES: dict[str, Source] = {
    GreenhouseSource.name: GreenhouseSource(),
    LeverSource.name: LeverSource(),
    AshbySource.name: AshbySource(),
    RemotiveSource.name: RemotiveSource(),
    HNAlgoliaSource.name: HNAlgoliaSource(),
}

# Aggregators cover the whole market rather than a company list, so they need no
# targets.yaml entry. Kept explicit so a missing section never reads as a
# misconfiguration.
TOKENLESS_SOURCES = frozenset({RemotiveSource.name, HNAlgoliaSource.name})


log = logging.getLogger(__name__)


def load_targets(path: Path) -> dict[str, list[str]]:
    """Read the per-ATS board token lists.

    `JOBHUNT_TARGETS_JSON` wins over the file, and **on Vercel the file is not
    read at all**.

    That asymmetry is deliberate. `profile/` is gitignored, but `vercel build`
    reads the working directory rather than git, and `excludeFiles` only governs
    the function bundle -- the source tree is uploaded separately. Verified in
    production: a deployed run crawled the boards listed in a local
    `profile/targets.yaml`, which means the directory shipped.

    Today that directory holds public board tokens. From Phase 2 it holds a home
    address, a phone number, work-authorization answers and a salary floor.
    Refusing to read it when VERCEL is set means personal data cannot reach a
    deployment by accident even if it is sitting in the build directory.
    """
    raw = os.environ.get("JOBHUNT_TARGETS_JSON")
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.error("JOBHUNT_TARGETS_JSON is not valid JSON; treating as empty")
            return {}
        return {k: list(v or []) for k, v in data.items()}

    if os.environ.get("VERCEL"):
        log.warning(
            "running on Vercel with no JOBHUNT_TARGETS_JSON; refusing to read %s "
            "so personal data cannot leak via the build directory",
            path,
        )
        return {}

    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {k: list(v or []) for k, v in data.items()}
