"""HTTP routes of the Research module.

GET  /api/research/status                         is Semrush on, caps, units used
GET  /api/projects/{project}/research              the latest snapshot (404 when none)
GET  /api/projects/{project}/research/estimate     worst-case units of a pull
POST /api/projects/{project}/research/pull         pull from Semrush (unlock token)
POST /api/projects/{project}/research/import       import an export (unlock token)
GET  /api/projects/{project}/research/movers       latest against previous snapshot
GET  /api/projects/{project}/research/workbook     the latest snapshot's workbook
GET  /api/projects/{project}/research/competitors  suggested competitors (from the search data)
POST /api/projects/{project}/research/competitors/import            a competitors export (token)
POST /api/projects/{project}/research/competitors/{domain}/add      add it to the project (token)
POST /api/projects/{project}/research/competitors/{domain}/dismiss  hide it (token)
POST /api/projects/{project}/research/competitors/{domain}/restore  suggest it again (token)
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
from src.modules.content_processor.projects.store import (
    ProjectNotFoundError,
    ProjectStore,
    SiteSpec,
)
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.research.build import movers
from src.modules.research.competitors import (
    SOURCE_LABELS,
    CompetitorBook,
    domain_of,
    read_competitors_csv,
)
from src.modules.research.importer import ImportError_
from src.modules.research.models import Mover, ResearchSnapshot
from src.modules.research.semrush import SemrushError
from src.modules.research.service import (
    ResearchSettings,
    competitor_book,
    competitors_store,
    estimate_units,
    import_competitors,
    import_csv,
    pull_project,
    research_store,
    unit_log,
)

__all__ = ["CompetitorsImportIn", "ImportIn", "PullIn", "register_research_routes"]

TokenHeader = Annotated[str | None, Header(alias="X-Project-Token")]
_MAX_FILE = 20 * 1024 * 1024


class PullIn(StrictModel):
    """Optional keywords whose demand and top results are pulled too."""

    keywords: list[str] = Field(default_factory=list, max_length=50)
    refresh: bool = Field(
        default=False, description="Pull every website again, even ones pulled recently"
    )


class ImportIn(StrictModel):
    """A Semrush Organic Positions export of one website."""

    website: str = Field(min_length=3, max_length=300)
    file_name: str = Field(max_length=300)
    content_base64: str = Field(max_length=_MAX_FILE * 2)


class CompetitorsImportIn(StrictModel):
    """A Semrush Organic Competitors export of the client's website."""

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


def _note(domain: str, book: CompetitorBook) -> str:
    found = next((s for s in book.suggestions if s.domain == domain), None)
    if found is None:
        return "Suggested by the search data."
    where = ", ".join(SOURCE_LABELS[s] for s in found.sources)
    return f"Suggested by the search data ({where})."


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
            "min_interval_ms": s.min_interval_ms,
            "fresh_days": s.fresh_days,
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
                projects,
                access,
                project_id,
                settings(),
                serp_keywords=body.keywords,
                refresh=body.refresh,
            )
        except SemrushError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return snapshot

    @app.post("/api/projects/{project_id}/research/import")
    def import_file(project_id: str, body: ImportIn, token: TokenHeader = None) -> ResearchSnapshot:
        unlocked(project_id, token)
        data = _decode(body.content_base64)
        try:
            snapshot, _files = import_csv(
                projects, access, project_id, body.website, data, database=settings().database
            )
        except (ImportError_, ValueError) as exc:
            if read_competitors_csv(data, datetime.now(UTC)) is not None:
                detail = (
                    "This is a Semrush Organic Competitors export: import it under Suggested "
                    "competitors."
                )
                raise HTTPException(status_code=422, detail=detail) from None
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return snapshot

    @app.get("/api/projects/{project_id}/research/competitors")
    def competitors(project_id: str) -> CompetitorBook:
        project(project_id)
        return competitor_book(projects, project_id)

    @app.post("/api/projects/{project_id}/research/competitors/import")
    def import_competitor_file(
        project_id: str, body: CompetitorsImportIn, token: TokenHeader = None
    ) -> CompetitorBook:
        unlocked(project_id, token)
        try:
            import_competitors(projects, project_id, _decode(body.content_base64))
        except ImportError_ as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return competitor_book(projects, project_id)

    def suggestion(project_id: str, domain: str, token: str | None) -> str:
        unlocked(project_id, token)
        clean = domain_of(domain)
        book = competitor_book(projects, project_id)
        if clean is None or all(s.domain != clean for s in book.suggestions):
            raise HTTPException(status_code=404, detail="That competitor is not suggested.")
        return clean

    @app.post("/api/projects/{project_id}/research/competitors/{domain}/add")
    def add_competitor(project_id: str, domain: str, token: TokenHeader = None) -> CompetitorBook:
        clean = suggestion(project_id, domain, token)
        book = competitor_book(projects, project_id)
        if any(s.domain == clean and s.status == "added" for s in book.suggestions):
            raise HTTPException(status_code=409, detail="That website is already in the project.")
        try:
            site = projects.add_site(
                project_id, SiteSpec(name=clean, website=clean, note=_note(clean, book))
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return competitors_store(projects).decide(
            projects.get(project_id), clean, "added", site.site_id
        )

    @app.post("/api/projects/{project_id}/research/competitors/{domain}/dismiss")
    def dismiss_competitor(
        project_id: str, domain: str, token: TokenHeader = None
    ) -> CompetitorBook:
        clean = suggestion(project_id, domain, token)
        return competitors_store(projects).decide(projects.get(project_id), clean, "dismissed")

    @app.post("/api/projects/{project_id}/research/competitors/{domain}/restore")
    def restore_competitor(
        project_id: str, domain: str, token: TokenHeader = None
    ) -> CompetitorBook:
        clean = suggestion(project_id, domain, token)
        return competitors_store(projects).decide(projects.get(project_id), clean, "suggested")

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
