"""HTTP routes of the action-card engine (served by the analyst web app's server).

    GET  /api/analyst/cards                                   the library, with availability
    GET  /api/analyst/cards/{card}                            one card in full
    POST /api/projects/{project}/analyst/preflight            {card, inputs, backend}
    POST /api/projects/{project}/analyst/runs                 {card, inputs, backend}
    GET  /api/projects/{project}/analyst/runs                 the project's runs
    GET  /api/projects/{project}/analyst/runs/{run}           record, deliverable, events
    GET  /api/projects/{project}/analyst/runs/{run}/events    live progress (Server-Sent Events)

Starting a run writes files and may spend money, so it needs the project's unlock
token (as every project change does) and passes the app's write guard. Runs go to
one background worker, one at a time; a run that was queued or running when the
server stopped is marked interrupted at the next start, never left "running".
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import Field
from starlette.background import BackgroundTask

from src.core.schemas import StrictModel
from src.modules.analyst.library import CARDS, card
from src.modules.analyst.models import ActionCard, Backend, Preflight, RunRecord
from src.modules.analyst.runs import CardRunner
from src.modules.analyst.tools import TOOLS
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectNotFoundError
from src.modules.reports.writers import markdown_to_docx, word_available

__all__ = ["AnalystWorker", "register_analyst_routes"]

TokenHeader = Annotated[str | None, Header(alias="X-Project-Token")]
_MAX_TEXT = 2_000_000
_EVENT_POLL_S = 0.5
_EVENT_TIMEOUT_S = 1_800.0


class CardSummary(StrictModel):
    """A card as the library lists it."""

    id: str
    title: str
    module: str
    category: str
    icon: str
    summary: str
    deliverable: str
    available: bool
    needs_modules: list[str] = Field(default_factory=list)


class RunRequest(StrictModel):
    """Run (or preflight) a card for this project."""

    card: str = Field(min_length=1, max_length=80)
    inputs: dict[str, str | list[str] | None] = Field(default_factory=dict)
    backend: Backend = "pack"


class RunDetail(StrictModel):
    """A run with its deliverable (or context pack) and its events."""

    record: RunRecord
    text: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


def _summary(c: ActionCard) -> CardSummary:
    missing = [t for t in c.required_tools if not TOOLS[t].built]
    return CardSummary(
        id=c.id,
        title=c.title,
        module=c.module,
        category=c.category,
        icon=c.icon,
        summary=c.summary,
        deliverable=c.deliverable,
        available=not missing,
        needs_modules=sorted({TOOLS[t].module for t in missing}),
    )


class AnalystWorker:
    """Runs queued cards one at a time in a background thread."""

    def __init__(self, runner: CardRunner) -> None:
        """Mark runs interrupted by an earlier stop, then accept new ones."""
        self.runner = runner
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyst")
        self._lock = threading.Lock()
        self.recover()

    def recover(self) -> int:
        """Runs left queued or running by a stopped server: mark them interrupted."""
        count = 0
        for record in self.runner.runs.all():
            if record.status in ("queued", "running"):
                self.runner.runs.save(
                    record.model_copy(
                        update={
                            "status": "failed",
                            "message": "interrupted: the server stopped during this run",
                            "finished_at": datetime.now(UTC),
                        }
                    )
                )
                count += 1
        return count

    def submit(self, chosen: ActionCard, record: RunRecord) -> None:
        """Queue a run."""
        self._pool.submit(self.runner.execute, chosen, record)

    def shutdown(self) -> None:
        """Stop accepting runs (the running one finishes)."""
        self._pool.shutdown(wait=False, cancel_futures=True)


def register_analyst_routes(
    app: FastAPI, *, runner: CardRunner, signer: TokenSigner, worker: AnalystWorker | None = None
) -> AnalystWorker:
    """Add the card routes to the app (before its catch-all routes)."""
    pool = worker or AnalystWorker(runner)

    def chosen_card(card_id: str) -> ActionCard:
        try:
            return card(card_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="There is no such action card.") from None

    def project(project_id: str) -> None:
        try:
            runner.store.get(project_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    def run_of(project_id: str, run_id: str) -> RunRecord:
        project(project_id)
        try:
            record = runner.runs.get(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="There is no such run.") from None
        if project_id not in record.project_ids:
            raise HTTPException(status_code=404, detail="There is no such run.")
        return record

    @app.get("/api/analyst/cards")
    def list_cards() -> list[CardSummary]:
        return [_summary(c) for c in CARDS]

    @app.get("/api/analyst/cards/{card_id}")
    def get_card(card_id: str) -> ActionCard:
        return chosen_card(card_id)

    @app.post("/api/projects/{project_id}/analyst/preflight")
    def preflight(project_id: str, body: RunRequest) -> Preflight:
        project(project_id)
        check, _play = runner.preflight(
            chosen_card(body.card), {**body.inputs, "project": project_id}, body.backend
        )
        return check

    @app.post("/api/projects/{project_id}/analyst/runs")
    def start_run(project_id: str, body: RunRequest, token: TokenHeader = None) -> RunRecord:
        project(project_id)
        if not signer.check(token, project_id):
            raise HTTPException(
                status_code=401, detail="Unlock this project with its password to run cards."
            )
        chosen = chosen_card(body.card)
        record = runner.start(chosen, {**body.inputs, "project": project_id}, body.backend)
        if record.status == "queued":
            pool.submit(chosen, record)
        return record

    @app.get("/api/projects/{project_id}/analyst/runs")
    def list_runs(project_id: str) -> list[RunRecord]:
        project(project_id)
        return [r for r in runner.runs.all() if project_id in r.project_ids]

    @app.get("/api/projects/{project_id}/analyst/runs/{run_id}")
    def get_run(project_id: str, run_id: str) -> RunDetail:
        record = run_of(project_id, run_id)
        folder = runner.runs.folder(run_id)
        name = record.files.get("deliverable") or record.files.get("context_pack")
        text = None
        if name and (folder / name).exists():
            text = (folder / name).read_text(encoding="utf-8")[:_MAX_TEXT]
        return RunDetail(record=record, text=text, events=runner.runs.events(run_id))

    @app.get("/api/projects/{project_id}/analyst/runs/{run_id}/result/{file_name}")
    def run_result(project_id: str, run_id: str, file_name: str) -> FileResponse:
        record = run_of(project_id, run_id)
        folder = runner.runs.folder(run_id)
        name = record.files.get("deliverable") or record.files.get("context_pack")
        if not name or not (folder / name).exists():
            raise HTTPException(status_code=404, detail="This run has no result to download yet.")
        source = folder / name
        title = f"{chosen_card(record.card_id).title} ({run_id})"
        stem = f"{record.card_id}_{run_id}_{name.removesuffix('.md')}"
        if file_name == "result.md":
            return FileResponse(
                source,
                filename=f"{stem}.md",
                media_type="text/markdown",
                content_disposition_type="attachment",
            )
        if file_name != "result.docx" or not word_available():
            raise HTTPException(status_code=404, detail="There is no such download.")
        out = Path(tempfile.mkdtemp(prefix="cp-card-"))
        path = markdown_to_docx(source.read_text(encoding="utf-8"), out / stem, title)
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content_disposition_type="attachment",
            background=BackgroundTask(shutil.rmtree, out, ignore_errors=True),
        )

    @app.get("/api/projects/{project_id}/analyst/runs/{run_id}/events")
    async def run_events(project_id: str, run_id: str) -> StreamingResponse:
        run_of(project_id, run_id)

        async def stream() -> AsyncIterator[str]:
            sent = 0
            waited = 0.0
            while waited < _EVENT_TIMEOUT_S:
                events = runner.runs.events(run_id, after=sent)
                for event in events:
                    sent += 1
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                    if event.get("type") == "end":
                        return
                await asyncio.sleep(_EVENT_POLL_S)
                waited += _EVENT_POLL_S
            yield 'data: {"type": "timeout"}\n\n'

        return StreamingResponse(stream(), media_type="text/event-stream")

    return pool
