"""Render settle: a page that goes quiet just before the window closes is confirmed."""

from __future__ import annotations

import time
from typing import Any

from src.integrations.browser_client import BrowserRenderer, RenderLimits

LIMITS = RenderLimits(
    settle_window_s=0.6, settle_stable_polls=2, settle_min_quiet_s=0.3, poll_interval_s=0.05
)


class _Page:
    """Text length changes until `stops_at` seconds (forever when None)."""

    def __init__(self, stops_at: float | None) -> None:
        self._t0 = time.perf_counter()
        self._stops_at = stops_at

    async def evaluate(self, _script: str) -> dict[str, Any]:
        elapsed = time.perf_counter() - self._t0
        if self._stops_at is None or elapsed < self._stops_at:
            return {"text": int(elapsed * 1000), "nodes": 10}
        return {"text": 999_999, "nodes": 10}


def _renderer() -> BrowserRenderer:
    return BrowserRenderer(
        user_agent="t", host_checker=lambda _h: (True, "t"), limits=LIMITS, channel=None
    )


async def test_late_but_quiet_page_is_confirmed_with_one_extension() -> None:
    # Quiet from 0.45 s; a 0.3 s quiet period cannot fit in the 0.6 s window.
    result = await _renderer()._settle(_Page(stops_at=0.45))
    assert result["stable"] is True
    assert result["window_extended"] is True
    assert result["quiet_s"] >= LIMITS.settle_min_quiet_s


async def test_page_that_keeps_changing_stays_unstable() -> None:
    result = await _renderer()._settle(_Page(stops_at=None))
    assert result["stable"] is False


async def test_page_that_settles_early_needs_no_extension() -> None:
    result = await _renderer()._settle(_Page(stops_at=0.05))
    assert result["stable"] is True
    assert result["window_extended"] is False
