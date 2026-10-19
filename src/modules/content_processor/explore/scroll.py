"""Scroll exploration (B-8 scroll and lazy load, B-9 virtualized lists, B-10 nested surfaces).

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
  nodes (virtualized list, B-9). Rows are collected at every step, before they
  are recycled, keyed by `aria-rowindex`/`aria-posinset` when present and by
  text otherwise, and returned as one synthetic state. The surface is visited
  only when the rows are complete: all of `aria-rowcount`/`aria-setsize` when
  the list declares a total, no holes in the row indices when it numbers rows,
  and otherwise the end reached (said as `total_unknown`).

Scroll steps never click and never submit; the explorer's side-effect guard
still applies to any request a scroll triggers.
"""

from __future__ import annotations

import asyncio
import html
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.explore.model import ControlRecord

__all__ = [
    "MAX_NESTED",
    "AccumulatedRows",
    "ScrollOutcome",
    "ScrollSurfaceResult",
    "explore_scroll_surfaces",
]

MAX_NESTED = 10
MAX_ROWS = 5000
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

# Leaf rows inside a surface: ARIA/list/table rows, else leaf elements with text.
ROWS_JS = """
(id) => {
  const root = id === 'window' ? document.body
    : document.querySelector(`[data-cp-scroll="${id.slice(7)}"]`);
  if (!root) { return null; }
  const rowSel = '[role=row],[role=listitem],[role=option],[role=article],[aria-rowindex],'
    + '[aria-posinset],li,tr';
  let rows = Array.from(root.querySelectorAll(rowSel)).filter((el) => !el.querySelector(rowSel));
  if (!rows.length) {
    rows = Array.from(root.querySelectorAll('*')).filter(
      (el) => el.childElementCount === 0 && (el.textContent || '').trim());
  }
  const out = [];
  for (const el of rows) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) { continue; }
    const text = (el.innerText || '').trim();
    if (!text) { continue; }
    const idx = el.getAttribute('aria-rowindex') || el.getAttribute('aria-posinset') || '';
    const html = el.outerHTML.replace(/ data-cp-[a-z-]+="[^"]*"/g, '');
    out.push([idx, text, html, el.tagName.toLowerCase()]);
  }
  const declared = root.closest('[aria-rowcount]') || root.querySelector('[aria-rowcount]');
  let total = declared ? Number(declared.getAttribute('aria-rowcount')) : 0;
  if (!total && rows.length) { total = Number(rows[0].getAttribute('aria-setsize') || 0); }
  return { rows: out, total: total > 0 ? total : 0 };
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
    rows_accumulated: int = Field(default=0, ge=0)
    rows_total: int | None = Field(default=None, description="Declared by the list, if any")
    row_index_gaps: int = Field(default=0, ge=0)
    stop_reason: str


class AccumulatedRows(StrictModel):
    """Rows of a virtualized surface collected before they were recycled (B-9)."""

    surface_id: str
    label: str
    html: str
    text: str
    rows: int = Field(ge=0)


class ScrollOutcome(StrictModel):
    """All surfaces of one page."""

    surfaces: list[ScrollSurfaceResult] = Field(default_factory=list)
    controls: list[ControlRecord] = Field(default_factory=list)
    accumulated: list[AccumulatedRows] = Field(default_factory=list)
    stop_reason: str | None = None


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if len(line.strip()) >= 2]


class _Rows:
    """First-seen rows of one surface, in scroll order."""

    def __init__(self) -> None:
        self.by_key: dict[str, tuple[str, str, str]] = {}
        self.total = 0

    def add(self, found: dict[str, Any] | None) -> None:
        if not found:
            return
        self.total = max(self.total, int(found.get("total") or 0))
        for idx, text, row_html, tag in found.get("rows", []):
            key = f"i:{idx}" if idx else f"t:{text}"
            if key not in self.by_key and len(self.by_key) < MAX_ROWS:
                self.by_key[key] = (str(text), str(row_html), str(tag))

    def index_gaps(self) -> int:
        idx = sorted(int(k[2:]) for k in self.by_key if k.startswith("i:") and k[2:].isdigit())
        if len(idx) < 2:
            return 0
        return (idx[-1] - idx[0] + 1) - len(idx)

    def as_state(self, surface_id: str, label: str) -> AccumulatedRows:
        rows = list(self.by_key.values())
        tags = {tag for _, _, tag in rows}
        body = "".join(h for _, h, _ in rows)
        if tags == {"tr"}:
            body = f"<table><tbody>{body}</tbody></table>"
        elif tags == {"li"}:
            body = f"<ul>{body}</ul>"
        page = (
            '<!DOCTYPE html><html><body><main><section data-cp-virtualized="'
            f'{html.escape(surface_id)}" aria-label="{html.escape(label)}">{body}'
            "</section></main></body></html>"
        )
        return AccumulatedRows(
            surface_id=surface_id,
            label=label,
            html=page,
            text="\n".join(t for t, _, _ in rows),
            rows=len(rows),
        )


async def _scroll_one(
    page: Any,
    surface: dict[str, Any],
    *,
    max_passes: int,
    deadline: float,
    settle: Callable[[Any], Awaitable[None]],
    end_grace_s: float,
    rows: _Rows,
) -> ScrollSurfaceResult:
    sid = str(surface["id"])
    rows.add(await page.evaluate(ROWS_JS, sid))
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
        rows.add(await page.evaluate(ROWS_JS, sid))
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
        rows_accumulated=len(rows.by_key),
        rows_total=rows.total or None,
        row_index_gaps=rows.index_gaps(),
        stop_reason=stop,
    )


def _verdict(result: ScrollSurfaceResult) -> tuple[str, str]:
    """Terminal outcome of one surface."""
    if result.recycled_lines < _RECYCLE_MIN_LINES:
        if result.reached_end:
            return "visited", "reached_end" + (":content_grew" if result.grew else "")
        return "unvisited", result.stop_reason
    n, total = result.rows_accumulated, result.rows_total
    count = f"{n}/{total}" if total else str(n)
    if not result.reached_end:
        return "unvisited", f"{result.stop_reason}:virtualized_rows={count}"
    if total and n < total:
        return "unvisited", f"virtualized_rows_incomplete:{count}"
    if result.row_index_gaps:
        gaps = result.row_index_gaps
        return "unvisited", f"virtualized_rows_incomplete:{count}:index_gaps={gaps}"
    if n == 0:
        return "unvisited", f"virtualized_content_recycled:{result.recycled_lines}"
    return "visited", f"virtualized_rows_accumulated:{count}" + ("" if total else ":total_unknown")


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
        rows = _Rows()
        result = await _scroll_one(
            page,
            surface,
            max_passes=max_passes,
            deadline=deadline,
            settle=settle,
            end_grace_s=end_grace_s,
            rows=rows,
        )
        outcome.surfaces.append(result)
        status, reason = _verdict(result)
        if result.recycled_lines >= _RECYCLE_MIN_LINES and rows.by_key:
            outcome.accumulated.append(rows.as_state(result.surface_id, result.label))
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
