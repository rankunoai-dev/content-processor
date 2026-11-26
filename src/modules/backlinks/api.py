"""HTTP routes of the Backlinks module.

GET  /api/backlinks/status                        is Ahrefs on, caps, units used
GET  /api/projects/{project}/backlinks            the latest snapshot (404 when none)
GET  /api/projects/{project}/backlinks/estimate   worst-case units of a pull
POST /api/projects/{project}/backlinks/pull       pull from Ahrefs (unlock token)
POST /api/projects/{project}/backlinks/import     import an Ahrefs export (unlock token)
GET  /api/projects/{project}/backlinks/workbook   the latest snapshot's workbook
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.analyst.playground import Playground
from src.modules.backlinks.ahrefs import AhrefsError
from src.modules.backlinks.importer import BacklinksImportError
from src.modules.backlinks.models import BacklinkSnapshot
from src.modules.backlinks.service import (
    BacklinksSettings,
    backlinks_store,
    estimate_units,
    import_csv,
    pull_project,
    unit_log,
)
from src.modules.connections.store import ConnectionStore, SavedConnectionError, bound_key
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

__all__ = ["BacklinksImportIn", "register_backlinks_routes"]

TokenHeader = Annotated[str | None, Header(alias="X-Project-Token")]
_MAX_FILE = 20 * 1024 * 1024


class BacklinksImportIn(StrictModel):
    """An Ahrefs export of one website: its referring domains or its top pages."""

    website: str = Field(min_length=3, max_length=300)
    kind: Literal["refdomains", "pages"]
    file_name: str = Field(max_length=300)
    content_base64: str = Field(max_length=_MAX_FILE * 2)


def _decode(content_base64: str) -> bytes:
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="The file could not be read.") from None
    if len(data) > _MAX_FILE:
        raise HTTPException(status_code=413, detail="The file is larger than 20 MB.")
    return data


def register_backlinks_routes(
    app: FastAPI,
    *,
    projects: ProjectStore,
    access: KnowledgeAccess,
    signer: TokenSigner,
    settings: Callable[[], BacklinksSettings] = BacklinksSettings.from_env,
    connections: ConnectionStore | None = None,
) -> None:
    """Add the backlinks routes (before the app's catch-all routes).

    `connections`: when given, a project that chose an Ahrefs connection pulls with its key.
    """
    store = backlinks_store(projects)

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
                detail="Unlock this project with its password to add backlink data.",
            )

    @app.get("/api/backlinks/status")
    def status() -> dict[str, Any]:
        s = settings()
        month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {
            "enabled": s.enabled and bool(s.api_key),
            "api_key_set": bool(s.api_key),
            "monthly_cap": s.monthly_units,
            "used_this_month": unit_log(projects).used(month),
            "pull_cap": s.pull_units,
            "refdomains_limit": s.refdomains_limit,
            "pages_limit": s.pages_limit,
            "min_interval_ms": s.min_interval_ms,
        }

    @app.get("/api/projects/{project_id}/backlinks")
    def latest(project_id: str) -> BacklinkSnapshot:
        project(project_id)
        found = store.latest(project_id)
        if found is None:
            raise HTTPException(status_code=404, detail="No backlink data has been added yet.")
        return found

    @app.get("/api/projects/{project_id}/backlinks/estimate")
    def estimate(project_id: str) -> dict[str, int]:
        project(project_id)
        s = settings()
        websites = len(Playground(projects, access, [project_id]).websites())
        return {
            "websites": websites,
            "refdomains_limit": s.refdomains_limit,
            "pages_limit": s.pages_limit,
            "worst_case_units": estimate_units(websites, s),
            "pull_cap": s.pull_units,
        }

    @app.post("/api/projects/{project_id}/backlinks/pull")
    def pull(project_id: str, token: TokenHeader = None) -> BacklinkSnapshot:
        unlocked(project_id, token)
        try:
            base = settings()
            if connections is not None:
                key = bound_key(connections, projects.get(project_id).connections, "ahrefs")
                if key is not None:
                    base = replace(base, api_key=key)
            snapshot, _files = pull_project(projects, access, project_id, base)
        except (AhrefsError, SavedConnectionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return snapshot

    @app.post("/api/projects/{project_id}/backlinks/import")
    def import_file(
        project_id: str, body: BacklinksImportIn, token: TokenHeader = None
    ) -> BacklinkSnapshot:
        unlocked(project_id, token)
        data = _decode(body.content_base64)
        try:
            snapshot, _files = import_csv(
                projects, access, project_id, body.website, body.kind, data
            )
        except (BacklinksImportError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return snapshot

    @app.get("/api/projects/{project_id}/backlinks/workbook")
    def workbook(project_id: str) -> FileResponse:
        snapshot = latest(project_id)
        path = store.workbook(snapshot)
        if not path.exists():
            raise HTTPException(status_code=404, detail="The workbook of this snapshot is missing.")
        return FileResponse(
            path,
            filename=f"{project_id}_backlinks.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
