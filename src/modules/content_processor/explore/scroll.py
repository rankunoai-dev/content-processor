"""Scroll exploration (B-8 scroll accumulation and lazy load; B-10 nested surfaces).

Every scroll surface is a control with a terminal outcome:

- the document itself (`window`) and up to `MAX_NESTED` nested containers whose
  content overflows (`overflow-y: auto|scroll`, visible, taller content than box);
- each surface is scrolled in overlapping steps (85% of its height), waiting for
  the DOM to settle after every step so IntersectionObserver lazy sections,
  `loading="lazy"` images and scroll-triggered fetches can arrive;
- at the bottom it waits once more: if the content grew, scrolling continues
  (lazy load); if it keeps growing past `max_scroll_passes` it is an infinite
  feed and the surface ends `unvisited:scroll_budget_exhausted` (PARTIAL);
- text seen while scrolling is accumulated; lines that were visible during the
  scroll but are no longer in the page afterwards mean the surface recycles its
  nodes (virtualized list, B-9), and the surface ends
  `unvisited:virtualized_content_recycled` instead of claiming completeness.

Scroll steps never click and never submit; the explorer's side-effect guard
still applies to any request a scroll triggers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.explore.model import ControlRecord

__all__ = ["MAX_NESTED", "ScrollOutcome", "ScrollSurfaceResult", "explore_scroll_surfaces"]

MAX_NESTED = 10
_STEP_FRACTION = 0.85
_RECYCLE_MIN_LINES = 3

SURFACES_JS = """
(maxNested) => {
  const out = [];
  const root = document.scrollingElement || document.documentElement;
  out.push({ id: 'window', label: 'page', scrollHeight: root.scrollHeight,
             clientHeight: window.innerHeight });
  const body = document.body;
  if (!body) { return out; }
  let next = Number(body.getAttribute('data-cp-scroll-next') || '0');
  const candidates = [];
  for (const el of body.querySelectorAll('*')) {
    const cs = window.getComputedStyle(el);
    if (!/(auto|scroll)/.test(cs.overflowY)) { continue; }
    if (el.scrollHeight <= el.clientHeight + 40 || el.clientHeight < 60) { continue; }
    if (cs.display === 'none' || cs.visibility === 'hidden') { continue; }
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 50) { continue; }
    candidates.push(el);
  }
  for (const el of candidates.slice(0, maxNested)) {
    if (!el.hasAttribute('data-cp-scroll')) {
      el.setAttribute('data-cp-scroll', String(next));
      next += 1;
    }
    const label = (el.getAttribute('aria-label') || el.id || el.className || el.tagName)
      .toString().trim().slice(0, 60);
    out.push({ id: 'nested:' + el.getAttribute('data-cp-scroll'), label,
               scrollHeight: el.scrollHeight, clientHeight: el.clientHeight });
  }
  body.setAttribute('data-cp-scroll-next', String(next));
  return out;
}
"""

STEP_JS = """
([id, fraction]) => {
  if (id === 'window') {
    const root = document.scrollingElement || document.documentElement;
    window.scrollBy(0, Math.max(1, Math.floor(window.innerHeight * fraction)));
    return { top: window.scrollY, clientHeight: window.innerHeight,
             scrollHeight: root.scrollHeight };
  }
  const el = document.querySelector(`[data-cp-scroll="${id.slice(7)}"]`);
  if (!el) { return null; }
  el.scrollTop = el.scrollTop + Math.max(1, Math.floor(el.clientHeight * fraction));
  return { top: el.scrollTop, clientHeight: el.clientHeight, scrollHeight: el.scrollHeight };
}
"""

METRICS_JS = """
(id) => {
  if (id === 'window') {
    const root = document.scrollingElement || document.documentElement;
    return { top: window.scrollY, clientHeight: window.innerHeight,
             scrollHeight: root.scrollHeight };
  }
  const el = document.querySelector(`[data-cp-scroll="${id.slice(7)}"]`);
  if (!el) { return null; }
  return { top: el.scrollTop, clientHeight: el.clientHeight, scrollHeight: el.scrollHeight };
}
"""

TEXT_JS = """
(id) => {
  if (id === 'window') { return document.body ? document.body.innerText : ''; }
  const el = document.querySelector(`[data-cp-scroll="${id.slice(7)}"]`);
  return el ? el.innerText : '';
}
"""

RESET_JS = """
() => {
  for (const el of document.querySelectorAll('[data-cp-scroll]')) { el.scrollTop = 0; }
  window.scrollTo(0, 0);
}
"""


class ScrollSurfaceResult(StrictModel):
    """What happened on one scroll surface."""

    surface_id: str
    label: str
    passes: int = Field(ge=0)
    reached_end: bool
    height_start: int = Field(ge=0)
    height_end: int = Field(ge=0)
    grew: bool
    recycled_lines: int = Field(ge=0)
    stop_reason: str


class ScrollOutcome(StrictModel):
    """All surfaces of one page."""

    surfaces: list[ScrollSurfaceResult] = Field(default_factory=list)
    controls: list[ControlRecord] = Field(default_factory=list)
    stop_reason: str | None = None


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if len(line.strip()) >= 2]


async def _scroll_one(
    page: Any,
    surface: dict[str, Any],
    *,
    max_passes: int,
    deadline: float,
    settle: Callable[[Any], Awaitable[None]],
    end_grace_s: float,
) -> ScrollSurfaceResult:
    sid = str(surface["id"])
    seen: dict[str, None] = dict.fromkeys(_lines(str(await page.evaluate(TEXT_JS, sid))))
    height_start = int(surface.get("scrollHeight") or 0)
    passes = 0
    reached_end = False
    stop = "scroll_budget_exhausted"
    metrics: dict[str, Any] | None = None
    while passes < max_passes:
        if time.perf_counter() >= deadline:
            stop = "time_budget_exhausted"
            break
        metrics = await page.evaluate(STEP_JS, [sid, _STEP_FRACTION])
        if metrics is None:
            stop = "surface_disappeared"
            break
        passes += 1
        await settle(page)
        for line in _lines(str(await page.evaluate(TEXT_JS, sid))):
            seen.setdefault(line, None)
        metrics = await page.evaluate(METRICS_JS, sid) or metrics
        at_end = metrics["top"] + metrics["clientHeight"] >= metrics["scrollHeight"] - 2
        if not at_end:
            continue
        before = int(metrics["scrollHeight"])
        await asyncio.sleep(end_grace_s)
        await settle(page)
        after = await page.evaluate(METRICS_JS, sid)
        if after is not None and int(after["scrollHeight"]) > before + 2:
            continue  # lazy content arrived at the bottom: keep going
        reached_end = True
        stop = "reached_end"
        break
    final = await page.evaluate(METRICS_JS, sid)
    height_end = int((final or metrics or surface).get("scrollHeight") or height_start)
    body_text = set(
        _lines(str(await page.evaluate("document.body ? document.body.innerText : ''")))
    )
    recycled = [line for line in seen if line not in body_text]
    return ScrollSurfaceResult(
        surface_id=sid,
        label=str(surface.get("label") or sid),
        passes=passes,
        reached_end=reached_end,
        height_start=height_start,
        height_end=height_end,
        grew=height_end > height_start + 2,
        recycled_lines=len(recycled),
        stop_reason=stop,
    )


async def explore_scroll_surfaces(
    page: Any,
    *,
    max_passes: int,
    deadline: float,
    settle: Callable[[Any], Awaitable[None]],
    end_grace_s: float = 0.8,
) -> ScrollOutcome:
    """Scroll every surface to its end (or budget) and account for each one."""
    outcome = ScrollOutcome()
    surfaces = list(await page.evaluate(SURFACES_JS, MAX_NESTED))
    for surface in surfaces:
        if time.perf_counter() >= deadline:
            outcome.stop_reason = "time_budget_exhausted"
            outcome.controls.append(
                ControlRecord(
                    control_id=f"scroll:{surface['id']}",
                    kind="scroll_surface",
                    label=str(surface.get("label") or surface["id"]),
                    status="unvisited",
                    reason="time_budget_exhausted",
                )
            )
            continue
        result = await _scroll_one(
            page,
            surface,
            max_passes=max_passes,
            deadline=deadline,
            settle=settle,
            end_grace_s=end_grace_s,
        )
        outcome.surfaces.append(result)
        if result.reached_end and result.recycled_lines >= _RECYCLE_MIN_LINES:
            status, reason = "unvisited", f"virtualized_content_recycled:{result.recycled_lines}"
        elif result.reached_end:
            status, reason = "visited", "reached_end" + (":content_grew" if result.grew else "")
        else:
            status, reason = "unvisited", result.stop_reason
        outcome.controls.append(
            ControlRecord(
                control_id=f"scroll:{result.surface_id}",
                kind="scroll_surface",
                label=result.label,
                status=status,
                reason=reason,
            )
        )
    await page.evaluate(RESET_JS)
    return outcome
