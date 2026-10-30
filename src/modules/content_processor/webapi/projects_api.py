"""Routes for App module 1 (Content Scraper): projects, URL inventory, runs and reports.

Reading is open to anyone using the app. Every change needs the project's unlock
token in the `X-Project-Token` header; the token comes from creating the project
or from `POST /api/projects/{id}/unlock` with the password, and lasts 8 hours.
Deleting a project asks for the password itself. Writes are JSON only (an imported
file travels base64-encoded), so the app's write guard (`app._write_refusal`) applies
to them unchanged.
"""

from __future__ import annotations

import base64
import binascii
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.projects.models import ProjectRecord, ProjectUrl, RunState
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import (
    MIN_PASSWORD_LENGTH,
    AttemptLimiter,
    TokenSigner,
)
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.projects.urls import (
    IMPORT_MAX_BYTES,
    UnreadableFileError,
    candidates_from_file,
    candidates_from_text,
)

__all__ = ["PROJECT_TOKEN_HEADER", "register_project_routes"]

PROJECT_TOKEN_HEADER = "X-Project-Token"  # noqa: S105 - a header name, not a secret

# The unlock token header; at module level so FastAPI can resolve the annotation.
TokenHeader = Annotated[str | None, Header(alias=PROJECT_TOKEN_HEADER)]


class ProjectOut(StrictModel):
    """A project as the app shows it (never the password hash)."""

    project_id: str
    name: str
    owner_name: str
    client_name: str
    created_at: datetime
    updated_at: datetime
    url_count: int
    last_run: RunState | None = None


class ProjectCreate(StrictModel):
    """The form that creates a project."""

    name: str = Field(min_length=1, max_length=120)
    owner_name: str = Field(min_length=1, max_length=120)
    client_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


class Unlock(StrictModel):
    """The project password."""

    password: str = Field(min_length=1, max_length=200)


class Unlocked(StrictModel):
    """Proof of the password for the next hours: send `token` with every change."""

    project: ProjectOut
    token: str
    expires_at: datetime


class UrlsIn(StrictModel):
    """Pasted text: one URL per line."""

    text: str = Field(max_length=IMPORT_MAX_BYTES)


class FileIn(StrictModel):
    """A file from the user's device, base64-encoded by the browser."""

    file_name: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(max_length=IMPORT_MAX_BYTES * 4 // 3 + 8)


class UrlsRemove(StrictModel):
    """URLs to take out of the project; `all` empties the list."""

    urls: list[str] = Field(default_factory=list, max_length=100_000)
    all: bool = False


class ProjectUpdate(StrictModel):
    """Rename the project or its client."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    client_name: str | None = Field(default=None, min_length=1, max_length=120)


class ImportOut(StrictModel):
    """What a paste or an import did."""

    added: int
    already_present: int
    duplicates_in_input: int
    rejected: int
    rejected_examples: list[dict[str, str]]
    over_limit: int
    url_count: int


class RemovedOut(StrictModel):
    """How many URLs were removed and how many remain."""

    removed: int
    url_count: int


class UrlsPage(StrictModel):
    """One page of the URL inventory."""

    total: int
    items: list[ProjectUrl]


class ReportFile(StrictModel):
    """A downloadable report of a run."""

    name: str
    size: int
    modified_at: datetime


def register_project_routes(
    app: FastAPI,
    *,
    store: ProjectStore,
    runner: ScrapeRunner,
    signer: TokenSigner,
    limiter: AttemptLimiter | None = None,
) -> None:
    """Add the project routes to the app (before its catch-all routes)."""
    attempts = limiter or AttemptLimiter()

    def project(project_id: str) -> ProjectRecord:
        try:
            return store.get(project_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    def out(record: ProjectRecord) -> ProjectOut:
        runs = store.runs(record.project_id)
        return ProjectOut(
            project_id=record.project_id,
            name=record.name,
            owner_name=record.owner_name,
            client_name=record.client_name,
            created_at=record.created_at,
            updated_at=record.updated_at,
            url_count=len(store.urls(record.project_id)),
            last_run=runs[0] if runs else None,
        )

    def require_token(project_id: str, token: str | None) -> ProjectRecord:
        record = project(project_id)
        if not signer.check(token, project_id):
            raise HTTPException(
                status_code=401,
                detail="Unlock this project with its password to make changes.",
            )
        return record

    def run_state(project_id: str, run_id: str) -> RunState:
        try:
            return store.run(project_id, run_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="This project has no such run.") from None

    @app.get("/api/projects")
    def list_projects() -> list[ProjectOut]:
        return [out(p) for p in store.projects()]

    @app.post("/api/projects")
    def create_project(body: ProjectCreate) -> Unlocked:
        record = store.create(
            name=body.name,
            owner_name=body.owner_name,
            client_name=body.client_name,
            password=body.password,
        )
        token, expires = signer.issue(record.project_id)
        return Unlocked(
            project=out(record), token=token, expires_at=datetime.fromtimestamp(expires, UTC)
        )

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> ProjectOut:
        return out(project(project_id))

    @app.post("/api/projects/{project_id}/unlock")
    def unlock(project_id: str, body: Unlock) -> Unlocked:
        record = project(project_id)
        wait = attempts.wait_s(project_id)
        if wait > 0:
            raise HTTPException(
                status_code=429,
                detail=f"Too many wrong passwords. Try again in {int(wait) + 1} seconds.",
            )
        if not store.check_password(project_id, body.password):
            attempts.failed(project_id)
            raise HTTPException(status_code=401, detail="That password is not right.")
        attempts.succeeded(project_id)
        token, expires = signer.issue(project_id)
        return Unlocked(
            project=out(record), token=token, expires_at=datetime.fromtimestamp(expires, UTC)
        )

    @app.post("/api/projects/{project_id}/update")
    def update(project_id: str, body: ProjectUpdate, token: TokenHeader = None) -> ProjectOut:
        require_token(project_id, token)
        return out(store.update(project_id, name=body.name, client_name=body.client_name))

    @app.post("/api/projects/{project_id}/delete")
    def delete(project_id: str, body: Unlock, token: TokenHeader = None) -> dict[str, str]:
        require_token(project_id, token)
        if not store.check_password(project_id, body.password):
            raise HTTPException(status_code=401, detail="That password is not right.")
        if any(r.status in ("queued", "running") for r in store.runs(project_id)):
            raise HTTPException(status_code=409, detail="Cancel the running scrape first.")
        store.delete(project_id)
        return {"deleted": project_id}

    # -- URLs -----------------------------------------------------------------

    @app.get("/api/projects/{project_id}/urls")
    def urls(
        project_id: str,
        q: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> UrlsPage:
        project(project_id)
        items = store.urls(project_id)
        if q:
            needle = q.lower()
            items = [u for u in items if needle in u.url.lower()]
        return UrlsPage(total=len(items), items=items[offset : offset + limit])

    def import_out(project_id: str, summary: dict[str, Any]) -> ImportOut:
        return ImportOut.model_validate(summary | {"url_count": len(store.urls(project_id))})

    @app.post("/api/projects/{project_id}/urls")
    def add_urls(project_id: str, body: UrlsIn, token: TokenHeader = None) -> ImportOut:
        require_token(project_id, token)
        result = store.add_urls(project_id, candidates_from_text(body.text))
        return import_out(project_id, result.summary())

    @app.post("/api/projects/{project_id}/urls/import")
    def import_file(project_id: str, body: FileIn, token: TokenHeader = None) -> ImportOut:
        require_token(project_id, token)
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="The file did not arrive intact.") from None
        try:
            candidates = candidates_from_file(body.file_name, data)
        except UnreadableFileError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if not candidates:
            raise HTTPException(
                status_code=422, detail="No web addresses (http:// or https://) were found in it."
            )
        result = store.add_urls(project_id, candidates)
        return import_out(project_id, result.summary())

    @app.post("/api/projects/{project_id}/urls/remove")
    def remove_urls(project_id: str, body: UrlsRemove, token: TokenHeader = None) -> RemovedOut:
        require_token(project_id, token)
        removed = store.remove_urls(project_id, None if body.all else body.urls)
        return RemovedOut(removed=removed, url_count=len(store.urls(project_id)))

    # -- runs -----------------------------------------------------------------

    @app.get("/api/projects/{project_id}/runs")
    def runs(project_id: str) -> list[RunState]:
        project(project_id)
        return store.runs(project_id)

    @app.post("/api/projects/{project_id}/runs")
    def start_run(project_id: str, token: TokenHeader = None) -> RunState:
        record = require_token(project_id, token)
        try:
            return runner.submit(project_id, started_by=record.owner_name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/api/projects/{project_id}/runs/{run_id}")
    def get_run(project_id: str, run_id: str) -> RunState:
        project(project_id)
        return run_state(project_id, run_id)

    @app.post("/api/projects/{project_id}/runs/{run_id}/cancel")
    def cancel_run(project_id: str, run_id: str, token: TokenHeader = None) -> RunState:
        require_token(project_id, token)
        run_state(project_id, run_id)
        return runner.cancel(project_id, run_id)

    @app.get("/api/projects/{project_id}/runs/{run_id}/downloads")
    def downloads(project_id: str, run_id: str) -> list[ReportFile]:
        project(project_id)
        run_state(project_id, run_id)
        return [
            ReportFile(
                name=name,
                size=path.stat().st_size,
                modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            )
            for name, path in store.report_files(project_id, run_id).items()
        ]

    @app.get("/api/projects/{project_id}/runs/{run_id}/downloads.zip")
    def download_all(project_id: str, run_id: str) -> FileResponse:
        project(project_id)
        run_state(project_id, run_id)
        files = store.report_files(project_id, run_id)
        if not files:
            raise HTTPException(status_code=404, detail="This run has no reports yet.")
        archive = store.run_dir(project_id, run_id) / f"{run_id}_reports.zip"
        newest = max(p.stat().st_mtime for p in files.values())
        if not archive.is_file() or archive.stat().st_mtime < newest:
            _zip(archive, files)
        return FileResponse(archive, filename=archive.name, content_disposition_type="attachment")

    @app.get("/api/projects/{project_id}/runs/{run_id}/downloads/{file_name}")
    def download(project_id: str, run_id: str, file_name: str) -> FileResponse:
        project(project_id)
        run_state(project_id, run_id)
        # The name is looked up in the folder listing, never joined onto a path.
        path = store.report_files(project_id, run_id).get(file_name)
        if path is None:
            raise HTTPException(status_code=404, detail="That file is not in this run's reports.")
        return FileResponse(path, filename=file_name, content_disposition_type="attachment")


def _zip(archive: Path, files: dict[str, Path]) -> None:
    tmp = archive.with_name(archive.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, path in files.items():
            bundle.write(path, arcname=name)
    tmp.replace(archive)
