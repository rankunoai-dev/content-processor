"""EgressGuard: a failed DNS lookup right after an approval is a blip, not a denial."""

from __future__ import annotations

from datetime import UTC, datetime

from src.modules.content_processor.cli import EgressGuard
from src.modules.content_processor.contracts import Authorization, RunScope
from src.modules.content_processor.enums import AuthorizationKind
from src.modules.content_processor.scope.ssrf import EgressDecision, IpClass

HOST = "www.example.com"


def _scope() -> RunScope:
    return RunScope(
        tenant_id="t",
        input_urls=[f"https://{HOST}/"],
        hosts_in_scope=[HOST],
        authorization=Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="public",
            approved_by="lead",
            approved_at=datetime(2026, 10, 21, tzinfo=UTC),
            hosts=[HOST],
        ),
    )


class _Resolver:
    def __init__(self) -> None:
        self.answer: str = "public"

    def __call__(self, host: str) -> EgressDecision:
        if self.answer == "public":
            return EgressDecision(
                host=host,
                allowed=True,
                resolved=["93.184.216.34"],
                classes=[IpClass.PUBLIC],
                reason="public",
            )
        if self.answer == "private":
            return EgressDecision(
                host=host,
                allowed=False,
                resolved=["10.0.0.5"],
                classes=[IpClass.PRIVATE],
                reason="non_public:private",
            )
        return EgressDecision(
            host=host,
            allowed=False,
            resolved=[],
            classes=[],
            reason="dns_failure:[Errno 11001] getaddrinfo failed",
        )


def _guard(now: list[float], resolver: _Resolver) -> EgressGuard:
    return EgressGuard(_scope(), check=resolver, clock=lambda: now[0])


def test_dns_failure_soon_after_approval_reuses_it() -> None:
    now, resolver = [0.0], _Resolver()
    guard = _guard(now, resolver)
    assert guard(HOST) == (True, "public")
    resolver.answer, now[0] = "fail", 30.0
    assert guard(HOST) == (True, "dns_failure_recent_approval")
    assert guard.pinned(HOST) == ["93.184.216.34"]


def test_dns_failure_long_after_approval_is_denied() -> None:
    now, resolver = [0.0], _Resolver()
    guard = _guard(now, resolver)
    guard(HOST)
    resolver.answer, now[0] = "fail", EgressGuard.DNS_BLIP_GRACE_S + 1
    allowed, reason = guard(HOST)
    assert not allowed and reason.startswith("dns_failure")
    assert guard.pinned(HOST) == []


def test_dns_failure_without_any_approval_is_denied() -> None:
    resolver = _Resolver()
    resolver.answer = "fail"
    allowed, _ = _guard([0.0], resolver)(HOST)
    assert not allowed


def test_private_answer_after_approval_is_still_denied() -> None:
    """Rebinding to a private address is an answer, not a blip: deny at once."""
    now, resolver = [0.0], _Resolver()
    guard = _guard(now, resolver)
    guard(HOST)
    resolver.answer, now[0] = "private", 5.0
    assert guard(HOST) == (False, "non_public:private")
    assert guard.pinned(HOST) == []
    # and a failure after that denial no longer reuses the old approval
    resolver.answer = "fail"
    assert guard(HOST)[0] is False
