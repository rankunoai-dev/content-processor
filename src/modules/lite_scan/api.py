"""HTTP routes of the competitor lite scan.

GET  /api/projects/{project}/sites/{site}/lite   the latest lite scan (sections, sample)
POST /api/projects/{project}/sites/{site}/lite   read the sitemap, choose the sample and
                                                 queue its scrape (unlock token)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.webapi.projects_api import TokenHeader
from src.modules.lite_scan.models import LiteInventory, LitePick, LiteSection
from src.modules.lite_scan.service import lite_store, start_lite_scan

__all__ = ["LiteSummary", "register_lite_routes"]


class LiteIn(StrictModel):
    """Pages a person wants in the sample whatever else is chosen."""

    pinned: list[str] = Field(default_factory=list, max_length=200)


class LiteSummary(StrictModel):
    """A lite scan as the app shows it (the inventory as a count, not every address)."""

    site_id: str
    website: str
    created_at: datetime
    run_id: str
    inventory: int
    sampled: int
    max_pages: int
    basis: str
    sitemap_files: int
    sitemap_note: str
    sections: list[LiteSection]
    sample: list[LitePick]
    run: RunState | None = None


def _summary(found: LiteInventory, run: RunState | None) -> LiteSummary:
    return LiteSummary(
        site_id=found.site_id,
        website=found.website,
        created_at=found.created_at,
        run_id=found.run_id,
        inventory=found.pages,
        sampled=len(found.sample),
        max_pages=found.max_pages,
        basis=found.basis(),
        sitemap_files=len(found.sitemap_files),
        sitemap_note=found.sitemap_note,
        sections=found.sections[:200],
        sample=found.sample,
        run=run,
    )


def register_lite_routes(
    app: FastAPI,
    *,
    projects: ProjectStore,
    runner: ScrapeRunner,
    signer: TokenSigner,
    sitemap_http: Callable[[], httpx.Client] | None = None,
) -> None:
    """Add the lite-scan routes (before the app's catch-all routes)."""
    store = lite_store(projects)

    def site(project_id: str, site_id: str) -> None:
        try:
            projects.site(project_id, site_id)
        except (ProjectNotFoundError, KeyError):
            raise HTTPException(status_code=404, detail="There is no such website.") from None

    def run_of(project_id: str, site_id: str, run_id: str) -> RunState | None:
        return next((r for r in projects.runs(project_id, site_id) if r.run_id == run_id), None)

    @app.get("/api/projects/{project_id}/sites/{site_id}/lite")
    def latest(project_id: str, site_id: str) -> LiteSummary:
        site(project_id, site_id)
        found = store.latest(project_id, site_id)
        if found is None:
            raise HTTPException(status_code=404, detail="No lite scan of this website yet.")
        return _summary(found, run_of(project_id, site_id, found.run_id))

    @app.post("/api/projects/{project_id}/sites/{site_id}/lite")
    def start(
        project_id: str, site_id: str, body: LiteIn | None = None, token: TokenHeader = None
    ) -> LiteSummary:
        site(project_id, site_id)
        if not signer.check(token, project_id):
            raise HTTPException(
                status_code=401, detail="Unlock this project with its password to scan."
            )
        record = projects.get(project_id)
        try:
            found, state = start_lite_scan(
                projects,
                runner,
                project_id,
                site_id,
                started_by=record.owner_name,
                pinned=(body or LiteIn()).pinned,
                http=sitemap_http,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return _summary(found, state)
