"""Core agentic infrastructure: configuration, logging, guardrails, tooling base.

This package is domain-agnostic on purpose. It must never import from
`src.modules` or `src.integrations` — the dependency arrow points inward only:

    modules ──▶ integrations ──▶ core

Everything a domain tool needs is re-exported here, so a new module imports from
one place.
"""

from src.core.base_tool import BaseTool
from src.core.config import Environment, Settings, get_settings
from src.core.errors import (
    ApprovalRequiredError,
    BudgetExceededError,
    ConfigurationError,
    GuardrailViolationError,
    IntegrationError,
    RankunoError,
    RateLimitExceededError,
    ToolExecutionError,
)
from src.core.guardrails import ApprovalProvider, GuardrailDecision, GuardrailEngine
from src.core.logger import get_logger, trace_context
from src.core.rate_limiter import CostLedger, RateLimiterRegistry, TokenBucket
from src.core.registry import ToolRegistry, registry
from src.core.retry import retry_policy, with_retries
from src.core.schemas import (
    ApprovalMode,
    ExecutionStatus,
    RiskClass,
    StrictModel,
    ToolMetadata,
    ToolResult,
)

__all__ = [
    "ApprovalMode",
    "ApprovalProvider",
    "ApprovalRequiredError",
    "BaseTool",
    "BudgetExceededError",
    "ConfigurationError",
    "CostLedger",
    "Environment",
    "ExecutionStatus",
    "GuardrailDecision",
    "GuardrailEngine",
    "GuardrailViolationError",
    "IntegrationError",
    "RankunoError",
    "RateLimitExceededError",
    "RateLimiterRegistry",
    "RiskClass",
    "Settings",
    "StrictModel",
    "TokenBucket",
    "ToolExecutionError",
    "ToolMetadata",
    "ToolRegistry",
    "ToolResult",
    "get_logger",
    "get_settings",
    "registry",
    "retry_policy",
    "trace_context",
    "with_retries",
]
