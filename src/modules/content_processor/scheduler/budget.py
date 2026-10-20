"""Run budgets (K9, P-2): measure what a run spends and say when it must stop.

`RunScope.budgets` declares four hard stops besides cost. They are measured
here, at the boundaries every worker already goes through, so no worker
changes:

| Budget | Measured as |
|---|---|
| `max_runtime_s` | wall-clock seconds since the meter started |
| `max_requests` | calls to a fetcher's `fetch` (robots included) and the renderer's `render` |
| `max_bytes` | bytes received: `FetchResult.received_bytes` plus rendered DOM and screenshot |
| `max_evidence_bytes` | bytes newly written to the evidence store (deduplicated writes are free) |

The run engine checks the meter before every task and stops the run when a
budget is reached (see `execute_run`). One task in flight can overshoot a
byte budget by at most that task's size, which the fetch limits already cap.

The meter covers one invocation of the engine. A durable run resumed by a
later `work` command starts its runtime, request and evidence counts again;
bytes are seeded from what the ledger recorded for the run's resources.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from src.integrations.browser_client import PageHook, RenderResult
from src.integrations.evidence_store import EvidenceRef, EvidenceStore
from src.integrations.http_client import FetchResult
from src.modules.content_processor.contracts import Budgets

__all__ = [
    "BudgetMeter",
    "BudgetName",
    "MeteredEvidenceStore",
    "MeteredFetcher",
    "MeteredRenderer",
]

BudgetName = Literal["runtime", "requests", "bytes", "evidence_bytes"]


class BudgetMeter:
    """Counters for one run invocation, compared against the scope's budgets."""

    def __init__(
        self,
        budgets: Budgets,
        *,
        seed_bytes: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start the runtime clock now; `seed_bytes` carries bytes already spent."""
        self.budgets = budgets
        self._clock = clock
        self._started = clock()
        self.requests = 0
        self.bytes_received = seed_bytes
        self.evidence_bytes = 0

    @property
    def elapsed_s(self) -> float:
        """Seconds since the meter started."""
        return self._clock() - self._started

    def remaining_runtime_s(self) -> float:
        """Seconds left before the runtime budget; never negative."""
        return max(0.0, self.budgets.max_runtime_s - self.elapsed_s)

    def exceeded(self) -> BudgetName | None:
        """The first budget reached, in a fixed order; None while all have room."""
        b = self.budgets
        if self.elapsed_s >= b.max_runtime_s:
            return "runtime"
        if self.requests >= b.max_requests:
            return "requests"
        if self.bytes_received >= b.max_bytes:
            return "bytes"
        if self.evidence_bytes >= b.max_evidence_bytes:
            return "evidence_bytes"
        return None

    def usage(self) -> dict[str, dict[str, float]]:
        """Used and limit per budget, for run reports."""
        b = self.budgets
        return {
            "runtime_s": {"used": round(self.elapsed_s, 3), "limit": b.max_runtime_s},
            "requests": {"used": self.requests, "limit": b.max_requests},
            "bytes": {"used": self.bytes_received, "limit": b.max_bytes},
            "evidence_bytes": {"used": self.evidence_bytes, "limit": b.max_evidence_bytes},
        }


class MeteredFetcher:
    """Counts requests and received bytes for any fetcher (T1, T2); delegates the rest."""

    def __init__(self, inner: Any, meter: BudgetMeter) -> None:
        """Wrap `inner` (an object with `async fetch(url)`)."""
        self._inner = inner
        self._meter = meter
        self.tier_name: str = str(inner.tier_name)

    async def aclose(self) -> None:
        """Close the wrapped fetcher."""
        await self._inner.aclose()

    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        """Fetch through the wrapped fetcher and count the request and its bytes."""
        self._meter.requests += 1
        result: FetchResult = await self._inner.fetch(url, headers=headers)
        self._meter.bytes_received += result.received_bytes
        return result

    def __getattr__(self, name: str) -> Any:
        """Anything else (e.g. impersonation details) comes from the wrapped fetcher."""
        return getattr(self._inner, name)


class MeteredRenderer:
    """Counts renders and rendered bytes; delegates start/stop and the rest."""

    def __init__(self, inner: Any, meter: BudgetMeter) -> None:
        """Wrap `inner` (an object with `async render(url, on_page=None)`)."""
        self._inner = inner
        self._meter = meter
        self.tier_name: str = str(inner.tier_name)

    async def start(self) -> None:
        """Start the wrapped renderer."""
        await self._inner.start()

    async def stop(self) -> None:
        """Stop the wrapped renderer."""
        await self._inner.stop()

    async def render(
        self, url: str, on_page: PageHook | None = None, asset_store: Any = None
    ) -> RenderResult:
        """Render through the wrapped renderer and count the request and its bytes."""
        self._meter.requests += 1
        if asset_store is None:
            result: RenderResult = await self._inner.render(url, on_page=on_page)
        else:
            result = await self._inner.render(url, on_page=on_page, asset_store=asset_store)
        self._meter.bytes_received += len(result.dom_html) + len(result.screenshot_jpeg)
        return result

    def __getattr__(self, name: str) -> Any:
        """Anything else comes from the wrapped renderer."""
        return getattr(self._inner, name)


class MeteredEvidenceStore:
    """Counts bytes newly written to an evidence store; reused objects cost nothing."""

    def __init__(self, inner: EvidenceStore, meter: BudgetMeter) -> None:
        """Wrap a content-addressed store."""
        self._inner = inner
        self._meter = meter

    def put(self, data: bytes, media_type: str) -> EvidenceRef:
        """Store and count the bytes unless the object already existed."""
        ref = self._inner.put(data, media_type)
        if not ref.reused:
            self._meter.evidence_bytes += ref.bytes
        return ref

    def get(self, sha256: str) -> bytes:
        """Read through."""
        return self._inner.get(sha256)

    def exists(self, sha256: str) -> bool:
        """Read through."""
        return self._inner.exists(sha256)
