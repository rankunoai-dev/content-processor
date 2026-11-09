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
    "PROMPTS",
    "PromptDef",
    "prompts_for",
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
EXTRA_TOOLS: Final = ("list_projects", "check_deliverable", "present_answer")

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
4. Before you answer with facts from the tools (an analysis, a summary, a list), call
   check_deliverable with your full draft and fix anything it reports (unknown citations,
   quotes that do not match, uncited claims).
5. You cannot scrape websites, pull from Semrush, change statuses or rebuild on request; say
   which button in the app does it when the user needs that.
{scope_rule}

How to present answers to the user (readability comes first):
- Give every answer with facts through present_answer: choose its type (answer, overview,
  findings, comparison, page_review, changes, keywords), a short title, a 1-3 sentence summary,
  and the parts that fit (sections, a table, prioritised items, next steps, limits), each fact
  with its reference. Then show the user its rendered_markdown exactly as returned, with
  nothing before or after it except, at most, one short sentence. If its status is "needs
  review", fix the problems it lists and call it again.
- check_deliverable remains for checking free text; if you use it, show its readable_markdown.
  The reference codes ([chunk:...], [fact:...]) are for the check only: never show them.
- Never show internal ids (theme_id, map_id, report_id, run ids), tool names or JSON field
  names. Call a theme by its name (the "name" field), never by its keyword list.
- Write for a marketing or SEO reader: short paragraphs or bullets, numbers with their meaning
  ("2 pages", "searched 6,600 times a month"), no jargon such as "chunk", "ledger" or "spoke".
- Pick the type by the question: one question -> answer; "overview"/"what is in" ->
  overview; gaps, issues, alerts -> findings; client vs competitors -> comparison; one URL ->
  page_review; "what changed" -> changes; rankings or demand -> keywords. Put lists of things
  with numbers in a table; put things to do in next_steps.

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
    if scope is None or "themes" in scope:
        out.append(
            ToolDef(
                "get_theme_pages",
                "Themes: Get theme pages",
                "[Themes] Every page of one theme of the project, in full (no truncation): URL, "
                "title, website, role, page type, funnel stage and words. Give the project_id "
                "and the theme id or name as get_theme_map lists them. Use it to list all the "
                "URLs of a theme. (read-only)",
                {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "The project, from list_projects",
                            "minLength": 1,
                            "maxLength": 80,
                        },
                        "theme": {
                            "type": "string",
                            "description": "The theme id (th-...) or its name, from get_theme_map",
                            "minLength": 1,
                            "maxLength": 200,
                        },
                        "website": {
                            "type": "string",
                            "description": "Optional: one website only, such as johnhancock.com",
                            "maxLength": 200,
                        },
                    },
                    "required": ["project_id", "theme"],
                    "additionalProperties": False,
                },
                "themes",
            )
        )
    from src.modules.mcp_server.templates import TYPES, PresentInput

    out.append(
        ToolDef(
            "present_answer",
            "Present answer",
            "Give the user an answer in a clear, attractive layout. Fill in the type ("
            + ", ".join(f"{k}: {v[2]}" for k, v in TYPES.items())
            + "), a title, a short summary and the parts that fit (sections, a table, "
            "prioritised items, next steps, limits), every fact with its [chunk:...] or "
            "[fact:...] reference. The app checks the references and quotes and returns "
            "rendered_markdown, with numbered, linked sources and no codes: show it to the "
            "user exactly as returned.",
            _with_project(PresentInput.model_json_schema()),
        )
    )
    out.append(
        ToolDef(
            "check_deliverable",
            "Check deliverable",
            "Check an answer before giving it to the user: every [chunk:...] and [fact:...] "
            "citation must be one a tool returned for this project, every quoted passage must "
            "match its source word for word, and statements of fact must be cited. Returns "
            "passed or needs review, the problems, and readable_markdown: the answer as the "
            "user should see it (numbered sources and links instead of reference codes).",
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


@dataclass(frozen=True)
class PromptDef:
    """A ready-made request in Claude Desktop's prompt menu."""

    name: str
    title: str
    description: str
    module: str | None
    arguments: tuple[tuple[str, str, bool], ...]
    template: str


_PROJECT_ARG: Final = ("project", "The project's name or id (leave empty to choose)", False)

PROMPTS: Final[tuple[PromptDef, ...]] = (
    PromptDef(
        "ask-the-pages",
        "Ask the pages",
        "A question answered from the scraped pages, with sources.",
        "content",
        (_PROJECT_ARG, ("question", "What you want to know", True)),
        "Answer this question about the project {project}: {question}\n\nSearch the pages "
        "(search_knowledge, get_page when needed), then present it with present_answer, type "
        "answer: sections for the main points, quotes where the exact wording matters.",
    ),
    PromptDef(
        "page-review",
        "Page review",
        "One page: its facts, what works and what to fix.",
        "content",
        (_PROJECT_ARG, ("url", "The page's address", True)),
        "Review the page {url} of the project {project}. Read it (get_page, get_page_facts) "
        "and present it with present_answer, type page_review: a table of its key facts "
        "(title, description, headings, structured data, calls to action), items for what "
        "works (no priority) and what to fix (with priority), and next steps.",
    ),
    PromptDef(
        "theme-overview",
        "Theme overview",
        "Every theme of the project: size, main page, who owns it.",
        "themes",
        (_PROJECT_ARG,),
        "Give me an overview of the themes of the project {project}. Read the theme map "
        "(get_theme_map) and present it with present_answer, type overview: a table with "
        "Theme, Pages, Main page and Owner (a reference in each row), and a section on what "
        "stands out.",
    ),
    PromptDef(
        "compare-websites",
        "Compare with competitors",
        "The client against its competitors, theme by theme.",
        "themes",
        (_PROJECT_ARG,),
        "Compare the client of the project {project} with its competitors. Use the theme map "
        "(get_theme_map) and present it with present_answer, type comparison: a table with one "
        "row per theme and one column per website, then where the client leads and where it "
        "trails.",
    ),
    PromptDef(
        "what-changed",
        "What changed",
        "Changes since the last scrape and the alerts that matter.",
        "monitoring",
        (_PROJECT_ARG,),
        "What changed on the websites of the project {project}? Read the monitoring alerts "
        "(get_monitoring_alerts, get_changes when needed) and present it with present_answer, "
        "type changes: one item per change with its priority, then next steps.",
    ),
    PromptDef(
        "keyword-report",
        "Keyword report",
        "Rankings and search demand per theme.",
        "research",
        (_PROJECT_ARG,),
        "Report on the search data of the project {project}: demand per theme "
        "(get_search_demand) and the client's best and weakest rankings (get_keyword_rankings). "
        "Present it with present_answer, type keywords: a table of themes with searches per "
        "month and top-10 rankings, items for opportunities, next steps.",
    ),
    PromptDef(
        "gap-report",
        "Gap report",
        "What the client is missing, ranked by priority.",
        "gaps",
        (_PROJECT_ARG,),
        "What is the client of the project {project} missing against its competitors? Read the "
        "gap findings (get_gap_findings) and present them with present_answer, type findings: "
        "one item per finding with its priority and a short figure, then next steps.",
    ),
)


def prompts_for(scope: Scope = None) -> list[PromptDef]:
    """The prompts of this connection's modules."""
    return [p for p in PROMPTS if scope is None or p.module is None or p.module in scope]
