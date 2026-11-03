"""The tools Claude Desktop sees: the analyst tools plus `list_projects` and `check_deliverable`.

Every analyst tool gets a required `project_id`; none of them spends money or contacts a
website (scrapes, Semrush pulls and decisions stay in the app, behind its unlock). Some
build missing local results on first use (theme map, gap report, monitoring check, SEO
inventory): that runs on this computer at no cost, inside the web app.

**Module scope.** A connection can be limited to some modules (`--modules themes`): it then
lists only those modules' tools (other modules' tools never reach Claude's context), its
instructions say so, `list_projects` shows only those modules' state, and the web app
refuses any other module's tool on that connection. A module's own results may still
mention what they were built from (a gap finding names its theme): that is the module's
output, not a look-up in another module.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Final

from src.modules.analyst.library import ANALYST_RULES
from src.modules.analyst.tools import TOOLS

__all__ = [
    "EXTRA_TOOLS",
    "INSTRUCTIONS",
    "MAX_DELIVERABLE_CHARS",
    "SCOPES",
    "Scope",
    "ToolDef",
    "definitions",
    "instructions",
    "parse_scope",
    "scope_label",
    "scope_of",
]

MAX_DELIVERABLE_CHARS: Final = 200_000
EXTRA_TOOLS: Final = ("list_projects", "check_deliverable")

Scope = frozenset[str] | None
"""None: every module. Otherwise the module keys this connection may use."""

# public module key -> (the analyst tools' module name, label)
SCOPES: Final[dict[str, tuple[str, str]]] = {
    "content": ("content", "Content (pages and search)"),
    "themes": ("theming", "Themes"),
    "monitoring": ("monitoring", "Monitoring"),
    "research": ("research", "Research"),
    "gaps": ("gap", "Gaps"),
}
_KEY_OF_MODULE: Final = {module: key for key, (module, _label) in SCOPES.items()}

_PROJECT_ID: Final = {
    "type": "string",
    "description": "The project to read, from list_projects (for example jh-test-cdccde)",
    "minLength": 1,
    "maxLength": 80,
}


def parse_scope(text: str | None) -> Scope:
    """`themes,gaps` -> frozenset; empty or None -> every module. Unknown keys raise ValueError."""
    if text is None or not text.strip():
        return None
    keys = frozenset(k.strip().lower() for k in text.split(",") if k.strip())
    unknown = sorted(keys - SCOPES.keys())
    if unknown:
        msg = f"unknown module(s) {', '.join(unknown)}; the modules are {', '.join(SCOPES)}"
        raise ValueError(msg)
    return keys or None


def scope_label(scope: Scope) -> str:
    """'every module' or the labels, in catalog order."""
    if scope is None:
        return "every module"
    return ", ".join(label for key, (_m, label) in SCOPES.items() if key in scope)


def scope_of(tool: str) -> str | None:
    """The module key of an analyst tool; None for list_projects, check_deliverable, unknown."""
    spec = TOOLS.get(tool)
    return _KEY_OF_MODULE.get(spec.module) if spec is not None else None


def instructions(scope: Scope = None) -> str:
    """What Claude is told when it connects, for this scope."""
    if scope is None:
        scope_rule = (
            "6. Module focus: each tool's description starts with its module in brackets, for "
            'example [Themes]. When the user asks to work with one module only ("only use '
            'Themes", "stay in Gaps"), call only that module\'s tools plus list_projects and '
            "check_deliverable, do not look anything up in another module, and if a question "
            "needs another module, say so instead of calling it. A connection limited to one "
            "module can also be added in Claude Desktop (see the app's MCP guide)."
        )
    else:
        scope_rule = (
            f"6. This connection is limited to: {scope_label(scope)}. Only those modules' tools "
            "exist here; do not use data from other modules or from other connectors for this "
            "project. If a question needs another module, say which one and stop."
        )
    return f"""\
You are connected to Content Processor, Rankuno's SEO analyst playground, read-only.

How to work:
1. Call list_projects first; pass its project_id to every other tool.
2. Read with the tools; never guess data a tool can give you.
3. Cite every fact with the reference the tool returned, in square brackets, for example
   [chunk:...] or [fact:...].
4. Before you give the user an analysis, call check_deliverable with the full text and fix
   anything it reports (unknown citations, quotes that do not match, uncited claims). Tell the
   user whether the check passed.
5. You cannot scrape websites, pull from Semrush, change statuses or rebuild on request; say
   which button in the app does it when the user needs that.
{scope_rule}

{ANALYST_RULES}"""


INSTRUCTIONS: Final = instructions(None)


@dataclass(frozen=True)
class ToolDef:
    """One tool as Claude Desktop sees it."""

    name: str
    title: str
    description: str
    input_schema: dict[str, object]
    module: str | None = None


def _with_project(schema: dict[str, object]) -> dict[str, object]:
    out = copy.deepcopy(schema)
    out.pop("title", None)
    properties = out.get("properties", {})
    required = out.get("required", [])
    out["type"] = "object"
    out["properties"] = {
        "project_id": _PROJECT_ID,
        **(properties if isinstance(properties, dict) else {}),
    }
    out["required"] = [
        "project_id",
        *[r for r in (required if isinstance(required, list) else []) if r != "project_id"],
    ]
    out["additionalProperties"] = False
    return out


def _title(name: str) -> str:
    return name.replace("_", " ").capitalize()


def definitions(scope: Scope = None) -> list[ToolDef]:
    """The tools of this scope, `list_projects` first and `check_deliverable` last."""
    out = [
        ToolDef(
            "list_projects",
            "List projects",
            "Every project: its id, client, websites (client or competitor) and which data "
            f"exists for {scope_label(scope)}, with dates. Call this first.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
    ]
    for name, spec in TOOLS.items():
        key = _KEY_OF_MODULE.get(spec.module)
        if not spec.built or spec.handler is None or key is None:
            continue
        if scope is not None and key not in scope:
            continue
        label = SCOPES[key][1].split(" (")[0]
        out.append(
            ToolDef(
                name,
                f"{label}: {_title(name)}",
                f"[{label}] {spec.description} (read-only)",
                _with_project(spec.input_model.model_json_schema()),
                key,
            )
        )
    out.append(
        ToolDef(
            "check_deliverable",
            "Check deliverable",
            "Check an analysis before giving it to the user: every [chunk:...] and [fact:...] "
            "citation must be one a tool returned for this project, every quoted passage must "
            "match its source word for word, and statements of fact must be cited. Returns "
            "passed or needs review, the problems, and a Sources table to append.",
            {
                "type": "object",
                "properties": {
                    "project_id": _PROJECT_ID,
                    "text": {
                        "type": "string",
                        "description": "The full analysis in Markdown, with its citations",
                        "minLength": 1,
                        "maxLength": MAX_DELIVERABLE_CHARS,
                    },
                },
                "required": ["project_id", "text"],
                "additionalProperties": False,
            },
        )
    )
    return out
