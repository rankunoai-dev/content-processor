"""HTTP routes of the Research module.

GET  /api/research/status                         is Semrush on, caps, units used
GET  /api/projects/{project}/research              the latest snapshot (404 when none)
GET  /api/projects/{project}/research/estimate     worst-case units of a pull
POST /api/projects/{project}/research/pull         pull from Semrush (unlock token)
POST /api/projects/{project}/research/import       import an export (unlock token)
GET  /api/projects/{project}/research/movers       latest against previous snapshot
GET  /api/projects/{project}/research/workbook     the latest snapshot's workbook
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.analyst.playground import Playground
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.research.build import movers
from src.modules.research.importer import ImportError_
from src.modules.research.models import Mover, ResearchSnapshot
from src.modules.research.semrush import SemrushError
from src.modules.research.service import (
    ResearchSettings,
    estimate_units,
    import_csv,
    pull_project,
    research_store,
    unit_log,
)

__all__ = ["ImportIn", "PullIn", "register_research_routes"]

TokenHeader = Annotated[str | None, Header(alias="X-Project-Token")]
_MAX_FILE = 20 * 1024 * 1024


class PullIn(StrictModel):
    """Optional keywords whose demand and top results are pulled too."""

    keywords: list[str] = Field(default_factory=list, max_length=50)


class ImportIn(StrictModel):
    """A Semrush Organic Positions export of one website."""

    website: str = Field(min_length=3, max_length=300)
    file_name: str = Field(max_length=300)
    content_base64: str = Field(max_length=_MAX_FILE * 2)


def register_research_routes(
    app: FastAPI,
    *,
    projects: ProjectStore,
    access: KnowledgeAccess,
    signer: TokenSigner,
    settings: Callable[[], ResearchSettings] = ResearchSettings.from_env,
) -> None:
    """Add the research routes (before the app's catch-all routes)."""
    store = research_store(projects)

    def project(project_id: str) -> None:
        try:
            projects.get(project_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    def unlocked(project_id: str, token: str | None) -> None:
        project(project_id)
        if not signer.check(token, project_id):
            raise HTTPException(
                status_code=401, detail="Unlock this project with its password to add research."
            )

    @app.get("/api/research/status")
    def status() -> dict[str, Any]:
        s = settings()
        month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {
            "enabled": s.enabled and bool(s.api_key),
            "api_key_set": bool(s.api_key),
            "monthly_cap": s.monthly_units,
            "used_this_month": unit_log(projects).used(month),
            "pull_cap": s.pull_units,
            "keyword_limit": s.keyword_limit,
            "database": s.database,
        }

    @app.get("/api/projects/{project_id}/research")
    def latest(project_id: str) -> ResearchSnapshot:
        project(project_id)
        found = store.latest(project_id)
        if found is None:
            raise HTTPException(status_code=404, detail="No research has been added yet.")
        return found

    @app.get("/api/projects/{project_id}/research/estimate")
    def estimate(project_id: str, keywords: int = 0) -> dict[str, int]:
        project(project_id)
        s = settings()
        websites = len(Playground(projects, access, [project_id]).websites())
        return {
            "websites": websites,
            "keyword_limit": s.keyword_limit,
            "worst_case_units": estimate_units(websites, s, max(0, min(keywords, 50))),
            "pull_cap": s.pull_units,
        }

    @app.post("/api/projects/{project_id}/research/pull")
    def pull(project_id: str, body: PullIn, token: TokenHeader = None) -> ResearchSnapshot:
        unlocked(project_id, token)
        try:
            snapshot, _files = pull_project(
                projects, access, project_id, settings(), serp_keywords=body.keywords
            )
        except SemrushError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return snapshot

    @app.post("/api/projects/{project_id}/research/import")
    def import_file(project_id: str, body: ImportIn, token: TokenHeader = None) -> ResearchSnapshot:
        unlocked(project_id, token)
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="The file could not be read.") from None
        if len(data) > _MAX_FILE:
            raise HTTPException(status_code=413, detail="The file is larger than 20 MB.")
        try:
            snapshot, _files = import_csv(
                projects, access, project_id, body.website, data, database=settings().database
            )
        except (ImportError_, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return snapshot

    @app.get("/api/projects/{project_id}/research/movers")
    def moves(project_id: str) -> list[Mover]:
        newest = latest(project_id)
        previous = store.previous(project_id)
        if previous is None:
            raise HTTPException(status_code=404, detail="Add research again later to compare.")
        return movers(previous, newest)

    @app.get("/api/projects/{project_id}/research/workbook")
    def workbook(project_id: str) -> FileResponse:
        snapshot = latest(project_id)
        path = store.workbook(snapshot)
        if not path.exists():
            raise HTTPException(status_code=404, detail="The workbook of this snapshot is missing.")
        return FileResponse(
            path,
            filename=f"{project_id}_research.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
