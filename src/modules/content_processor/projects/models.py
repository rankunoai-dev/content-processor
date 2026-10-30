"""Contracts for projects, their websites, URL inventories and scraping runs.

A project holds one client website and any number of competitor websites; each
website has its own URL list, runs, reports and knowledge space
(docs/app/ARCHITECTURE_PROJECTS_AND_SITES.md).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "RUN_ACTIVE",
    "KnowledgeStatus",
    "ProjectRecord",
    "ProjectUrl",
    "RunState",
    "RunStatus",
    "SiteRecord",
    "SiteRole",
]

RunStatus = Literal["queued", "running", "finished", "failed", "cancelled", "interrupted"]
RUN_ACTIVE: frozenset[str] = frozenset({"queued", "running"})
SiteRole = Literal["client", "competitor"]
KnowledgeStatus = Literal["", "building", "ready", "failed", "skipped"]


class SiteRecord(StrictModel):
    """One website of a project: the client's or a competitor's."""

    site_id: str
    name: str = Field(min_length=1, max_length=120)
    role: SiteRole
    domains: list[str] = Field(
        default_factory=list, description="e.g. flynns.com; subdomains count as the same site"
    )
    created_at: datetime


class ProjectRecord(StrictModel):
    """A project as stored. `password_hash` never leaves the server."""

    project_id: str
    name: str = Field(min_length=1, max_length=120)
    owner_name: str = Field(min_length=1, max_length=120)
    client_name: str = Field(min_length=1, max_length=120)
    password_hash: str
    created_at: datetime
    updated_at: datetime
    sites: list[SiteRecord] = Field(default_factory=list)


class ProjectUrl(StrictModel):
    """One URL of a website's inventory."""

    url: str
    added_at: datetime


class RunState(StrictModel):
    """Progress and outcome of one scraping run of one website."""

    run_id: str
    project_id: str
    site_id: str = ""
    status: RunStatus = "queued"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    started_by: str
    url_count: int = Field(ge=0)
    batch_size: int = Field(ge=1)
    batches_total: int = Field(ge=0)
    batches_done: int = Field(default=0, ge=0)
    current_batch: int | None = None
    current_attempt: int | None = None
    pages_done: int = Field(default=0, ge=0)
    results: dict[str, int] = Field(default_factory=dict, description="badge -> pages so far")
    message: str = ""
    reports_ready: bool = False
    knowledge_status: KnowledgeStatus = ""
    knowledge_message: str = ""
