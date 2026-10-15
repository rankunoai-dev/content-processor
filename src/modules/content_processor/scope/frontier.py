"""Frontier building: input URLs → deduplicated, prioritised fetch plan.

Every requested URL ends in exactly one of two places: a `FrontierEntry` (as the
canonical requester or as an alias of one) or an `ExcludedInput` with a reason.
Nothing is dropped silently (cases 91, 92, 100, 189).
"""

from __future__ import annotations

from collections import Counter

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.contracts import RunScope
from src.modules.content_processor.enums import PriorityClass
from src.modules.content_processor.scope.normalize import (
    InvalidInputUrl,
    NormalizedUrl,
    normalize_url,
)
from src.modules.content_processor.scope.ssrf import (
    EgressDecision,
    IpClass,
    Resolver,
    check_host,
)

__all__ = ["ExcludedInput", "FrontierEntry", "FrontierPlan", "build_frontier", "host_in_scope"]


class FrontierEntry(StrictModel):
    """One scheduled fetch."""

    schedule_key: str
    requested_url: str
    aliases: list[str] = Field(default_factory=list)
    host: str
    mandatory: bool
    priority: PriorityClass
    fragment_routes: list[str] = Field(default_factory=list)
    dropped_params: list[str] = Field(default_factory=list)


class ExcludedInput(StrictModel):
    """A requested URL that will not be fetched, and why."""

    requested_url: str
    reason: str


class FrontierPlan(StrictModel):
    """Result of planning a run's inputs."""

    entries: list[FrontierEntry]
    excluded: list[ExcludedInput]
    hosts: dict[str, int]
    egress: dict[str, EgressDecision]

    @property
    def requested_count(self) -> int:
        """Every requested URL, including aliases and exclusions (the denominator)."""
        aliased = sum(len(e.aliases) for e in self.entries)
        return len(self.entries) + aliased + len(self.excluded)


def host_in_scope(host: str, hosts_in_scope: list[str]) -> bool:
    """True when `host` equals or is a subdomain of a declared in-scope host."""
    for declared in hosts_in_scope:
        d = declared.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def _priority_for(normalized: NormalizedUrl, mandatory: bool) -> PriorityClass:
    if mandatory:
        return PriorityClass.MANDATORY
    path = normalized.schedule_key.lower()
    if path.endswith((".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt")):
        return PriorityClass.DOCUMENT
    if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")):
        return PriorityClass.EMBEDDED_ASSET
    return PriorityClass.HTML_PAGE


def _private_fixture_allowed(scope: RunScope, decision: EgressDecision) -> bool:
    """Development-only: loopback/private fixture hosts may be fetched when declared."""
    return bool(
        scope.egress_allow_private
        and decision.classes
        and all(c in (IpClass.LOOPBACK, IpClass.PRIVATE) for c in decision.classes)
    )


def build_frontier(scope: RunScope, resolver: Resolver | None = None) -> FrontierPlan:
    """Plan the fetch frontier for a scope.

    Args:
        scope: Validated run scope.
        resolver: DNS resolver override (tests inject one). `None` uses the OS.

    Returns:
        The plan. `requested_count` equals the number of input URLs plus any
        mandatory URLs not already present, so the denominator is intact.
    """
    mandatory_keys: set[str] = set()
    for raw in scope.mandatory_urls:
        try:
            mandatory_keys.add(normalize_url(raw).schedule_key)
        except InvalidInputUrl:
            continue  # reported below when the raw URL is processed

    by_key: dict[str, FrontierEntry] = {}
    excluded: list[ExcludedInput] = []
    egress: dict[str, EgressDecision] = {}
    host_counter: Counter[str] = Counter()
    seen_raw: set[str] = set()

    ordered_inputs = list(scope.input_urls) + [
        u for u in scope.mandatory_urls if u not in scope.input_urls
    ]
    for raw in ordered_inputs:
        if raw in seen_raw:
            continue  # exact duplicate line in the input; counted once by design
        seen_raw.add(raw)
        try:
            norm = normalize_url(raw)
        except InvalidInputUrl as exc:
            excluded.append(ExcludedInput(requested_url=raw, reason=exc.reason))
            continue
        if not host_in_scope(norm.host, scope.hosts_in_scope):
            excluded.append(ExcludedInput(requested_url=raw, reason="out_of_scope_host"))
            continue
        if norm.host not in egress:
            egress[norm.host] = (
                check_host(norm.host, resolver) if resolver else check_host(norm.host)
            )
        if not egress[norm.host].allowed and _private_fixture_allowed(scope, egress[norm.host]):
            egress[norm.host] = egress[norm.host].model_copy(
                update={"allowed": True, "reason": "allowed_private_fixture"}
            )
        if not egress[norm.host].allowed:
            excluded.append(
                ExcludedInput(requested_url=raw, reason=f"egress_denied:{egress[norm.host].reason}")
            )
            continue

        mandatory = norm.schedule_key in mandatory_keys
        existing = by_key.get(norm.schedule_key)
        if existing is not None:
            existing.aliases.append(raw)
            existing.mandatory = existing.mandatory or mandatory
            if mandatory:
                existing.priority = PriorityClass.MANDATORY
            if norm.fragment_route and norm.fragment_route not in existing.fragment_routes:
                existing.fragment_routes.append(norm.fragment_route)
            continue

        by_key[norm.schedule_key] = FrontierEntry(
            schedule_key=norm.schedule_key,
            requested_url=raw,
            host=norm.host,
            mandatory=mandatory,
            priority=_priority_for(norm, mandatory),
            fragment_routes=[norm.fragment_route] if norm.fragment_route else [],
            dropped_params=norm.dropped_params,
        )
        host_counter[norm.host] += 1

    entries = sorted(by_key.values(), key=lambda e: (int(e.priority), e.schedule_key))
    return FrontierPlan(entries=entries, excluded=excluded, hosts=dict(host_counter), egress=egress)
