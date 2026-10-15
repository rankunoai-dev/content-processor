"""Tests for quota throttling and spend control."""

from __future__ import annotations

import threading

import pytest

from src.core.errors import BudgetExceededError, RateLimitExceededError
from src.core.rate_limiter import CostLedger, RateLimiterRegistry, TokenBucket


class TestTokenBucket:
    def test_starts_full(self):
        bucket = TokenBucket(key="t", capacity=3, refill_per_second=1.0)
        assert all(bucket.try_acquire() for _ in range(3))
        assert bucket.try_acquire() is False

    def test_rejects_invalid_configuration(self):
        with pytest.raises(ValueError, match="capacity"):
            TokenBucket(key="t", capacity=0, refill_per_second=1.0)
        with pytest.raises(ValueError, match="refill"):
            TokenBucket(key="t", capacity=1, refill_per_second=0.0)

    def test_cannot_request_more_than_capacity(self):
        """Otherwise `acquire` would block forever on an unsatisfiable request."""
        bucket = TokenBucket(key="t", capacity=2, refill_per_second=1.0)
        with pytest.raises(ValueError):
            bucket.try_acquire(3)

    def test_per_minute_constructor(self):
        bucket = TokenBucket.per_minute("gsc", 120)
        assert bucket.capacity == 120
        assert bucket.refill_per_second == pytest.approx(2.0)

    def test_time_until_available_is_zero_when_full(self):
        bucket = TokenBucket(key="t", capacity=5, refill_per_second=5.0)
        assert bucket.time_until_available() == pytest.approx(0.0)

    def test_time_until_available_reports_deficit(self):
        bucket = TokenBucket(key="t", capacity=1, refill_per_second=1.0)
        assert bucket.try_acquire() is True
        assert bucket.time_until_available() > 0.0

    def test_acquire_times_out_rather_than_hanging(self):
        bucket = TokenBucket(key="t", capacity=1, refill_per_second=0.01)
        bucket.acquire()
        with pytest.raises(RateLimitExceededError) as exc_info:
            bucket.acquire(timeout_s=0.05)
        assert exc_info.value.key == "t"

    def test_acquire_succeeds_after_refill(self):
        bucket = TokenBucket(key="t", capacity=1, refill_per_second=100.0)
        bucket.acquire()
        bucket.acquire(timeout_s=1.0)  # ~10ms of refill; must not raise

    def test_concurrent_acquire_never_oversubscribes(self):
        """Ten threads, five tokens, no refill in the test window."""
        bucket = TokenBucket(key="t", capacity=5, refill_per_second=0.001)
        granted: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            got = bucket.try_acquire()
            with lock:
                granted.append(got)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(granted) == 5


class TestRateLimiterRegistry:
    def test_same_key_returns_the_same_bucket(self, settings, monkeypatch):
        monkeypatch.setattr("src.core.rate_limiter.get_settings", lambda: settings)
        reg = RateLimiterRegistry()
        assert reg.get_or_create("google.gsc") is reg.get_or_create("google.gsc")

    def test_distinct_keys_are_independent(self, settings, monkeypatch):
        monkeypatch.setattr("src.core.rate_limiter.get_settings", lambda: settings)
        reg = RateLimiterRegistry()
        assert reg.get_or_create("a") is not reg.get_or_create("b")


class TestCostLedger:
    def test_tracks_cumulative_spend(self):
        ledger = CostLedger(ceiling_usd=1.0)
        ledger.charge(0.25)
        ledger.charge(0.25)
        assert ledger.spent_usd == pytest.approx(0.5)
        assert ledger.remaining_usd == pytest.approx(0.5)

    def test_refuses_spend_past_the_ceiling(self):
        ledger = CostLedger(ceiling_usd=1.0)
        ledger.charge(0.9)
        with pytest.raises(BudgetExceededError):
            ledger.charge(0.2)
        assert ledger.spent_usd == pytest.approx(0.9), "refused spend must not be recorded"

    def test_rejects_negative_charges(self):
        with pytest.raises(ValueError):
            CostLedger(ceiling_usd=1.0).charge(-1.0)

    def test_zero_cost_is_always_allowed(self):
        ledger = CostLedger(ceiling_usd=0.0)
        assert ledger.charge(0.0) == pytest.approx(0.0)

    def test_concurrent_charges_cannot_breach_the_ceiling(self):
        """Check-then-increment must be atomic, or two tools both 'afford' it."""
        ledger = CostLedger(ceiling_usd=1.0)
        refused = 0
        lock = threading.Lock()

        def worker() -> None:
            nonlocal refused
            try:
                ledger.charge(0.1)
            except BudgetExceededError:
                with lock:
                    refused += 1

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert ledger.spent_usd <= 1.0 + 1e-9
        assert refused == 10
