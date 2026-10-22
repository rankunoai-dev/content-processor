"""Paid-tier governance: per-host approvals and the spend guard (A-7, A-8, K10).

Decided 2026-10-15 (ARCHITECTURE.md section 19): runs cost $0 by default. A
host may enter a paid tier (T4 proxies, T5 unblocker) only after an operator
approves that host, for a tier, with a cap; every other host stays at T3 and
keeps returning NEEDS_APPROVAL. Before every paid request the guard checks, in
order, and refuses with a reason when any fails:

1. the kill switch (`BRIGHTDATA_ENABLED`) is on;
2. a monthly cap is set (`BRIGHTDATA_MONTHLY_CAP_USD`; 0 means unset);
3. the host has a current approval covering the tier;
4. the request's worst-case cost fits the run cap (the smaller of
   `BRIGHTDATA_RUN_CAP_USD` and the scope's `cost_ceiling_usd`), the daily
   cap, the monthly cap, the host's approved cap for this run and the per-URL
   cap.

The worst case is reserved before the request (a paid fetch has a small body
cap), so no cap can be crossed; the actual cost is charged after it. Spend and
approvals are append-only JSON-lines files under `CP_SPEND_DIR`, so the
history is the audit trail and totals survive restarts. One process writes at
a time per file; parallel workers in several processes could each pass a check
before the other's charge lands (bounded by one worst case per worker).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, TypeVar
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator

from src.core.schemas import StrictModel
from src.integrations.http_client import FetchResult
from src.modules.content_processor.contracts import Task
from src.modules.content_processor.enums import (
    PAID_TIERS,
    AccessTier,
    EntityStatus,
    PriorityClass,
    TaskKind,
    tier_rank,
)
from src.modules.content_processor.governor.escalation import HostAccessState
from src.modules.content_processor.ledger.keys import task_key
from src.modules.content_processor.workers.ports import LedgerPort

__all__ = [
    "Approval",
    "GuardedFetcher",
    "PricedFetcher",
    "SpendCaps",
    "SpendEntry",
    "SpendGuard",
    "SpendStore",
    "apply_approval",
    "approvals_lookup",
    "requeue_blocked",
]

PaidTier = Literal["T4_PROXIED", "T5_MANAGED_UNBLOCKER"]
_LOCK = threading.Lock()


class SpendEntry(StrictModel):
    """One charged paid request."""

    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tenant_id: str
    run_id: str
    host: str
    url: str
    tier: str
    vendor: str
    usd: float = Field(ge=0.0)


class Approval(StrictModel):
    """An operator's permission for one host to use a paid tier, with a cap per run."""

    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tenant_id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    tier: PaidTier
    cap_usd: float = Field(gt=0.0, le=100.0, description="Most this host may spend per run.")
    approved_by: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)
    expires_at: datetime | None = None
    revoked: bool = False

    @field_validator("host")
    @classmethod
    def _plain_host(cls, value: str) -> str:
        """A host name, not a URL (the governor keys hosts by name)."""
        host = value.strip().lower()
        if "/" in host or ":" in host or " " in host:
            msg = "host must be a plain host name such as www.example.com"
            raise ValueError(msg)
        return host


class SpendCaps(StrictModel):
    """Every cap in force for one run, in dollars (0 refuses everything)."""

    enabled: bool
    run_usd: float = Field(ge=0.0)
    daily_usd: float = Field(ge=0.0)
    monthly_usd: float = Field(ge=0.0)
    url_usd: float = Field(ge=0.0)


def _append(path: Path, line: str) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class SpendStore:
    """`spend.jsonl` (charges) and `approvals.jsonl` under one folder."""

    def __init__(self, folder: Path) -> None:
        """Files are created on first write."""
        self.spend_path = folder / "spend.jsonl"
        self.approvals_path = folder / "approvals.jsonl"

    def charge(self, entry: SpendEntry) -> None:
        """Record one charged request."""
        _append(self.spend_path, entry.model_dump_json())

    def charges(self) -> list[SpendEntry]:
        """Every charge, oldest first (damaged lines skipped)."""
        return list(_read(self.spend_path, SpendEntry))

    def approve(self, approval: Approval) -> None:
        """Record an approval (or a revocation)."""
        _append(self.approvals_path, approval.model_dump_json())

    def approvals(self) -> list[Approval]:
        """Every approval line, oldest first."""
        return list(_read(self.approvals_path, Approval))

    def active_approval(
        self, tenant_id: str, host: str, now: datetime | None = None
    ) -> Approval | None:
        """The latest approval for the host, unless revoked or expired."""
        when = now or datetime.now(UTC)
        latest: Approval | None = None
        for approval in self.approvals():
            if approval.tenant_id == tenant_id and approval.host == host.lower():
                latest = approval
        if latest is None or latest.revoked:
            return None
        if latest.expires_at is not None and latest.expires_at <= when:
            return None
        return latest


_Model = TypeVar("_Model", bound=StrictModel)


def _read(path: Path, model: type[_Model]) -> list[_Model]:
    if not path.is_file():
        return []
    out: list[_Model] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                out.append(model.model_validate_json(line))
            except ValidationError:
                continue
    return out


class SpendGuard:
    """Decides whether one paid request may run, and records what it cost."""

    def __init__(
        self,
        store: SpendStore,
        caps: SpendCaps,
        *,
        tenant_id: str,
        run_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the guard to one run."""
        self._store = store
        self._caps = caps
        self._tenant = tenant_id
        self._run = run_id
        self._clock = clock

    def refusal(self, host: str, url: str, tier: AccessTier, worst_case_usd: float) -> str | None:
        """None when the request may run; otherwise the first rule it breaks."""
        caps = self._caps
        if not caps.enabled:
            return "kill_switch_off"
        if caps.monthly_usd <= 0:
            return "monthly_cap_unset"
        now = self._clock()
        approval = self._store.active_approval(self._tenant, host, now)
        if approval is None:
            return f"no_approval:{host}"
        if tier_rank(tier) > tier_rank(AccessTier(approval.tier)):
            return f"approved_only_to:{approval.tier}"
        run = day = month = on_host = on_url = 0.0
        for c in self._store.charges():
            if c.at.year == now.year and c.at.month == now.month:
                month += c.usd
                if c.at.date() == now.date():
                    day += c.usd
            if c.run_id == self._run:
                run += c.usd
                if c.host == host:
                    on_host += c.usd
                if c.url == url:
                    on_url += c.usd
        for name, spent, cap in (
            ("run_cap", run, caps.run_usd),
            ("daily_cap", day, caps.daily_usd),
            ("monthly_cap", month, caps.monthly_usd),
            ("host_cap", on_host, approval.cap_usd),
            ("url_cap", on_url, caps.url_usd),
        ):
            if spent + worst_case_usd > cap:
                return f"{name}:{spent:.4f}+{worst_case_usd:.4f}>{cap:.2f}"
        return None

    def charge(self, host: str, url: str, tier: str, vendor: str, usd: float) -> None:
        """Record what a finished paid request cost."""
        self._store.charge(
            SpendEntry(
                at=self._clock(),
                tenant_id=self._tenant,
                run_id=self._run,
                host=host,
                url=url,
                tier=tier,
                vendor=vendor,
                usd=max(0.0, usd),
            )
        )


class PricedFetcher(Protocol):
    """A paid fetcher (`integrations/proxy_provider.PaidFetcher`) as the guard sees it."""

    tier_name: str
    vendor: str

    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        """Acquire `url`."""
        ...

    async def aclose(self) -> None:
        """Release connections."""
        ...

    def worst_case_usd(self) -> float:
        """Most one fetch can cost."""
        ...

    def cost_usd(self, result: FetchResult) -> float:
        """What a finished fetch cost."""
        ...


class GuardedFetcher:
    """A paid fetcher that asks the guard first and charges afterwards (a `Fetcher`)."""

    def __init__(self, inner: PricedFetcher, guard: SpendGuard) -> None:
        """Wrap one paid tier."""
        self._inner = inner
        self._guard = guard
        self.tier_name = inner.tier_name

    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        """Refuse without sending anything, or fetch and charge the actual cost."""
        host = (urlsplit(url).hostname or "").lower()
        reason = self._guard.refusal(
            host, url, AccessTier(self.tier_name), self._inner.worst_case_usd()
        )
        if reason is not None:
            return FetchResult(requested_url=url, final_url=url, spend_refused=reason)
        result = await self._inner.fetch(url, headers)
        if result.egress_denied is None:
            self._guard.charge(
                host, url, self.tier_name, self._inner.vendor, self._inner.cost_usd(result)
            )
        return result

    async def aclose(self) -> None:
        """Close the wrapped fetcher."""
        await self._inner.aclose()


def apply_approval(
    state: HostAccessState, approval: Approval | None, scope_ceiling: AccessTier
) -> bool:
    """Raise a host's ceiling to its approved tier (never past the scope's); True if raised."""
    if approval is None:
        return False
    target = AccessTier(approval.tier)
    if tier_rank(target) > tier_rank(scope_ceiling):
        target = scope_ceiling
    if tier_rank(target) <= tier_rank(state.ceiling):
        return False
    state.ceiling = target
    return True


def requeue_blocked(ledger: LedgerPort, run_id: str, host: str, tier: AccessTier) -> int:
    """Queue the host's BLOCKED URLs again after an approval; returns how many were queued."""
    if tier not in PAID_TIERS:
        return 0
    tasks: list[Task] = []
    for resource in ledger.list_resources(run_id):
        if resource.status is not EntityStatus.BLOCKED:
            continue
        if (urlsplit(resource.requested_url).hostname or "").lower() != host.lower():
            continue
        tasks.append(
            Task(
                run_id=run_id,
                kind=TaskKind.FETCH,
                idempotency_key=task_key(
                    run_id, TaskKind.FETCH, resource.schedule_key, f"approved={tier.value}"
                ),
                host=host.lower(),
                priority=PriorityClass.MANDATORY if resource.mandatory else PriorityClass.HTML_PAGE,
                payload={
                    "schedule_key": resource.schedule_key,
                    "requested_url": resource.requested_url,
                    "aliases": list(resource.aliases),
                    "mandatory": resource.mandatory,
                    "approved_tier": tier.value,
                },
            )
        )
    return ledger.enqueue_followups(tasks) if tasks else 0


def approvals_lookup(store: SpendStore, tenant_id: str) -> Callable[[str], Approval | None]:
    """`host -> active approval` for the fetch worker."""
    return lambda host: store.active_approval(tenant_id, host)
