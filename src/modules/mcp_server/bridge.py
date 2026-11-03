"""The web app side of the MCP server: runs the read-only tools and keeps their ledger.

The stdio server Claude Desktop starts never touches the app data; it posts each call
here, so the web app stays the only writer. Every reference a tool hands out is kept in a
per-project ledger on disk (`<app data>/mcp/ledgers/<project>.json`), so
`check_deliverable` still knows them after Claude Desktop or the app restarts.

GET  /api/mcp/tools   the tool list (names, descriptions, input schemas)
POST /api/mcp/call    {"tool": ..., "arguments": {...}} -> {"text": ..., "is_error": ...}
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import Field

from src.core.logger import get_logger
from src.core.schemas import StrictModel
from src.modules.analyst.playground import EvidenceLedger, LedgerEntry, Playground, ScopeError
from src.modules.analyst.tools import run_tool
from src.modules.analyst.verify import sources_section, verify_deliverable
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.mcp_server.catalog import (
    MAX_DELIVERABLE_CHARS,
    SCOPES,
    Scope,
    definitions,
    parse_scope,
    scope_label,
    scope_of,
)

__all__ = ["CallIn", "CallOut", "McpBridge", "register_mcp_routes"]

_log = get_logger(__name__)
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class CallIn(StrictModel):
    """One tool call from the stdio server."""

    tool: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    scope: list[str] | None = Field(
        default=None, max_length=10, description="Modules this connection may use; None: all"
    )


class CallOut(StrictModel):
    """The tool's JSON text, and whether it is an error."""

    text: str
    is_error: bool


def _error(message: str) -> tuple[str, bool]:
    return json.dumps({"error": message}, ensure_ascii=False), True


# What list_projects shows per module (the rest of each project's status is left out).
_STATUS_FIELDS: dict[str, tuple[str, ...]] = {
    "content": ("finished_scrapes", "last_scrape", "searchable_scrapes"),
    "themes": ("themes_built", "themes"),
    "monitoring": ("last_monitoring_check", "alerts", "high_alerts"),
    "research": ("research_added", "keywords"),
    "gaps": ("gaps_built", "findings", "open_findings", "high_findings"),
}
_COMMON_FIELDS = ("project_id", "name", "client", "websites")


def _scope(keys: list[str] | None) -> Scope:
    if keys is None:
        return None
    unknown = sorted(set(keys) - SCOPES.keys())
    if unknown:
        msg = f"unknown module(s) {', '.join(unknown)}"
        raise ValueError(msg)
    return frozenset(keys)


class McpBridge:
    """Runs tools for Claude Desktop against the projects, one ledger per project."""

    def __init__(self, projects: ProjectStore, access: KnowledgeAccess) -> None:
        """Bind to the app's projects and knowledge spaces."""
        self.projects = projects
        self.access = access
        self.root = Path(str(projects.root)) / "mcp" / "ledgers"
        self._lock = threading.Lock()
        self._names = {d.name for d in definitions()}

    # -- ledger ------------------------------------------------------------------------

    def _ledger_path(self, project_id: str) -> Path:
        return self.root / f"{project_id}.json"

    def ledger(self, project_id: str) -> EvidenceLedger:
        """Everything the tools have shown Claude for this project (empty at first)."""
        ledger = EvidenceLedger()
        path = self._ledger_path(project_id)
        with self._lock:
            if path.exists():
                for ref, entry in json.loads(path.read_text(encoding="utf-8")).items():
                    ledger.entries[ref] = LedgerEntry(**entry)
        return ledger

    def _remember(self, project_id: str, shown: EvidenceLedger) -> None:
        if not shown.entries:
            return
        path = self._ledger_path(project_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            data.update({ref: asdict(entry) for ref, entry in shown.entries.items()})
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)

    # -- tools -------------------------------------------------------------------------

    def call(
        self, tool: str, arguments: dict[str, Any], scope: list[str] | None = None
    ) -> tuple[str, bool]:
        """Run one tool within the connection's modules; returns (JSON text, is_error)."""
        try:
            allowed = _scope(scope)
        except ValueError as exc:
            return _error(str(exc))
        if tool not in self._names:
            return _error(f"unknown tool {tool!r}; the tools are: {', '.join(sorted(self._names))}")
        module = scope_of(tool)
        if allowed is not None and module is not None and module not in allowed:
            return _error(
                f"this connection is limited to {scope_label(allowed)}; {tool} belongs to "
                f"{scope_label(frozenset({module}))}. Do not use another module here: tell the "
                "user which module the question needs."
            )
        if tool == "list_projects":
            listed = self.list_projects(allowed)
            return json.dumps({"projects": listed}, ensure_ascii=False), False
        args = dict(arguments)
        project_id = str(args.pop("project_id", "") or "")
        if not _PROJECT_ID.match(project_id):
            return _error("project_id is required; call list_projects for the ids")
        try:
            self.projects.get(project_id)
        except ProjectNotFoundError:
            return _error(f"there is no project {project_id!r}; call list_projects for the ids")
        if tool == "check_deliverable":
            return self.check(project_id, args)
        try:
            play = Playground(self.projects, self.access, [project_id])
            text, is_error = run_tool(play, tool, args)
        except ScopeError as exc:
            return _error(str(exc))
        except Exception as exc:  # noqa: BLE001 - one failing tool must not stop the server
            _log.exception("mcp tool %s failed", tool)
            return _error(f"the tool failed ({type(exc).__name__}); see the app's log")
        if not is_error:
            self._remember(project_id, play.ledger)
        return text, is_error

    def check(self, project_id: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Verify a deliverable against everything shown for this project."""
        text = args.pop("text", None)
        if args:
            return _error(f"unexpected arguments: {', '.join(sorted(args))}")
        if not isinstance(text, str) or not text.strip():
            return _error("text is required: the full analysis with its citations")
        if len(text) > MAX_DELIVERABLE_CHARS:
            return _error(f"text is longer than {MAX_DELIVERABLE_CHARS} characters")
        ledger = self.ledger(project_id)
        result = verify_deliverable(text, ledger)
        payload = {
            "status": "passed" if result.passed else "needs review",
            **result.model_dump(),
            "sources_markdown": sources_section(text, ledger, result),
            "how_to_fix": (
                "Unknown citations were not returned by a tool for this project: call the tool "
                "again or remove them. Failed quotes must be copied word for word. Cite or remove "
                "uncited lines (a few are allowed)."
            ),
        }
        return json.dumps(payload, ensure_ascii=False), False

    def list_projects(self, scope: Scope = None) -> list[dict[str, Any]]:
        """Projects with their websites and which results exist, with dates (for `scope`)."""
        from src.modules.workspace.status import project_statuses

        statuses = project_statuses(self.projects)
        if scope is None:
            return statuses
        keep = set(_COMMON_FIELDS)
        for key in scope:
            keep |= set(_STATUS_FIELDS[key])
        return [{k: v for k, v in s.items() if k in keep} for s in statuses]


def register_mcp_routes(app: FastAPI, *, projects: ProjectStore, access: KnowledgeAccess) -> None:
    """Add the MCP bridge routes (before the app's catch-all routes)."""
    bridge = McpBridge(projects, access)

    @app.get("/api/mcp/tools")
    def tools(modules: str | None = None) -> list[dict[str, Any]]:
        try:
            scope = parse_scope(modules)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return [asdict(d) for d in definitions(scope)]

    @app.post("/api/mcp/call")
    def call(body: CallIn) -> CallOut:
        text, is_error = bridge.call(body.tool, body.arguments, body.scope)
        return CallOut(text=text, is_error=is_error)
