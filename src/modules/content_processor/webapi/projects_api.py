"""Routes for App module 1 (Content Scraper): projects, their websites, URLs, runs, reports.

A project holds the client's website and any number of competitor websites; each
website has its own URL list, runs, reports and knowledge space
(docs/app/ARCHITECTURE_PROJECTS_AND_SITES.md). The knowledge routes let the bot
search any set of websites, of one project or several, and say where each
passage came from.

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
import os
import tempfile
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import Field

from src.core.logger import get_logger
from src.core.schemas import StrictModel
from src.modules.content_processor.knowledge.embedders import Embedder
from src.modules.content_processor.knowledge.federated import Space, federated_search
from src.modules.content_processor.knowledge.vector_store import VectorStore
from src.modules.content_processor.projects.models import (
    MAX_NOTE_LENGTH,
    MAX_SITE_TAGS,
    RUN_ACTIVE,
    OnTurn,
    ProjectRecord,
    ProjectUrl,
    RunState,
    SiteRecord,
    SiteRole,
)
from src.modules.content_processor.projects.project_reports import (
    ProjectReport,
    build_status,
    project_report_files,
    project_reports,
    report_sources,
    start_project_reports,
    zip_project_report,
)
from src.modules.content_processor.projects.runner import QueueEntry, ScrapeRunner
from src.modules.content_processor.projects.security import (
    MIN_PASSWORD_LENGTH,
    AttemptLimiter,
    TokenSigner,
)
from src.modules.content_processor.projects.store import (
    ProjectNotFoundError,
    ProjectStore,
    SiteSpec,
)
from src.modules.content_processor.projects.urls import (
    IMPORT_MAX_BYTES,
    ImportResult,
    UnreadableFileError,
    candidates_from_file,
    candidates_from_text,
)
from src.modules.content_processor.projects.websites import WebsiteGraph

__all__ = ["PROJECT_TOKEN_HEADER", "KnowledgeAccess", "register_project_routes"]

PROJECT_TOKEN_HEADER = "X-Project-Token"  # noqa: S105 - a header name, not a secret
_logger = get_logger("content_processor.webapi.projects")

# The unlock token header; at module level so FastAPI can resolve the annotation.
TokenHeader = Annotated[str | None, Header(alias=PROJECT_TOKEN_HEADER)]


@dataclass
class KnowledgeAccess:
    """How the bot opens a website's knowledge space and the embedder for its model."""

    open_store: Callable[[Space], VectorStore]
    embedder_for: Callable[[str], Embedder]


# -- shapes -----------------------------------------------------------------------


class SiteOut(StrictModel):
    """A website of a project, with its counts and latest scrape."""

    site_id: str
    name: str
    role: str
    main: bool = Field(description="True for the client's main website")
    domains: list[str]
    note: str
    tags: list[str]
    url_count: int
    last_run: RunState | None = None
    namespace: str


class ProjectOut(StrictModel):
    """A project as the app shows it (never the password hash)."""

    project_id: str
    name: str
    owner_name: str
    client_name: str
    created_at: datetime
    updated_at: datetime
    url_count: int
    sites: list[SiteOut]
    last_run: RunState | None = None


class SiteIn(StrictModel):
    """A website as entered: its name, usually its address, a note and tags.

    `role` client adds another website or brand of the client (never a competitor
    of the client's other websites); competitor (the default) adds a competitor.
    """

    name: str = Field(min_length=1, max_length=120)
    website: str | None = Field(default=None, max_length=500)
    note: str = Field(default="", max_length=MAX_NOTE_LENGTH)
    tags: list[str] = Field(default_factory=list, max_length=MAX_SITE_TAGS)
    role: SiteRole = "competitor"

    def spec(self) -> SiteSpec:
        """As the store takes it."""
        return SiteSpec(name=self.name, website=self.website, note=self.note, tags=tuple(self.tags))


class SiteUpdate(StrictModel):
    """Changes to a website; fields left out stay as they are."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)
    tags: list[str] | None = Field(default=None, max_length=MAX_SITE_TAGS)
    role: SiteRole | None = None


class ProjectCreate(StrictModel):
    """The form that creates a project with its client and competitor websites."""

    name: str = Field(min_length=1, max_length=120)
    owner_name: str = Field(min_length=1, max_length=120)
    client_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)
    client_website: str | None = Field(default=None, max_length=500)
    client_note: str = Field(default="", max_length=MAX_NOTE_LENGTH)
    client_tags: list[str] = Field(default_factory=list, max_length=MAX_SITE_TAGS)
    client_brands: list[SiteIn] = Field(
        default_factory=list,
        max_length=50,
        description="the client's other websites or brands (never its competitors)",
    )
    competitors: list[SiteIn] = Field(default_factory=list, max_length=50)


class StartIn(StrictModel):
    """How a scrape waits for its turn in the shared queue (one scrape runs at a time)."""

    on_turn: OnTurn = Field(
        default="auto",
        description="auto: start by itself when its turn comes; notify: email me then and wait",
    )
    email: str | None = Field(default=None, max_length=254)
    email_when_done: bool = False


class QueueItem(StrictModel):
    """One scrape of the shared queue."""

    run: RunState
    project_name: str
    site_name: str
    position: int = Field(description="place in the waiting line (1 runs next); 0 otherwise")
    wait_s: float = Field(description="rough wait before it starts, in seconds")


class QueueOut(StrictModel):
    """The shared queue: what runs now, what waits (in order) and what waits for its person."""

    running: list[QueueItem]
    waiting: list[QueueItem]
    ready: list[QueueItem]
    seconds_per_url: float
    email_sends: bool = Field(description="False: emails are saved to the outbox, not sent")
    ready_hours: float = Field(description="a scrape whose turn came is cancelled after this")


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
    """URLs to take out of a website; `all` empties its list."""

    urls: list[str] = Field(default_factory=list, max_length=100_000)
    all: bool = False


class ProjectUpdate(StrictModel):
    """Rename the project or its client."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    client_name: str | None = Field(default=None, min_length=1, max_length=120)


class ImportOut(StrictModel):
    """What a paste or an import did to one website's list."""

    site_id: str
    site_name: str
    added: int
    already_present: int
    duplicates_in_input: int
    rejected: int
    rejected_examples: list[dict[str, str]]
    over_limit: int
    url_count: int


class RoutedOut(StrictModel):
    """A project-level paste or import, sorted to the websites by domain."""

    sites: list[ImportOut]
    unknown_domains: dict[str, int]
    rejected: list[dict[str, str]]


class RemovedOut(StrictModel):
    """How many URLs were removed and how many remain."""

    removed: int
    url_count: int


class UrlsPage(StrictModel):
    """One page of a website's URL inventory."""

    total: int
    items: list[ProjectUrl]


class ReportFile(StrictModel):
    """A downloadable report of a run."""

    name: str
    size: int
    modified_at: datetime


class ProjectReportsIn(StrictModel):
    """What to put in a project report."""

    include_competitors: bool = False
    wait: bool = Field(default=False, description="build before answering (tests, scripts)")


class ReportSourceOut(StrictModel):
    """A website and the scrape a project report reads for it."""

    site_id: str
    site_name: str
    role: str
    main: bool
    run_id: str | None
    finished_at: datetime | None
    pages: int
    reason: str


class ProjectReportOut(StrictModel):
    """One built project report."""

    stamp: str
    built_at: datetime
    include_competitors: bool
    pages: int
    contradictions: int
    contradictions_high: int
    differs_by_condition: int
    time_bound: int
    duplicate_pairs: int
    duplicate_pairs_across_websites: int
    websites: list[ReportSourceOut]
    files: list[ReportFile]


class ProjectReportsOut(StrictModel):
    """What a new project report would use, and the reports built so far (newest first)."""

    sources: list[ReportSourceOut]
    reports: list[ProjectReportOut]
    building: bool = False
    building_since: datetime | None = None
    last_error: str = ""


def _report_out(report: ProjectReport, files: dict[str, Path]) -> ProjectReportOut:
    m = report.manifest
    topics = m.get("contradictions", {})
    dup = m.get("duplication", {})
    return ProjectReportOut(
        stamp=report.stamp,
        built_at=datetime.fromisoformat(m["built_at"]),
        include_competitors=bool(m.get("include_competitors")),
        pages=int(m.get("pages", 0)),
        contradictions=int(topics.get("contradictions", 0)),
        contradictions_high=int(topics.get("contradictions_high", 0)),
        differs_by_condition=int(topics.get("differs_by_condition", 0)),
        time_bound=int(topics.get("time_bound", 0)),
        duplicate_pairs=int(dup.get("pairs", 0)),
        duplicate_pairs_across_websites=int(dup.get("pairs_across_websites", 0)),
        websites=[
            ReportSourceOut.model_validate(w)
            for w in m.get("websites", [])
            if w.get("role") == "client" or m.get("include_competitors")
        ],
        files=[
            ReportFile(
                name=name,
                size=path.stat().st_size,
                modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            )
            for name, path in files.items()
        ],
    )


class SpaceOut(StrictModel):
    """One website's knowledge space, as the bot sees it."""

    namespace: str
    project_id: str
    project_name: str
    site_id: str
    site_name: str
    role: str
    ready: bool
    note: str


class WebsiteOut(StrictModel):
    """A unique website across all projects, and the websites it competes with."""

    key: str
    name: str
    domains: list[str]
    projects: list[str]
    competitors: list[str]
    same_client: list[str] = Field(description="the same client's other websites or brands")
    note: str
    tags: list[str]
    searchable: bool
    freshest_project: str | None


class HitOut(StrictModel):
    """A passage the bot found, with the website and project it came from."""

    rank: int
    score: float
    website: str
    project_id: str
    project_name: str
    site_id: str
    site_name: str
    role: str
    url: str
    headings: list[str]
    text: str
    chunk_id: str
    evidence_ids: list[str]


class SearchOut(StrictModel):
    """The fused results and which spaces were searched or skipped (with why)."""

    query: str
    scope: list[str] = Field(default_factory=list, description="websites considered")
    hits: list[HitOut]
    searched: list[str]
    skipped: list[dict[str, str]]
    notes: list[str] = Field(
        default_factory=list, description="websites searched by exact words only, and why"
    )


# -- routes -----------------------------------------------------------------------


def register_project_routes(
    app: FastAPI,
    *,
    store: ProjectStore,
    runner: ScrapeRunner,
    signer: TokenSigner,
    limiter: AttemptLimiter | None = None,
    knowledge: KnowledgeAccess | None = None,
) -> None:
    """Add the project and knowledge routes to the app (before its catch-all routes)."""
    attempts = limiter or AttemptLimiter()

    def project(project_id: str) -> ProjectRecord:
        try:
            return store.get(project_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    def site(project_id: str, site_id: str) -> SiteRecord:
        project(project_id)
        try:
            return store.site(project_id, site_id)
        except ProjectNotFoundError:
            raise HTTPException(
                status_code=404, detail="This project has no such website."
            ) from None

    def site_out(record: ProjectRecord, s: SiteRecord) -> SiteOut:
        runs = store.runs(record.project_id, s.site_id)
        main = record.main_site()
        return SiteOut(
            site_id=s.site_id,
            name=s.name,
            role=s.role,
            main=main is not None and main.site_id == s.site_id,
            domains=s.domains,
            note=s.note,
            tags=s.tags,
            url_count=len(store.urls(record.project_id, s.site_id)),
            last_run=runs[0] if runs else None,
            namespace=runner.namespace(record.project_id, s.site_id),
        )

    def out(record: ProjectRecord) -> ProjectOut:
        sites = [site_out(record, s) for s in record.sites]
        runs = store.runs(record.project_id)
        return ProjectOut(
            project_id=record.project_id,
            name=record.name,
            owner_name=record.owner_name,
            client_name=record.client_name,
            created_at=record.created_at,
            updated_at=record.updated_at,
            url_count=sum(s.url_count for s in sites),
            sites=sites,
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

    def spaces_of(p: ProjectRecord) -> list[Space]:
        """One project's websites as knowledge spaces."""
        return [
            Space(
                namespace=runner.namespace(p.project_id, s.site_id),
                project_id=p.project_id,
                project_name=p.name,
                site_id=s.site_id,
                site_name=s.name,
                role=s.role,
            )
            for s in p.sites
        ]

    def run_state(project_id: str, site_id: str, run_id: str) -> RunState:
        site(project_id, site_id)
        try:
            return store.run(project_id, site_id, run_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="This website has no such run.") from None

    def unlocked(record: ProjectRecord) -> Unlocked:
        token, expires = signer.issue(record.project_id)
        return Unlocked(
            project=out(record), token=token, expires_at=datetime.fromtimestamp(expires, UTC)
        )

    def import_out(project_id: str, s: SiteRecord, result: ImportResult) -> ImportOut:
        summary: dict[str, Any] = result.summary()
        return ImportOut.model_validate(
            summary
            | {
                "site_id": s.site_id,
                "site_name": s.name,
                "url_count": len(store.urls(project_id, s.site_id)),
            }
        )

    def decode_file(body: FileIn) -> list[str]:
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
        return candidates

    def routed(project_id: str, candidates: list[str]) -> RoutedOut:
        result = store.add_urls_routed(project_id, candidates)
        names = {s.site_id: s for s in store.sites(project_id)}
        return RoutedOut(
            sites=[import_out(project_id, names[sid], r) for sid, r in result.per_site.items()],
            unknown_domains=result.unknown_domains,
            rejected=[{"value": v, "reason": r} for v, r in result.rejected[:20]],
        )

    # -- projects -------------------------------------------------------------

    @app.get("/api/projects")
    def list_projects() -> list[ProjectOut]:
        return [out(p) for p in store.projects()]

    @app.post("/api/projects")
    def create_project(body: ProjectCreate) -> Unlocked:
        try:
            record = store.create(
                name=body.name,
                owner_name=body.owner_name,
                client_name=body.client_name,
                password=body.password,
                client_site=SiteSpec(
                    name=body.client_name,
                    website=body.client_website,
                    note=body.client_note,
                    tags=tuple(body.client_tags),
                ),
                client_brands=[b.spec() for b in body.client_brands],
                competitors=[c.spec() for c in body.competitors],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return unlocked(record)

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
        return unlocked(record)

    @app.post("/api/projects/{project_id}/update")
    def update(project_id: str, body: ProjectUpdate, token: TokenHeader = None) -> ProjectOut:
        require_token(project_id, token)
        try:
            record = store.update(project_id, name=body.name, client_name=body.client_name)
        except ValueError:  # a name of spaces only, trimmed to nothing
            raise HTTPException(
                status_code=422, detail="Give the project and its client a name."
            ) from None
        return out(record)

    @app.post("/api/projects/{project_id}/delete")
    def delete(project_id: str, body: Unlock, token: TokenHeader = None) -> dict[str, str]:
        record = require_token(project_id, token)
        if not store.check_password(project_id, body.password):
            raise HTTPException(status_code=401, detail="That password is not right.")
        if any(r.status in RUN_ACTIVE for r in store.runs(project_id)):
            raise HTTPException(status_code=409, detail="Cancel the running scrapes first.")
        if knowledge is not None:
            for space in spaces_of(record):
                try:
                    knowledge_store = knowledge.open_store(space)
                    try:
                        if knowledge_store.meta():  # built at some point: empty it
                            knowledge_store.reset()
                    finally:
                        knowledge_store.close()
                except Exception:  # noqa: BLE001 - a knowledge store never blocks a delete
                    _logger.exception("knowledge_space_delete_failed", extra={"space": space})
        store.delete(project_id)
        return {"deleted": project_id}

    # -- websites -------------------------------------------------------------

    @app.post("/api/projects/{project_id}/sites")
    def add_site(project_id: str, body: SiteIn, token: TokenHeader = None) -> SiteOut:
        require_token(project_id, token)
        try:
            new = store.add_site(project_id, body.spec(), body.role)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return site_out(store.get(project_id), new)

    @app.post("/api/projects/{project_id}/sites/{site_id}/update")
    def update_site(
        project_id: str, site_id: str, body: SiteUpdate, token: TokenHeader = None
    ) -> SiteOut:
        require_token(project_id, token)
        site(project_id, site_id)
        try:
            changed = store.update_site(
                project_id,
                site_id,
                name=body.name,
                note=body.note,
                tags=body.tags,
                role=body.role,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return site_out(store.get(project_id), changed)

    @app.post("/api/projects/{project_id}/sites/{site_id}/delete")
    def remove_site(project_id: str, site_id: str, token: TokenHeader = None) -> dict[str, str]:
        require_token(project_id, token)
        site(project_id, site_id)
        if any(r.status in RUN_ACTIVE for r in store.runs(project_id, site_id)):
            raise HTTPException(status_code=409, detail="Cancel this website's scrape first.")
        try:
            store.remove_site(project_id, site_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"deleted": site_id}

    # -- URLs -----------------------------------------------------------------

    @app.post("/api/projects/{project_id}/urls")
    def add_urls_any_site(project_id: str, body: UrlsIn, token: TokenHeader = None) -> RoutedOut:
        require_token(project_id, token)
        return routed(project_id, candidates_from_text(body.text))

    @app.post("/api/projects/{project_id}/urls/import")
    def import_any_site(project_id: str, body: FileIn, token: TokenHeader = None) -> RoutedOut:
        require_token(project_id, token)
        return routed(project_id, decode_file(body))

    @app.get("/api/projects/{project_id}/sites/{site_id}/urls")
    def urls(
        project_id: str,
        site_id: str,
        q: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> UrlsPage:
        site(project_id, site_id)
        items = store.urls(project_id, site_id)
        if q:
            needle = q.lower()
            items = [u for u in items if needle in u.url.lower()]
        return UrlsPage(total=len(items), items=items[offset : offset + limit])

    @app.post("/api/projects/{project_id}/sites/{site_id}/urls")
    def add_urls(
        project_id: str, site_id: str, body: UrlsIn, token: TokenHeader = None
    ) -> ImportOut:
        require_token(project_id, token)
        s = site(project_id, site_id)
        result = store.add_urls(project_id, site_id, candidates_from_text(body.text))
        return import_out(project_id, store.site(project_id, s.site_id), result)

    @app.post("/api/projects/{project_id}/sites/{site_id}/urls/import")
    def import_file(
        project_id: str, site_id: str, body: FileIn, token: TokenHeader = None
    ) -> ImportOut:
        require_token(project_id, token)
        site(project_id, site_id)
        result = store.add_urls(project_id, site_id, decode_file(body))
        return import_out(project_id, store.site(project_id, site_id), result)

    @app.post("/api/projects/{project_id}/sites/{site_id}/urls/remove")
    def remove_urls(
        project_id: str, site_id: str, body: UrlsRemove, token: TokenHeader = None
    ) -> RemovedOut:
        require_token(project_id, token)
        site(project_id, site_id)
        removed = store.remove_urls(project_id, site_id, None if body.all else body.urls)
        return RemovedOut(removed=removed, url_count=len(store.urls(project_id, site_id)))

    # -- runs -----------------------------------------------------------------

    @app.get("/api/projects/{project_id}/runs")
    def all_runs(project_id: str) -> list[RunState]:
        project(project_id)
        return store.runs(project_id)

    @app.get("/api/queue")
    def shared_queue() -> QueueOut:
        running, waiting, ready = runner.queue()

        def item(e: QueueEntry) -> QueueItem:
            return QueueItem(
                run=e.state,
                project_name=e.project_name,
                site_name=e.site_name,
                position=e.position,
                wait_s=round(e.wait_s),
            )

        return QueueOut(
            running=[item(e) for e in running],
            waiting=[item(e) for e in waiting],
            ready=[item(e) for e in ready],
            seconds_per_url=round(runner.seconds_per_url(), 1),
            email_sends=runner.email_sends,
            ready_hours=runner.ready_hours,
        )

    @app.post("/api/projects/{project_id}/runs")
    def start_all(
        project_id: str, body: StartIn | None = None, token: TokenHeader = None
    ) -> list[RunState]:
        record = require_token(project_id, token)
        how = body or StartIn()
        try:
            return runner.submit_all(
                project_id,
                started_by=record.owner_name,
                on_turn=how.on_turn,
                email=how.email,
                email_when_done=how.email_when_done,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/api/projects/{project_id}/sites/{site_id}/runs")
    def runs(project_id: str, site_id: str) -> list[RunState]:
        site(project_id, site_id)
        return store.runs(project_id, site_id)

    @app.post("/api/projects/{project_id}/sites/{site_id}/runs")
    def start_run(
        project_id: str, site_id: str, body: StartIn | None = None, token: TokenHeader = None
    ) -> RunState:
        record = require_token(project_id, token)
        site(project_id, site_id)
        how = body or StartIn()
        try:
            return runner.submit(
                project_id,
                site_id,
                started_by=record.owner_name,
                on_turn=how.on_turn,
                email=how.email,
                email_when_done=how.email_when_done,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}/start")
    def start_ready(
        project_id: str, site_id: str, run_id: str, token: TokenHeader = None
    ) -> RunState:
        require_token(project_id, token)
        run_state(project_id, site_id, run_id)
        try:
            return runner.start_ready(project_id, site_id, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}")
    def get_run(project_id: str, site_id: str, run_id: str) -> RunState:
        return run_state(project_id, site_id, run_id)

    @app.post("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}/cancel")
    def cancel_run(
        project_id: str, site_id: str, run_id: str, token: TokenHeader = None
    ) -> RunState:
        require_token(project_id, token)
        run_state(project_id, site_id, run_id)
        return runner.cancel(project_id, site_id, run_id)

    @app.post("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}/resume")
    def resume_run(
        project_id: str, site_id: str, run_id: str, token: TokenHeader = None
    ) -> RunState:
        require_token(project_id, token)
        run_state(project_id, site_id, run_id)
        try:
            return runner.resume(project_id, site_id, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}/downloads")
    def downloads(project_id: str, site_id: str, run_id: str) -> list[ReportFile]:
        run_state(project_id, site_id, run_id)
        return [
            ReportFile(
                name=name,
                size=path.stat().st_size,
                modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            )
            for name, path in store.report_files(project_id, site_id, run_id).items()
        ]

    @app.get("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}/downloads.zip")
    def download_all(project_id: str, site_id: str, run_id: str) -> FileResponse:
        run_state(project_id, site_id, run_id)
        files = store.report_files(project_id, site_id, run_id)
        if not files:
            raise HTTPException(status_code=404, detail="This run has no reports yet.")
        archive = store.run_dir(project_id, site_id, run_id) / f"{run_id}_reports.zip"
        _zip(archive, files)
        return FileResponse(archive, filename=archive.name, content_disposition_type="attachment")

    @app.get("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}/downloads/{file_name}")
    def download(project_id: str, site_id: str, run_id: str, file_name: str) -> FileResponse:
        run_state(project_id, site_id, run_id)
        # The name is looked up in the folder listing, never joined onto a path.
        path = store.report_files(project_id, site_id, run_id).get(file_name)
        if path is None:
            raise HTTPException(status_code=404, detail="That file is not in this run's reports.")
        return FileResponse(path, filename=file_name, content_disposition_type="attachment")

    # -- project reports (all of the client's websites) ------------------------

    def reports_out(project_id: str) -> ProjectReportsOut:
        record = project(project_id)
        status = build_status(project_id)
        return ProjectReportsOut(
            building=bool(status and status.building),
            building_since=status.started_at if status and status.building else None,
            last_error=status.error if status else "",
            sources=[
                ReportSourceOut.model_validate(s.as_dict()) for s in report_sources(store, record)
            ],
            reports=[
                _report_out(r, project_report_files(store, project_id, r.stamp))
                for r in project_reports(store, project_id)
            ],
        )

    @app.get("/api/projects/{project_id}/reports")
    def list_project_reports(project_id: str) -> ProjectReportsOut:
        return reports_out(project_id)

    @app.post("/api/projects/{project_id}/reports")
    def build_reports(
        project_id: str, body: ProjectReportsIn | None = None, token: TokenHeader = None
    ) -> ProjectReportsOut:
        record = require_token(project_id, token)
        how = body or ProjectReportsIn()
        try:
            start_project_reports(
                store,
                project_id,
                include_competitors=how.include_competitors,
                built_by=record.owner_name,
                wait=how.wait,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return reports_out(project_id)

    @app.get("/api/projects/{project_id}/reports/{stamp}/downloads.zip")
    def download_project_reports(project_id: str, stamp: str) -> FileResponse:
        project(project_id)
        archive = zip_project_report(store, project_id, stamp)
        if archive is None:
            raise HTTPException(status_code=404, detail="There is no such project report.")
        return FileResponse(archive, filename=archive.name, content_disposition_type="attachment")

    @app.get("/api/projects/{project_id}/reports/{stamp}/downloads/{file_name}")
    def download_project_report(project_id: str, stamp: str, file_name: str) -> FileResponse:
        project(project_id)
        path = project_report_files(store, project_id, stamp).get(file_name)
        if path is None:
            raise HTTPException(status_code=404, detail="That file is not in this project report.")
        return FileResponse(path, filename=file_name, content_disposition_type="attachment")

    # -- knowledge (the bot) --------------------------------------------------

    def spaces(selected: list[str] | None = None) -> list[Space]:
        """Every website of every project as a knowledge space (or only the selected)."""
        out_spaces: list[Space] = []
        for p in store.projects():
            for space in spaces_of(p):
                if selected is None or space.namespace in selected:
                    out_spaces.append(space)
        return out_spaces

    @app.get("/api/knowledge/spaces")
    def list_spaces() -> list[SpaceOut]:
        listed: list[SpaceOut] = []
        for space in spaces():
            ready, note = False, "not indexed yet: scrape this website first"
            if knowledge is not None:
                knowledge_store = knowledge.open_store(space)
                try:
                    counts = knowledge_store.counts()
                    model = knowledge_store.meta().get("model")
                finally:
                    knowledge_store.close()
                if model:
                    ready = True
                    note = f"{counts.get('chunk_vectors', 0)} passages, model {model}"
            listed.append(SpaceOut(**space.__dict__, ready=ready, note=note))
        return listed

    def graph() -> WebsiteGraph:
        return WebsiteGraph.build(store, runner.namespace)

    @app.get("/api/knowledge/websites")
    def list_websites() -> list[WebsiteOut]:
        g = graph()
        listed = []
        for w in g.websites():
            fresh = w.freshest()
            listed.append(
                WebsiteOut(
                    key=w.key,
                    name=w.name,
                    domains=sorted(w.domains),
                    projects=sorted({c.project_name for c in w.copies}),
                    competitors=[c.name for k in sorted(w.competitors) if (c := g.get(k))],
                    same_client=[s.name for s in g.sisters_of(w)],
                    note=w.note,
                    tags=w.tags,
                    searchable=fresh is not None,
                    freshest_project=fresh.project_name if fresh else None,
                )
            )
        return listed

    @app.get("/api/knowledge/search")
    def search(
        q: Annotated[str, Query(min_length=2, max_length=500)],
        space: Annotated[list[str] | None, Query()] = None,
        project: Annotated[list[str] | None, Query(alias="project")] = None,
        website: Annotated[str | None, Query(max_length=500)] = None,
        top_k: Annotated[int, Query(ge=1, le=50)] = 8,
        per_site: Annotated[int | None, Query(ge=1, le=20)] = None,
    ) -> SearchOut:
        """Search a scope of knowledge spaces.

        With `website`, the scope is that website, the same client's other websites,
        and every website any of them competes with in any project, each searched
        once in its freshest copy, whatever project asked.
        Otherwise the scope is the given spaces and/or projects.
        """
        if knowledge is None:
            raise HTTPException(status_code=503, detail="The knowledge store is not set up.")
        not_built: list[dict[str, str]] = []
        considered: list[str] = []
        site_of: dict[str, str] = {}
        role_of: dict[str, str] = {}
        if website:
            g = graph()
            target = g.find(website)
            if target is None:
                raise HTTPException(
                    status_code=404, detail=f"No project has a website at {website}."
                )
            scope = []
            family = g.family_keys(target)
            for w in g.competitive_scope(target):
                considered.append(w.name)
                fresh = w.freshest()
                if fresh is None:
                    not_built.append(
                        {
                            "space": w.key,
                            "site": w.name,
                            "reason": "not scraped yet in any project",
                        }
                    )
                    continue
                scope.append(fresh.space())
                site_of[fresh.namespace] = w.key
                # Relative to the website asked about: its sister websites are the client's.
                role_of[fresh.namespace] = "client" if w.key in family else "competitor"
        else:
            scope = spaces(space)
            if project:
                scope = [s for s in scope if s.project_id in project]
            if not scope:
                raise HTTPException(status_code=404, detail="None of those websites exist.")
            considered = [s.site_name for s in scope]
        result = federated_search(
            scope,
            q,
            open_store=knowledge.open_store,
            embedder_for=knowledge.embedder_for,
            top_k=top_k,
            per_site=per_site,
        )
        return SearchOut(
            query=q,
            scope=considered,
            hits=[
                HitOut(
                    rank=h.rank,
                    score=round(h.score, 6),
                    website=site_of.get(h.space.namespace, h.space.site_name),
                    project_id=h.space.project_id,
                    project_name=h.space.project_name,
                    site_id=h.space.site_id,
                    site_name=h.space.site_name,
                    role=role_of.get(h.space.namespace, h.space.role),
                    url=h.hit.chunk.url,
                    headings=h.hit.chunk.heading_path,
                    text=h.hit.chunk.verbatim_text,
                    chunk_id=h.hit.chunk.chunk_id,
                    evidence_ids=h.hit.chunk.evidence_ids,
                )
                for h in result.hits
            ],
            searched=[s.namespace for s in result.searched],
            skipped=not_built
            + [{"space": s.namespace, "site": s.site_name, "reason": r} for s, r in result.skipped],
            notes=[f"{s.site_name}: {why}" for s, why in [*result.words_only, *result.lighter]],
        )


_ZIP_LOCK = threading.Lock()


def _zip(archive: Path, files: dict[str, Path]) -> None:
    """Make the archive unless it is newer than every file in it.

    One archive is written at a time, through a temporary file of its own, so two
    downloads at once never share a half-written file; the second finds it made.
    """
    newest = max(p.stat().st_mtime for p in files.values())
    with _ZIP_LOCK:
        if archive.is_file() and archive.stat().st_mtime >= newest:
            return
        fd, tmp_name = tempfile.mkstemp(
            dir=archive.parent, prefix=f".{archive.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with (
                os.fdopen(fd, "wb") as handle,
                zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as bundle,
            ):
                for name, path in files.items():
                    bundle.write(path, arcname=name)
            os.replace(tmp, archive)
        finally:
            tmp.unlink(missing_ok=True)
