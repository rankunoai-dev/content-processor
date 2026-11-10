"""Run budgets (K9, P-2): measure what a run spends and say when it must stop.

`RunScope.budgets` declares four hard stops besides cost. They are measured
here, at the boundaries every worker already goes through, so no worker
changes:

| Budget | Measured as |
|---|---|
| `max_runtime_s` | seconds the run was awake and working (see below) |
| `max_requests` | calls to a fetcher's `fetch` (robots included) and the renderer's `render` |
| `max_bytes` | bytes received: `FetchResult.received_bytes` plus rendered DOM and screenshot |
| `max_evidence_bytes` | bytes newly written to disk by the evidence store, after packing |

The run engine checks the meter before every task and stops the run when a
budget is reached (see `execute_run`). One task in flight can overshoot a
byte budget by at most that task's size, which the fetch limits already cap.

The meter covers one invocation of the engine. A durable run resumed by a
later `work` command starts its runtime, request and evidence counts again;
bytes are seeded from what the ledger recorded for the run's resources.

Runtime is not the wall clock. `time.monotonic()` keeps counting while the
computer sleeps (on Windows it does), so a batch that was asleep for a night
would wake up over its runtime budget and end its remaining pages
BUDGET_EXHAUSTED. The meter therefore adds time in the increments it observes,
each time it is consulted, and drops any single increment longer than
`SLEEP_GAP_S`: the engine consults the meter between tasks, and nothing in a
batch blocks that long between two consultations (the longest legitimate waits
are one wait for held tasks, `--max-wait` 360 s, plus one render of up to
`max_page_time_s` 150 s). Dropped gaps are reported as `runtime_s.excluded_s`.
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
    "SLEEP_GAP_S",
    "BudgetMeter",
    "BudgetName",
    "MeteredEvidenceStore",
    "MeteredFetcher",
    "MeteredRenderer",
]

BudgetName = Literal["runtime", "requests", "bytes", "evidence_bytes"]

SLEEP_GAP_S = 600.0
"""A single unobserved stretch longer than this is the computer asleep, not the run working."""


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
        self._last_tick = clock()
        self._used_s = 0.0
        self._excluded_s = 0.0
        self.requests = 0
        self.bytes_received = seed_bytes
        self.evidence_bytes = 0

    def _tick(self) -> None:
        """Credit the time since the last look; a gap longer than `SLEEP_GAP_S` was sleep."""
        now = self._clock()
        gap = now - self._last_tick
        self._last_tick = now
        if gap <= 0:
            return
        if gap > SLEEP_GAP_S:
            self._excluded_s += gap
        else:
            self._used_s += gap

    @property
    def elapsed_s(self) -> float:
        """Seconds the run has been working since the meter started (sleep left out)."""
        self._tick()
        return self._used_s

    @property
    def excluded_s(self) -> float:
        """Seconds left out of the runtime because the computer was asleep."""
        self._tick()
        return self._excluded_s

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
            "runtime_s": {
                "used": round(self.elapsed_s, 3),
                "limit": b.max_runtime_s,
                "excluded_s": round(self.excluded_s, 3),
            },
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
        self._meter.bytes_received += sum(len(a.body) for a in result.captured_assets)
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
            # Bytes actually added on disk (K20: packed evidence is far smaller).
            self._meter.evidence_bytes += (
                ref.bytes if ref.stored_bytes is None else ref.stored_bytes
            )
        return ref

    def get(self, sha256: str) -> bytes:
        """Read through."""
        return self._inner.get(sha256)

    def exists(self, sha256: str) -> bool:
        """Read through."""
        return self._inner.exists(sha256)
