"""Group-scoped asset cache for the browser (Bit 2).

Pages in one template group reference the same stylesheets, scripts, fonts and
images. Within a run, each static asset is fetched from the client site once and
served from memory to the other pages of the group. Only static resources are
cached (stylesheet, script, font, image). HTML, JSON, POST and anything marked
`Cache-Control: no-store` always go to the origin.

The cache lives for one group, so a page never reads an asset that belongs to a
different template family, and it is dropped when the group finishes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["CACHEABLE_RESOURCE_TYPES", "AssetCache", "CachedAsset", "CacheStats"]

CACHEABLE_RESOURCE_TYPES = frozenset({"stylesheet", "script", "font", "image"})
DEFAULT_MAX_BYTES = 32 * 1024 * 1024


@dataclass
class CachedAsset:
    """One stored response: status, headers and body, enough to replay it."""

    status: int
    headers: dict[str, str]
    body: bytes


@dataclass
class CacheStats:
    """Counters for one group, reported in the run artifact."""

    origin_requests: int = 0
    origin_bytes: int = 0
    cache_hits: int = 0
    cache_bytes_served: int = 0
    not_cacheable: int = 0
    stored: int = 0
    skipped_too_large: int = 0


@dataclass
class AssetCache:
    """In-memory store of static assets for one template group."""

    group_id: str
    max_bytes: int = DEFAULT_MAX_BYTES
    stats: CacheStats = field(default_factory=CacheStats)
    _store: dict[str, CachedAsset] = field(default_factory=dict)
    _stored_bytes: int = 0

    def __len__(self) -> int:
        """Number of stored assets."""
        return len(self._store)

    def lookup(self, url: str, resource_type: str, method: str) -> CachedAsset | None:
        """Return a stored asset for a cacheable GET, counting the hit."""
        if not self._is_cacheable_request(resource_type, method):
            return None
        asset = self._store.get(url)
        if asset is not None:
            self.stats.cache_hits += 1
            self.stats.cache_bytes_served += len(asset.body)
        return asset

    def record_origin(
        self,
        url: str,
        resource_type: str,
        method: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        """Account for an origin response and store it if it is a cacheable static asset."""
        self.stats.origin_requests += 1
        self.stats.origin_bytes += len(body)
        if not self._is_cacheable_request(resource_type, method):
            self.stats.not_cacheable += 1
            return
        if status != 200 or _no_store(headers):
            self.stats.not_cacheable += 1
            return
        if url in self._store:
            return
        if self._stored_bytes + len(body) > self.max_bytes:
            self.stats.skipped_too_large += 1
            return
        self._store[url] = CachedAsset(status=status, headers=dict(headers), body=body)
        self._stored_bytes += len(body)
        self.stats.stored += 1

    @staticmethod
    def _is_cacheable_request(resource_type: str, method: str) -> bool:
        return method.upper() == "GET" and resource_type in CACHEABLE_RESOURCE_TYPES


def _no_store(headers: dict[str, str]) -> bool:
    lowered = {k.lower(): v for k, v in headers.items()}
    return "no-store" in lowered.get("cache-control", "").lower()


async def install(context: Any, cache: AssetCache) -> None:
    """Route every request of a Playwright context through the group cache."""

    async def handle(route: Any, request: Any) -> None:
        url = request.url
        rtype = request.resource_type
        method = request.method
        hit = cache.lookup(url, rtype, method)
        if hit is not None:
            await route.fulfill(status=hit.status, headers=hit.headers, body=hit.body)
            return
        response = await route.fetch()
        body = await response.body()
        cache.record_origin(url, rtype, method, response.status, dict(response.headers), body)
        await route.fulfill(response=response)

    await context.route("**/*", handle)
