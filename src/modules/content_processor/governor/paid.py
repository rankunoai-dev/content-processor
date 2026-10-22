"""Paid tiers in a run: whether they are on, the fetchers, and the operator commands (A-6, A-8).

    cli approve --tenant rankuno-dev --host www.example.com --tier T4_PROXIED
                --cap 5 --by gaurav [--expires-days 7] [--note "..."]
    cli revoke  --tenant rankuno-dev --host www.example.com --by gaurav
    cli spend   [--run <run id>]

(`cli` is `python -m src.modules.content_processor.cli`.)

Paid fetchers are built only when every switch allows it: `BRIGHTDATA_ENABLED`,
a monthly cap, the scope's `access_tier_ceiling` at T4 or T5 (which needs
`budgets.cost_ceiling_usd` > 0) and the zone credentials. Even then a host
uses them only after `approve`; every request also passes `SpendGuard`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.config import Settings, get_settings
from src.integrations.http_client import FetchLimits, HostChecker
from src.integrations.proxy_provider import BrightDataZone, ProxiedFetcher
from src.integrations.unblocker_provider import UnlockerFetcher
from src.modules.content_processor.contracts import RunScope
from src.modules.content_processor.enums import AccessTier, tier_rank
from src.modules.content_processor.governor.spend import (
    Approval,
    GuardedFetcher,
    SpendCaps,
    SpendGuard,
    SpendStore,
)

__all__ = [
    "build_paid_fetchers",
    "paid_tier_status",
    "register",
    "spend_caps",
    "spend_store",
]


def spend_store(settings: Settings | None = None) -> SpendStore:
    """The spend and approval logs (`CP_SPEND_DIR`)."""
    return SpendStore((settings or get_settings()).cp_spend_dir)


def spend_caps(scope: RunScope, settings: Settings | None = None) -> SpendCaps:
    """Caps for one run: the run cap is the smaller of the setting and the scope's ceiling."""
    s = settings or get_settings()
    return SpendCaps(
        enabled=s.brightdata_enabled,
        run_usd=min(s.brightdata_run_cap_usd, scope.budgets.cost_ceiling_usd),
        daily_usd=s.brightdata_daily_cap_usd,
        monthly_usd=s.brightdata_monthly_cap_usd,
        url_usd=s.brightdata_url_cap_usd,
    )


def _zone(s: Settings, zone: str | None, password: Any) -> BrightDataZone | None:
    if not (s.brightdata_customer_id and zone and password):
        return None
    return BrightDataZone(
        customer_id=s.brightdata_customer_id,
        zone=zone,
        password=password,
        host=s.brightdata_proxy_host,
        port=s.brightdata_proxy_port,
    )


def paid_tier_status(scope: RunScope, settings: Settings | None = None) -> str:
    """`on(T4,T5)` or `off(<first reason>)`, recorded on every run report."""
    s = settings or get_settings()
    if not s.brightdata_enabled:
        return "off(kill_switch)"
    if s.brightdata_monthly_cap_usd <= 0:
        return "off(monthly_cap_unset)"
    if tier_rank(scope.access_tier_ceiling) < tier_rank(AccessTier.T4_PROXIED):
        return "off(scope_ceiling)"
    tiers = []
    if _zone(s, s.brightdata_proxy_zone, s.brightdata_proxy_zone_password):
        tiers.append("T4")
    if (
        tier_rank(scope.access_tier_ceiling) >= tier_rank(AccessTier.T5_MANAGED_UNBLOCKER)
        and _zone(s, s.brightdata_unlocker_zone, s.brightdata_unlocker_zone_password)
        and s.brightdata_ca_cert_path is not None
    ):
        tiers.append("T5")
    return f"on({','.join(tiers)})" if tiers else "off(no_credentials)"


def build_paid_fetchers(
    scope: RunScope,
    *,
    run_id: str,
    host_checker: HostChecker,
    user_agent: str,
    accept_language: str,
    settings: Settings | None = None,
    store: SpendStore | None = None,
) -> list[GuardedFetcher]:
    """T4 and T5 fetchers behind the spend guard, or none when paid tiers are off."""
    s = settings or get_settings()
    status = paid_tier_status(scope, s)
    if not status.startswith("on("):
        return []
    guard = SpendGuard(
        store or spend_store(s), spend_caps(scope, s), tenant_id=scope.tenant_id, run_id=run_id
    )
    limits = FetchLimits(max_bytes=min(s.brightdata_paid_max_bytes, scope.budgets.max_bytes))
    country = scope.geography or None
    fetchers: list[GuardedFetcher] = []
    proxy_zone = _zone(s, s.brightdata_proxy_zone, s.brightdata_proxy_zone_password)
    if "T4" in status and proxy_zone is not None:
        t4 = ProxiedFetcher(
            host_checker=host_checker,
            zone=proxy_zone,
            session_salt=run_id,
            user_agent=user_agent,
            usd_per_gb=s.brightdata_proxy_usd_per_gb,
            country=country,
            accept_language=accept_language,
            limits=limits,
            ca_cert=s.brightdata_ca_cert_path,
        )
        fetchers.append(GuardedFetcher(t4, guard))
    unlocker_zone = _zone(s, s.brightdata_unlocker_zone, s.brightdata_unlocker_zone_password)
    if "T5" in status and unlocker_zone is not None:
        t5 = UnlockerFetcher(
            host_checker=host_checker,
            zone=unlocker_zone,
            session_salt=run_id,
            user_agent=user_agent,
            usd_per_1k_requests=s.brightdata_unlocker_usd_per_1k,
            ca_cert=s.brightdata_ca_cert_path,
            country=country,
            accept_language=accept_language,
            limits=limits,
        )
        fetchers.append(GuardedFetcher(t5, guard))
    return fetchers


# -- operator commands ------------------------------------------------------------------


def _emit(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def cmd_approve(args: argparse.Namespace) -> int:
    """Let one host use a paid tier, with a cap per run."""
    expires = datetime.now(UTC) + timedelta(days=args.expires_days) if args.expires_days else None
    approval = Approval(
        tenant_id=args.tenant,
        host=args.host,
        tier=args.tier,
        cap_usd=args.cap,
        approved_by=args.by,
        note=args.note or "",
        expires_at=expires,
    )
    store = spend_store()
    store.approve(approval)
    _emit({"approved": approval.model_dump(), "log": str(store.approvals_path)})
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    """Withdraw a host's paid-tier approval (the log keeps the history)."""
    store = spend_store()
    current = store.active_approval(args.tenant, args.host)
    if current is None:
        _emit({"revoked": None, "detail": "no active approval for this host"})
        return 0
    revoked = current.model_copy(
        update={"at": datetime.now(UTC), "approved_by": args.by, "revoked": True}
    )
    store.approve(revoked)
    _emit({"revoked": revoked.model_dump(), "log": str(store.approvals_path)})
    return 0


def cmd_spend(args: argparse.Namespace) -> int:
    """Totals so far: this month, today, per run, per host (and the caps in force)."""
    s = get_settings()
    store = spend_store(s)
    now = datetime.now(UTC)
    charges = [c for c in store.charges() if args.run is None or c.run_id == args.run]
    month = [c for c in charges if (c.at.year, c.at.month) == (now.year, now.month)]
    by_run: defaultdict[str, float] = defaultdict(float)
    by_host: defaultdict[str, float] = defaultdict(float)
    for c in charges:
        by_run[c.run_id] += c.usd
        by_host[c.host] += c.usd
    _emit(
        {
            "kill_switch": "on" if s.brightdata_enabled else "off",
            "caps_usd": {
                "run": s.brightdata_run_cap_usd,
                "daily": s.brightdata_daily_cap_usd,
                "monthly": s.brightdata_monthly_cap_usd,
                "url": s.brightdata_url_cap_usd,
            },
            "spent_usd": {
                "this_month": round(sum(c.usd for c in month), 4),
                "today": round(sum(c.usd for c in month if c.at.date() == now.date()), 4),
                "by_run": {k: round(v, 4) for k, v in by_run.items()},
                "by_host": {k: round(v, 4) for k, v in by_host.items()},
            },
            "requests": len(charges),
            "active_approvals": [
                a.model_dump()
                for a in {(a.tenant_id, a.host): a for a in store.approvals()}.values()
                if store.active_approval(a.tenant_id, a.host, now) is not None
            ],
        }
    )
    return 0


def register(subparsers: Any) -> None:
    """Add `approve`, `revoke` and `spend` to the CLI."""
    p = subparsers.add_parser("approve", help="let one host use a paid tier, with a cap")
    p.add_argument("--tenant", required=True)
    p.add_argument("--host", required=True, help="host name, e.g. www.example.com")
    p.add_argument("--tier", required=True, choices=["T4_PROXIED", "T5_MANAGED_UNBLOCKER"])
    p.add_argument("--cap", type=float, default=5.0, help="USD per run for this host (default 5)")
    p.add_argument("--by", required=True, help="who approves")
    p.add_argument("--note", default=None)
    p.add_argument("--expires-days", type=float, default=None)
    p.set_defaults(func=cmd_approve)
    r = subparsers.add_parser("revoke", help="withdraw a host's paid-tier approval")
    r.add_argument("--tenant", required=True)
    r.add_argument("--host", required=True)
    r.add_argument("--by", required=True)
    r.set_defaults(func=cmd_revoke)
    s = subparsers.add_parser("spend", help="paid-tier spend so far and the caps in force")
    s.add_argument("--run", default=None)
    s.set_defaults(func=cmd_spend)
