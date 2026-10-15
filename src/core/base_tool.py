"""`BaseTool` — the mandatory base class for every capability in the platform.

A tool subclass supplies three things: declarative `metadata`, an `Input` model,
an `Output` model, and an `execute()` body. Everything cross-cutting is handled
here, once:

    validate input -> guardrail check -> rate limit -> charge budget
      -> execute -> validate output -> audit log -> ToolResult

This is why domain modules stay small: a new SEO or PPC tool inherits HITL
enforcement, quota protection, spend control, tracing and structured error
handling without writing any of it.

`run()` never raises across its boundary. Failures come back as a `ToolResult`
with a non-SUCCESS status so an agent loop can reason about them.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from src.core.errors import (
    ApprovalRequiredError,
    BudgetExceededError,
    RankunoError,
    RateLimitExceededError,
)
from src.core.guardrails import GuardrailEngine
from src.core.logger import get_logger, trace_context
from src.core.rate_limiter import CostLedger, RateLimiterRegistry
from src.core.schemas import ExecutionStatus, ToolMetadata, ToolResult

__all__ = ["BaseTool"]

_logger = get_logger("core.base_tool")

# Shared across all tools in the process, so independent tools hitting the same
# upstream quota or the same budget genuinely contend with each other.
_RATE_LIMITERS = RateLimiterRegistry()
_COST_LEDGER = CostLedger()

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class BaseTool(ABC, Generic[InputT, OutputT]):
    """Abstract base for every tool.

    Subclasses MUST set the three class variables below. They are checked at
    subclass-creation time, so a malformed tool fails at import rather than
    halfway through an agent run.
    """

    metadata: ClassVar[ToolMetadata]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Reject subclasses that omit their declarative contract."""
        super().__init_subclass__(**kwargs)

        # Intermediate abstract bases are allowed to defer the declaration.
        if getattr(cls, "__abstractmethods__", None):
            return

        for attr in ("metadata", "input_model", "output_model"):
            if not hasattr(cls, attr):
                msg = f"{cls.__name__} must declare a class-level '{attr}'."
                raise TypeError(msg)

        if not isinstance(getattr(cls, "metadata", None), ToolMetadata):
            msg = f"{cls.__name__}.metadata must be a ToolMetadata instance."
            raise TypeError(msg)

    def __init__(
        self,
        guardrails: GuardrailEngine | None = None,
        cost_ledger: CostLedger | None = None,
    ) -> None:
        """Build a tool instance.

        Args:
            guardrails: Engine to authorize against. Defaults to a deny-by-default
                engine, so an unwired tool cannot perform a write or a spend.
            cost_ledger: Ledger to charge. Defaults to the process-wide ledger.
        """
        self._guardrails = guardrails or GuardrailEngine()
        self._ledger = cost_ledger or _COST_LEDGER

    @abstractmethod
    def execute(self, payload: InputT) -> OutputT:
        """Perform the tool's actual work.

        Called only after validation, authorization, rate limiting and budgeting
        have all passed. Implementations should raise on failure — `run()`
        converts exceptions into a structured result.

        Args:
            payload: Validated input.

        Returns:
            An instance of `output_model`.
        """
        raise NotImplementedError

    def describe_invocation(self, payload: InputT) -> str:
        """Human-readable summary shown to the operator at an approval prompt.

        Override this in tools that perform writes or spends: "approve
        `ads.update_budget`?" is far less useful than naming the campaign and
        the amount.
        """
        return f"{type(self).metadata.name} with {payload!r}"

    def run(self, raw_input: BaseModel | dict[str, object]) -> ToolResult[BaseModel]:
        """Execute the full governed pipeline. Does not raise.

        Args:
            raw_input: Either an instance of `input_model` or a dict to validate
                against it.

        Returns:
            A `ToolResult` carrying either the output or the failure reason.
        """
        meta = type(self).metadata
        started = time.perf_counter()

        with trace_context() as trace_id:
            try:
                payload = self._validate_input(raw_input)
                decision = self._guardrails.enforce(meta, self.describe_invocation(payload))
                self._apply_rate_limit(meta)
                self._ledger.charge(meta.estimated_cost_usd)

                output = self.execute(payload)
                self._validate_output(output)

                elapsed_ms = (time.perf_counter() - started) * 1000
                _logger.info(
                    "tool_succeeded",
                    extra={"tool": meta.name, "duration_ms": round(elapsed_ms, 2)},
                )
                return ToolResult[BaseModel](
                    status=ExecutionStatus.SUCCESS,
                    tool=meta.name,
                    data=output,
                    approval_mode=decision.mode,
                    requires_human_review=decision.requires_human_review,
                    duration_ms=elapsed_ms,
                    cost_usd=meta.estimated_cost_usd,
                    trace_id=trace_id,
                )
            except Exception as exc:  # noqa: BLE001 - boundary must not leak
                return self._failure(exc, meta.name, started, trace_id)

    # -- internals ---------------------------------------------------------

    def _validate_input(self, raw_input: BaseModel | dict[str, object]) -> InputT:
        """Coerce and validate the caller's input against `input_model`."""
        model = type(self).input_model
        if isinstance(raw_input, model):
            return raw_input  # type: ignore[return-value]
        if isinstance(raw_input, BaseModel):
            msg = (
                f"{type(self).metadata.name} expects {model.__name__}, "
                f"got {type(raw_input).__name__}."
            )
            raise TypeError(msg)
        return model.model_validate(raw_input)  # type: ignore[return-value]

    def _validate_output(self, output: object) -> None:
        """Fail loudly if `execute()` returned the wrong type.

        Catching this here prevents an unvalidated shape from propagating into
        a report or a downstream tool.
        """
        model = type(self).output_model
        if not isinstance(output, model):
            msg = (
                f"{type(self).metadata.name}.execute() must return "
                f"{model.__name__}, got {type(output).__name__}."
            )
            raise TypeError(msg)

    def _apply_rate_limit(self, meta: ToolMetadata) -> None:
        """Block until the tool's shared quota bucket allows the call."""
        if meta.rate_limit_key is None:
            return
        bucket = _RATE_LIMITERS.get_or_create(meta.rate_limit_key)
        bucket.acquire(timeout_s=_timeout())

    def _failure(
        self, exc: Exception, tool_name: str, started: float, trace_id: str
    ) -> ToolResult[BaseModel]:
        """Map an exception onto the appropriate non-success status."""
        status = {
            ApprovalRequiredError: ExecutionStatus.BLOCKED_PENDING_APPROVAL,
            RateLimitExceededError: ExecutionStatus.RATE_LIMITED,
            BudgetExceededError: ExecutionStatus.BUDGET_EXCEEDED,
        }.get(type(exc), ExecutionStatus.FAILED)

        # Expected, policy-driven outcomes are informational; anything else is a
        # genuine defect and gets a stack trace in the audit log.
        expected = isinstance(exc, RankunoError | ValidationError)
        log = _logger.info if expected else _logger.exception
        log(
            "tool_failed",
            extra={"tool": tool_name, "status": status, "error": str(exc)},
        )

        return ToolResult[BaseModel](
            status=status,
            tool=tool_name,
            error=str(exc),
            duration_ms=(time.perf_counter() - started) * 1000,
            trace_id=trace_id,
        )


def _timeout() -> float:
    """Rate-limit wait ceiling, read lazily so tests can override settings."""
    from src.core.config import get_settings

    return get_settings().default_timeout_s
