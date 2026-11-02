"""Contracts for projects, their websites, URL inventories and scraping runs.

A project holds the client's website (its main website, plus any other websites or
brands of the same client) and any number of competitor websites; each website has
its own URL list, runs, reports and knowledge space, and a note and tags that say
what it is (docs/app/ARCHITECTURE_PROJECTS_AND_SITES.md). The client's websites are
never competitors of one another.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "RUN_ACTIVE",
    "MAX_NOTE_LENGTH",
    "MAX_SITE_TAGS",
    "MAX_TAG_LENGTH",
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
MAX_SITE_TAGS = 12
MAX_TAG_LENGTH = 40
MAX_NOTE_LENGTH = 2000


class SiteRecord(StrictModel):
    """One website of a project: one of the client's, or a competitor's.

    Every `client` website belongs to the client (its main website is the first one;
    the others are its other websites or brands); they are never compared with one
    another as competitors.
    """

    site_id: str
    name: str = Field(min_length=1, max_length=120)
    role: SiteRole
    domains: list[str] = Field(
        default_factory=list,
        description=(
            "addresses: a domain (flynns.com; subdomains count as the same site) or, when "
            "one domain holds several websites, a subdomain or a section (client.com/brand-a)"
        ),
    )
    created_at: datetime
    note: str = Field(default="", max_length=MAX_NOTE_LENGTH, description="what this website is")
    tags: list[str] = Field(default_factory=list, max_length=MAX_SITE_TAGS)


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

    def main_site(self) -> SiteRecord | None:
        """The client's main website: the first client website."""
        return next((s for s in self.sites if s.role == "client"), None)


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
