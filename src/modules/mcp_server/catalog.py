"""The tools Claude Desktop sees: the analyst tools plus `list_projects` and `check_deliverable`.

Every analyst tool gets a required `project_id`; none of them spends money or contacts a
website (scrapes, Semrush pulls and decisions stay in the app, behind its unlock). Some
build missing local results on first use (theme map, gap report, monitoring check, SEO
inventory): that runs on this computer at no cost, inside the web app.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Final

from src.modules.analyst.library import ANALYST_RULES
from src.modules.analyst.tools import TOOLS

__all__ = ["EXTRA_TOOLS", "INSTRUCTIONS", "MAX_DELIVERABLE_CHARS", "ToolDef", "definitions"]

MAX_DELIVERABLE_CHARS: Final = 200_000
EXTRA_TOOLS: Final = ("list_projects", "check_deliverable")

_PROJECT_ID: Final = {
    "type": "string",
    "description": "The project to read, from list_projects (for example jh-test-cdccde)",
    "minLength": 1,
    "maxLength": 80,
}

INSTRUCTIONS: Final = f"""\
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

{ANALYST_RULES}"""


@dataclass(frozen=True)
class ToolDef:
    """One tool as Claude Desktop sees it."""

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]


def _with_project(schema: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(schema)
    out.pop("title", None)
    out["type"] = "object"
    out["properties"] = {"project_id": _PROJECT_ID, **out.get("properties", {})}
    out["required"] = ["project_id", *[r for r in out.get("required", []) if r != "project_id"]]
    out["additionalProperties"] = False
    return out


def _title(name: str) -> str:
    return name.replace("_", " ").capitalize()


def definitions() -> list[ToolDef]:
    """Every tool, `list_projects` first."""
    out = [
        ToolDef(
            "list_projects",
            "List projects",
            "Every project: its id, client, websites (client or competitor) and which data "
            "exists (scrapes, themes, monitoring, research, gaps) with dates. Call this first.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
    ]
    for name, spec in TOOLS.items():
        if not spec.built or spec.handler is None:
            continue
        out.append(
            ToolDef(
                name,
                _title(name),
                f"{spec.description} (module: {spec.module}; read-only)",
                _with_project(spec.input_model.model_json_schema()),
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
