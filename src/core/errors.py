"""Exception hierarchy for the Rankuno automation platform.

Every exception raised by first-party code inherits from `RankunoError` so a
caller can distinguish "our code decided to stop" from "a dependency blew up".
"""

from __future__ import annotations

__all__ = [
    "ApprovalRequiredError",
    "BudgetExceededError",
    "ConfigurationError",
    "GuardrailViolationError",
    "IntegrationError",
    "RankunoError",
    "RateLimitExceededError",
    "ToolExecutionError",
]


class RankunoError(Exception):
    """Base class for all first-party errors."""


class ConfigurationError(RankunoError):
    """Required settings are missing, malformed, or mutually inconsistent."""


class GuardrailViolationError(RankunoError):
    """An action was attempted that policy forbids outright."""


class ApprovalRequiredError(GuardrailViolationError):
    """A MANDATORY_HITL action was attempted without operator approval.

    This is deliberately a hard error rather than a silent no-op: an unapproved
    write or spend must never be mistaken for a completed one.
    """

    def __init__(self, tool: str, reason: str) -> None:
        """Record which tool was blocked and why."""
        self.tool = tool
        self.reason = reason
        super().__init__(f"Human approval required for '{tool}': {reason}")


class RateLimitExceededError(RankunoError):
    """The local token bucket refused the call before it reached the network."""

    def __init__(self, key: str, retry_after_s: float) -> None:
        """Record the exhausted bucket and when capacity returns."""
        self.key = key
        self.retry_after_s = retry_after_s
        super().__init__(f"Rate limit '{key}' exhausted; retry in {retry_after_s:.2f}s")


class BudgetExceededError(RankunoError):
    """The call would push cumulative spend past the configured ceiling."""

    def __init__(self, attempted_usd: float, spent_usd: float, ceiling_usd: float) -> None:
        """Record the spend attempt that was refused."""
        self.attempted_usd = attempted_usd
        self.spent_usd = spent_usd
        self.ceiling_usd = ceiling_usd
        super().__init__(
            f"Spend of ${attempted_usd:.4f} refused: ${spent_usd:.4f} already spent "
            f"against a ${ceiling_usd:.2f} ceiling."
        )


class IntegrationError(RankunoError):
    """An external API failed in a way retries could not resolve."""

    def __init__(self, service: str, detail: str) -> None:
        """Record which upstream service failed."""
        self.service = service
        self.detail = detail
        super().__init__(f"Integration '{service}' failed: {detail}")


class ToolExecutionError(RankunoError):
    """A tool's own logic failed. Wraps the original exception as `__cause__`."""
