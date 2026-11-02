"""HTTP routes of the Gap Analysis module.

GET  /api/projects/{project}/gaps                          the latest report
POST /api/projects/{project}/gaps                          build a new one (unlock token)
POST /api/projects/{project}/gaps/findings/{id}/status     record a decision (unlock token)
GET  /api/projects/{project}/gaps/workbook                 the latest report's workbook
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
from src.modules.gaps.models import FindingStatus, GapReport, StatusEntry
from src.modules.gaps.service import build_project_gaps, gap_store

__all__ = ["StatusIn", "register_gap_routes"]

TokenHeader = Annotated[str | None, Header(alias="X-Project-Token")]


class StatusIn(StrictModel):
    """A decision about a finding."""

    status: FindingStatus
    note: str = Field(default="", max_length=2000)


def register_gap_routes(
    app: FastAPI, *, projects: ProjectStore, access: KnowledgeAccess, signer: TokenSigner
) -> None:
    """Add the gap routes (before the app's catch-all routes)."""
    store = gap_store(projects)

    def project(project_id: str) -> None:
        try:
            projects.get(project_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    def unlocked(project_id: str, token: str | None) -> None:
        project(project_id)
        if not signer.check(token, project_id):
            raise HTTPException(
                status_code=401, detail="Unlock this project with its password to change gaps."
            )

    @app.get("/api/projects/{project_id}/gaps")
    def latest(project_id: str) -> GapReport:
        project(project_id)
        report = store.latest(project_id)
        if report is None:
            raise HTTPException(status_code=404, detail="No gap analysis has been built yet.")
        return report

    @app.post("/api/projects/{project_id}/gaps")
    def build(project_id: str, token: TokenHeader = None) -> GapReport:
        unlocked(project_id, token)
        report, _files = build_project_gaps(projects, access, project_id)
        return report

    @app.post("/api/projects/{project_id}/gaps/findings/{finding_id}/status")
    def set_status(
        project_id: str, finding_id: str, body: StatusIn, token: TokenHeader = None
    ) -> StatusEntry:
        unlocked(project_id, token)
        try:
            return store.set_status(project_id, finding_id, body.status, body.note)
        except KeyError:
            raise HTTPException(status_code=404, detail="There is no such finding.") from None

    @app.get("/api/projects/{project_id}/gaps/workbook")
    def workbook(project_id: str) -> FileResponse:
        report = latest(project_id)
        path = store.workbook(report)
        if not path.exists():
            raise HTTPException(status_code=404, detail="The workbook of this report is missing.")
        return FileResponse(
            path,
            filename=f"{project_id}_gaps.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
