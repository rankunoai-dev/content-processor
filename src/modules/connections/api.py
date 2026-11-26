"""HTTP routes for connections and for the connection each project uses.

    GET    /api/connections                       every connection, whether its key is set (viewer)
    POST   /api/connections                       save a connection (admin)
    DELETE /api/connections/{connection_id}       remove one no project uses (admin)
    PUT    /api/projects/{project_id}/connections choose a project's connections
                                                   (editor, unlock token)

The key values are never returned: a connection shows only whether its variable is set.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.connections.store import (
    PLATFORMS,
    Connection,
    ConnectionStore,
    SavedConnectionError,
)
from src.modules.content_processor.projects.security import ACTING, TokenSigner
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore

__all__ = ["register_connection_routes"]

TokenHeader = Annotated[str | None, Header(alias="X-Project-Token")]


class ConnectionIn(StrictModel):
    """A connection to save."""

    platform: str
    label: str = Field(min_length=1, max_length=80)
    secret_env: str = Field(
        min_length=3,
        max_length=64,
        description="the NAME of the environment variable that holds the key, e.g. SEMRUSH_AGENCY",
    )
    property_ref: str = Field(
        default="", max_length=200, description="GA4 property id or Search Console site, if any"
    )


class ProjectConnectionsIn(StrictModel):
    """The connection a project uses per platform; an empty id clears that platform."""

    connections: dict[str, str] = Field(default_factory=dict)


def _view(store: ConnectionStore, projects: ProjectStore, item: Connection) -> dict[str, Any]:
    used = [p.name for p in projects.projects() if item.connection_id in p.connections.values()]
    return {
        "connection_id": item.connection_id,
        "platform": item.platform,
        "label": item.label,
        "secret_env": item.secret_env,
        "key_set": store.key_set(item),
        "property_ref": item.property_ref,
        "created_by": item.created_by,
        "created_at": item.created_at.isoformat(),
        "used_by": used,
    }


def register_connection_routes(
    app: FastAPI,
    *,
    projects: ProjectStore,
    connections: ConnectionStore,
    signer: TokenSigner,
) -> None:
    """Add the connection routes (the access gate decides who may call each one)."""

    def project(project_id: str) -> None:
        try:
            projects.get(project_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    @app.get("/api/connections")
    def list_connections() -> dict[str, Any]:
        return {
            "platforms": list(PLATFORMS),
            "connections": [_view(connections, projects, c) for c in connections.all()],
        }

    @app.post("/api/connections", status_code=201)
    def save_connection(body: ConnectionIn) -> dict[str, Any]:
        try:
            item = connections.add(
                platform=body.platform,
                label=body.label,
                secret_env=body.secret_env,
                property_ref=body.property_ref,
                created_by=ACTING.get() or "administrator",
            )
        except SavedConnectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return _view(connections, projects, item)

    @app.delete("/api/connections/{connection_id}")
    def remove_connection(connection_id: str) -> dict[str, Any]:
        try:
            item = connections.get(connection_id)
            connections.remove(connection_id, in_use_by=projects.projects_using(connection_id))
        except SavedConnectionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"removed": item.connection_id}

    @app.put("/api/projects/{project_id}/connections")
    def choose_connections(
        project_id: str, body: ProjectConnectionsIn, token: TokenHeader = None
    ) -> dict[str, Any]:
        project(project_id)
        if not signer.check(token, project_id):
            raise HTTPException(
                status_code=401, detail="Unlock this project with its password to change it."
            )
        chosen: dict[str, str] = {}
        for platform, cid in body.connections.items():
            if platform not in PLATFORMS:
                raise HTTPException(status_code=422, detail=f"unknown platform {platform!r}")
            if not cid:
                continue
            try:
                item = connections.get(cid)
            except SavedConnectionError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            if item.platform != platform:
                raise HTTPException(
                    status_code=422,
                    detail=f"connection {cid!r} is a {item.platform} connection, not {platform}",
                )
            chosen[platform] = cid
        record = projects.set_connections(project_id, chosen)
        return {"project_id": record.project_id, "connections": record.connections}
