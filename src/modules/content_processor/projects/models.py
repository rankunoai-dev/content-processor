"""Contracts for projects, their URL inventory and their scraping runs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "RUN_ACTIVE",
    "ProjectRecord",
    "ProjectUrl",
    "RunState",
    "RunStatus",
]

RunStatus = Literal["queued", "running", "finished", "failed", "cancelled", "interrupted"]
RUN_ACTIVE: frozenset[str] = frozenset({"queued", "running"})


class ProjectRecord(StrictModel):
    """A project as stored. `password_hash` never leaves the server."""

    project_id: str
    name: str = Field(min_length=1, max_length=120)
    owner_name: str = Field(min_length=1, max_length=120)
    client_name: str = Field(min_length=1, max_length=120)
    password_hash: str
    created_at: datetime
    updated_at: datetime


class ProjectUrl(StrictModel):
    """One URL of the project's inventory."""

    url: str
    added_at: datetime


class RunState(StrictModel):
    """Progress and outcome of one scraping run of a project."""

    run_id: str
    project_id: str
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
