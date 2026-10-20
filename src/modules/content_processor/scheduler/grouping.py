"""Run-level grouping: which fetched pages share a template, and their asset caches.

The scheduler calls `refresh()` after each fetch stage. It reads the run's fetched
HTML from the evidence store, groups the pages (grouping.group_urls), and hands
each grouped page the asset cache of its group through `asset_store_for(url)`.
Singletons and excluded pages get no cache: sharing needs at least two pages.

Budget: each group's cache is capped at `per_group_bytes`, and all caches
together at `total_bytes`. A group whose cache would exceed the remaining budget
is rendered without one, and the run reports it, rather than failing.

Limit (documented in RFC 0001 section 8): render tasks are still leased in
enqueue order, so a group's pages are not yet rendered back to back. Cache hits
depend on how the leases interleave. Group-ordered leasing needs a ledger
contract change (see CONTRACT_REQUESTS.md).
"""

from __future__ import annotations

from typing import Any

from src.integrations.evidence_store import EvidenceStore
from src.modules.content_processor.contracts import Resource
from src.modules.content_processor.grouping import PageSignal, group_urls
from src.modules.content_processor.grouping.asset_cache import AssetCache
from src.modules.content_processor.grouping.cluster import DEFAULT_SIMILARITY_THRESHOLD
from src.modules.content_processor.workers.ports import LedgerPort

__all__ = ["DEFAULT_PER_GROUP_BYTES", "DEFAULT_TOTAL_BYTES", "RunGroups"]

DEFAULT_PER_GROUP_BYTES = 32 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 128 * 1024 * 1024


class RunGroups:
    """Groups one run's fetched pages and hands out group-scoped asset caches."""

    def __init__(
        self,
        ledger: LedgerPort,
        evidence: EvidenceStore,
        *,
        run_id: str,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        per_group_bytes: int = DEFAULT_PER_GROUP_BYTES,
        total_bytes: int = DEFAULT_TOTAL_BYTES,
    ) -> None:
        """Bind to one run; nothing is grouped until `refresh()`."""
        self._ledger = ledger
        self._evidence = evidence
        self._run_id = run_id
        self._threshold = similarity_threshold
        self._per_group = per_group_bytes
        self._total = total_bytes
        self._group_of: dict[str, str] = {}
        self._members: dict[str, list[str]] = {}
        self._caches: dict[str, AssetCache] = {}
        self._denied: list[str] = []
        self.excluded: dict[str, str] = {}

    # -- grouping ----------------------------------------------------------------

    def refresh(self) -> None:
        """Re-group every fetched HTML page of the run (cheap for runs of this size)."""
        pages: list[PageSignal] = []
        aliases: dict[str, list[str]] = {}
        for resource in self._ledger.list_resources(self._run_id):
            signal = self._signal(resource)
            if signal is None:
                continue
            pages.append(signal)
            aliases[signal.url] = _aliases(resource)
        result = group_urls(pages, similarity_threshold=self._threshold)
        self.excluded = {e.url: e.reason for e in result.excluded}
        self._group_of = {}
        self._members = {}
        for group in result.groups:
            if group.reason != "template_match":
                continue
            self._members[group.group_id] = list(group.members)
            for url in group.members:
                for alias in aliases.get(url, [url]):
                    self._group_of[alias] = group.group_id

    def _signal(self, resource: Resource) -> PageSignal | None:
        if resource.payload_sha256 is None or not (resource.sniffed_mime or "").startswith(
            "text/html"
        ):
            return None
        if not self._evidence.exists(resource.payload_sha256):
            return None
        body = self._evidence.get(resource.payload_sha256).decode("utf-8", errors="replace")
        return PageSignal(
            url=resource.requested_url,
            http_status=resource.http_status,
            media_type=resource.sniffed_mime,
            body=body,
        )

    # -- asset caches ------------------------------------------------------------

    def group_of(self, url: str) -> str | None:
        """Template group of a URL, or None for singletons, shells and excluded pages."""
        return self._group_of.get(url)

    def asset_store_for(self, url: str) -> AssetCache | None:
        """The group's asset cache for this page, created on first use within the budget."""
        group_id = self._group_of.get(url)
        if group_id is None:
            return None
        cache = self._caches.get(group_id)
        if cache is not None:
            return cache
        left = self._total - sum(c.stored_bytes for c in self._caches.values())
        if left <= 0:
            if group_id not in self._denied:
                self._denied.append(group_id)
            return None
        cache = AssetCache(group_id=group_id, max_bytes=min(self._per_group, left))
        self._caches[group_id] = cache
        return cache

    # -- reporting ---------------------------------------------------------------

    def stats(self) -> list[dict[str, Any]]:
        """Per-group summary for the run artifact: members and cache counters."""
        rows: list[dict[str, Any]] = []
        for group_id, members in sorted(self._members.items()):
            cache = self._caches.get(group_id)
            row: dict[str, Any] = {"group_id": group_id, "members": len(members)}
            if cache is not None:
                s = cache.stats
                row |= {
                    "origin_requests": s.origin_requests,
                    "cache_hits": s.cache_hits,
                    "stored_assets": len(cache),
                    "stored_bytes": cache.stored_bytes,
                    "not_cacheable": s.not_cacheable,
                    "skipped_too_large": s.skipped_too_large,
                }
            else:
                row["cache"] = "not_used" if group_id not in self._denied else "over_budget"
            rows.append(row)
        return rows


def _aliases(resource: Resource) -> list[str]:
    urls = {resource.requested_url}
    if resource.final_url:
        urls.add(resource.final_url)
    return sorted(urls)
