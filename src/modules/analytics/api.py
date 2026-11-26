"""Analytics routes: GA4 import, pull, settings, report and workbook.

GET  /api/analytics/status                          is a GA4 pull possible (no secrets shown)
GET  /api/projects/{project}/analytics              the latest report (built on the fly)
GET  /api/projects/{project}/analytics/settings     the project's GA4 property and website
POST /api/projects/{project}/analytics/settings     set them (unlock token)
POST /api/projects/{project}/analytics/import       import GA4's landing page CSV (unlock token)
POST /api/projects/{project}/analytics/pull         pull two periods from the Data API (unlock)
GET  /api/projects/{project}/analytics/workbook     the workbook
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.analytics.importer import Ga4ImportError
from src.modules.analytics.models import AnalyticsReport, AnalyticsSettings
from src.modules.analytics.service import (
    analytics_store,
    build_analytics_report,
    import_ga4_csv,
    pull_ga4,
    set_property,
)
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

__all__ = ["register_analytics_routes"]

TokenHeader = Annotated[str | None, Header(alias="X-Project-Token")]
_MAX_FILE = 20 * 1024 * 1024


class Ga4ImportIn(StrictModel):
    """A GA4 landing page export (CSV) of the client's website."""

    website: str | None = Field(default=None, max_length=300)
    file_name: str = Field(max_length=300)
    content_base64: str = Field(max_length=_MAX_FILE * 2)


class Ga4SettingsIn(StrictModel):
    """The GA4 property (numeric id) and the website it measures."""

    property_id: str | None = Field(default=None, pattern=r"^\d{4,15}$")
    website: str | None = Field(default=None, max_length=300)


class Ga4ImportOut(StrictModel):
    """What an import or pull kept."""

    snapshots: list[dict[str, Any]]


def register_analytics_routes(
    app: FastAPI, *, projects: ProjectStore, access: KnowledgeAccess, signer: TokenSigner
) -> None:
    """Add the analytics routes (before the app's catch-all routes)."""
    store = analytics_store(projects)

    def project(project_id: str) -> None:
        try:
            projects.get(project_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    def unlocked(project_id: str, token: str | None) -> None:
        project(project_id)
        if not signer.check(token, project_id):
            raise HTTPException(
                status_code=401, detail="Unlock this project with its password to add analytics."
            )

    def summary(snapshots: list[Any]) -> Ga4ImportOut:
        return Ga4ImportOut(
            snapshots=[
                {
                    "snapshot_id": s.snapshot_id,
                    "website": s.website,
                    "source": s.source,
                    "start_date": s.start_date,
                    "end_date": s.end_date,
                    "pages": len(s.rows),
                    "sessions": s.sessions,
                    "notes": s.notes,
                }
                for s in snapshots
            ]
        )

    @app.get("/api/analytics/status")
    def status() -> dict[str, Any]:
        from src.modules.analytics.ga4 import Ga4Settings

        s = Ga4Settings.from_env()
        return {
            "enabled": s.enabled,
            "credentials_set": bool(s.credentials_file),
            "pull_refused_because": s.refusal(),
            "max_requests": s.max_requests,
        }

    @app.get("/api/projects/{project_id}/analytics/settings")
    def get_settings_(project_id: str) -> AnalyticsSettings:
        project(project_id)
        return store.settings(project_id)

    @app.post("/api/projects/{project_id}/analytics/settings")
    def save_settings_(
        project_id: str, body: Ga4SettingsIn, token: TokenHeader = None
    ) -> AnalyticsSettings:
        unlocked(project_id, token)
        return set_property(projects, project_id, body.property_id, body.website)

    @app.post("/api/projects/{project_id}/analytics/import")
    def import_file(project_id: str, body: Ga4ImportIn, token: TokenHeader = None) -> Ga4ImportOut:
        unlocked(project_id, token)
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="The file could not be read.") from None
        if len(data) > _MAX_FILE:
            raise HTTPException(status_code=413, detail="The file is larger than 20 MB.")
        try:
            snapshot = import_ga4_csv(projects, project_id, body.website, data)
        except (Ga4ImportError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return summary([snapshot])

    @app.post("/api/projects/{project_id}/analytics/pull")
    def pull(project_id: str, token: TokenHeader = None) -> Ga4ImportOut:
        from src.modules.analytics.ga4 import Ga4Error

        unlocked(project_id, token)
        try:
            return summary(pull_ga4(projects, project_id))
        except Ga4Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/api/projects/{project_id}/analytics")
    def report(project_id: str) -> AnalyticsReport:
        project(project_id)
        built = build_analytics_report(projects, access, project_id)
        if built is None:
            raise HTTPException(status_code=404, detail="No GA4 data has been added yet.")
        return built[0]

    @app.get("/api/projects/{project_id}/analytics/workbook")
    def workbook(project_id: str) -> FileResponse:
        report(project_id)
        path = store.workbook(project_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="The analytics workbook is missing.")
        return FileResponse(
            path,
            filename=f"{project_id}_analytics.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
