"""HTTP routes of the Theming module.

GET  /api/projects/{project}/themes            the latest theme map
POST /api/projects/{project}/themes            build a new one (project unlock token)
GET  /api/projects/{project}/themes/workbook   the latest map's workbook (.xlsx)
GET  /api/projects/{project}/themes/changes    the latest map against the previous one
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.theming.models import ThemeMap
from src.modules.theming.service import build_project_themes, theme_store, workbook_path
from src.modules.theming.store import compare_maps

__all__ = ["register_theming_routes"]

TokenHeader = Annotated[str | None, Header(alias="X-Project-Token")]


def register_theming_routes(
    app: FastAPI, *, projects: ProjectStore, access: KnowledgeAccess, signer: TokenSigner
) -> None:
    """Add the theme routes (before the app's catch-all routes)."""
    store = theme_store(projects)

    def project(project_id: str) -> None:
        try:
            projects.get(project_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    def latest(project_id: str) -> ThemeMap:
        project(project_id)
        found = store.latest(project_id)
        if found is None:
            raise HTTPException(status_code=404, detail="No theme map has been built yet.")
        return found

    @app.get("/api/projects/{project_id}/themes")
    def get_themes(project_id: str) -> ThemeMap:
        return latest(project_id)

    @app.post("/api/projects/{project_id}/themes")
    def build_themes(project_id: str, token: TokenHeader = None) -> ThemeMap:
        project(project_id)
        if not signer.check(token, project_id):
            raise HTTPException(
                status_code=401, detail="Unlock this project with its password to build themes."
            )
        theme_map, _files = build_project_themes(projects, access, project_id)
        return theme_map

    @app.get("/api/projects/{project_id}/themes/workbook")
    def workbook(project_id: str) -> FileResponse:
        theme_map = latest(project_id)
        path = workbook_path(store, theme_map)
        if not path.exists():
            raise HTTPException(status_code=404, detail="The workbook of this map is missing.")
        return FileResponse(
            path,
            filename=f"{project_id}_themes.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/api/projects/{project_id}/themes/changes")
    def changes(project_id: str) -> dict[str, Any]:
        newest = latest(project_id)
        history = store.history(project_id)
        if len(history) < 2:
            raise HTTPException(status_code=404, detail="Build the theme map again to compare.")
        return compare_maps(store.get(project_id, history[1]), newest)
