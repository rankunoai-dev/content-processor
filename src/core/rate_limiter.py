"""Client-side rate limiting and spend control.

Two independent protections, both applied *before* a request leaves the process:

* `TokenBucket` — throttles request volume against a provider's documented
  quota. Cheaper and far more reliable than discovering the limit via 429s, and
  it keeps us from getting an API key banned during a scrape.
* `CostLedger` — a hard ceiling on cumulative spend for the process lifetime, so
  a runaway agent loop cannot quietly burn a budget.

Both are thread-safe and use a monotonic clock, so they behave correctly across
NTP adjustments and daylight-saving transitions.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from src.core.config import get_settings
from src.core.errors import BudgetExceededError, RateLimitExceededError
from src.core.logger import get_logger

__all__ = ["CostLedger", "RateLimiterRegistry", "TokenBucket"]

_logger = get_logger("core.rate_limiter")


@dataclass
class TokenBucket:
    """A classic token bucket.

    Attributes:
        key: Identifier of the upstream quota this bucket protects.
        capacity: Maximum burst size, in tokens.
        refill_per_second: Sustained rate at which tokens are replenished.
    """

    key: str
    capacity: int
    refill_per_second: float

    _tokens: float = field(init=False)
    _updated_at: float = field(init=False)
    _lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the configuration and start the bucket full."""
        if self.capacity <= 0:
            msg = f"Bucket '{self.key}' capacity must be positive."
            raise ValueError(msg)
        if self.refill_per_second <= 0:
            msg = f"Bucket '{self.key}' refill rate must be positive."
            raise ValueError(msg)

        self._tokens = float(self.capacity)
        self._updated_at = time.monotonic()
        self._lock = threading.Lock()

    @classmethod
    def per_minute(cls, key: str, requests_per_minute: int) -> TokenBucket:
        """Build a bucket from a requests-per-minute quota."""
        return cls(
            key=key,
            capacity=requests_per_minute,
            refill_per_second=requests_per_minute / 60.0,
        )

    def _refill_locked(self) -> None:
        """Add tokens accrued since the last update. Caller must hold the lock."""
        now = time.monotonic()
        elapsed = now - self._updated_at
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            self._updated_at = now

    def try_acquire(self, tokens: int = 1) -> bool:
        """Take `tokens` if available, without blocking.

        Returns:
            True if the tokens were taken, False if the bucket is short.
        """
        if tokens > self.capacity:
            msg = (
                f"Cannot acquire {tokens} tokens from bucket '{self.key}' "
                f"of capacity {self.capacity}."
            )
            raise ValueError(msg)

        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def time_until_available(self, tokens: int = 1) -> float:
        """Seconds until `tokens` would be available. Zero if available now."""
        with self._lock:
            self._refill_locked()
            deficit = tokens - self._tokens
            return max(0.0, deficit / self.refill_per_second)

    def acquire(self, tokens: int = 1, *, timeout_s: float | None = None) -> None:
        """Block until `tokens` are available.

        Args:
            tokens: How many tokens to take.
            timeout_s: Give up after this long. `None` waits indefinitely.

        Raises:
            RateLimitExceededError: If `timeout_s` elapsed first.
        """
        deadline = None if timeout_s is None else time.monotonic() + timeout_s

        while True:
            if self.try_acquire(tokens):
                return

            wait_s = self.time_until_available(tokens)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or wait_s > remaining:
                    raise RateLimitExceededError(self.key, wait_s)

            _logger.debug("rate_limit_wait", extra={"bucket": self.key, "wait_s": round(wait_s, 3)})
            # Never spin: even a satisfied deficit sleeps a tick so a contended
            # bucket cannot pin a core.
            time.sleep(max(wait_s, 0.001))


class RateLimiterRegistry:
    """Process-wide map of quota key to bucket.

    Tools sharing an upstream quota MUST share a key (see
    `ToolMetadata.rate_limit_key`) so their traffic is limited jointly rather
    than each getting a full allowance.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def get_or_create(self, key: str, requests_per_minute: int | None = None) -> TokenBucket:
        """Return the bucket for `key`, creating it on first use."""
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                rpm = requests_per_minute or get_settings().default_requests_per_minute
                bucket = TokenBucket.per_minute(key, rpm)
                self._buckets[key] = bucket
                _logger.debug("bucket_created", extra={"bucket": key, "rpm": rpm})
            return bucket

    def reset(self) -> None:
        """Drop all buckets. Tests only."""
        with self._lock:
            self._buckets.clear()


class CostLedger:
    """Tracks cumulative spend against a hard ceiling."""

    def __init__(self, ceiling_usd: float | None = None) -> None:
        """Create a ledger.

        Args:
            ceiling_usd: Maximum cumulative spend. Defaults to
                `MAX_SESSION_SPEND_USD`.
        """
        self._ceiling = (
            ceiling_usd if ceiling_usd is not None else get_settings().max_session_spend_usd
        )
        self._spent = 0.0
        self._lock = threading.Lock()

    @property
    def spent_usd(self) -> float:
        """Total recorded spend so far."""
        with self._lock:
            return self._spent

    @property
    def remaining_usd(self) -> float:
        """Headroom left under the ceiling."""
        with self._lock:
            return max(0.0, self._ceiling - self._spent)

    def charge(self, amount_usd: float) -> float:
        """Record a spend, refusing it if it would breach the ceiling.

        The check and the increment happen under one lock, so concurrent tools
        cannot both pass a check that only one of them could afford.

        Args:
            amount_usd: Cost of the operation about to be performed.

        Returns:
            The new cumulative total.

        Raises:
            ValueError: If `amount_usd` is negative.
            BudgetExceededError: If the ceiling would be breached.
        """
        if amount_usd < 0:
            msg = "Cost must not be negative."
            raise ValueError(msg)

        with self._lock:
            if self._spent + amount_usd > self._ceiling:
                raise BudgetExceededError(amount_usd, self._spent, self._ceiling)
            self._spent += amount_usd
            total = self._spent

        if amount_usd > 0:
            _logger.info(
                "spend_recorded",
                extra={"amount_usd": amount_usd, "total_usd": round(total, 6)},
            )
        return total

    def reset(self) -> None:
        """Zero the ledger. Tests and explicit session boundaries only."""
        with self._lock:
            self._spent = 0.0
