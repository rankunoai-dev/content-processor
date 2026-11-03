"""Answer templates: Claude fills in a structured answer, the app checks and renders it.

`present_answer` takes a type (answer, overview, findings, comparison, page review, what
changed, keywords), a title, a short summary and, per type, sections, a table, prioritised
items and next steps, each fact carrying its `[chunk:...]`/`[fact:...]` reference. The app
verifies the references and quotes against the project's ledger, then renders a Markdown
page Claude Desktop shows well: a heading with the type's icon, an "In short" quote, the
type's own layout, next steps, a "Sources checked" line and numbered, linked sources. The
reference codes never reach the reader.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import Field, model_validator

from src.core.schemas import StrictModel
from src.modules.analyst.models import Verification
from src.modules.analyst.playground import EvidenceLedger
from src.modules.analyst.verify import readable_version, verify_deliverable

__all__ = ["TYPES", "AnswerType", "Item", "PresentInput", "Section", "TableIn", "render_answer"]

AnswerType = Literal[
    "answer", "overview", "findings", "comparison", "page_review", "changes", "keywords"
]

# type -> (icon, label, what it is for)
TYPES: Final[dict[str, tuple[str, str, str]]] = {
    "answer": ("💡", "Answer", "a question answered from the pages"),
    "overview": ("🧭", "Overview", "a project, module or theme at a glance"),
    "findings": ("🎯", "Findings", "gaps, issues or alerts ranked by priority"),
    "comparison": ("⚖️", "Comparison", "the client against its competitors side by side"),
    "page_review": ("📄", "Page review", "one page: its facts, strengths and issues"),
    "changes": ("🔔", "What changed", "changes between scrapes and what they mean"),
    "keywords": ("🔎", "Keywords", "rankings, search demand and keyword opportunities"),
}
_PRIORITY: Final = {"high": ("🔴", "High priority"), "medium": ("🟠", "Medium priority"),
                    "low": ("🔵", "Low priority")}  # fmt: skip


class Section(StrictModel):
    """A titled part of the answer."""

    heading: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=6000, description="Markdown, facts cited")


class Item(StrictModel):
    """One finding, change, issue or point."""

    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=2000, description="Facts cited")
    priority: Literal["high", "medium", "low"] | None = Field(
        default=None,
        description="findings/changes: how urgent; page_review: set it for issues, leave it "
        "empty for strengths",
    )
    metric: str = Field(default="", max_length=80, description='A short figure: "6,600 / month"')


class TableIn(StrictModel):
    """A table: column names and rows of cells (cite facts in a cell)."""

    columns: list[str] = Field(min_length=1, max_length=8)
    rows: list[list[str]] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _same_width(self) -> TableIn:
        for i, row in enumerate(self.rows, start=1):
            if len(row) != len(self.columns):
                msg = f"row {i} has {len(row)} cells; the table has {len(self.columns)} columns"
                raise ValueError(msg)
        return self


class PresentInput(StrictModel):
    """A structured answer to check and render."""

    type: AnswerType = Field(description="; ".join(f"{k}: {v[2]}" for k, v in TYPES.items()))
    title: str = Field(min_length=3, max_length=120)
    summary: str = Field(min_length=1, max_length=1500, description="1-3 sentences, cited")
    sections: list[Section] = Field(default_factory=list, max_length=8)
    items: list[Item] = Field(default_factory=list, max_length=30)
    table: TableIn | None = None
    next_steps: list[str] = Field(default_factory=list, max_length=8)
    limits: str = Field(default="", max_length=1000, description="What the data does not cover")


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def _table(table: TableIn) -> list[str]:
    lines = [
        "| " + " | ".join(_cell(c) for c in table.columns) + " |",
        "|" + "|".join("---" for _ in table.columns) + "|",
    ]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in table.rows]
    return [*lines, ""]


def _item_line(item: Item, marker: str) -> list[str]:
    head = f"{marker} **{item.title.strip()}**"
    if item.metric:
        head += f" · {item.metric.strip()}"
    lines = [head]
    if item.detail.strip():
        lines.append(f"   {' '.join(item.detail.split())}")
    return lines


def _items_by_priority(items: list[Item]) -> list[str]:
    out: list[str] = []
    for level in ("high", "medium", "low", None):
        group = [i for i in items if i.priority == level]
        if not group:
            continue
        icon, label = _PRIORITY[level] if level else ("▫️", "Other")
        out += [f"### {icon} {label}", ""]
        for n, item in enumerate(group, start=1):
            out += [*_item_line(item, f"{n}."), ""]
    return out


def _items_plain(items: list[Item]) -> list[str]:
    out: list[str] = []
    for item in items:
        icon = _PRIORITY[item.priority][0] if item.priority else "•"
        out += [*_item_line(item, f"- {icon}"), ""]
    return out


def _review_items(items: list[Item]) -> list[str]:
    good = [i for i in items if i.priority is None]
    issues = [i for i in items if i.priority is not None]
    out: list[str] = []
    if good:
        out += ["### ✅ What works", ""]
        for item in good:
            out += [*_item_line(item, "-"), ""]
    if issues:
        out += ["### ⚠️ What to fix", ""]
        for item in sorted(
            issues, key=lambda i: ("high", "medium", "low").index(i.priority or "low")
        ):
            out += [*_item_line(item, f"- {_PRIORITY[item.priority or 'low'][0]}"), ""]
    return out


def _sections(sections: list[Section]) -> list[str]:
    out: list[str] = []
    for s in sections:
        out += [f"### {s.heading.strip()}", "", s.body.strip(), ""]
    return out


def _body(inp: PresentInput) -> list[str]:
    table = _table(inp.table) if inp.table else []
    if inp.type == "findings":
        return [*_items_by_priority(inp.items), *_sections(inp.sections), *table]
    if inp.type == "page_review":
        return [*table, *_review_items(inp.items), *_sections(inp.sections)]
    if inp.type in ("overview", "comparison", "keywords"):
        return [*table, *_sections(inp.sections), *_items_plain(inp.items)]
    if inp.type == "changes":
        return [*_items_plain(inp.items), *_sections(inp.sections), *table]
    return [*_sections(inp.sections), *_items_plain(inp.items), *table]


def _content(inp: PresentInput) -> str:
    """The facts alone, laid out so the checker sees every claim (no template chrome)."""
    lines = ["## Summary", inp.summary, ""]
    for s in inp.sections:
        lines += [f"## {s.heading}", s.body, ""]
    if inp.items:
        lines.append("## Items")
        lines += [f"- {i.title}: {i.detail} {i.metric}".strip() for i in inp.items]
        lines.append("")
    if inp.table:
        lines += _table(inp.table)
    if inp.next_steps:
        lines += ["## Recommendations", *[f"- {s}" for s in inp.next_steps], ""]
    if inp.limits:
        lines += ["## Limits", inp.limits]
    return "\n".join(lines)


def _check_line(check: Verification) -> str:
    quotes = check.quotes_checked - len(check.quotes_failed)
    quote_part = (
        f" · {quotes} of {check.quotes_checked} quotes match word for word"
        if (check.quotes_checked)
        else ""
    )
    if check.passed:
        return (
            f"✅ **Sources checked** · {check.resolved} of {check.references} citations point "
            f"to the pages{quote_part}"
        )
    return (
        f"⚠️ **Needs review** · {check.resolved} of {check.references} citations found"
        f"{quote_part} · {len(check.uncited_lines)} statement(s) without a source"
    )


def render_answer(
    inp: PresentInput, ledger: EvidenceLedger, project_name: str, now: datetime | None = None
) -> tuple[str, Verification]:
    """Check the answer and render it; returns (Markdown for the reader, the check)."""
    check = verify_deliverable(_content(inp), ledger)
    icon, label, _what = TYPES[inp.type]
    when = (now or datetime.now(UTC)).strftime("%d %b %Y").lstrip("0")
    lines = [
        f"## {icon} {inp.title.strip()}",
        f"*{project_name} · {label} · {when}*",
        "",
        f"> **In short:** {' '.join(inp.summary.split())}",
        "",
        *_body(inp),
    ]
    if inp.next_steps:
        lines += ["### 👉 Next steps", ""]
        lines += [f"{n}. {step.strip()}" for n, step in enumerate(inp.next_steps, start=1)]
        lines.append("")
    if inp.limits.strip():
        lines += [f"*Limits: {' '.join(inp.limits.split())}*", ""]
    lines += ["---", "", _check_line(check)]
    return readable_version("\n".join(lines), ledger), check
