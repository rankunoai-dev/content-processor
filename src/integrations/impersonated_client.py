"""T2 impersonated HTTP acquisition (playbook section 2, tier T2).

Same chain as T1 (`ChainFetcher`: egress per hop, typed redirects, body cap,
meta refresh, canonical hints); only the transport differs. `curl_cffi` sends
a real browser's TLS and HTTP/2 fingerprint and that browser's default header
set, so the User-Agent is left to the impersonation target: a Chrome TLS hello
with a different UA string is itself a fingerprint mismatch (playbook §6).

`curl_cffi` is the optional `unblock` extra. It is imported on first use, so
the module (and T1-only deployments) work without it; `impersonation_available()`
tells the caller whether a T2 fetcher can be built. Environment proxies are
ignored (`trust_env=False`): T2 is a $0 tier and never routes through a proxy.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from src.integrations.http_client import ChainFetcher, FetchLimits, HostChecker, RawResponse

__all__ = [
    "DEFAULT_IMPERSONATION",
    "ImpersonatedFetcher",
    "ImpersonationUnavailableError",
    "impersonation_available",
]

DEFAULT_IMPERSONATION = "chrome"
"""curl_cffi target alias for the newest Chrome profile the installed version ships."""

_CURL_PARTIAL_FILE = 18
"""CURLE_PARTIAL_FILE: the server closed before Content-Length bytes arrived."""


class ImpersonationUnavailableError(RuntimeError):
    """`curl_cffi` is not installed; install the `unblock` extra to enable T2."""


class StreamSession(Protocol):
    """The part of `curl_cffi.requests.AsyncSession` this fetcher uses."""

    def stream(self, method: str, url: str, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        """Open a streamed request."""
        ...

    async def close(self) -> None:
        """Release connections."""
        ...


def _curl_requests() -> Any:
    try:
        return importlib.import_module("curl_cffi.requests")
    except ImportError as exc:
        msg = "T2 needs curl_cffi: pip install 'content-processor[unblock]'"
        raise ImpersonationUnavailableError(msg) from exc


def impersonation_available() -> bool:
    """True when `curl_cffi` can be imported, so a T2 fetcher can be built."""
    try:
        _curl_requests()
    except ImpersonationUnavailableError:
        return False
    return True


def _flatten_headers(headers: Any) -> dict[str, str]:
    """Lower-case names; join repeated headers (e.g. Set-Cookie) like httpx does."""
    pairs = headers.multi_items() if hasattr(headers, "multi_items") else headers.items()
    flat: dict[str, str] = {}
    for name, value in pairs:
        key = str(name).lower()
        flat[key] = f"{flat[key]}, {value}" if key in flat else str(value)
    return flat


class ImpersonatedFetcher(ChainFetcher):
    """Async T2 fetcher. One instance per worker; reuse connections."""

    tier_name = "T2_IMPERSONATED_HTTP"

    def __init__(
        self,
        *,
        host_checker: HostChecker,
        impersonate: str = DEFAULT_IMPERSONATION,
        accept_language: str = "en-US,en;q=0.9",
        limits: FetchLimits | None = None,
        session: StreamSession | None = None,
    ) -> None:
        """Build the fetcher.

        Args:
            host_checker: Egress policy, checked before every hop.
            impersonate: curl_cffi browser target (e.g. `chrome`, `safari`).
            accept_language: Matches the scope locale, like T1.
            limits: Caps shared with T1.
            session: Injected session (tests). `None` builds a curl_cffi
                `AsyncSession` and raises `ImpersonationUnavailableError`
                when curl_cffi is missing.
        """
        super().__init__(host_checker=host_checker, limits=limits)
        self._impersonate = impersonate
        self._accept_language = accept_language
        self._errors: tuple[type[BaseException], ...] = (OSError,)
        self._chunked_error: type[BaseException] | None = None
        if session is None:
            curl = _curl_requests()
            exceptions = importlib.import_module("curl_cffi.requests.exceptions")
            self._errors = (curl.RequestsError, OSError)
            self._chunked_error = exceptions.ChunkedEncodingError
            session = curl.AsyncSession(trust_env=False, max_clients=4)
        self._session: StreamSession = session

    async def aclose(self) -> None:
        """Close pooled connections."""
        await self._session.close()

    @asynccontextmanager
    async def _open(self, url: str) -> AsyncGenerator[RawResponse, None]:
        async with self._session.stream(
            "GET",
            url,
            impersonate=self._impersonate,
            allow_redirects=False,
            headers={"Accept-Language": self._accept_language},
            timeout=(self._limits.connect_timeout_s, self._limits.read_timeout_s),
        ) as response:
            yield RawResponse(
                status=int(response.status_code),
                headers=_flatten_headers(response.headers),
                chunks=response.aiter_content(),
            )

    def _is_transport_error(self, exc: Exception) -> bool:
        return isinstance(exc, self._errors)

    def _is_truncation(self, exc: Exception) -> bool:
        if self._chunked_error is not None and isinstance(exc, self._chunked_error):
            return True
        return getattr(exc, "code", None) == _CURL_PARTIAL_FILE
