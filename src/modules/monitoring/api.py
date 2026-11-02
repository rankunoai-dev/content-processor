"""HTTP routes of the Monitoring module.

GET  /api/projects/{project}/monitoring            the latest check (404 when none)
POST /api/projects/{project}/monitoring            run a check now (project unlock token)
GET  /api/projects/{project}/monitoring/workbook   the latest check's workbook
GET  /api/projects/{project}/monitoring/watch      how the project is watched
POST /api/projects/{project}/monitoring/watch      change it (project unlock token)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.monitoring.models import MonitorCheck, WatchSettings
from src.modules.monitoring.service import monitor_store, run_project_check

__all__ = ["WatchIn", "register_monitoring_routes"]

TokenHeader = Annotated[str | None, Header(alias="X-Project-Token")]


class WatchIn(StrictModel):
    """The watch settings a person may change."""

    auto_check: bool
    rescrape_every_days: int = Field(ge=0, le=90)
    push_threshold: int = Field(ge=1, le=100)


def register_monitoring_routes(
    app: FastAPI, *, projects: ProjectStore, access: KnowledgeAccess, signer: TokenSigner
) -> None:
    """Add the monitoring routes (before the app's catch-all routes)."""
    store = monitor_store(projects)

    def project(project_id: str) -> None:
        try:
            projects.get(project_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    def unlocked(project_id: str, token: str | None) -> None:
        project(project_id)
        if not signer.check(token, project_id):
            raise HTTPException(
                status_code=401,
                detail="Unlock this project with its password to change monitoring.",
            )

    @app.get("/api/projects/{project_id}/monitoring")
    def latest(project_id: str) -> MonitorCheck:
        project(project_id)
        check = store.latest(project_id)
        if check is None:
            raise HTTPException(status_code=404, detail="No monitoring check has been run yet.")
        return check

    @app.post("/api/projects/{project_id}/monitoring")
    def run(project_id: str, token: TokenHeader = None) -> MonitorCheck:
        unlocked(project_id, token)
        check, _files = run_project_check(projects, access, project_id)
        return check

    @app.get("/api/projects/{project_id}/monitoring/workbook")
    def workbook(project_id: str) -> FileResponse:
        check = latest(project_id)
        path = store.workbook(check)
        if not path.exists():
            raise HTTPException(status_code=404, detail="The workbook of this check is missing.")
        return FileResponse(
            path,
            filename=f"{project_id}_monitoring.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/api/projects/{project_id}/monitoring/watch")
    def get_watch(project_id: str) -> WatchSettings:
        project(project_id)
        return store.watch(project_id)

    @app.post("/api/projects/{project_id}/monitoring/watch")
    def set_watch(project_id: str, body: WatchIn, token: TokenHeader = None) -> WatchSettings:
        unlocked(project_id, token)
        current = store.watch(project_id)
        return store.set_watch(project_id, current.model_copy(update=body.model_dump()))
