"""T1 polite HTTP acquisition (playbook section 2, tier T1).

One `fetch()` call produces one `FetchResult` with the whole typed redirect
chain, the raw body (capped), integrity signals and the sniffed media type.
Every hop is checked against the egress policy before a connection is made.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import Field
from selectolax.parser import HTMLParser

from src.core.logger import get_logger
from src.core.schemas import StrictModel
from src.integrations.sniff import parse_content_disposition_name, sniff_media_type

__all__ = ["FetchLimits", "FetchResult", "Hop", "HostChecker", "HttpFetcher"]

_logger = get_logger("integrations.http_client")

HopKind = Literal["http", "meta", "canonical_hint"]
HostChecker = Callable[[str], tuple[bool, str]]
"""Returns (allowed, reason) for a hostname. Wired to the SSRF classifier."""

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_META_REFRESH_PREFIX = "url="


class Hop(StrictModel):
    """One hop in the acquisition chain."""

    kind: HopKind
    from_url: str
    to_url: str
    http_status: int | None = None


class FetchLimits(StrictModel):
    """Caps applied to every fetch."""

    max_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    max_hops: int = Field(default=10, ge=0, le=30)
    connect_timeout_s: float = Field(default=10.0, gt=0)
    read_timeout_s: float = Field(default=30.0, gt=0)
    total_timeout_s: float = Field(default=120.0, gt=0)


class FetchResult(StrictModel):
    """Everything observed while acquiring one URL."""

    requested_url: str
    final_url: str
    hops: list[Hop] = Field(default_factory=list)
    http_status: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""
    declared_mime: str | None = None
    sniffed_mime: str = "application/x-empty"
    content_disposition_name: str | None = None
    declared_length: int | None = None
    received_bytes: int = 0
    truncated: bool = False
    limit_hit: bool = False
    sha256: str | None = None
    elapsed_ms: float = 0.0
    transport_error: str | None = None
    egress_denied: str | None = None

    @property
    def acquired(self) -> bool:
        """True when a status and a body (possibly empty) were received."""
        return self.http_status is not None and self.transport_error is None


def _meta_refresh_target(body: bytes, base_url: str) -> str | None:
    tree = HTMLParser(body[:65536])
    for node in tree.css("meta"):
        if (node.attributes.get("http-equiv") or "").strip().lower() != "refresh":
            continue
        content = (node.attributes.get("content") or "").strip()
        lower = content.lower()
        idx = lower.find(_META_REFRESH_PREFIX)
        if idx == -1:
            continue
        target = content[idx + len(_META_REFRESH_PREFIX) :].strip().strip("'\"")
        if target:
            return urljoin(base_url, target)
    return None


def _canonical_hint(body: bytes, base_url: str) -> str | None:
    tree = HTMLParser(body[:65536])
    for node in tree.css("link"):
        rel = (node.attributes.get("rel") or "").lower().split()
        href = node.attributes.get("href")
        if "canonical" in rel and href:
            return urljoin(base_url, href.strip())
    return None


class HttpFetcher:
    """Async T1 fetcher. One instance per worker; reuse connections."""

    def __init__(
        self,
        *,
        host_checker: HostChecker,
        user_agent: str,
        accept_language: str = "en-US,en;q=0.9",
        limits: FetchLimits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the client with a fixed header profile (case 107)."""
        self._check = host_checker
        self._limits = limits or FetchLimits()
        timeout = httpx.Timeout(
            connect=self._limits.connect_timeout_s,
            read=self._limits.read_timeout_s,
            write=self._limits.read_timeout_s,
            pool=self._limits.connect_timeout_s,
        )
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": accept_language,
                "Accept-Encoding": "gzip, deflate, br",
            },
        )

    async def aclose(self) -> None:
        """Close pooled connections."""
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        """Acquire `url`, following HTTP and meta redirects within limits."""
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._limits.total_timeout_s):
                result = await self._fetch_chain(url)
        except TimeoutError:
            result = FetchResult(requested_url=url, final_url=url, transport_error="total_timeout")
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result

    async def _fetch_chain(self, url: str) -> FetchResult:
        hops: list[Hop] = []
        current = url
        for _ in range(self._limits.max_hops + 1):
            host = urlsplit(current).hostname or ""
            allowed, reason = self._check(host)
            if not allowed:
                return FetchResult(
                    requested_url=url, final_url=current, hops=hops, egress_denied=reason
                )
            try:
                async with self._client.stream("GET", current) as response:
                    status = response.status_code
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    location = headers.get("location")
                    if status in _REDIRECT_STATUSES and location:
                        target = urljoin(current, location)
                        hops.append(
                            Hop(kind="http", from_url=current, to_url=target, http_status=status)
                        )
                        current = target
                        continue
                    body, limit_hit, truncated = await self._read_body(response)
            except httpx.HTTPError as exc:
                return FetchResult(
                    requested_url=url,
                    final_url=current,
                    hops=hops,
                    transport_error=f"{type(exc).__name__}:{exc}"[:300],
                )

            declared = headers.get("content-type")
            sniffed = sniff_media_type(body, declared)
            if status < 400 and sniffed == "text/html" and not limit_hit:
                meta_target = _meta_refresh_target(body, current)
                if meta_target and meta_target != current:
                    hops.append(
                        Hop(kind="meta", from_url=current, to_url=meta_target, http_status=status)
                    )
                    current = meta_target
                    continue
                canonical = _canonical_hint(body, current)
                if canonical and canonical != current:
                    hops.append(Hop(kind="canonical_hint", from_url=current, to_url=canonical))
            return self._build(url, current, hops, status, headers, body, limit_hit, truncated)
        return FetchResult(
            requested_url=url, final_url=current, hops=hops, transport_error="too_many_hops"
        )

    async def _read_body(self, response: httpx.Response) -> tuple[bytes, bool, bool]:
        chunks: list[bytes] = []
        received = 0
        limit_hit = False
        truncated = False
        try:
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > self._limits.max_bytes:
                    chunks.append(chunk[: self._limits.max_bytes - (received - len(chunk))])
                    limit_hit = True
                    break
                chunks.append(chunk)
        except httpx.RemoteProtocolError as exc:
            _logger.warning("body_truncated", extra={"url": str(response.url), "error": str(exc)})
            truncated = True
        return b"".join(chunks), limit_hit, truncated

    @staticmethod
    def _build(
        url: str,
        final_url: str,
        hops: list[Hop],
        status: int,
        headers: dict[str, str],
        body: bytes,
        limit_hit: bool,
        truncated: bool,
    ) -> FetchResult:
        declared_length: int | None = None
        raw_length = headers.get("content-length", "")
        if raw_length.isdigit():
            declared_length = int(raw_length)
            if not limit_hit and len(body) < declared_length:
                truncated = True
        declared = headers.get("content-type")
        return FetchResult(
            requested_url=url,
            final_url=final_url,
            hops=hops,
            http_status=status,
            headers=headers,
            body=body,
            declared_mime=declared.split(";", 1)[0].strip().lower() if declared else None,
            sniffed_mime=sniff_media_type(body, declared),
            content_disposition_name=parse_content_disposition_name(
                headers.get("content-disposition")
            ),
            declared_length=declared_length,
            received_bytes=len(body),
            truncated=truncated,
            limit_hit=limit_hit,
            sha256=hashlib.sha256(body).hexdigest(),
        )
