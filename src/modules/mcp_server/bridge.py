"""The web app side of the MCP server: runs the tools and keeps their ledger.

The stdio server Claude Desktop starts never touches the app data; it posts each call
here, so the web app stays the only writer. Every reference a tool hands out is kept in a
per-project ledger on disk (`<app data>/mcp/ledgers/<project>.json`), so
`check_deliverable` still knows them after Claude Desktop or the app restarts.
`save_deliverable` checks an answer the same way and stores it in the analyst run history
(`src.modules.analyst.desktop`), the one thing a connection may write.

Confidential projects are closed to Claude Desktop entirely, because everything a tool
returns goes to Anthropic: no tool runs for them, `list_projects` leaves them out (only
their number is given), and a project whose websites are read from a confidential
project's scrape (the freshest copy of a shared website) is closed too. A project is
confidential when its "Confidential" option is on or the analyst data policy file says
`client_confidential`; a project whose record cannot be read counts as confidential.

GET  /api/mcp/tools    the tool list (names, descriptions, input schemas)
POST /api/mcp/call     {"tool": ..., "arguments": {...}} -> {"text": ..., "is_error": ...}
GET  /api/mcp/blocked  {"projects": [ids closed to Claude Desktop]} (for the stdio
                       server's own tools: theme pages and the scrape queue)
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field

from src.core.logger import get_logger
from src.core.schemas import StrictModel
from src.modules.accounts.store import role_at_least
from src.modules.analyst.desktop import DESKTOP_AUTHOR, save_deliverable
from src.modules.analyst.library import card as card_of
from src.modules.analyst.playground import EvidenceLedger, LedgerEntry, Playground, ScopeError
from src.modules.analyst.policy import DataPolicy
from src.modules.analyst.runs import RunStore
from src.modules.analyst.tools import run_tool
from src.modules.analyst.verify import readable_version, sources_section, verify_deliverable
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.mcp_server.catalog import (
    MAX_DELIVERABLE_CHARS,
    MAX_TITLE_CHARS,
    SCOPES,
    Scope,
    definitions,
    parse_scope,
    scope_label,
    scope_of,
)

__all__ = ["CallIn", "CallOut", "McpBridge", "register_mcp_routes"]

WRITING_TOOLS = frozenset({"save_deliverable"})
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
_COMMON_FIELDS = ("project_id", "name", "client", "websites", "confidential")


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

    def __init__(
        self, projects: ProjectStore, access: KnowledgeAccess, runs: RunStore | None = None
    ) -> None:
        """Bind to the app's projects, knowledge spaces and (for saving) the analyst runs."""
        self.projects = projects
        self.access = access
        self.runs = runs
        self.root = Path(str(projects.root)) / "mcp" / "ledgers"
        self.policy = DataPolicy(Path(str(projects.root)) / "analyst", projects)
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

    # -- confidential projects --------------------------------------------------------

    def confidential(self, project_id: str) -> bool:
        """The project's own option or the data policy file; unreadable counts as confidential."""
        try:
            self.projects.get(project_id)
            return self.policy.data_class(project_id) == "client_confidential"
        except Exception:  # noqa: BLE001 - fail closed: when in doubt, nothing goes out
            return True

    def closed_because(self, project_id: str) -> str | None:
        """Why Claude Desktop may not use this project (None when it may).

        Closed when the project is confidential, or when one of the websites it reads
        (its own and their competitors) is read from a confidential project's scrape.
        The confidential project is never named.
        """
        if self.confidential(project_id):
            return "own"
        try:
            play = Playground(self.projects, self.access, [project_id])
            for website in play.websites():
                fresh = website.freshest()
                if (
                    fresh is not None
                    and fresh.project_id != project_id
                    and self.confidential(fresh.project_id)
                ):
                    return website.name
        except Exception:  # noqa: BLE001 - fail closed
            _log.exception("mcp confidential check failed for %s", project_id)
            return "unknown"
        return None

    def blocked(self) -> list[str]:
        """Every project closed to Claude Desktop."""
        return [p.project_id for p in self.projects.projects() if self.closed_because(p.project_id)]

    @staticmethod
    def _closed(name: str, why: str) -> tuple[str, bool]:
        if why == "own":
            reason = f"the project {name!r} is confidential"
        elif why == "unknown":
            reason = f"whether the project {name!r} uses confidential data could not be checked"
        else:
            reason = f"the project {name!r} reads {why} from the scrape of a confidential project"
        return _error(
            f"{reason}, so Claude Desktop gets nothing from it: no tool runs for it. Tell the "
            "user plainly that this project can only be worked on in the app itself, and do not "
            "guess its contents."
        )

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
            return json.dumps(self.list_projects(allowed), ensure_ascii=False), False
        args = dict(arguments)
        project_id = str(args.pop("project_id", "") or "")
        if not _PROJECT_ID.match(project_id):
            return _error("project_id is required; call list_projects for the ids")
        try:
            record = self.projects.get(project_id)
        except ProjectNotFoundError:
            return _error(f"there is no project {project_id!r}; call list_projects for the ids")
        why = self.closed_because(project_id)
        if why is not None:
            return self._closed(record.name, why)
        if tool == "check_deliverable":
            return self.check(project_id, args)
        if tool == "present_answer":
            return self.present(project_id, args)
        if tool == "save_deliverable":
            return self.save(project_id, args, allowed)
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

    def present(self, project_id: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Check a structured answer and render it with its type's template."""
        from pydantic import ValidationError

        from src.modules.mcp_server.templates import PresentInput, render_answer

        try:
            answer = PresentInput.model_validate(args)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
            )
            return _error(f"invalid answer: {problems}")
        name = self.projects.get(project_id).name
        markdown, check = render_answer(answer, self.ledger(project_id), name)
        payload = {
            "status": "passed" if check.passed else "needs review",
            "rendered_markdown": markdown,
            "unknown_refs": check.unknown_refs,
            "quotes_failed": check.quotes_failed,
            "uncited_lines": check.uncited_lines,
            "how_to_present": (
                "Show rendered_markdown to the user exactly as it is."
                if check.passed
                else "Fix the unknown references, failed quotes and uncited lines, then call "
                "present_answer again; show rendered_markdown once it passes."
            ),
        }
        return json.dumps(payload, ensure_ascii=False), False

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
            "readable_markdown": readable_version(text, ledger),
            "sources_markdown": sources_section(text, ledger, result),
            "how_to_present": (
                "Show the user readable_markdown (numbered sources, no reference codes), not the "
                "text you sent, and say in one plain line whether the check passed."
            ),
            "how_to_fix": (
                "Unknown citations were not returned by a tool for this project: call the tool "
                "again or remove them. Failed quotes must be copied word for word. Cite or remove "
                "uncited lines (a few are allowed)."
            ),
        }
        return json.dumps(payload, ensure_ascii=False), False

    def save(self, project_id: str, args: dict[str, Any], scope: Scope) -> tuple[str, bool]:
        """Check an answer written in Claude Desktop and keep it in the analyst run history.

        The check never blocks the save (a deliverable that needs review is stored with
        that result). Confidential projects never get here: every tool is refused for them.
        """
        if self.runs is None:
            return _error(
                "this app runs without the analyst module, so deliverables cannot be saved; "
                "start it with its projects (python -m src.modules.content_processor.webapi)"
            )
        title = args.pop("title", None)
        markdown = args.pop("markdown", None)
        card_key = args.pop("card_key", None)
        module = args.pop("module", None)
        if args:
            return _error(f"unexpected arguments: {', '.join(sorted(args))}")
        if not isinstance(title, str) or not title.strip():
            return _error("title is required: a short name for the deliverable")
        if len(title) > MAX_TITLE_CHARS:
            return _error(f"title is longer than {MAX_TITLE_CHARS} characters")
        if not isinstance(markdown, str) or not markdown.strip():
            return _error("markdown is required: the full answer with its citations")
        if len(markdown) > MAX_DELIVERABLE_CHARS:
            return _error(f"markdown is longer than {MAX_DELIVERABLE_CHARS} characters")
        card = None
        if card_key is not None:
            try:
                card = card_of(str(card_key))
            except KeyError:
                return _error(
                    f"there is no action card {card_key!r}; leave card_key out unless the user "
                    "asked for a card"
                )
        if module is not None:
            if module not in SCOPES:
                return _error(f"unknown module {module!r}; the modules are {', '.join(SCOPES)}")
            if scope is not None and module not in scope:
                return _error(
                    f"this connection is limited to {scope_label(scope)}; it cannot save under "
                    f"{scope_label(frozenset({str(module)}))}"
                )
        if module is None and scope is not None and len(scope) == 1:
            module = next(iter(scope))
        author = DESKTOP_AUTHOR if scope is None else f"{DESKTOP_AUTHOR} ({scope_label(scope)})"
        ledger = self.ledger(project_id)
        try:
            record, check, readable = save_deliverable(
                self.runs,
                project_id=project_id,
                title=title,
                markdown=markdown,
                ledger=ledger,
                author=author,
                card=card,
                module=str(module) if module is not None else None,
            )
        except ValueError as exc:
            return _error(str(exc))
        except OSError as exc:
            _log.exception("save_deliverable failed")
            return _error(f"the deliverable could not be saved ({type(exc).__name__})")
        status = "passed" if check.passed else "needs review"
        where = f'the Analyst cards tab of the project, as "{record.title}"'
        if record.version > 1:
            where += f" (version {record.version})"
        payload = {
            "status": status,
            "saved": True,
            "run_id": record.run_id,
            "title": record.title,
            "version": record.version,
            "versions": len(record.versions),
            "author": record.author,
            "saved_at": record.finished_at.isoformat() if record.finished_at else None,
            **check.model_dump(),
            "readable_markdown": readable,
            "sources_markdown": sources_section(markdown, ledger, check),
            "how_to_present": (
                f"Tell the user in one plain line that the answer is saved in {where} and that "
                f"the check {status}. Show readable_markdown, never the reference codes."
            ),
            "how_to_fix": (
                "It is saved as it is. If the check needs review, fix the unknown citations, "
                "failed quotes or uncited lines and save again with the same title: that adds a "
                "new version."
            ),
        }
        return json.dumps(payload, ensure_ascii=False), False

    def list_projects(self, scope: Scope = None) -> dict[str, Any]:
        """Projects with their websites and which results exist, with dates (for `scope`).

        Projects closed to Claude Desktop are left out; only their number is given.
        """
        from src.modules.workspace.status import project_statuses

        closed = set(self.blocked())
        statuses = [s for s in project_statuses(self.projects) if s["project_id"] not in closed]
        if scope is not None:
            keep = set(_COMMON_FIELDS)
            for key in scope:
                keep |= set(_STATUS_FIELDS[key])
            statuses = [{k: v for k, v in s.items() if k in keep} for s in statuses]
        out: dict[str, Any] = {"projects": statuses}
        if closed:
            out["hidden_projects"] = len(closed)
            out["hidden_note"] = (
                f"{len(closed)} project(s) are confidential, or use a confidential project's "
                "data, and are not shown: Claude Desktop gets nothing from them. If the user asks "
                "for one, say it can only be worked on in the app itself."
            )
        return out


def register_mcp_routes(
    app: FastAPI,
    *,
    projects: ProjectStore,
    access: KnowledgeAccess,
    runs: RunStore | None = None,
) -> None:
    """Add the MCP bridge routes (before the app's catch-all routes).

    `runs` is the analyst run history `save_deliverable` writes to; without it the tool
    answers that saving is not available here.
    """
    bridge = McpBridge(projects, access, runs)

    @app.get("/api/mcp/tools")
    def tools(modules: str | None = None) -> list[dict[str, Any]]:
        try:
            scope = parse_scope(modules)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return [asdict(d) for d in definitions(scope)]

    @app.get("/api/mcp/blocked")
    def blocked() -> dict[str, list[str]]:
        return {"projects": bridge.blocked()}

    @app.post("/api/mcp/call")
    def call(body: CallIn, request: Request) -> CallOut:
        # every tool reads, except save_deliverable, which writes into a project's run
        # history: with team accounts that needs an editor (the gate let a viewer in)
        caller = getattr(request.state, "caller", None)
        if (
            body.tool in WRITING_TOOLS
            and caller is not None
            and not role_at_least(caller.role, "editor")
        ):
            return CallOut(
                text=json.dumps(
                    {"error": f"saving needs the editor role; this token acts as {caller.role}"}
                ),
                is_error=True,
            )
        text, is_error = bridge.call(body.tool, body.arguments, body.scope)
        return CallOut(text=text, is_error=is_error)
