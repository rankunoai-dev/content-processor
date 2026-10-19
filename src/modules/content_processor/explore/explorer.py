"""State explorer (B-1 framework, B-2 disclosures/accordions, B-3 tabs).

Runs on a live Playwright page *after* the base state was captured. It
discovers controls, clicks only what the scope's interaction policy allows,
blocks any side effect a click would trigger (non-GET requests, page
navigation; case 147), captures each revealed state with fresh visibility
annotations, and accounts for every control it found.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Request, Route

from src.core.logger import get_logger
from src.integrations.browser_client import ANNOTATE_JS, SETTLE_PROBE_JS
from src.modules.content_processor.contracts import InteractionPolicy
from src.modules.content_processor.explore.model import (
    ControlKind,
    ControlRecord,
    ExplorationResult,
    ExploredState,
)
from src.modules.content_processor.explore.scroll import explore_scroll_surfaces

__all__ = ["IMPLEMENTED_ACTIONS", "StateExplorer"]

_logger = get_logger("content_processor.explore")

IMPLEMENTED_ACTIONS: frozenset[str] = frozenset({"expand_disclosure", "select_tab", "scroll"})
_ACTION_FOR_KIND: dict[str, str] = {
    "disclosure": "expand_disclosure",
    "accordion": "expand_disclosure",
    "find_reveal": "expand_disclosure",
    "tab": "select_tab",
}
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CLEAR_ANNOTATIONS_JS = """
() => {
  const marked = '[data-cp-hidden],[data-cp-icon],[data-cp-collapsed]';
  for (const el of document.querySelectorAll(marked)) {
    el.removeAttribute('data-cp-hidden');
    el.removeAttribute('data-cp-icon');
    el.removeAttribute('data-cp-collapsed');
  }
}
"""

DISCOVER_JS = """
() => {
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const labelOf = (el) => (el.getAttribute('aria-label') || el.innerText || el.textContent || '')
    .trim().replace(/\\s+/g, ' ').slice(0, 80);
  const body = document.body;
  if (!body) { return []; }
  let next = Number(body.getAttribute('data-cp-ctl-next') || '0');
  const out = [];
  const tag = (el, kind) => {
    if (!el.hasAttribute('data-cp-ctl')) {
      el.setAttribute('data-cp-ctl', String(next));
      next += 1;
    }
    out.push({
      id: el.getAttribute('data-cp-ctl'),
      kind,
      label: labelOf(el),
      visible: isVisible(el),
      tag: el.tagName.toLowerCase(),
      href: el.getAttribute('href') || '',
      type: (el.getAttribute('type') || '').toLowerCase(),
      inNav: !!el.closest('nav,[role=menubar],[role=navigation],[role=menu]'),
      expanded: el.getAttribute('aria-expanded'),
      selected: el.getAttribute('aria-selected'),
      open: kind === 'disclosure' ? el.parentElement.open : null,
    });
  };
  document.querySelectorAll('details > summary').forEach((s) => tag(s, 'disclosure'));
  document.querySelectorAll('[aria-expanded]').forEach((el) => {
    if (el.tagName.toLowerCase() !== 'summary') { tag(el, 'accordion'); }
  });
  document.querySelectorAll('[role="tab"]').forEach((el) => tag(el, 'tab'));
  document.querySelectorAll('[hidden="until-found"]').forEach((el) => tag(el, 'find_reveal'));
  body.setAttribute('data-cp-ctl-next', String(next));
  return out;
}
"""

REVEAL_UNTIL_FOUND_JS = """
(id) => {
  const el = document.querySelector(`[data-cp-ctl="${id}"]`);
  if (!el) { return false; }
  el.dispatchEvent(new Event('beforematch', { bubbles: true }));
  el.removeAttribute('hidden');
  return true;
}
"""


class StateExplorer:
    """Explores disclosures and tabs on one page within policy and budgets."""

    def __init__(
        self,
        policy: InteractionPolicy,
        *,
        max_explore_s: float = 45.0,
        click_timeout_s: float = 3.0,
        settle_max_s: float = 3.0,
        settle_poll_s: float = 0.25,
    ) -> None:
        """Bind the explorer to a scope's interaction policy."""
        self._policy = policy
        self._allowed = set(policy.allowed)
        self._budget = policy.max_actions_per_url
        self._max_s = max_explore_s
        self._click_timeout_ms = click_timeout_s * 1000
        self._settle_max_s = settle_max_s
        self._settle_poll_s = settle_poll_s
        self._visible: dict[str, bool] = {}

    async def explore(self, page: Any) -> ExplorationResult:
        """Explore the page. Never raises; failures are recorded on the result."""
        started = time.perf_counter()
        result = ExplorationResult(
            action_budget=self._budget,
            unexplored_action_kinds=sorted(self._allowed - IMPLEMENTED_ACTIONS),
        )
        unsafe: list[str] = []
        self._visible = {}

        async def guard(route: Route, request: Request) -> None:
            if request.method.upper() not in _SAFE_METHODS:
                unsafe.append(f"{request.method} {request.url}")
                await route.abort("blockedbyclient")
                return
            if request.is_navigation_request() and request.frame == page.main_frame:
                unsafe.append(f"navigation {request.url}")
                await route.abort("blockedbyclient")
                return
            await route.fallback()

        await page.route("**/*", guard)
        try:
            await self._loop(page, result, unsafe, started)
        except PlaywrightError as exc:
            result.error = f"PlaywrightError:{str(exc).splitlines()[0][:200]}"
        finally:
            with contextlib.suppress(PlaywrightError):
                await page.unroute("**/*", guard)
        result.unsafe_requests = unsafe
        result.elapsed_s = round(time.perf_counter() - started, 3)
        return result

    # -- main loop ----------------------------------------------------------------

    async def _loop(
        self, page: Any, result: ExplorationResult, unsafe: list[str], started: float
    ) -> None:
        records: dict[str, ControlRecord] = {}
        order: list[str] = []
        last_text = str(await page.evaluate("document.body ? document.body.innerText : ''"))
        seen_states: set[str] = {_digest(last_text)}

        if "scroll" in self._allowed:
            # Scroll first: lazy sections can bring new disclosures and tabs with them.
            scrolled = await explore_scroll_surfaces(
                page,
                max_passes=self._policy.max_scroll_passes,
                deadline=started + self._max_s,
                settle=self._settle,
            )
            for rec in scrolled.controls:
                records[rec.control_id] = rec
                order.append(rec.control_id)
            result.scroll_surfaces = [s.model_dump() for s in scrolled.surfaces]
            if scrolled.stop_reason:
                result.stop_reason = scrolled.stop_reason
            text, dom = await self._capture(page)
            key = _digest(text)
            if key not in seen_states:
                seen_states.add(key)
                window = next(
                    (r for r in scrolled.controls if r.control_id == "scroll:window"), None
                )
                state = ExploredState(
                    index=len(result.states) + 1,
                    control_id=window.control_id if window else "scroll:window",
                    kind="scroll_surface",
                    label="scroll",
                    inner_text=text,
                    dom_html=dom,
                    text_delta_chars=len(text) - len(last_text),
                )
                result.states.append(state)
                for rec in scrolled.controls:
                    if rec.status == "visited":
                        rec.state_index = state.index
                last_text = text

        while True:
            await self._scan(page, records, order)
            target = next(
                (
                    records[i]
                    for i in order
                    if records[i].status == "pending" and self._actionable(records[i])
                ),
                None,
            )
            if target is None:
                break
            if result.actions_used >= self._budget:
                result.stop_reason = "action_budget_exhausted"
                break
            if time.perf_counter() - started >= self._max_s:
                result.stop_reason = "time_budget_exhausted"
                break

            unsafe_before = len(unsafe)
            clicked = await self._act(page, target)
            result.actions_used += 1
            if not clicked:
                target.status, target.reason = "unvisited", "click_failed"
                continue
            await self._settle(page)
            if len(unsafe) > unsafe_before:
                target.status = "unvisited"
                target.reason = f"unsafe_side_effect:{unsafe[unsafe_before][:80]}"
                continue

            text, dom = await self._capture(page)
            target.status = "visited"
            key = _digest(text)
            if key in seen_states:
                target.reason = "no_new_visible_state"
                last_text = text
                continue
            seen_states.add(key)
            state = ExploredState(
                index=len(result.states) + 1,
                control_id=target.control_id,
                kind=target.kind,
                label=target.label,
                inner_text=text,
                dom_html=dom,
                text_delta_chars=len(text) - len(last_text),
            )
            result.states.append(state)
            target.state_index = state.index
            last_text = text

        for cid in order:
            rec = records[cid]
            if rec.status != "pending":
                continue
            rec.status = "unvisited"
            if result.stop_reason:
                rec.reason = result.stop_reason
            elif not self._actionable(rec):
                rec.reason = "never_visible"
            else:
                rec.reason = "not_reached"
        result.controls = [records[i] for i in order]

    def _actionable(self, rec: ControlRecord) -> bool:
        """Visible controls can be clicked; find-in-page regions are revealed without a click."""
        return rec.kind == "find_reveal" or self._visible.get(rec.control_id, False)

    async def _scan(self, page: Any, records: dict[str, ControlRecord], order: list[str]) -> None:
        found = await page.evaluate(DISCOVER_JS)
        for item in found:
            cid = str(item["id"])
            self._visible[cid] = bool(item.get("visible"))
            if cid in records:
                continue
            kind: ControlKind = item["kind"]
            rec = ControlRecord(control_id=cid, kind=kind, label=str(item.get("label") or ""))
            records[cid] = rec
            order.append(cid)
            self._classify(rec, item)

    def _classify(self, rec: ControlRecord, item: dict[str, Any]) -> None:
        """Decide the initial status of a newly discovered control."""
        if rec.kind == "accordion" and item.get("inNav"):
            rec.kind = "menu"
            rec.status, rec.reason = "excluded", "site_navigation_menu"
            return
        if rec.kind == "disclosure" and item.get("open"):
            rec.status, rec.reason = "initially_open", "details_open"
            return
        if rec.kind == "accordion" and item.get("expanded") == "true":
            rec.status, rec.reason = "initially_open", "aria_expanded_true"
            return
        if rec.kind == "tab" and item.get("selected") == "true":
            rec.status, rec.reason = "initially_open", "tab_initially_selected"
            return
        href = str(item.get("href") or "")
        if item.get("tag") == "a" and href and not href.startswith(("#", "javascript:void")):
            rec.status, rec.reason = "unvisited", "navigates_away"
            return
        if item.get("type") == "submit":
            rec.status, rec.reason = "unvisited", "submit_control"
            return
        action = _ACTION_FOR_KIND.get(rec.kind)
        if action is None or action not in self._allowed:
            rec.status, rec.reason = "unvisited", f"policy_disallows:{action}"

    async def _act(self, page: Any, rec: ControlRecord) -> bool:
        try:
            if rec.kind == "find_reveal":
                return bool(await page.evaluate(REVEAL_UNTIL_FOUND_JS, rec.control_id))
            await page.locator(f'[data-cp-ctl="{rec.control_id}"]').first.click(
                timeout=self._click_timeout_ms
            )
        except PlaywrightError as exc:
            _logger.info(
                "explore_click_failed", extra={"control": rec.label, "error": str(exc)[:120]}
            )
            return False
        return True

    async def _settle(self, page: Any) -> None:
        deadline = time.perf_counter() + self._settle_max_s
        previous: tuple[int, int] | None = None
        stable = 0
        while time.perf_counter() < deadline:
            await asyncio.sleep(self._settle_poll_s)
            try:
                probe = await page.evaluate(SETTLE_PROBE_JS)
            except PlaywrightError:
                return
            point = (int(probe.get("text", 0)), int(probe.get("nodes", 0)))
            if point == previous:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
            previous = point

    async def _capture(self, page: Any) -> tuple[str, bytes]:
        await page.evaluate(CLEAR_ANNOTATIONS_JS)
        await page.evaluate(ANNOTATE_JS)
        text = str(await page.evaluate("document.body ? document.body.innerText : ''"))
        dom = (await page.content()).encode("utf-8")
        return text, dom


def _digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
