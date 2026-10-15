from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.modules.content_processor.contracts import Authorization, Budgets, RunScope
from src.modules.content_processor.enums import AccessTier, AuthorizationKind, RobotsPolicy


def _auth(kind: AuthorizationKind, reference: str = "ref") -> Authorization:
    return Authorization(
        kind=kind,
        reference=reference,
        approved_by="lead",
        approved_at=datetime(2026, 10, 15, tzinfo=UTC),
        hosts=["example.com"],
    )


def _scope(**overrides):
    base = {
        "tenant_id": "t1",
        "input_urls": ["https://example.com/"],
        "hosts_in_scope": ["example.com"],
        "authorization": _auth(AuthorizationKind.PUBLIC_THIRD_PARTY),
    }
    base.update(overrides)
    return RunScope(**base)


def test_robots_defaults_follow_authorization_kind():
    assert _scope().robots_policy is RobotsPolicy.RESPECT
    owner = _scope(authorization=_auth(AuthorizationKind.OWNER_AUTHORIZED))
    assert owner.robots_policy is RobotsPolicy.INFORMATIONAL


def test_explicit_robots_policy_is_kept():
    s = _scope(robots_policy=RobotsPolicy.INFORMATIONAL)
    assert s.robots_policy is RobotsPolicy.INFORMATIONAL


def test_respect_with_exceptions_requires_paths():
    with pytest.raises(ValidationError, match="robots_exceptions"):
        _scope(robots_policy=RobotsPolicy.RESPECT_WITH_EXCEPTIONS)
    auth = _auth(AuthorizationKind.OWNER_AUTHORIZED)
    auth.robots_exceptions = ["/private-docs/"]
    s = _scope(authorization=auth, robots_policy=RobotsPolicy.RESPECT_WITH_EXCEPTIONS)
    assert s.robots_policy is RobotsPolicy.RESPECT_WITH_EXCEPTIONS


@pytest.mark.parametrize("tier", [AccessTier.T4_PROXIED, AccessTier.T5_MANAGED_UNBLOCKER])
def test_paid_tiers_need_a_cost_ceiling(tier: AccessTier):
    with pytest.raises(ValidationError, match="cost_ceiling_usd"):
        _scope(access_tier_ceiling=tier, budgets=Budgets(cost_ceiling_usd=0.0))
    s = _scope(access_tier_ceiling=tier, budgets=Budgets(cost_ceiling_usd=5.0))
    assert s.access_tier_ceiling is tier


def test_t6_requires_valid_dual_signoff_token():
    with pytest.raises(ValidationError, match="dual sign-off"):
        _scope(access_tier_ceiling=AccessTier.T6_POLICY_GATED, budgets=Budgets(cost_ceiling_usd=5))
    expired = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    with pytest.raises(ValidationError, match="dual sign-off"):
        _scope(
            access_tier_ceiling=AccessTier.T6_POLICY_GATED,
            budgets=Budgets(cost_ceiling_usd=5),
            authorization=_auth(
                AuthorizationKind.PUBLIC_THIRD_PARTY, f"t6:TCK-1:lead:client:{expired}"
            ),
        )
    same_person = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    with pytest.raises(ValidationError, match="dual sign-off"):
        _scope(
            access_tier_ceiling=AccessTier.T6_POLICY_GATED,
            budgets=Budgets(cost_ceiling_usd=5),
            authorization=_auth(
                AuthorizationKind.PUBLIC_THIRD_PARTY, f"t6:TCK-1:lead:lead:{same_person}"
            ),
        )
    valid = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    s = _scope(
        access_tier_ceiling=AccessTier.T6_POLICY_GATED,
        budgets=Budgets(cost_ceiling_usd=5),
        authorization=_auth(AuthorizationKind.PUBLIC_THIRD_PARTY, f"t6:TCK-1:lead:client:{valid}"),
    )
    assert s.authorization.t6_approval() is not None


def test_authenticated_client_requires_owner_authorization():
    with pytest.raises(ValidationError, match="OWNER_AUTHORIZED"):
        _scope(access_context="authenticated_client")
    s = _scope(
        access_context="authenticated_client",
        authorization=_auth(AuthorizationKind.OWNER_AUTHORIZED),
    )
    assert s.access_context == "authenticated_client"


def test_scope_hash_is_stable_and_ignores_recrawl_pointer():
    a = _scope()
    b = _scope(recrawl_of_run_id="run-old")
    assert a.scope_hash() == b.scope_hash()
    c = _scope(locale="hi-IN")
    assert c.scope_hash() != a.scope_hash()


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        _scope(unexpected="x")
