"""T5 managed unblocker: Bright Data Web Unlocker behind the `Fetcher` interface (playbook §2, T5).

The Web Unlocker is reached like a proxy (same chain as T1 and T4) and solves
the vendor-side challenges itself. Bright Data re-signs TLS for this product,
so its CA certificate is required: this fetcher refuses to start without it
rather than ever switching certificate checks off. It is priced per request.
Every response is still run through the block classifier by the fetch worker,
because unblockers return challenge pages too.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx

from src.integrations.http_client import FetchLimits, FetchResult, HostChecker
from src.integrations.proxy_provider import BrightDataZone, ProxiedFetcher

__all__ = ["UnlockerFetcher"]


class UnlockerFetcher(ProxiedFetcher):
    """T5: Bright Data Web Unlocker zone, priced per request."""

    tier_name = "T5_MANAGED_UNBLOCKER"
    vendor = "brightdata_unlocker"

    def __init__(
        self,
        *,
        host_checker: HostChecker,
        zone: BrightDataZone,
        session_salt: str,
        user_agent: str,
        usd_per_1k_requests: float,
        ca_cert: Path | None,
        country: str | None = None,
        accept_language: str = "en-US,en;q=0.9",
        limits: FetchLimits | None = None,
        transport_factory: Callable[[str], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        """Bind the unlocker zone.

        Raises:
            ValueError: No CA certificate was given (`BRIGHTDATA_CA_CERT_PATH`).
        """
        if ca_cert is None and transport_factory is None:
            msg = "the Web Unlocker needs Bright Data's CA certificate (BRIGHTDATA_CA_CERT_PATH)"
            raise ValueError(msg)
        super().__init__(
            host_checker=host_checker,
            zone=zone,
            session_salt=session_salt,
            user_agent=user_agent,
            usd_per_gb=1.0,  # unused: priced per request below
            country=country,
            accept_language=accept_language,
            limits=limits,
            ca_cert=ca_cert,
            transport_factory=transport_factory,
        )
        self._usd_per_request = usd_per_1k_requests / 1000.0

    def worst_case_usd(self) -> float:
        """Every hop of the chain is one billed request."""
        return (self._limits.max_hops + 1) * self._usd_per_request

    def cost_usd(self, result: FetchResult) -> float:
        """One billed request per hop actually made."""
        return (len(result.hops) + 1) * self._usd_per_request
