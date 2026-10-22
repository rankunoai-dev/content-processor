"""T4 network identity: Bright Data proxies behind the `Fetcher` interface (playbook §2, T4).

`ProxiedFetcher` runs the same chain as T1 (egress check per hop, typed
redirects, body cap, meta refresh) through a Bright Data zone. Each host gets
its own sticky session (`-session-<id>` derived from the run and the host), so
one host sees one exit IP for the run (playbook §6, never rotate inside a
session); `-country-<cc>` matches the scope's geography.

Paid fetchers report their cost: `worst_case_usd()` before a request (used to
reserve spend so a cap can never be crossed) and `cost_usd(result)` after it.
Prices come from settings and are deliberately on the high side. Nothing here
decides whether a paid request may run: that is `governor/spend.py`, which
wraps these fetchers.

TLS verification is never switched off. Bright Data's CA certificate can be
supplied (`ca_cert`) for zones that re-sign TLS.
"""

from __future__ import annotations

import hashlib
import ssl
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field, SecretStr

from src.core.schemas import StrictModel
from src.integrations.http_client import (
    ChainFetcher,
    FetchLimits,
    FetchResult,
    HostChecker,
    RawResponse,
    accept_encoding,
)

__all__ = [
    "BrightDataZone",
    "PaidFetcher",
    "ProxiedFetcher",
    "brightdata_username",
    "session_id",
]

_HEADER_BYTES_PER_REQUEST = 16 * 1024
"""Allowance for request and response headers when pricing by volume."""


class BrightDataZone(StrictModel):
    """One Bright Data zone (a proxy or Web Unlocker product) and how to reach it."""

    customer_id: str = Field(min_length=1)
    zone: str = Field(min_length=1)
    password: SecretStr
    host: str = "brd.superproxy.io"
    port: int = Field(default=33335, ge=1, le=65535)


def session_id(salt: str, host: str) -> str:
    """Sticky session id for one host within one run (letters and digits only)."""
    return hashlib.sha256(f"{salt}|{host}".encode()).hexdigest()[:16]


def brightdata_username(
    customer_id: str, zone: str, *, country: str | None = None, session: str | None = None
) -> str:
    """`brd-customer-<id>-zone-<zone>[-country-<cc>][-session-<id>]`."""
    name = f"brd-customer-{customer_id}-zone-{zone}"
    if country:
        name += f"-country-{country.lower()}"
    if session:
        name += f"-session-{session}"
    return name


class PaidFetcher(ChainFetcher):
    """A paid tier: the shared chain through a vendor endpoint, with a price."""

    tier_name = ""
    vendor = ""

    def worst_case_usd(self) -> float:
        """The most one `fetch()` can cost (reserved before the request)."""
        raise NotImplementedError

    def cost_usd(self, result: FetchResult) -> float:
        """What one finished `fetch()` cost."""
        raise NotImplementedError


class ProxiedFetcher(PaidFetcher):
    """T4: Bright Data proxy zone, sticky session per host, priced per GB."""

    tier_name = "T4_PROXIED"
    vendor = "brightdata_proxy"

    def __init__(
        self,
        *,
        host_checker: HostChecker,
        zone: BrightDataZone,
        session_salt: str,
        user_agent: str,
        usd_per_gb: float,
        country: str | None = None,
        accept_language: str = "en-US,en;q=0.9",
        limits: FetchLimits | None = None,
        ca_cert: Path | None = None,
        transport_factory: Callable[[str], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        """Bind the zone; connections are opened per host on first use.

        Args:
            host_checker: Egress policy, checked on every hop before the proxy is used.
            zone: Bright Data zone credentials.
            session_salt: Usually the run id: same host, same run, same session.
            user_agent: Ordinary browser UA (paid tiers are third-party only).
            usd_per_gb: Price used for reservation and charging.
            country: Exit country (scope geography); None lets the zone decide.
            accept_language: Matches the scope locale.
            limits: Caps; `max_bytes` bounds the worst-case cost of a request.
            ca_cert: Vendor CA bundle added to the system trust store.
            transport_factory: Tests inject a transport built from the proxy URL.
        """
        super().__init__(host_checker=host_checker, limits=limits)
        self._zone = zone
        self._salt = session_salt
        self._country = country
        self._usd_per_gb = usd_per_gb
        self._ca_cert = ca_cert
        self._factory = transport_factory
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": accept_language,
            "Accept-Encoding": accept_encoding(),
        }
        self._clients: dict[str, httpx.AsyncClient] = {}

    def proxy_url(self, host: str) -> str:
        """The proxy URL (with credentials) used for `host`."""
        user = brightdata_username(
            self._zone.customer_id,
            self._zone.zone,
            country=self._country,
            session=session_id(self._salt, host),
        )
        password = quote(self._zone.password.get_secret_value(), safe="")
        return f"http://{quote(user, safe='-')}:{password}@{self._zone.host}:{self._zone.port}"

    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self._ca_cert is not None:
            context.load_verify_locations(cafile=str(self._ca_cert))
        return context

    def _client_for(self, host: str) -> httpx.AsyncClient:
        client = self._clients.get(host)
        if client is None:
            proxy = self.proxy_url(host)
            transport = (
                self._factory(proxy)
                if self._factory is not None
                else httpx.AsyncHTTPTransport(proxy=proxy, verify=self._ssl_context())
            )
            client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(
                    connect=self._limits.connect_timeout_s,
                    read=self._limits.read_timeout_s,
                    write=self._limits.read_timeout_s,
                    pool=self._limits.connect_timeout_s,
                ),
                transport=transport,
                headers=self._headers,
            )
            self._clients[host] = client
        return client

    async def aclose(self) -> None:
        """Close every per-host session."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    @asynccontextmanager
    async def _open(
        self, url: str, headers: dict[str, str] | None = None
    ) -> AsyncGenerator[RawResponse, None]:
        client = self._client_for(urlsplit(url).hostname or "")
        async with client.stream("GET", url, headers=headers) as response:
            yield RawResponse(
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                chunks=response.aiter_bytes(),
            )

    def _is_transport_error(self, exc: Exception) -> bool:
        return isinstance(exc, httpx.HTTPError)

    def _is_truncation(self, exc: Exception) -> bool:
        return isinstance(exc, httpx.RemoteProtocolError)

    def worst_case_usd(self) -> float:
        """Body cap plus header allowance for every hop, at the GB price."""
        requests = self._limits.max_hops + 1
        volume = self._limits.max_bytes + requests * _HEADER_BYTES_PER_REQUEST
        return volume / 1e9 * self._usd_per_gb

    def cost_usd(self, result: FetchResult) -> float:
        """Received bytes plus header allowance per request made, at the GB price."""
        requests = len(result.hops) + 1
        volume = result.received_bytes + requests * _HEADER_BYTES_PER_REQUEST
        return volume / 1e9 * self._usd_per_gb
