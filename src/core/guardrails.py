"""HITL guardrail engine — the enforcement point for the governance matrix.

`docs/SDLC_GUIDELINES.md` states the policy in prose; this module is the
executable version of it. Every tool invocation passes through
`GuardrailEngine.authorize()` before any side effect occurs.

Design stance: **deny by default**. If no approval provider is wired in, a
MANDATORY_HITL action is refused rather than auto-approved. An unattended agent
run must never be able to spend money or mutate a client site by default.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.core.config import Environment, Settings, get_settings
from src.core.errors import ApprovalRequiredError
from src.core.logger import get_logger
from src.core.schemas import ApprovalMode, RiskClass, StrictModel, ToolMetadata

__all__ = [
    "ApprovalProvider",
    "AutoApproveProvider",
    "CallbackApprovalProvider",
    "DenyByDefaultProvider",
    "GuardrailDecision",
    "GuardrailEngine",
]

_logger = get_logger("core.guardrails")

# The static policy table. Mirrors the matrix in docs/SDLC_GUIDELINES.md §3 —
# if you change one, change the other in the same commit (drift protocol).
_BASE_POLICY: dict[RiskClass, ApprovalMode] = {
    RiskClass.READ: ApprovalMode.AUTOMATIC,
    RiskClass.DRAFT: ApprovalMode.OPERATOR_REVIEW,
    RiskClass.WRITE: ApprovalMode.MANDATORY_HITL,
    RiskClass.FINANCIAL: ApprovalMode.MANDATORY_HITL,
}


class GuardrailDecision(StrictModel):
    """Outcome of evaluating one action against policy."""

    allowed: bool
    mode: ApprovalMode
    reason: str
    requires_human_review: bool = False


@runtime_checkable
class ApprovalProvider(Protocol):
    """Supplies operator approval for MANDATORY_HITL actions.

    Implementations bridge to whatever the surrounding runtime offers — an
    Antigravity `ask_permission` call, a CLI prompt, a Slack approval, a ticket.
    """

    def request_approval(self, metadata: ToolMetadata, context: str) -> bool:
        """Return True only if a human explicitly approved the action."""
        ...


class DenyByDefaultProvider:
    """Refuses every approval request. The default in unattended contexts."""

    def request_approval(self, metadata: ToolMetadata, context: str) -> bool:
        """Always deny, and record the refusal in the audit log."""
        _logger.warning(
            "approval_denied_no_provider",
            extra={"tool": metadata.name, "risk": metadata.risk_class, "context": context},
        )
        return False


class AutoApproveProvider:
    """Approves everything.

    **Test and local-development use only.** Wiring this into a staging or
    production runtime defeats the entire HITL layer; `GuardrailEngine` logs a
    prominent warning whenever it is used outside development.
    """

    def request_approval(self, metadata: ToolMetadata, context: str) -> bool:
        """Approve unconditionally."""
        return True


class CallbackApprovalProvider:
    """Delegates to a caller-supplied predicate (CLI prompt, MCP call, webhook)."""

    def __init__(self, callback: object) -> None:
        """Store the callable used to obtain approval.

        Args:
            callback: Any callable accepting `(ToolMetadata, str)` and returning
                a bool.

        Raises:
            TypeError: If `callback` is not callable.
        """
        if not callable(callback):
            msg = "CallbackApprovalProvider requires a callable."
            raise TypeError(msg)
        self._callback = callback

    def request_approval(self, metadata: ToolMetadata, context: str) -> bool:
        """Ask the callback, treating any failure as a denial."""
        try:
            return bool(self._callback(metadata, context))
        except Exception:  # noqa: BLE001 - a broken approver must never mean "yes"
            _logger.exception("approval_callback_failed", extra={"tool": metadata.name})
            return False


class GuardrailEngine:
    """Evaluates actions against the governance matrix."""

    def __init__(
        self,
        approval_provider: ApprovalProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Build an engine.

        Args:
            approval_provider: Source of human approval. Defaults to
                `DenyByDefaultProvider`.
            settings: Configuration override, primarily for tests.
        """
        self._settings = settings or get_settings()
        self._provider: ApprovalProvider = approval_provider or DenyByDefaultProvider()

        is_dev = self._settings.environment is Environment.DEVELOPMENT
        if isinstance(self._provider, AutoApproveProvider) and not is_dev:
            _logger.warning(
                "auto_approve_outside_development",
                extra={"environment": self._settings.environment},
            )

    def policy_for(self, risk_class: RiskClass) -> ApprovalMode:
        """Return the effective approval mode for a risk class.

        Applies the base matrix, then the configuration overrides that let an
        operator *tighten* — never loosen — the defaults.
        """
        mode = _BASE_POLICY[risk_class]

        if risk_class is RiskClass.WRITE and not self._settings.require_approval_for_writes:
            mode = ApprovalMode.OPERATOR_REVIEW
        if risk_class is RiskClass.FINANCIAL and not self._settings.require_approval_for_spend:
            mode = ApprovalMode.OPERATOR_REVIEW

        return mode

    def authorize(self, metadata: ToolMetadata, context: str = "") -> GuardrailDecision:
        """Decide whether an action may proceed.

        Args:
            metadata: The declaring tool's metadata.
            context: Human-readable description of the specific invocation,
                shown to the operator when approval is requested.

        Returns:
            A decision. Callers MUST check `.allowed` before acting.
        """
        if not self._settings.guardrails_enabled:
            _logger.warning("guardrails_bypassed", extra={"tool": metadata.name})
            return GuardrailDecision(
                allowed=True,
                mode=ApprovalMode.AUTOMATIC,
                reason="Guardrails disabled by configuration.",
            )

        mode = self.policy_for(metadata.risk_class)

        if mode is ApprovalMode.AUTOMATIC:
            return GuardrailDecision(allowed=True, mode=mode, reason="Read-only action.")

        if mode is ApprovalMode.OPERATOR_REVIEW:
            return GuardrailDecision(
                allowed=True,
                mode=mode,
                reason="Output is advisory and must be reviewed before use.",
                requires_human_review=True,
            )

        approved = self._provider.request_approval(metadata, context)
        _logger.info(
            "hitl_decision",
            extra={
                "tool": metadata.name,
                "risk": metadata.risk_class,
                "approved": approved,
                "cost_usd": metadata.estimated_cost_usd,
            },
        )
        denial_reason = (
            f"Operator approval required for a '{metadata.risk_class}' action and was not granted."
        )
        return GuardrailDecision(
            allowed=approved,
            mode=mode,
            reason="Operator approved." if approved else denial_reason,
            requires_human_review=True,
        )

    def enforce(self, metadata: ToolMetadata, context: str = "") -> GuardrailDecision:
        """Authorize, raising instead of returning a denial.

        Raises:
            ApprovalRequiredError: If the action was not permitted.
        """
        decision = self.authorize(metadata, context)
        if not decision.allowed:
            raise ApprovalRequiredError(metadata.name, decision.reason)
        return decision
