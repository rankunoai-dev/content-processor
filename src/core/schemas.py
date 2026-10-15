"""Canonical data contracts shared by every tool, connector and agent.

Nothing in this repository is allowed to pass loosely-typed dictionaries across a
module boundary. Every boundary uses one of the models defined here, or a
Pydantic model that composes them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ApprovalMode",
    "ExecutionStatus",
    "RiskClass",
    "StrictModel",
    "ToolMetadata",
    "ToolResult",
]


class StrictModel(BaseModel):
    """Base model for every contract in the repository.

    Rejects unknown fields so that an upstream API silently renaming a key
    surfaces as a validation error instead of a `None` propagating downstream.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, validate_assignment=True)


class RiskClass(StrEnum):
    """Classifies what a capability is allowed to touch.

    Drives the HITL governance matrix in `docs/SDLC_GUIDELINES.md`. Every tool
    MUST declare one; there is no default.
    """

    READ = "read"
    """Read-only analytics: crawls, SERP reads, GSC metric pulls, clustering."""

    DRAFT = "draft"
    """Generates artifacts for a human to review. No external side effects."""

    WRITE = "write"
    """Mutates state outside this repository (client sites, CMS, live configs)."""

    FINANCIAL = "financial"
    """Spends money: ad budget, paid API credits, metered inference."""


class ApprovalMode(StrEnum):
    """Outcome of a guardrail evaluation for a given `RiskClass`."""

    AUTOMATIC = "automatic"
    """Proceed without asking."""

    OPERATOR_REVIEW = "operator_review"
    """Proceed, but the output is marked as requiring human review."""

    MANDATORY_HITL = "mandatory_hitl"
    """Must not proceed until an operator explicitly approves."""


class ExecutionStatus(StrEnum):
    """Terminal state of a single tool invocation."""

    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED_PENDING_APPROVAL = "blocked_pending_approval"
    RATE_LIMITED = "rate_limited"
    BUDGET_EXCEEDED = "budget_exceeded"


class ToolMetadata(StrictModel):
    """Static, declarative description of a capability.

    Registered once per tool class. The guardrail engine, the rate limiter and
    the audit log all read from this object, so it is the single place a
    reviewer needs to look to understand a tool's blast radius.
    """

    name: str = Field(min_length=1, pattern=r"^[a-z0-9_.]+$")
    version: str = Field(default="0.1.0", pattern=r"^\d+\.\d+\.\d+$")
    summary: str = Field(min_length=1, max_length=200)
    risk_class: RiskClass
    owner: str = Field(default="ai-automation", min_length=1)

    rate_limit_key: str | None = Field(
        default=None,
        description="Shared bucket name. Tools hitting the same upstream quota "
        "MUST share a key (e.g. 'google.gsc') so limits compose.",
    )
    estimated_cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Worst-case spend for one invocation. Non-zero requires RiskClass.FINANCIAL.",
    )

    @model_validator(mode="after")
    def _cost_implies_financial(self) -> ToolMetadata:
        """Enforce the cost/risk invariant at declaration time, not call time.

        A paid capability classified as READ would bypass the mandatory-HITL
        spend gate entirely, so this is a validation error rather than a warning.
        """
        if self.estimated_cost_usd > 0.0 and self.risk_class is not RiskClass.FINANCIAL:
            msg = (
                f"Tool '{self.name}' declares a non-zero cost "
                f"({self.estimated_cost_usd}) but risk_class is "
                f"'{self.risk_class}'. Paid capabilities must be FINANCIAL."
            )
            raise ValueError(msg)
        return self


PayloadT = TypeVar("PayloadT")


class ToolResult(StrictModel, Generic[PayloadT]):
    """Uniform envelope returned by every tool invocation.

    Tools never raise across their public boundary — failures become a result
    with `status != SUCCESS`, so an agent loop can reason about them instead of
    crashing.
    """

    status: ExecutionStatus
    tool: str
    data: PayloadT | None = None
    error: str | None = None
    approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC
    requires_human_review: bool = False
    duration_ms: float = Field(default=0.0, ge=0.0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    trace_id: str | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        """True only when the tool ran to completion successfully."""
        return self.status is ExecutionStatus.SUCCESS
