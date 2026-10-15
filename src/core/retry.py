"""Retry policy for external calls.

`docs/SDLC_GUIDELINES.md` §4.4 requires exponential backoff on every outbound
HTTP call. This module is the one sanctioned way to satisfy that requirement, so
backoff behaviour is consistent and tunable from a single place.

Only *transient* failures are retried. Retrying a 401 or a 400 wastes quota and
delays a real error reaching the operator, so authentication and validation
failures fail fast.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.core.config import get_settings
from src.core.errors import IntegrationError, RateLimitExceededError
from src.core.logger import get_logger

__all__ = ["TRANSIENT_ERRORS", "retry_policy", "with_retries"]

_logger = get_logger("core.retry")

ReturnT = TypeVar("ReturnT")

# Exception types considered worth a second attempt. Extend deliberately — every
# addition here is a decision to spend more quota on a failing call.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    RateLimitExceededError,
    IntegrationError,
)


def _log_retry(state: RetryCallState) -> None:
    """Emit an audit record for each retry attempt."""
    exc = state.outcome.exception() if state.outcome else None
    _logger.warning(
        "retry_attempt",
        extra={
            "attempt": state.attempt_number,
            "seconds_elapsed": round(state.seconds_since_start or 0.0, 3),
            "error": repr(exc) if exc else None,
        },
    )


def retry_policy(
    max_attempts: int | None = None,
    *,
    initial_wait_s: float = 1.0,
    max_wait_s: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = TRANSIENT_ERRORS,
) -> Retrying:
    """Build a configured `Retrying` controller.

    Uses exponential backoff with full jitter: without jitter, several
    concurrent workers hitting the same 429 retry in lockstep and re-trigger it.

    Args:
        max_attempts: Total attempts including the first. Defaults to
            `DEFAULT_MAX_RETRIES + 1`.
        initial_wait_s: Backoff before the second attempt.
        max_wait_s: Ceiling on any single backoff interval.
        retry_on: Exception types that justify another attempt.

    Returns:
        A `Retrying` instance usable as an iterator or via `.__call__`.
    """
    attempts = max_attempts if max_attempts is not None else get_settings().default_max_retries + 1

    return Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=initial_wait_s, max=max_wait_s),
        retry=retry_if_exception_type(retry_on),
        before_sleep=_log_retry,
        reraise=True,
    )


def with_retries(
    func: Callable[[], ReturnT],
    *,
    max_attempts: int | None = None,
    retry_on: tuple[type[BaseException], ...] = TRANSIENT_ERRORS,
) -> ReturnT:
    """Run a zero-argument callable under the standard retry policy.

    Args:
        func: The operation to attempt. Bind arguments with a lambda or
            `functools.partial`.
        max_attempts: Override the configured attempt count.
        retry_on: Override which exceptions are treated as transient.

    Returns:
        Whatever `func` returns on its first successful attempt.

    Raises:
        BaseException: The final exception, re-raised once attempts are spent.
    """
    controller = retry_policy(max_attempts, retry_on=retry_on)
    return controller(func)
