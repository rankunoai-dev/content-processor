"""HTTP acquisition: the shared fetch chain and the T1 polite fetcher (playbook §2).

One `fetch()` call produces one `FetchResult` with the whole typed redirect
chain, the raw body (capped), integrity signals and the sniffed media type.
Every hop is checked against the egress policy before a connection is made.

`ChainFetcher` owns that chain; a tier only supplies how one GET is opened.
`HttpFetcher` (T1, httpx) and `ImpersonatedFetcher` (T2, curl_cffi, in
`impersonated_client.py`) both satisfy the `Fetcher` protocol workers depend on.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from pydantic import Field
from selectolax.parser import HTMLParser

from src.core.logger import get_logger
from src.core.schemas import StrictModel
from src.integrations.sniff import (
    parse_content_disposition_name,
    refine_zip_media_type,
    sniff_media_type,
)

__all__ = [
    "ChainFetcher",
    "FetchLimits",
    "FetchResult",
    "Fetcher",
    "HostChecker",
    "Pinner",
    "PinningBackend",
    "Hop",
    "HttpFetcher",
    "RawResponse",
]

_logger = get_logger("integrations.http_client")

HopKind = Literal["http", "meta", "canonical_hint"]
HostChecker = Callable[[str], tuple[bool, str]]
# IPs the egress check just approved for a host; the T1 client connects to them (case 93).
Pinner = Callable[[str], Sequence[str]]
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
    spend_refused: str | None = None
    """A paid tier refused to make the request (cap, approval or kill switch); no bytes sent."""

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


def accept_encoding() -> str:
    """Encodings this client can decode: `br` only when a brotli decoder is installed.

    httpx decodes brotli through `brotli` or `brotlicffi`; advertising `br`
    without one lets a server answer with bytes that are never decoded.
    """
    if any(importlib.util.find_spec(m) is not None for m in ("brotli", "brotlicffi")):
        return "gzip, deflate, br"
    return "gzip, deflate"


class PinningBackend(httpcore.AsyncNetworkBackend):
    """Connects to the IP approved for a host instead of resolving it again.

    httpcore passes the URL's host to `connect_tcp` and uses it separately for
    TLS SNI and certificate verification, so only the TCP target changes.
    A host with no pin (no check ran, or the check returned no address) is
    refused rather than resolved, so nothing reaches an unchecked address.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        """Wrap the default backend."""
        self._inner = inner
        self._pins: dict[str, list[str]] = {}

    def pin(self, host: str, addresses: Sequence[str]) -> None:
        """Pin `host` to its approved addresses, tried in order (or clear it)."""
        if addresses:
            self._pins[host.lower()] = list(addresses)
        else:
            self._pins.pop(host.lower(), None)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Open TCP to the first pinned address for `host` that accepts the connection."""
        targets = self._pins.get(host.lower())
        if not targets:
            raise httpcore.ConnectError(f"egress_not_pinned:{host}")
        last: Exception | None = None
        for target in targets:
            try:
                return await self._inner.connect_tcp(
                    target,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last = exc
        assert last is not None  # noqa: S101 - targets is non-empty
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Unix sockets are never used for egress."""
        raise httpcore.ConnectError("egress_unix_socket_refused")

    async def sleep(self, seconds: float) -> None:
        """Delegate."""
        await self._inner.sleep(seconds)


class Fetcher(Protocol):
    """What a worker needs from an acquisition tier."""

    tier_name: str
    """Access tier value this fetcher implements; recorded on every acquired resource."""

    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        """Acquire `url` and report everything observed; transport errors are data.

        `headers` are sent on the first hop only (conditional-GET validators).
        """
        ...

    async def aclose(self) -> None:
        """Release pooled connections."""
        ...


@dataclass
class RawResponse:
    """One opened GET, before its body is read."""

    status: int
    headers: dict[str, str]
    """Lower-cased names; repeated headers joined with ', '."""
    chunks: AsyncIterator[bytes]


class ChainFetcher(ABC):
    """Redirects, egress per hop, body cap, meta refresh and canonical hints.

    Subclasses implement `_open` (one GET, no redirect following) and say which
    exceptions are transport failures and which mean a body ended early.
    """

    tier_name: str = ""

    def __init__(self, *, host_checker: HostChecker, limits: FetchLimits | None = None) -> None:
        """Bind the egress checker and the caps shared by every tier."""
        self._check = host_checker
        self._limits = limits or FetchLimits()

    def _pin(self, host: str) -> None:  # noqa: B027 - optional hook, default no-op
        """Connect the next request for `host` to the IPs just checked (no-op by default)."""

    @abstractmethod
    def _open(
        self, url: str, headers: dict[str, str] | None = None
    ) -> AbstractAsyncContextManager[RawResponse]:
        """Open one GET for `url` without following redirects."""

    @abstractmethod
    def _is_transport_error(self, exc: Exception) -> bool:
        """True when `exc` is a network or protocol failure of this client."""

    @abstractmethod
    def _is_truncation(self, exc: Exception) -> bool:
        """True when `exc`, raised while reading a body, means the body ended early."""

    @abstractmethod
    async def aclose(self) -> None:
        """Close pooled connections."""

    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        """Acquire `url`, following HTTP and meta redirects within limits.

        `headers` (e.g. If-None-Match) go on the first request only; a 304 is
        returned as a result with an empty body, never followed.
        """
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._limits.total_timeout_s):
                result = await self._fetch_chain(url, headers)
        except TimeoutError:
            result = FetchResult(requested_url=url, final_url=url, transport_error="total_timeout")
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result

    async def _fetch_chain(
        self, url: str, first_headers: dict[str, str] | None = None
    ) -> FetchResult:
        hops: list[Hop] = []
        current = url
        for _ in range(self._limits.max_hops + 1):
            host = urlsplit(current).hostname or ""
            allowed, reason = self._check(host)
            if not allowed:
                return FetchResult(
                    requested_url=url, final_url=current, hops=hops, egress_denied=reason
                )
            self._pin(host)
            try:
                extra = first_headers if not hops else None
                async with self._open(current, extra) as response:
                    status = response.status
                    headers = response.headers
                    location = headers.get("location")
                    if status in _REDIRECT_STATUSES and location:
                        target = urljoin(current, location)
                        hops.append(
                            Hop(kind="http", from_url=current, to_url=target, http_status=status)
                        )
                        current = target
                        continue
                    body, limit_hit, truncated = await self._read_body(response, current)
            except Exception as exc:
                if not self._is_transport_error(exc):
                    raise
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

    async def _read_body(self, response: RawResponse, url: str) -> tuple[bytes, bool, bool]:
        chunks: list[bytes] = []
        received = 0
        limit_hit = False
        truncated = False
        try:
            async for chunk in response.chunks:
                received += len(chunk)
                if received > self._limits.max_bytes:
                    chunks.append(chunk[: self._limits.max_bytes - (received - len(chunk))])
                    limit_hit = True
                    break
                chunks.append(chunk)
        except Exception as exc:
            if not self._is_truncation(exc):
                raise
            _logger.warning("body_truncated", extra={"url": url, "error": str(exc)})
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
            sniffed_mime=refine_zip_media_type(body, sniff_media_type(body, declared)),
            content_disposition_name=parse_content_disposition_name(
                headers.get("content-disposition")
            ),
            declared_length=declared_length,
            received_bytes=len(body),
            truncated=truncated,
            limit_hit=limit_hit,
            sha256=hashlib.sha256(body).hexdigest(),
        )


class HttpFetcher(ChainFetcher):
    """Async T1 fetcher (httpx). One instance per worker; reuse connections."""

    tier_name = "T1_POLITE_HTTP"

    def __init__(
        self,
        *,
        host_checker: HostChecker,
        user_agent: str,
        accept_language: str = "en-US,en;q=0.9",
        limits: FetchLimits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        pinner: Pinner | None = None,
    ) -> None:
        """Build the client with a fixed header profile (case 107).

        With `pinner` (and the default transport) every connection goes to an IP
        the egress check approved, never to a fresh DNS answer, which closes
        the rebinding window between check and connect (case 93). The URL,
        `Host` header, TLS SNI and certificate check keep the real host name.
        """
        super().__init__(host_checker=host_checker, limits=limits)
        self._pinner = pinner
        self._pinning: PinningBackend | None = None
        if pinner is not None and transport is None:
            pinned = httpx.AsyncHTTPTransport()
            self._pinning = PinningBackend(pinned._pool._network_backend)  # noqa: SLF001
            pinned._pool._network_backend = self._pinning  # noqa: SLF001
            transport = pinned
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
                "Accept-Encoding": accept_encoding(),
            },
        )

    def _pin(self, host: str) -> None:
        if self._pinning is None or self._pinner is None:
            return
        self._pinning.pin(host, self._pinner(host))

    async def aclose(self) -> None:
        """Close pooled connections."""
        await self._client.aclose()

    @asynccontextmanager
    async def _open(
        self, url: str, headers: dict[str, str] | None = None
    ) -> AsyncGenerator[RawResponse, None]:
        async with self._client.stream("GET", url, headers=headers) as response:
            yield RawResponse(
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                chunks=response.aiter_bytes(),
            )

    def _is_transport_error(self, exc: Exception) -> bool:
        return isinstance(exc, httpx.HTTPError)

    def _is_truncation(self, exc: Exception) -> bool:
        return isinstance(exc, httpx.RemoteProtocolError)
