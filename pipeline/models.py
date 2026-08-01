"""Canonical job shapes. RawJob is what sources emit; Job is what we store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RawJob:
    """A posting as a source reported it, before any interpretation.

    Adapters do the minimum: map the source's field names onto these, and pass
    salary through only when the source states it structurally. Everything
    derived lives in normalize.py so it is derived one way for every source.
    """

    source: str
    source_job_id: str
    company_name: str
    title: str
    location: str | None
    description: str
    apply_url: str
    posted_at: datetime | None
    # Lever and Ashby both publish a structured work arrangement. It beats any
    # text inference, and it is a string rather than a bool on purpose: Ashby
    # reports isRemote=True alongside workplaceType="Hybrid" for the same job,
    # so a boolean would systematically over-report remote.
    remote_type_hint: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_source: str = "none"


@dataclass(frozen=True)
class Job:
    """A normalized posting, ready to persist."""

    fingerprint: str
    source: str
    source_job_id: str
    company_name: str
    normalized_company: str
    title: str
    location: str | None
    remote_type: str
    salary_min: int | None
    salary_max: int | None
    salary_source: str
    description: str
    apply_url: str
    posted_at: datetime | None
