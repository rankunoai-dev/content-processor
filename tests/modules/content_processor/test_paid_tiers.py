"""Paid tiers (A-6, A-7, A-8, K10): Bright Data fetchers, approvals and the spend guard.

No network and no spend: every paid fetcher runs over a mock transport.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from src.core.config import Environment, Settings
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, FetchResult, HttpFetcher
from src.integrations.proxy_provider import (
    BrightDataZone,
    ProxiedFetcher,
    brightdata_username,
    session_id,
)
from src.integrations.unblocker_provider import UnlockerFetcher
from src.modules.content_processor.contracts import Authorization, Budgets, Resource, RunScope
from src.modules.content_processor.enums import (
    AccessTier,
    AuthorizationKind,
    EntityStatus,
    TaskKind,
)
from src.modules.content_processor.governor import paid
from src.modules.content_processor.governor.escalation import HostAccessState
from src.modules.content_processor.governor.spend import (
    Approval,
    GuardedFetcher,
    SpendCaps,
    SpendEntry,
    SpendGuard,
    SpendStore,
    apply_approval,
    approvals_lookup,
    requeue_blocked,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.scope.frontier import build_frontier
from src.modules.content_processor.workers.fetch_worker import FetchWorker
from src.modules.content_processor.workers.ports import RobotsCache

HOST = "shop.example.com"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ZONE = BrightDataZone(customer_id="hl_123", zone="isp1", password=SecretStr("p@ss/word"))
PAGE = b"<html><body><main><p>Paid tier page with real content.</p></main></body></html>"


def _ok(_h: str) -> tuple[bool, str]:
    return True, "test"


def _proxied(seen: list[str] | None = None, body: bytes = PAGE, **kw) -> ProxiedFetcher:
    def factory(proxy: str) -> httpx.AsyncBaseTransport:
        if seen is not None:
            seen.append(proxy)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})

        return httpx.MockTransport(handler)

    options = {
        "host_checker": _ok,
        "zone": ZONE,
        "session_salt": "run-1",
        "user_agent": "Mozilla/5.0",
        "usd_per_gb": 10.0,
        "country": "US",
        "limits": FetchLimits(max_bytes=1_000_000, max_hops=4),
        "transport_factory": factory,
    }
    options.update(kw)
    return ProxiedFetcher(**options)


def _store(tmp_path: Path) -> SpendStore:
    return SpendStore(tmp_path / "spend")


def _approve(store: SpendStore, tier: str = "T4_PROXIED", cap: float = 5.0, **kw) -> None:
    store.approve(
        Approval(tenant_id="t1", host=HOST, tier=tier, cap_usd=cap, approved_by="op", **kw)  # type: ignore[arg-type]
    )


def _caps(**kw: float | bool) -> SpendCaps:
    values: dict[str, float | bool] = {
        "enabled": True,
        "run_usd": 5.0,
        "daily_usd": 25.0,
        "monthly_usd": 100.0,
        "url_usd": 0.25,
    }
    values.update(kw)
    return SpendCaps(**values)  # type: ignore[arg-type]


def _guard(store: SpendStore, **kw: float | bool) -> SpendGuard:
    return SpendGuard(store, _caps(**kw), tenant_id="t1", run_id="run-1", clock=lambda: NOW)


# -- Bright Data providers ------------------------------------------------------------------


def test_username_and_sticky_sessions():
    assert brightdata_username("hl_1", "isp1") == "brd-customer-hl_1-zone-isp1"
    assert (
        brightdata_username("hl_1", "isp1", country="US", session="abc")
        == "brd-customer-hl_1-zone-isp1-country-us-session-abc"
    )
    assert session_id("run-1", "a.com") == session_id("run-1", "a.com")
    assert session_id("run-1", "a.com") != session_id("run-1", "b.com")
    assert session_id("run-1", "a.com") != session_id("run-2", "a.com")


async def test_proxied_fetcher_uses_one_session_per_host_and_prices_by_volume():
    seen: list[str] = []
    fetcher = _proxied(seen)
    try:
        first = await fetcher.fetch(f"https://{HOST}/a")
        await fetcher.fetch(f"https://{HOST}/b")
        await fetcher.fetch("https://other.example.com/c")
    finally:
        await fetcher.aclose()
    assert first.http_status == 200 and first.body == PAGE
    assert len(seen) == 2, "one client (one exit IP) per host"
    proxy = urlsplit(seen[0])
    assert proxy.hostname == "brd.superproxy.io" and proxy.port == 33335
    assert unquote(proxy.username or "").startswith(
        "brd-customer-hl_123-zone-isp1-country-us-session-"
    )
    assert unquote(proxy.password or "") == "p@ss/word"
    assert fetcher.tier_name == "T4_PROXIED"
    worst = fetcher.worst_case_usd()
    assert worst == pytest.approx((1_000_000 + 5 * 16384) / 1e9 * 10)
    cost = fetcher.cost_usd(first)
    assert 0 < cost < worst
    assert cost == pytest.approx((len(PAGE) + 16384) / 1e9 * 10)


def test_unlocker_needs_the_vendor_certificate_and_prices_per_request():
    with pytest.raises(ValueError, match="CA certificate"):
        UnlockerFetcher(
            host_checker=_ok,
            zone=ZONE,
            session_salt="r",
            user_agent="u",
            usd_per_1k_requests=3.0,
            ca_cert=None,
        )
    t5 = UnlockerFetcher(
        host_checker=_ok,
        zone=ZONE,
        session_salt="r",
        user_agent="u",
        usd_per_1k_requests=3.0,
        ca_cert=None,
        limits=FetchLimits(max_hops=2),
        transport_factory=lambda _p: httpx.MockTransport(lambda _r: httpx.Response(200)),
    )
    assert t5.tier_name == "T5_MANAGED_UNBLOCKER"
    assert t5.worst_case_usd() == pytest.approx(3 * 0.003)
    result = FetchResult(requested_url="u", final_url="u", http_status=200)
    assert t5.cost_usd(result) == pytest.approx(0.003)


# -- the spend guard ------------------------------------------------------------------------


def test_guard_allows_an_approved_host_within_every_cap(tmp_path: Path):
    store = _store(tmp_path)
    _approve(store)
    assert _guard(store).refusal(HOST, f"https://{HOST}/", AccessTier.T4_PROXIED, 0.01) is None


@pytest.mark.parametrize(
    ("setup", "caps", "tier", "expected"),
    [
        ("approve", {"enabled": False}, "T4_PROXIED", "kill_switch_off"),
        ("approve", {"monthly_usd": 0.0}, "T4_PROXIED", "monthly_cap_unset"),
        ("none", {}, "T4_PROXIED", f"no_approval:{HOST}"),
        ("approve", {}, "T5_MANAGED_UNBLOCKER", "approved_only_to:T4_PROXIED"),
        ("expired", {}, "T4_PROXIED", f"no_approval:{HOST}"),
        ("revoked", {}, "T4_PROXIED", f"no_approval:{HOST}"),
    ],
)
def test_guard_refuses_without_every_switch(
    tmp_path: Path, setup: str, caps: dict[str, float | bool], tier: str, expected: str
):
    store = _store(tmp_path)
    if setup == "approve":
        _approve(store)
    elif setup == "expired":
        _approve(store, expires_at=NOW - timedelta(minutes=1))
    elif setup == "revoked":
        _approve(store)
        _approve(store, revoked=True)
    guard = _guard(store, **caps)
    assert guard.refusal(HOST, f"https://{HOST}/", AccessTier(tier), 0.01) == expected


@pytest.mark.parametrize(
    ("charges", "caps", "cap_name"),
    [
        ([("run-1", HOST, "u1", 4.995)], {}, "run_cap"),
        ([("run-0", "x.com", "u0", 24.995)], {}, "daily_cap"),
        ([("run-0", "x.com", "u0", 99.995)], {"daily_usd": 1000.0}, "monthly_cap"),
        ([("run-1", HOST, "u1", 1.995)], {"run_usd": 50.0}, "host_cap"),
        ([("run-1", HOST, f"https://{HOST}/", 0.245)], {}, "url_cap"),
    ],
)
def test_guard_refuses_when_the_worst_case_would_cross_a_cap(
    tmp_path: Path,
    charges: list[tuple[str, str, str, float]],
    caps: dict[str, float],
    cap_name: str,
):
    store = _store(tmp_path)
    _approve(store, cap=2.0 if cap_name == "host_cap" else 5.0)
    for run, host, url, usd in charges:
        store.charge(
            SpendEntry(
                at=NOW,
                tenant_id="t1",
                run_id=run,
                host=host,
                url=url,
                tier="T4",
                vendor="v",
                usd=usd,
            )
        )
    reason = _guard(store, **caps).refusal(HOST, f"https://{HOST}/", AccessTier.T4_PROXIED, 0.01)
    assert reason is not None and reason.startswith(f"{cap_name}:")


def test_charges_from_last_month_do_not_count_today(tmp_path: Path):
    store = _store(tmp_path)
    _approve(store)
    old = NOW - timedelta(days=40)
    store.charge(
        SpendEntry(
            at=old, tenant_id="t1", run_id="old", host="x", url="u", tier="T4", vendor="v", usd=99.0
        )
    )
    assert _guard(store).refusal(HOST, "u2", AccessTier.T4_PROXIED, 0.01) is None


async def test_guarded_fetcher_refuses_before_sending_and_charges_after(tmp_path: Path):
    store = _store(tmp_path)
    seen: list[str] = []
    guarded = GuardedFetcher(_proxied(seen), _guard(store))
    refused = await guarded.fetch(f"https://{HOST}/page")
    assert refused.spend_refused == f"no_approval:{HOST}" and refused.http_status is None
    assert seen == [] and store.charges() == []
    _approve(store)
    try:
        ok = await guarded.fetch(f"https://{HOST}/page")
    finally:
        await guarded.aclose()
    assert ok.http_status == 200 and ok.spend_refused is None
    (charge,) = store.charges()
    assert (
        charge.host == HOST and charge.tier == "T4_PROXIED" and charge.vendor == "brightdata_proxy"
    )
    assert charge.usd == pytest.approx((len(PAGE) + 16384) / 1e9 * 10)


# -- approvals in the governor ----------------------------------------------------------------


def test_approval_raises_the_ceiling_but_never_past_the_scope(tmp_path: Path):
    approval = Approval(
        tenant_id="t1", host=HOST, tier="T5_MANAGED_UNBLOCKER", cap_usd=5, approved_by="op"
    )
    state = HostAccessState(host=HOST, ceiling=AccessTier.T3_FIDELITY_BROWSER)
    assert apply_approval(state, approval, AccessTier.T4_PROXIED)
    assert state.ceiling is AccessTier.T4_PROXIED
    assert not apply_approval(state, approval, AccessTier.T4_PROXIED), "already there"
    assert not apply_approval(state, None, AccessTier.T5_MANAGED_UNBLOCKER)
    with pytest.raises(ValueError, match="plain host name"):
        Approval(
            tenant_id="t1", host="https://x.com/", tier="T4_PROXIED", cap_usd=5, approved_by="o"
        )
    with pytest.raises(ValueError):
        Approval(
            tenant_id="t1", host="x.com", tier="T3_FIDELITY_BROWSER", cap_usd=5, approved_by="o"
        )  # type: ignore[arg-type]


def test_requeue_blocked_urls_of_the_approved_host():
    ledger = MemoryLedger(base_delay_s=0)
    for path, status in (("/a", EntityStatus.BLOCKED), ("/b", EntityStatus.EXTRACTED_UNVERIFIED)):
        url = f"https://{HOST}{path}"
        ledger.upsert_resource(
            Resource(
                resource_id=f"r{path}",
                run_id="run-1",
                requested_url=url,
                schedule_key=url,
                status=status,
            ),
            HOST,
        )
    other = "https://x.com/c"
    ledger.upsert_resource(
        Resource(
            resource_id="rc",
            run_id="run-1",
            requested_url=other,
            schedule_key=other,
            status=EntityStatus.BLOCKED,
        ),
        "x.com",
    )
    assert requeue_blocked(ledger, "run-1", HOST, AccessTier.T4_PROXIED) == 1
    (task,) = list(ledger._tasks.values())
    assert task.kind is TaskKind.FETCH and task.payload["approved_tier"] == "T4_PROXIED"
    assert task.payload["requested_url"] == f"https://{HOST}/a"
    assert requeue_blocked(ledger, "run-1", HOST, AccessTier.T3_FIDELITY_BROWSER) == 0


# -- when paid tiers are on -------------------------------------------------------------------


def _settings(tmp_path: Path, **kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "environment": Environment.DEVELOPMENT,
        "brightdata_enabled": True,
        "brightdata_monthly_cap_usd": 50.0,
        "brightdata_customer_id": "hl_123",
        "brightdata_proxy_zone": "isp1",
        "brightdata_proxy_zone_password": "pw",
        "cp_spend_dir": tmp_path / "spend",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _scope(ceiling: AccessTier = AccessTier.T5_MANAGED_UNBLOCKER, cost: float = 5.0) -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=[f"https://{HOST}/"],
        hosts_in_scope=[HOST],
        authorization=Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="public",
            approved_by="lead",
            approved_at=NOW,
            hosts=[HOST],
        ),
        access_tier_ceiling=ceiling,
        budgets=Budgets(cost_ceiling_usd=cost),
    )


@pytest.mark.parametrize(
    ("overrides", "ceiling", "expected"),
    [
        ({"brightdata_enabled": False}, AccessTier.T5_MANAGED_UNBLOCKER, "off(kill_switch)"),
        (
            {"brightdata_monthly_cap_usd": 0.0},
            AccessTier.T5_MANAGED_UNBLOCKER,
            "off(monthly_cap_unset)",
        ),
        ({}, AccessTier.T3_FIDELITY_BROWSER, "off(scope_ceiling)"),
        ({"brightdata_proxy_zone": None}, AccessTier.T4_PROXIED, "off(no_credentials)"),
        ({}, AccessTier.T5_MANAGED_UNBLOCKER, "on(T4)"),
        (
            {
                "brightdata_unlocker_zone": "unl1",
                "brightdata_unlocker_zone_password": "pw2",
                "brightdata_ca_cert_path": Path("ca.crt"),
            },
            AccessTier.T5_MANAGED_UNBLOCKER,
            "on(T4,T5)",
        ),
    ],
)
def test_paid_tier_status(
    tmp_path: Path, overrides: dict[str, object], ceiling: AccessTier, expected: str
):
    cost = 5.0 if ceiling in (AccessTier.T4_PROXIED, AccessTier.T5_MANAGED_UNBLOCKER) else 0.0
    assert (
        paid.paid_tier_status(_scope(ceiling, cost), _settings(tmp_path, **overrides)) == expected
    )


def test_build_paid_fetchers_follows_the_status(tmp_path: Path):
    common = {"run_id": "run-1", "host_checker": _ok, "user_agent": "u", "accept_language": "en"}
    off = paid.build_paid_fetchers(
        _scope(), settings=_settings(tmp_path, brightdata_enabled=False), **common
    )
    assert off == []
    on = paid.build_paid_fetchers(_scope(), settings=_settings(tmp_path), **common)
    assert [f.tier_name for f in on] == ["T4_PROXIED"]
    caps = paid.spend_caps(_scope(cost=2.0), _settings(tmp_path))
    assert caps.run_usd == 2.0, "the smaller of the setting and the scope"


# -- end to end through the fetch worker ---------------------------------------------------


async def _run_worker(tmp_path: Path, store: SpendStore, guard: SpendGuard) -> MemoryLedger:
    scope = _scope()
    ledger = MemoryLedger(ceiling=scope.access_tier_ceiling, base_delay_s=0)
    ledger.robots[HOST] = RobotsCache(body="User-agent: *\nAllow: /\n", status=200, fetched_at=NOW)
    ledger.load_host_state("t1", HOST).tier = AccessTier.T3_FIDELITY_BROWSER
    plan = build_frontier(scope, resolver=lambda _h: ["93.184.216.34"])
    ledger.enqueue(fetch_tasks_for("run-1", plan))

    def challenge(_r: httpx.Request) -> httpx.Response:
        body = b"<html><body>Just a moment... cf-chl challenge-platform</body></html>"
        return httpx.Response(200, content=body, headers={"cf-mitigated": "challenge"})

    t1 = HttpFetcher(host_checker=_ok, user_agent="t", transport=httpx.MockTransport(challenge))
    t4 = GuardedFetcher(_proxied(), guard)
    worker = FetchWorker(
        ledger=ledger,
        fetcher=t1,
        evidence=LocalEvidenceStore(tmp_path / "ev", "t1"),
        scope=scope,
        run_id="run-1",
        worker_id="w",
        escalation_fetchers=[t4],
        approvals=approvals_lookup(store, "t1"),
    )
    try:
        await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
        for task in ledger._tasks.values():
            task.not_before = None  # skip the escalation cooldown
        await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    finally:
        await t1.aclose()
        await t4.aclose()
    return ledger


async def test_unapproved_host_stops_at_needs_approval_and_spends_nothing(tmp_path: Path):
    store = _store(tmp_path)
    ledger = await _run_worker(tmp_path, store, _guard(store))
    res = ledger.resources[f"https://{HOST}/"]
    assert (
        res.status is EntityStatus.BLOCKED
        and res.acquired_at_tier is AccessTier.T1_POLITE_HTTP  # best free HTTP fetcher here
    )
    assert any((e.reason or "").startswith("paid_tier_available:T4") for e in ledger.events)
    assert store.charges() == []


async def test_approved_host_escalates_into_t4_and_is_charged(tmp_path: Path):
    store = _store(tmp_path)
    _approve(store)
    ledger = await _run_worker(tmp_path, store, _guard(store))
    res = ledger.resources[f"https://{HOST}/"]
    assert res.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert res.acquired_at_tier is AccessTier.T4_PROXIED
    assert ledger.hosts[HOST].tier is AccessTier.T4_PROXIED
    assert len(store.charges()) >= 1 and all(c.tier == "T4_PROXIED" for c in store.charges())


async def test_approved_host_over_its_cap_is_refused_not_charged(tmp_path: Path):
    store = _store(tmp_path)
    _approve(store)
    ledger = await _run_worker(tmp_path, store, _guard(store, run_usd=0.0001))
    res = ledger.resources[f"https://{HOST}/"]
    assert res.status is EntityStatus.BLOCKED
    assert (res.status_reason or "").startswith("paid_tier_refused:run_cap:")
    assert store.charges() == []


# -- operator commands -------------------------------------------------------------------


def test_approve_revoke_and_spend_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    settings = _settings(tmp_path)
    monkeypatch.setattr(paid, "get_settings", lambda: settings)
    parser = argparse.ArgumentParser()
    paid.register(parser.add_subparsers(dest="command"))

    def run(*argv: str) -> dict:
        args = parser.parse_args(list(argv))
        assert args.func(args) == 0
        return json.loads(capsys.readouterr().out)

    out = run(
        "approve",
        "--tenant",
        "t1",
        "--host",
        "Shop.Example.com",
        "--tier",
        "T4_PROXIED",
        "--cap",
        "3",
        "--by",
        "gaurav",
        "--expires-days",
        "7",
    )
    assert out["approved"]["host"] == HOST and out["approved"]["cap_usd"] == 3.0
    store = paid.spend_store(settings)
    assert store.active_approval("t1", HOST) is not None
    store.charge(
        SpendEntry(
            tenant_id="t1",
            run_id="run-1",
            host=HOST,
            url="u",
            tier="T4_PROXIED",
            vendor="v",
            usd=0.5,
        )
    )
    report = run("spend")
    assert report["spent_usd"]["this_month"] == 0.5 and report["spent_usd"]["by_host"] == {
        HOST: 0.5
    }
    assert [a["host"] for a in report["active_approvals"]] == [HOST]
    assert run("spend", "--run", "other")["requests"] == 0
    revoked = run("revoke", "--tenant", "t1", "--host", HOST, "--by", "gaurav")
    assert revoked["revoked"]["revoked"] is True and store.active_approval("t1", HOST) is None
    assert run("revoke", "--tenant", "t1", "--host", HOST, "--by", "gaurav")["revoked"] is None
    assert run("spend")["active_approvals"] == []
