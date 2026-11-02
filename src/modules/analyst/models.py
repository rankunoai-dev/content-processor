"""Contracts of the action-card engine: cards, their inputs, runs and checks.

An action card is a packaged senior-analyst workflow: a persona and rules
(`system_prompt`), a multi-phase task (`prompt_template` with `{{name}}` slots),
the inputs a person fills in, the tools Claude may call, the tool calls made up
front (`prefetch`), and the shape of the deliverable. A card is data: adding one
needs no code, and every run records the card version it used.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "CARD_SCHEMA_VERSION",
    "ActionCard",
    "Backend",
    "CardParameter",
    "ParameterKind",
    "Prefetch",
    "Preflight",
    "RunRecord",
    "RunStatus",
    "Verification",
]

CARD_SCHEMA_VERSION: Final = "card-v1"

ParameterKind = Literal["project", "website", "url", "keyword", "topic", "text", "choice"]
Backend = Literal["pack", "claude"]
RunStatus = Literal["queued", "running", "finished", "failed", "refused"]


class CardParameter(StrictModel):
    """One input a person fills in (or the project fills in for them)."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    kind: ParameterKind
    description: str
    required: bool = True
    default: str | None = Field(
        default=None,
        description="Literal value, or `client`/`competitors` for a project's websites",
    )
    choices: list[str] = Field(default_factory=list)
    many: bool = Field(default=False, description="A list (comma-separated in the CLI)")


class Prefetch(StrictModel):
    """A tool call made before Claude starts, so the first turn already has the core data."""

    tool: str
    args: dict[str, Any] = Field(
        default_factory=dict, description="Values may be `{{param}}` slots, filled at run time"
    )
    for_each: str | None = Field(
        default=None, description="A list parameter: one call per item, bound as `{{item}}`"
    )


class ActionCard(StrictModel):
    """A senior-analyst workflow: prompt, inputs, tools and deliverable."""

    card_schema: Literal["card-v1"] = CARD_SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: int = Field(ge=1, description="Raise when the prompt changes; runs record it")
    title: str
    module: Literal["content", "theming", "research", "monitoring", "gap", "playground"]
    category: str
    icon: str
    summary: str = Field(description="What the card produces, in plain words")
    deliverable: str = Field(
        description="The name of the output, e.g. 'Trust & Credibility Matrix'"
    )
    parameters: list[CardParameter]
    persona: str = Field(description="Who Claude acts as; joined to the shared analyst rules")
    prompt_template: str
    required_tools: list[str] = Field(min_length=1)
    optional_tools: list[str] = Field(default_factory=list)
    prefetch: list[Prefetch] = Field(default_factory=list)
    output_format: str = Field(description="The exact sections and tables of the deliverable")


class Preflight(StrictModel):
    """Whether a card can run with these inputs, and what is missing or weak."""

    card_id: str
    runnable: bool
    inputs: dict[str, Any] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list, description="Each one blocks the run")
    warnings: list[str] = Field(default_factory=list, description="The run goes ahead, weaker")
    tools: list[str] = Field(default_factory=list, description="Tools the run may call")
    missing_tools: list[str] = Field(default_factory=list, description="Optional tools not built")


class Verification(StrictModel):
    """Code's check of the deliverable against what the tools returned in this run."""

    references: int = Field(ge=0, description="Citations found")
    resolved: int = Field(ge=0, description="Citations that point at data a tool returned")
    unknown_refs: list[str] = Field(default_factory=list)
    quotes_checked: int = Field(default=0, ge=0)
    quotes_failed: list[str] = Field(default_factory=list)
    uncited_lines: list[str] = Field(
        default_factory=list, description="Claims (numbers or long sentences) without a citation"
    )
    passed: bool


class RunRecord(StrictModel):
    """One run of a card: inputs, backend, model, cost, outcome. The audit trail."""

    run_id: str
    card_id: str
    card_version: int
    backend: Backend
    status: RunStatus
    created_at: datetime
    finished_at: datetime | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    project_ids: list[str] = Field(default_factory=list)
    model: str | None = None
    tool_calls: int = Field(default=0, ge=0)
    turns: int = Field(default=0, ge=0)
    usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = Field(default=0.0, ge=0.0)
    cap_usd: float | None = None
    message: str = ""
    verification: Verification | None = None
    files: dict[str, str] = Field(default_factory=dict)
