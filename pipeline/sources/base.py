"""The contract every source implements. Adding a source is one new module."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

from pipeline.config import Settings
from pipeline.http import PoliteSession
from pipeline.models import RawJob


@dataclass
class SourceConfig:
    session: PoliteSession
    targets: dict[str, list[str]]
    settings: Settings
    # Adapters append here when a single board fails. Without it a run in which
    # every board errored still reports errors=0, because each adapter swallows
    # its own failure to keep the rest of the run alive -- so the digest looks
    # healthy while nothing was ingested.
    errors: list[str] = field(default_factory=list)


class Source(Protocol):
    """A source yields RawJob and does no interpretation of its own.

    Yielding rather than returning a list matters: the orchestrator can stop
    mid-source when its wall-clock budget expires, without the source having
    fetched everything first.
    """

    name: str

    # Minimum seconds between fetches of this source. Zero means "every tick".
    # Sources whose operators publish a rate expectation declare it here rather
    # than relying on the orchestrator to remember (see remotive).
    min_interval_seconds: float

    def fetch(self, cfg: SourceConfig) -> AsyncIterator[RawJob]: ...
