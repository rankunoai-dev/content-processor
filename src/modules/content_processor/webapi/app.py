"""The analyst API and the built web app, served from one process.

Every route is GET except one: `POST /api/runs/{run}/decisions`, which appends
a reviewer's decision to the run's decisions log (`export/decisions.py`). That
write is accepted only as JSON carrying the `X-Requested-With: content-processor`
header (a page on another site cannot send it without a cross-origin grant,
which this server never gives) and, when the browser sends an Origin, only
from a local host; every other method or path is refused as read-only. The
server binds 127.0.0.1 (see `__main__`), accepts only
local Host headers (DNS-rebinding guard) and sends a strict
Content-Security-Policy. Downloads are limited to the files that exist in the
run's reports folder; a name is matched against the folder listing, never
joined onto a path, so `..` and absolute paths cannot escape it.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from src.modules.content_processor.content_types import catalogue, scope_fragment
from src.modules.content_processor.export.bundle import BUNDLE_FILES, bundle_file_name
from src.modules.content_processor.export.decisions import (
    Decision,
    append_decision,
    decisions_path,
)
from src.modules.content_processor.webapi import reports
from src.modules.content_processor.webapi.models import (
    ContradictionsResponse,
    DecisionIn,
    DecisionOut,
    DecisionsResponse,
    DownloadFile,
    DownloadsResponse,
    DuplicatesResponse,
    Overview,
    PageDetail,
    PagesResponse,
    ReviewResponse,
    RunsResponse,
)
from src.modules.content_processor.webapi.store import (
    PageQuery,
    RunData,
    RunNotFoundError,
    RunStore,
    SortField,
)

__all__ = ["DEFAULT_ALLOWED_HOSTS", "WRITE_HEADER", "WRITE_HEADER_VALUE", "create_app"]

WRITE_HEADER = "X-Requested-With"
WRITE_HEADER_VALUE = "content-processor"
_DECISIONS_PATH = re.compile(r"^/api/runs/[^/]+/decisions$")

DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _split(values: Sequence[str] | None) -> list[str]:
    """Accept both `?status=A&status=B` and `?status=A,B`."""
    out: list[str] = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def create_app(
    exports_dir: Path,
    *,
    static_dir: Path | None = None,
    allowed_hosts: Sequence[str] = DEFAULT_ALLOWED_HOSTS,
) -> FastAPI:
    """Build the app for one exports folder (and, optionally, the built web app)."""
    store = RunStore(exports_dir)
    app = FastAPI(
        title="Content processor analyst API",
        version="1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in ("GET", "HEAD"):
            refusal = _write_refusal(request, allowed_hosts)
            if refusal is not None:
                return refusal
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def load(name: str) -> RunData:
        try:
            return store.get(name)
        except RunNotFoundError:
            raise HTTPException(
                status_code=404,
                detail=f"There is no run called '{name}' in the exports folder.",
            ) from None

    @app.get("/api/runs")
    def list_runs() -> RunsResponse:
        runs = [store.summary(store.get(n)) for n in store.names()]
        return RunsResponse(exports_dir=str(exports_dir), runs=runs)

    @app.get("/api/runs/{run}/overview")
    def overview(run: str) -> Overview:
        return store.overview(load(run))

    @app.get("/api/runs/{run}/pages")
    def pages(
        run: str,
        status: Annotated[list[str] | None, Query()] = None,
        host: Annotated[list[str] | None, Query()] = None,
        content_type: Annotated[list[str] | None, Query(alias="format")] = None,
        issue: Annotated[list[str] | None, Query()] = None,
        has_issues: bool | None = None,
        q: Annotated[str, Query(max_length=200)] = "",
        sort: SortField = "status",
        order: Literal["asc", "desc"] = "asc",
        page: Annotated[int, Query(ge=1, le=100_000)] = 1,
        size: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> PagesResponse:
        query = PageQuery(
            status=_split(status),
            host=_split(host),
            content_type=_split(content_type),
            issue=_split(issue),
            has_issues=has_issues,
            q=q,
            sort=sort,
            order=order,
            page=page,
            size=size,
        )
        return store.pages(load(run), query)

    @app.get("/api/runs/{run}/pages/{page_key}")
    def page_detail(run: str, page_key: str) -> PageDetail:
        detail = store.detail(load(run), page_key)
        if detail is None:
            raise HTTPException(status_code=404, detail="This page is not part of the run.")
        return detail

    @app.get("/api/runs/{run}/pages/{page_key}/raw")
    def page_raw(run: str, page_key: str) -> dict[str, Any]:
        raw = store.raw(load(run), page_key)
        if raw is None:
            raise HTTPException(status_code=404, detail="This page is not part of the run.")
        return raw

    @app.get("/api/runs/{run}/duplicates")
    def duplicates(run: str) -> DuplicatesResponse:
        return reports.duplicates(load(run))

    @app.get("/api/runs/{run}/contradictions")
    def contradictions(run: str) -> ContradictionsResponse:
        data = load(run)
        return reports.with_contradiction_decisions(
            reports.contradictions(data), reports.decision_log(data)
        )

    @app.get("/api/runs/{run}/review")
    def review(run: str) -> ReviewResponse:
        data = load(run)
        return reports.with_review_decisions(reports.review(data), reports.decision_log(data))

    @app.get("/api/runs/{run}/decisions")
    def decisions(run: str) -> DecisionsResponse:
        data = load(run)
        log = reports.decision_log(data)
        return DecisionsResponse(
            path=decisions_path(data.path.parent, data.name).name,
            items=[DecisionOut.model_validate(d.model_dump()) for d in reversed(log)],
        )

    @app.post("/api/runs/{run}/decisions")
    def save_decision(run: str, body: DecisionIn) -> DecisionOut:
        data = load(run)
        if body.kind == "contradiction":
            known = {i.pair_id for i in reports.contradictions(data).items}
        else:
            known = {i.url for i in reports.review(data).items}
        if body.key not in known:
            raise HTTPException(status_code=404, detail="That item is not part of this run.")
        try:
            decision = Decision(
                reviewer=body.reviewer.strip(),
                kind=body.kind,
                key=body.key,
                decision=body.decision.strip().lower(),
                note=body.note.strip(),
            )
        except ValidationError as exc:
            reason = exc.errors()[0].get("msg", "invalid decision")
            raise HTTPException(status_code=422, detail=f"Not saved: {reason}.") from None
        append_decision(decisions_path(data.path.parent, data.name), decision)
        return DecisionOut.model_validate(decision.model_dump())

    def report_files(data: RunData) -> dict[str, Path]:
        folder = data.reports_dir
        if not folder.is_dir():
            return {}
        return {p.name: p for p in sorted(folder.iterdir()) if p.is_file()}

    @app.get("/api/runs/{run}/downloads")
    def downloads(run: str) -> DownloadsResponse:
        data = load(run)
        files = report_files(data)
        return DownloadsResponse(
            folder=data.reports_dir.name if data.reports_dir.is_dir() else None,
            files=[
                DownloadFile.model_validate(
                    {"name": n, "size": p.stat().st_size, "modified_at": p.stat().st_mtime}
                )
                for n, p in files.items()
            ],
        )

    @app.get("/api/runs/{run}/downloads/{file_name}")
    def download(run: str, file_name: str) -> FileResponse:
        path = report_files(load(run)).get(file_name)
        if path is None:
            raise HTTPException(status_code=404, detail="That file is not in this run's reports.")
        # Reports from different runs must not share a name once downloaded: files
        # written before run names were added get the run's name in front.
        download_name = file_name
        if file_name in BUNDLE_FILES:
            download_name = bundle_file_name(run, file_name)
        return FileResponse(path, filename=download_name, content_disposition_type="attachment")

    @app.get("/api/content-types")
    def content_types() -> dict[str, Any]:
        """The content types a run can select, with what the engine supports (read-only)."""
        return {"types": [t.model_dump() for t in catalogue()]}

    @app.get("/api/content-types/scope")
    def content_type_scope(
        types: Annotated[str, Query(max_length=300, description="comma-separated ids")] = "",
    ) -> dict[str, Any]:
        """The scope filter and command for a selection; nothing is started (read-only)."""
        selected = [t.strip() for t in types.split(",") if t.strip()]
        try:
            return scope_fragment(selected).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/{rest:path}")
    def api_not_found(rest: str) -> JSONResponse:
        return JSONResponse({"detail": "Unknown API address."}, status_code=404)

    _mount_web_app(app, static_dir)
    return app


def _mount_web_app(app: FastAPI, static_dir: Path | None) -> None:
    """Serve the built app: real files from the build folder, index.html for app routes."""
    root = static_dir.resolve() if static_dir is not None else None
    index = root / "index.html" if root is not None else None

    def listing() -> dict[str, Path]:
        if root is None or not root.is_dir():
            return {}
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}

    files = listing()

    @app.get("/{rest:path}", include_in_schema=False)
    def web_app(rest: str) -> Response:
        nonlocal files
        if index is None or not index.is_file():
            return JSONResponse(
                {"detail": "The web app is not built. Run `npm run build` in webui/."},
                status_code=404,
            )
        if rest and rest not in files and "." in rest.rsplit("/", 1)[-1]:
            files = listing()  # the app was rebuilt while the server ran
        path = files.get(rest)
        if path is not None:
            cache = (
                "public, max-age=31536000, immutable" if rest.startswith("assets/") else "no-cache"
            )
            return FileResponse(path, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _write_refusal(request: Request, allowed_hosts: Sequence[str]) -> JSONResponse | None:
    """None when this non-GET request is an acceptable decision save, else the refusal."""
    if request.method != "POST" or not _DECISIONS_PATH.match(request.url.path):
        return JSONResponse({"detail": "This server is read-only."}, status_code=405)
    if request.headers.get(WRITE_HEADER) != WRITE_HEADER_VALUE:
        return JSONResponse(
            {"detail": "Decisions can only be saved from the web app."}, status_code=403
        )
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"detail": "Decisions must be sent as JSON."}, status_code=415)
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).hostname not in set(allowed_hosts):
        return JSONResponse({"detail": "Decisions can only be saved locally."}, status_code=403)
    return None
