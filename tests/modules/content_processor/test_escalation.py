from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.modules.content_processor.enums import AccessTier, BlockKind
from src.modules.content_processor.governor.escalation import (
    CONSECUTIVE_FAILURES_TO_OPEN,
    DECAY_AFTER_CLEAN,
    HOST_ESCALATION_DISTINCT_URLS,
    ActionKind,
    CircuitState,
    HostAccessState,
    escalates,
)

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _state(ceiling: AccessTier = AccessTier.T5_MANAGED_UNBLOCKER) -> HostAccessState:
    return HostAccessState(host="example.com", ceiling=ceiling)


def test_fingerprint_block_escalates_to_t2_then_js_challenge_to_t3():
    s = _state()
    a = s.record_block(BlockKind.FINGERPRINT, now=T0)
    assert a.kind is ActionKind.ESCALATE and a.tier is AccessTier.T2_IMPERSONATED_HTTP
    assert a.not_before == T0 + timedelta(seconds=60)
    a = s.record_block(BlockKind.JS_CHALLENGE, now=T0)
    assert a.kind is ActionKind.ESCALATE and s.tier is AccessTier.T3_FIDELITY_BROWSER
    assert s.session_generation == 2


def test_one_blocked_url_escalates_only_itself():
    s = _state()
    for n in (1, 2):
        a = s.record_block(BlockKind.FINGERPRINT, now=T0, distinct_urls=n)
        assert a.kind is ActionKind.ESCALATE and a.tier is AccessTier.T2_IMPERSONATED_HTTP
        assert a.reason == "escalate_url_on:FINGERPRINT"
        assert a.not_before == T0 + timedelta(seconds=60)
    assert s.tier is AccessTier.T1_POLITE_HTTP and s.session_generation == 0


def test_host_moves_after_three_distinct_urls_block_at_its_tier():
    s = _state()
    a = s.record_block(BlockKind.FINGERPRINT, now=T0, distinct_urls=HOST_ESCALATION_DISTINCT_URLS)
    assert a.kind is ActionKind.ESCALATE and a.reason == "escalate_on:FINGERPRINT"
    assert s.tier is AccessTier.T2_IMPERSONATED_HTTP and s.session_generation == 1


def test_url_blocked_again_at_its_own_higher_tier_goes_one_higher():
    # The host is at T1; this URL was already retried at T2 and blocked there too.
    s = _state()
    a = s.record_block(
        BlockKind.FINGERPRINT, now=T0, at_tier=AccessTier.T2_IMPERSONATED_HTTP, distinct_urls=1
    )
    assert a.kind is ActionKind.ESCALATE and a.tier is AccessTier.T3_FIDELITY_BROWSER
    assert s.tier is AccessTier.T1_POLITE_HTTP
    # Above the ceiling it is the ceiling that answers, not the host tier.
    s = _state(ceiling=AccessTier.T2_IMPERSONATED_HTTP)
    a = s.record_block(
        BlockKind.FINGERPRINT, now=T0, at_tier=AccessTier.T2_IMPERSONATED_HTTP, distinct_urls=1
    )
    assert a.kind is ActionKind.BLOCKED and a.reason == "ceiling:T2_IMPERSONATED_HTTP"


def test_escalates_names_the_kinds_a_higher_tier_may_pass():
    assert escalates(BlockKind.FINGERPRINT) and escalates(BlockKind.JS_CHALLENGE)
    assert not escalates(BlockKind.AUTH_WALL) and not escalates(BlockKind.SOFT_404)


def test_paid_tier_above_ceiling_asks_operator_instead_of_escalating():
    s = _state(ceiling=AccessTier.T3_FIDELITY_BROWSER)
    s.record_block(BlockKind.JS_CHALLENGE, now=T0)  # -> T3
    a = s.record_block(BlockKind.INTERACTIVE_CAPTCHA, now=T0)  # wants T5 (paid)
    assert a.kind is ActionKind.NEEDS_APPROVAL
    assert a.proposed_tier is AccessTier.T5_MANAGED_UNBLOCKER
    assert a.reason == "paid_tier_available:T5_MANAGED_UNBLOCKER"
    assert s.tier is AccessTier.T3_FIDELITY_BROWSER
    # Operator raises the ceiling for this host with a cap; the next block escalates.
    s.ceiling = AccessTier.T5_MANAGED_UNBLOCKER
    a = s.record_block(BlockKind.INTERACTIVE_CAPTCHA, now=T0)
    assert a.kind is ActionKind.ESCALATE and s.tier is AccessTier.T5_MANAGED_UNBLOCKER


def test_free_tier_above_ceiling_is_blocked_not_asked():
    s = _state(ceiling=AccessTier.T2_IMPERSONATED_HTTP)
    s.record_block(BlockKind.FINGERPRINT, now=T0)  # -> T2
    a = s.record_block(BlockKind.JS_CHALLENGE, now=T0)  # wants T3 (free, above ceiling)
    assert a.kind is ActionKind.BLOCKED
    assert a.reason == "ceiling:T2_IMPERSONATED_HTTP"


def test_top_of_ladder_is_blocked():
    s = _state(ceiling=AccessTier.T6_POLICY_GATED)
    s.tier = AccessTier.T6_POLICY_GATED
    a = s.record_block(BlockKind.FINGERPRINT, now=T0)
    assert a.kind is ActionKind.BLOCKED and a.reason == "no_higher_tier"


def test_terminal_blocks_and_exclusions_do_not_escalate():
    s = _state()
    assert s.record_block(BlockKind.AUTH_WALL, now=T0).kind is ActionKind.BLOCKED
    assert s.record_block(BlockKind.PAYWALL, now=T0).kind is ActionKind.BLOCKED
    assert s.record_block(BlockKind.LEGAL, now=T0).kind is ActionKind.EXCLUDED
    assert s.record_block(BlockKind.PAY_PER_CRAWL, now=T0).kind is ActionKind.EXCLUDED
    assert s.record_block(BlockKind.AI_OPTOUT, now=T0).kind is ActionKind.EXCLUDED
    assert s.tier is AccessTier.T1_POLITE_HTTP


def test_rate_limit_backs_off_then_opens_circuit():
    s = _state()
    a1 = s.record_block(BlockKind.RATE, now=T0)
    assert a1.kind is ActionKind.BACKOFF
    s.record_block(BlockKind.RATE, now=T0 + timedelta(minutes=1))
    a3 = s.record_block(BlockKind.RATE, now=T0 + timedelta(minutes=2))
    assert a3.kind is ActionKind.CIRCUIT_OPEN
    assert s.circuit is CircuitState.OPEN
    gate = s.can_attempt(now=T0 + timedelta(minutes=5))
    assert gate.kind is ActionKind.CIRCUIT_OPEN
    gate = s.can_attempt(now=T0 + timedelta(minutes=20))
    assert gate.kind is ActionKind.CONTINUE and s.circuit is CircuitState.HALF_OPEN


def test_half_open_failure_reopens_and_success_closes():
    s = _state()
    for i in range(5):
        s.record_transient_failure(now=T0 + timedelta(seconds=i))
    assert s.circuit is CircuitState.OPEN
    s.can_attempt(now=T0 + timedelta(minutes=16))
    assert s.circuit is CircuitState.HALF_OPEN
    a = s.record_transient_failure(now=T0 + timedelta(minutes=16))
    assert a.kind is ActionKind.CIRCUIT_OPEN
    s.can_attempt(now=T0 + timedelta(minutes=40))
    s.record_success()
    assert s.circuit is CircuitState.CLOSED


def test_tier_decays_after_clean_streak():
    s = _state()
    s.record_block(BlockKind.FINGERPRINT, now=T0)
    assert s.tier is AccessTier.T2_IMPERSONATED_HTTP
    for _ in range(DECAY_AFTER_CLEAN - 1):
        assert s.record_success().reason == ""
    a = s.record_success()
    assert a.reason == "tier_decayed" and s.tier is AccessTier.T1_POLITE_HTTP


def test_shadow_block_escalates_one_tier_from_current():
    s = _state()
    s.record_block(BlockKind.JS_CHALLENGE, now=T0)  # T3
    a = s.record_block(BlockKind.SHADOW_BLOCK_SUSPECTED, now=T0)
    assert a.kind is ActionKind.ESCALATE and s.tier is AccessTier.T4_PROXIED


def test_wrong_payload_is_transient_not_escalation():
    s = _state()
    a = s.record_block(BlockKind.WRONG_PAYLOAD, now=T0)
    assert a.kind is ActionKind.BACKOFF and s.tier is AccessTier.T1_POLITE_HTTP


def test_not_found_pages_never_count_towards_the_breaker():
    s = _state()
    s.record_success()
    for i in range(CONSECUTIVE_FAILURES_TO_OPEN + 2):
        a = s.record_block(BlockKind.SOFT_404, now=T0 + timedelta(seconds=i))
        assert a.kind is ActionKind.CONTINUE and a.reason == "not_found"
    assert s.circuit is CircuitState.CLOSED
    assert s.consecutive_failures == 0 and s.clean_streak == 1
    assert s.tier is AccessTier.T1_POLITE_HTTP
