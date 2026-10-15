"""Per-host access state machine (playbook §3) and circuit breaker.

Pure logic, no I/O: the ledger persists `HostAccessState` and workers call its
methods. Every transition returns an `Action` the caller must honour.

Paid tiers (T4+) are never entered automatically: when a block would need one,
the machine returns `NEEDS_APPROVAL` with the proposed tier and the host stays
at its ceiling until an operator raises it for that host with a cost cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from src.modules.content_processor.enums import (
    PAID_TIERS,
    TIER_ORDER,
    AccessTier,
    BlockKind,
    tier_rank,
)

__all__ = ["Action", "ActionKind", "CircuitState", "HostAccessState"]

CIRCUIT_OPEN_FOR = timedelta(minutes=15)
ESCALATION_COOLDOWN = timedelta(seconds=60)
DECAY_AFTER_CLEAN = 200
CONSECUTIVE_FAILURES_TO_OPEN = 5
RATE_WINDOW = timedelta(minutes=10)
RATE_HITS_TO_OPEN = 3


class CircuitState(StrEnum):
    """Classic three-state breaker."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class ActionKind(StrEnum):
    """What the caller must do after reporting an outcome."""

    CONTINUE = "CONTINUE"
    BACKOFF = "BACKOFF"
    ESCALATE = "ESCALATE"
    BLOCKED = "BLOCKED"
    EXCLUDED = "EXCLUDED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"


@dataclass(frozen=True)
class Action:
    """Instruction returned by the state machine."""

    kind: ActionKind
    tier: AccessTier
    not_before: datetime | None = None
    reason: str = ""
    proposed_tier: AccessTier | None = None
    """For NEEDS_APPROVAL: the paid tier that would be tried if the operator approves."""


_NEXT_FOR_KIND: dict[BlockKind, AccessTier | None] = {
    BlockKind.FINGERPRINT: AccessTier.T2_IMPERSONATED_HTTP,
    BlockKind.IP_REPUTATION: AccessTier.T4_PROXIED,
    BlockKind.JS_CHALLENGE: AccessTier.T3_FIDELITY_BROWSER,
    BlockKind.INTERACTIVE_CAPTCHA: AccessTier.T5_MANAGED_UNBLOCKER,
    BlockKind.SHADOW_BLOCK_SUSPECTED: None,  # one tier up from current
}
_TERMINAL_BLOCKED = frozenset({BlockKind.AUTH_WALL, BlockKind.PAYWALL, BlockKind.GEO_GATE})
_EXCLUDED = frozenset({BlockKind.LEGAL, BlockKind.PAY_PER_CRAWL, BlockKind.AI_OPTOUT})


@dataclass
class HostAccessState:
    """Mutable per-host state. Persisted by the ledger between calls."""

    host: str
    ceiling: AccessTier
    tier: AccessTier = AccessTier.T1_POLITE_HTTP
    circuit: CircuitState = CircuitState.CLOSED
    circuit_opened_at: datetime | None = None
    consecutive_failures: int = 0
    clean_streak: int = 0
    block_score: float = 0.0
    rate_hits: list[datetime] = field(default_factory=list)
    session_generation: int = 0

    def _now(self, now: datetime | None) -> datetime:
        return now or datetime.now(UTC)

    def _next_tier(self, target: AccessTier | None) -> AccessTier | None:
        current = tier_rank(self.tier)
        candidate = tier_rank(target) if target is not None else current + 1
        candidate = max(candidate, current + 1)
        if candidate >= len(TIER_ORDER):
            return None
        return TIER_ORDER[candidate]

    def can_attempt(self, now: datetime | None = None) -> Action:
        """Gate a request: honours the breaker and returns the tier to use."""
        now = self._now(now)
        if self.circuit is CircuitState.OPEN:
            assert self.circuit_opened_at is not None  # noqa: S101 - invariant
            reopen_at = self.circuit_opened_at + CIRCUIT_OPEN_FOR
            if now < reopen_at:
                return Action(ActionKind.CIRCUIT_OPEN, self.tier, reopen_at, "circuit_open")
            self.circuit = CircuitState.HALF_OPEN
        return Action(ActionKind.CONTINUE, self.tier)

    def record_success(self) -> Action:
        """A clean, expected payload at the current tier."""
        self.consecutive_failures = 0
        self.clean_streak += 1
        self.circuit = CircuitState.CLOSED
        self.circuit_opened_at = None
        self.block_score = max(0.0, self.block_score - 0.05)
        if self.clean_streak >= DECAY_AFTER_CLEAN and tier_rank(self.tier) > 1:
            self.tier = TIER_ORDER[tier_rank(self.tier) - 1]
            self.clean_streak = 0
            self.session_generation += 1
            return Action(ActionKind.CONTINUE, self.tier, reason="tier_decayed")
        return Action(ActionKind.CONTINUE, self.tier)

    def record_transient_failure(self, now: datetime | None = None) -> Action:
        """Transport/5xx failure: same tier, bounded backoff, breaker counting."""
        now = self._now(now)
        self.clean_streak = 0
        self.consecutive_failures += 1
        if self.circuit is CircuitState.HALF_OPEN or (
            self.consecutive_failures >= CONSECUTIVE_FAILURES_TO_OPEN
        ):
            return self._open_circuit(now, "consecutive_failures")
        backoff = timedelta(seconds=min(60, 2**self.consecutive_failures))
        return Action(ActionKind.BACKOFF, self.tier, now + backoff, "transient")

    def record_block(self, kind: BlockKind, now: datetime | None = None) -> Action:
        """A classified block at the current tier. Decides escalate/blocked/excluded."""
        now = self._now(now)
        self.clean_streak = 0
        self.block_score = min(1.0, self.block_score + 0.25)

        if kind in _EXCLUDED:
            return Action(ActionKind.EXCLUDED, self.tier, reason=kind.value.lower())
        if kind in _TERMINAL_BLOCKED:
            return Action(ActionKind.BLOCKED, self.tier, reason=kind.value.lower())
        if kind is BlockKind.RATE:
            self.rate_hits = [t for t in self.rate_hits if now - t <= RATE_WINDOW] + [now]
            if len(self.rate_hits) >= RATE_HITS_TO_OPEN:
                return self._open_circuit(now, "rate_limited_repeatedly")
            return Action(ActionKind.BACKOFF, self.tier, now + timedelta(seconds=30), "rate")
        if kind in (BlockKind.TRANSPORT, BlockKind.SOFT_404, BlockKind.WRONG_PAYLOAD):
            return self.record_transient_failure(now)

        target = self._next_tier(_NEXT_FOR_KIND.get(kind))
        if target is None:
            return Action(ActionKind.BLOCKED, self.tier, reason="no_higher_tier")
        if tier_rank(target) > tier_rank(self.ceiling):
            if target in PAID_TIERS:
                # Policy (2026-10-15): paid rungs are never taken automatically. The
                # host stays BLOCKED at the ceiling and the operator is asked.
                return Action(
                    ActionKind.NEEDS_APPROVAL,
                    self.tier,
                    reason=f"paid_tier_available:{target.value}",
                    proposed_tier=target,
                )
            return Action(ActionKind.BLOCKED, self.tier, reason=f"ceiling:{self.ceiling}")
        self.tier = target
        self.session_generation += 1
        self.consecutive_failures = 0
        return Action(
            ActionKind.ESCALATE, target, now + ESCALATION_COOLDOWN, f"escalate_on:{kind.value}"
        )

    def _open_circuit(self, now: datetime, reason: str) -> Action:
        self.circuit = CircuitState.OPEN
        self.circuit_opened_at = now
        self.consecutive_failures = 0
        return Action(ActionKind.CIRCUIT_OPEN, self.tier, now + CIRCUIT_OPEN_FOR, reason)
