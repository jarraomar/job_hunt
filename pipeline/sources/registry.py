"""Name to source mapping, and the target-employer list loader."""

from __future__ import annotations

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


def load_targets(path: Path) -> dict[str, list[str]]:
    """Read the per-ATS board token lists. A missing file is not an error."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {k: list(v or []) for k, v in data.items()}
