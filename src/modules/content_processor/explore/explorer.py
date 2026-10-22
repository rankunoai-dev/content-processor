"""State explorer (B-1 framework; B-2 to B-12 interactions and capture).

Runs on a live Playwright page *after* the base state was captured. It
discovers controls, acts only where the scope's interaction policy allows,
blocks any side effect an action would trigger (non-GET requests, page
navigation; case 147), captures each revealed state with fresh visibility
annotations, and accounts for every control it found.

Order on each page:

1. consent overlay, answered with the declared choice (B-6), so it cannot
   intercept later clicks;
2. composed capture: open and closed shadow roots (B-12);
3. scroll surfaces, lazy load and virtualized rows (B-8, B-9, B-10);
4. frames, each read on its own (B-11);
5. controls: disclosures, accordions, tabs (B-2, B-3), dialogs (B-6), hover
   triggers (B-5), and the iterative ones, carousels (B-4) and
   pagination/load-more (B-7), which are clicked until they are exhausted;
6. keyboard traversal of the focus order (B-5).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
from typing import Any, get_args
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Request, Route

from src.core.logger import get_logger
from src.integrations.browser_client import ANALYTICS_HOSTS, ANNOTATE_JS, SETTLE_PROBE_JS
from src.modules.content_processor.contracts import AllowedAction, InteractionPolicy
from src.modules.content_processor.explore.compose import (
    ClosedRoot,
    capture_frames,
    compose_page,
    read_closed_roots,
)
from src.modules.content_processor.explore.deep import deep
from src.modules.content_processor.explore.interact import (
    CAROUSEL_TEXT_JS,
    CLOSE_DIALOGS_JS,
    CONSENT_JS,
    CONSENT_VISIBLE_JS,
    CONTROL_STATE_JS,
    DISCOVER_DIALOG_JS,
    DISCOVER_EXTRA_JS,
    FOCUS_JS,
    MAX_ITERATIONS,
)
from src.modules.content_processor.explore.model import (
    ControlKind,
    ControlRecord,
    ExplorationResult,
    ExploredState,
    mark_responsive_duplicates,
)
from src.modules.content_processor.explore.scroll import explore_scroll_surfaces

__all__ = ["IMPLEMENTED_ACTIONS", "StateExplorer"]

_logger = get_logger("content_processor.explore")

IMPLEMENTED_ACTIONS: frozenset[str] = frozenset(get_args(AllowedAction))
_ACTION_FOR_KIND: dict[str, str] = {
    "disclosure": "expand_disclosure",
    "accordion": "expand_disclosure",
    "find_reveal": "expand_disclosure",
    "tab": "select_tab",
    "carousel": "select_tab",
    "hover": "hover_info",
    "dialog": "open_informational_dialog",
    "load_more": "paginate_get",
    "pagination": "paginate_get",
}
_ITERATIVE = frozenset({"carousel", "load_more", "pagination"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Non-GET requests to these are telemetry or consent logging: blocked, never charged
# to the control being clicked (they also fire late, after the action that caused them).
TELEMETRY_HOSTS: tuple[str, ...] = (
    *ANALYTICS_HOSTS,
    "onetrust.com",
    "cookielaw.org",
    "cookiebot.com",
    "consensu.org",
    "trustarc.com",
    "adobedc.net",
    "omtrdc.net",
    "demdex.net",
    "facebook.net",
    "facebook.com",
    "bat.bing.com",
    "ads.linkedin.com",
    "analytics.tiktok.com",
    # real-user monitoring and error reporting
    "nr-data.net",
    "newrelic.com",
    "sentry.io",
    "datadoghq.com",
    "datadoghq.eu",
    "bugsnag.com",
    "rollbar.com",
    "dynatrace.com",
    "quantummetric.com",
    "contentsquare.net",
    "glassboxdigital.io",
    "akstat.io",
    "go-mpulse.net",
    # feedback and session-replay services seen on live sites
    "medallia.com",
    "decibelinsight.net",
)
_BODY_TEXT = "document.body ? document.body.innerText : ''"

CLEAR_ANNOTATIONS_JS = """
() => {
  const marked = '[data-cp-hidden],[data-cp-icon],[data-cp-collapsed],[data-cp-block],'
    + '[data-cp-inline-box]';
  const found = [];
  const collect = (root) => {
    found.push(...root.querySelectorAll(marked));
    for (const el of root.querySelectorAll('*')) { if (el.shadowRoot) { collect(el.shadowRoot); } }
  };
  collect(document);
  for (const el of found) {
    el.removeAttribute('data-cp-hidden');
    el.removeAttribute('data-cp-icon');
    el.removeAttribute('data-cp-collapsed');
    el.removeAttribute('data-cp-block');
    el.removeAttribute('data-cp-inline-box');
  }
}
"""

# Widgets that are not the site's content: consent managers and media players.
THIRD_PARTY_CHROME = (
    "#onetrust-consent-sdk,#onetrust-pc-sdk,#CybotCookiebotDialog,#didomi-host,"
    ".truste_box_overlay,#usercentrics-root,.qc-cmp2-container,.video-js,.vjs-control-bar,"
    ".vjs-menu,.vjs-modal-dialog,.jwplayer,.plyr,.ytp-chrome-bottom"
)
# Site utilities: sign-in and account menus, the utility bar, language, country and
# region selectors, chat widgets. Opening them reveals no page content, so they are
# not explored and never count as a gap.
SITE_UTILITIES = (
    '[class*="signin" i],[class*="sign-in" i],[class*="login" i],[class*="log-in" i],'
    '[class*="account-menu" i],[class*="my-account" i],[class*="utilities" i],'
    '[class*="utility-nav" i],[class*="utility-bar" i],[class*="language-select" i],'
    '[class*="language-switch" i],[class*="country-select" i],[class*="region-select" i],'
    '[class*="locale" i],[class*="webchat" i],[class*="web-chat" i],[class*="chatbot" i],'
    '[class*="livechat" i],[class*="live-chat" i],[class*="virtual-assistant" i],'
    '[id*="webchat" i],[id*="chatbot" i]'
)
_UTILITY_LABEL = re.compile(
    r"^(sign ?in|log ?in|login|sign ?up|register|my account|account|language|country|region|"
    r"select (a )?(language|country|region)|chat( with us)?|live chat)$",
    re.IGNORECASE,
)

DISCOVER_JS = (
    """
() => {
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const labelOf = (el) => (el.getAttribute('aria-label') || el.innerText || el.textContent || '')
    .trim().replace(/\\s+/g, ' ').slice(0, 80);
  const CHROME = '"""
    + THIRD_PARTY_CHROME
    + """';
  const UTILITIES = '"""
    + SITE_UTILITIES
    + """';
  const body = document.body;
  if (!body) { return []; }
  let next = Number(body.getAttribute('data-cp-ctl-next') || '0');
  const out = [];
  const tag = (el, kind, extra) => {
    if (!el.hasAttribute('data-cp-ctl')) {
      el.setAttribute('data-cp-ctl', String(next));
      next += 1;
    }
    out.push(Object.assign({
      id: el.getAttribute('data-cp-ctl'),
      kind,
      label: labelOf(el),
      visible: isVisible(el),
      tag: el.tagName.toLowerCase(),
      href: el.getAttribute('href') || '',
      abs: el.href || '',
      type: (el.getAttribute('type') || '').toLowerCase(),
      inNav: !!el.closest('nav,[role=menubar],[role=navigation],[role=menu]'),
      chrome: !!el.closest(CHROME),
      utility: !!el.closest(UTILITIES),
      expanded: el.getAttribute('aria-expanded'),
      selected: el.getAttribute('aria-selected'),
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      open: kind === 'disclosure' ? el.parentElement.open : null,
    }, extra || {}));
  };
  document.querySelectorAll('details > summary').forEach((s) => tag(s, 'disclosure'));
"""
    + DISCOVER_DIALOG_JS
    + """
  document.querySelectorAll('[aria-expanded]').forEach((el) => {
    if (el.tagName.toLowerCase() !== 'summary') { tag(el, 'accordion'); }
  });
  document.querySelectorAll('[role="tab"]').forEach((el) => tag(el, 'tab'));
  document.querySelectorAll('[hidden="until-found"]').forEach((el) => tag(el, 'find_reveal'));
"""
    + DISCOVER_EXTRA_JS
    + """
  body.setAttribute('data-cp-ctl-next', String(next));
  return out;
}
"""
)

REVEAL_UNTIL_FOUND_JS = """
(id) => {
  const el = document.querySelector(`[data-cp-ctl="${id}"]`);
  if (!el) { return false; }
  el.dispatchEvent(new Event('beforematch', { bubbles: true }));
  el.removeAttribute('hidden');
  return true;
}
"""

# A plain text element in the viewport: clicking it can trigger nothing on the site.
INERT_POINT_JS = """
() => {
  const busy = 'a,button,label,summary,details,input,select,textarea,[role=button],[onclick],'
    + '[tabindex],[aria-expanded],[aria-haspopup],[contenteditable]';
  for (const el of document.querySelectorAll('main p, main h1, main h2, p, h1, h2, h3')) {
    if (el.closest(busy) || el.querySelector(busy)) { continue; }
    const r = el.getBoundingClientRect();
    if (r.width > 20 && r.height > 8 && r.top >= 0 && r.bottom <= window.innerHeight) {
      return { x: r.left + Math.min(10, r.width / 2), y: r.top + r.height / 2 };
    }
  }
  return null;
}
"""

BLUR_JS = "() => { const a = document.activeElement; if (a && a.blur) { a.blur(); } }"


# Every document query in these scripts also covers open shadow roots.
DISCOVER_JS = deep(DISCOVER_JS)
REVEAL_UNTIL_FOUND_JS = deep(REVEAL_UNTIL_FOUND_JS)


class StateExplorer:
    """Explores one page's states within policy and budgets."""

    def __init__(
        self,
        policy: InteractionPolicy,
        *,
        max_explore_s: float = 90.0,
        click_timeout_s: float = 3.0,
        settle_max_s: float = 3.0,
        settle_poll_s: float = 0.25,
    ) -> None:
        """Bind the explorer to a scope's interaction policy."""
        self._policy = policy
        self._allowed = set(policy.allowed)
        self._budget = policy.max_actions_per_url
        self._max_s = max_explore_s
        self._configured_max_s = max_explore_s
        self._click_timeout_ms = click_timeout_s * 1000
        self._settle_max_s = settle_max_s
        self._settle_poll_s = settle_poll_s
        self._visible: dict[str, bool] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self._iterations: dict[str, int] = {}
        self._carousel_seen: dict[str, set[str]] = {}
        self._followups: list[str] = []
        self._closed: list[ClosedRoot] | None = None
        self._composition: dict[str, Any] = {}
        self._background: set[str] = set()
        self._acting = False
        self._last_error = ""

    async def explore(self, page: Any, *, time_left_s: float | None = None) -> ExplorationResult:
        """Explore the page. Never raises; failures are recorded on the result.

        `time_left_s` is what the caller's page-time cap still allows; the explorer
        stops in time instead of being cut off with nothing recorded.
        """
        started = time.perf_counter()
        if time_left_s is not None:
            self._max_s = min(self._configured_max_s, max(0.0, time_left_s))
        result = ExplorationResult(
            action_budget=self._budget,
            unexplored_action_kinds=sorted(self._allowed - IMPLEMENTED_ACTIONS),
        )
        unsafe: list[str] = []
        self._background.clear()
        self._acting = False
        self._visible.clear()
        self._info.clear()
        self._iterations.clear()
        self._carousel_seen.clear()
        self._followups.clear()

        async def guard(route: Route, request: Request) -> None:
            if request.method.upper() not in _SAFE_METHODS:
                key = _endpoint(request.url)
                if not self._acting:
                    # Fired with no action of ours running (sensor beacons, analytics):
                    # still blocked, but never charged to a control later.
                    self._background.add(key)
                if self._acting and (key in self._background or _is_telemetry(request.url)):
                    result.background_requests_blocked += 1
                else:
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
            # A short idle window lets background beacons show themselves first.
            await asyncio.sleep(self._settle_poll_s * 4)
            await self._loop(page, result, unsafe, started)
        except PlaywrightError as exc:
            result.error = f"PlaywrightError:{str(exc).splitlines()[0][:200]}"
        except Exception as exc:  # noqa: BLE001 - never lose the controls already explored
            _logger.exception("explore_failed")
            result.error = (
                f"{type(exc).__name__}:{str(exc).splitlines()[0][:200] if str(exc) else ''}"
            )
        finally:
            with contextlib.suppress(PlaywrightError):
                await page.unroute("**/*", guard)
        result.unsafe_requests = unsafe
        result.followup_urls = list(dict.fromkeys(self._followups))
        result.composition = dict(self._composition)
        result.elapsed_s = round(time.perf_counter() - started, 3)
        return result

    # -- main loop ----------------------------------------------------------------

    async def _loop(
        self, page: Any, result: ExplorationResult, unsafe: list[str], started: float
    ) -> None:
        records: dict[str, ControlRecord] = {}
        order: list[str] = []
        deadline = started + self._max_s

        def add(rec: ControlRecord) -> None:
            records[rec.control_id] = rec
            order.append(rec.control_id)

        def new_state(
            control_id: str, kind: ControlKind, label: str, text: str, dom: bytes, base: str
        ) -> ExploredState:
            state = ExploredState(
                index=len(result.states) + 1,
                control_id=control_id,
                kind=kind,
                label=label,
                inner_text=text,
                dom_html=dom,
                text_delta_chars=len(text) - len(base),
            )
            result.states.append(state)
            return state

        last_text = str(await page.evaluate(_BODY_TEXT))
        seen_states: set[str] = {_digest(last_text)}

        consent = await self._consent(page, unsafe)
        if consent is not None:
            add(consent)
            last_text = str(await page.evaluate(_BODY_TEXT))
            seen_states.add(_digest(last_text))

        # B-12: the composed tree (shadow roots) is part of every later capture.
        self._closed = await read_closed_roots(page)
        self._composition = {
            "closed_roots_via_cdp": self._closed is not None,
            "open_roots": 0,
            "closed_roots": 0,
            "closed_roots_read": 0,
        }
        text, dom = await self._capture(page)
        comp = self._composition
        if comp["open_roots"] or comp["closed_roots"]:
            unread = comp["closed_roots"] - comp["closed_roots_read"]
            rec = ControlRecord(
                control_id="shadow:roots",
                kind="shadow_root",
                label=f"open={comp['open_roots']};closed={comp['closed_roots']}",
                status="unvisited" if unread else "visited",
                reason=f"closed_shadow_root_unreadable:{unread}"
                if unread
                else "composed_tree_read",
            )
            add(rec)
            if _digest(text) not in seen_states:
                seen_states.add(_digest(text))
                rec.state_index = new_state(
                    rec.control_id, "shadow_root", "composed_tree", text, dom, last_text
                ).index
                last_text = text

        if "scroll" in self._allowed:
            # Scroll first: lazy sections can bring new controls and frames with them.
            scrolled = await explore_scroll_surfaces(
                page,
                max_passes=self._policy.max_scroll_passes,
                deadline=deadline,
                settle=self._settle,
            )
            for rec in scrolled.controls:
                add(rec)
            result.scroll_surfaces = [s.model_dump() for s in scrolled.surfaces]
            if scrolled.stop_reason:
                result.stop_reason = scrolled.stop_reason
            text, dom = await self._capture(page)
            if _digest(text) not in seen_states:
                seen_states.add(_digest(text))
                state = new_state("scroll:window", "scroll_surface", "scroll", text, dom, last_text)
                for rec in scrolled.controls:
                    if rec.status == "visited":
                        rec.state_index = state.index
                last_text = text
            for acc in scrolled.accumulated:
                # B-9: rows collected before the list recycled them, as one state.
                state = new_state(
                    f"scroll:{acc.surface_id}",
                    "scroll_surface",
                    f"virtualized:{acc.label}"[:80],
                    acc.text,
                    acc.html.encode("utf-8"),
                    "",
                )
                records[f"scroll:{acc.surface_id}"].state_index = state.index

        await self._frames(page, add, result)
        await self._calibrate(page)

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
            if time.perf_counter() >= deadline:
                result.stop_reason = "time_budget_exhausted"
                break
            if target.kind in _ITERATIVE and await self._exhausted_before_click(page, target):
                continue

            unsafe_before = len(unsafe)
            self._acting = True
            try:
                clicked = await self._act(page, target)
                result.actions_used += 1
                if clicked:
                    await self._settle(page)
            finally:
                self._acting = False
            if not clicked:
                target.status, target.reason = "unvisited", f"click_failed:{self._last_error}"
                await self._restore(page, target)
                continue
            if len(unsafe) > unsafe_before:
                target.status = "unvisited"
                target.reason = f"unsafe_side_effect:{unsafe[unsafe_before][:80]}"
                await self._restore(page, target)
                continue

            text, dom = await self._capture(page)
            key = _digest(text)
            fresh = key not in seen_states
            if fresh:
                seen_states.add(key)
                state = new_state(
                    target.control_id, target.kind, target.label, text, dom, last_text
                )
                target.state_index = target.state_index or state.index
            last_text = text
            await self._restore(page, target)
            if target.kind in _ITERATIVE:
                await self._after_iteration(page, target, fresh)
                continue
            target.status = "visited"
            if not fresh:
                _note(target, "no_new_visible_state")

        if "keyboard_traverse" in self._allowed and result.stop_reason is None:
            add(await self._keyboard(page, result, seen_states, new_state, deadline))

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
        mark_responsive_duplicates(result.controls)

    async def _calibrate(self, page: Any) -> None:
        """Click plain text once so interaction-triggered beacons are learned as background.

        Bot-management sensors and analytics POST on every pointer event. Fired here,
        by a click that cannot do anything on the site, they are known to be
        background, so a later click on a real control is not charged with them.
        """
        point = await page.evaluate(INERT_POINT_JS)
        if point is None:
            return
        x, y = float(point["x"]), float(point["y"])
        try:
            await page.mouse.move(x, y, steps=4)
            await page.mouse.click(x, y)
        except PlaywrightError:
            return
        await self._settle(page)

    # -- B-6 consent ----------------------------------------------------------------

    async def _consent(self, page: Any, unsafe: list[str]) -> ControlRecord | None:
        """Answer a consent overlay with the declared choice (reject non-essential)."""
        found = dict(await page.evaluate(CONSENT_JS))
        if not found.get("found"):
            return None
        rec = ControlRecord(
            control_id="consent:banner", kind="consent", label=str(found.get("label") or "")
        )
        if "open_informational_dialog" not in self._allowed:
            rec.status, rec.reason = "excluded", "consent_overlay_left_in_place:policy"
            return rec
        if not found.get("choice"):
            why = "only_accept_offered" if found.get("acceptOnly") else "no_choice_found"
            rec.status, rec.reason = "excluded", f"consent_overlay_left_in_place:{why}"
            return rec
        before = len(unsafe)
        try:
            await page.locator('[data-cp-consent="1"]').first.click(timeout=self._click_timeout_ms)
        except PlaywrightError:
            rec.status, rec.reason = "unvisited", "click_failed"
            return rec
        await self._settle(page)
        hide_deadline = time.perf_counter() + 3.0
        while await page.evaluate(CONSENT_VISIBLE_JS):
            if time.perf_counter() >= hide_deadline:
                rec.status = "unvisited"
                rec.reason = f"consent_overlay_persisted:clicked={found.get('choice')}"
                return rec
            await asyncio.sleep(self._settle_poll_s)
        blocked = len(unsafe) - before
        rec.status = "visited"
        rec.reason = "declared_choice:reject_non_essential" + (
            f";consent_requests_blocked={blocked}" if blocked else ""
        )
        return rec

    # -- B-11 frames ----------------------------------------------------------------

    async def _frames(self, page: Any, add: Any, result: ExplorationResult) -> None:
        scripts = [CLEAR_ANNOTATIONS_JS, ANNOTATE_JS]
        frames = await capture_frames(page, scripts)
        for _ in range(2):
            # Widgets such as stock tickers replace their iframe while we read it: the
            # old frame is detached, and its replacement may not be laid out yet (zero
            # size, so it looks hidden). Look again for what was not read.
            unsettled = [f for f in frames if f.hidden or (f.error and "detached" in f.error)]
            if not unsettled:
                break
            await self._settle(page)
            read = {f.frame_path for f in frames if not f.hidden and not f.error}
            again = await capture_frames(page, scripts, skip=read)
            if any(not f.hidden and not f.error for f in again):
                frames = [f for f in frames if f not in unsettled] + again
            elif all(f.hidden for f in unsettled):
                break  # still hidden after settling: tracking pixels, not a race
        report: list[dict[str, Any]] = []
        for n, fc in enumerate(frames):
            rec = ControlRecord(
                control_id=f"frame:{n}",
                kind="frame",
                label=(fc.frame_path if fc.kind == "srcdoc" else f"{fc.kind}:{fc.frame_path}")[:80],
            )
            if fc.hidden:
                rec.status, rec.reason = "excluded", "hidden_frame"
            elif fc.error:
                rec.status, rec.reason = "unvisited", f"frame_unreadable:{fc.error[:60]}"
            elif not fc.text.strip():
                rec.status, rec.reason = "visited", "empty_frame"
            else:
                state = ExploredState(
                    index=len(result.states) + 1,
                    control_id=rec.control_id,
                    kind="frame",
                    label=(fc.title or fc.frame_path)[:80],
                    inner_text=fc.text,
                    dom_html=fc.html.encode("utf-8"),
                    text_delta_chars=len(fc.text),
                    frame_path=fc.frame_path,
                )
                result.states.append(state)
                rec.status, rec.reason, rec.state_index = "visited", f"{fc.kind}_read", state.index
            add(rec)
            report.append(
                {
                    "frame_path": fc.frame_path,
                    "kind": fc.kind,
                    "hidden": fc.hidden,
                    "chars": len(fc.text),
                    "error": fc.error,
                }
            )
        self._composition["frames"] = report

    # -- B-5 keyboard ----------------------------------------------------------------

    async def _keyboard(
        self,
        page: Any,
        result: ExplorationResult,
        seen_states: set[str],
        new_state: Any,
        deadline: float,
    ) -> ControlRecord:
        """Tab once through the focus order and keep text that focus reveals."""
        rec = ControlRecord(control_id="keyboard:tab", kind="keyboard", label="focus order")
        limit = max(20, 4 * self._budget)
        await page.evaluate(BLUR_JS)
        quick = _digest(str(await page.evaluate(_BODY_TEXT)))
        first: str | None = None
        stop_labels: list[str] = []
        presses = 0
        result.actions_used += 1
        while True:
            if time.perf_counter() >= deadline:
                rec.status, rec.reason = "unvisited", "time_budget_exhausted"
                break
            if presses >= limit:
                rec.status, rec.reason = "unvisited", f"tab_stops_exhausted:{presses}"
                break
            await page.keyboard.press("Tab")
            presses += 1
            focus = await page.evaluate(FOCUS_JS)
            if focus is None and first is None and presses >= 3:
                rec.status, rec.reason = "visited", "no_focusable_elements"
                break
            if focus is not None:
                if first is None:
                    first = str(focus["id"])
                elif str(focus["id"]) == first:
                    rec.status, rec.reason = "visited", f"focus_cycle_complete:{presses - 1}"
                    rec.label = " | ".join(stop_labels)
                    break
                stop_labels.append(str(focus["label"]))
            now = _digest(str(await page.evaluate(_BODY_TEXT)))
            if now == quick:
                continue
            quick = now
            await self._settle(page)
            text, dom = await self._capture(page)
            if _digest(text) in seen_states:
                continue
            seen_states.add(_digest(text))
            label = f"focus:{focus['label'] if focus else 'document'}"[:80]
            state = new_state(rec.control_id, "keyboard", label, text, dom, "")
            rec.state_index = rec.state_index or state.index
        await page.evaluate(BLUR_JS)
        return rec

    # -- controls -------------------------------------------------------------------

    def _actionable(self, rec: ControlRecord) -> bool:
        """Visible controls can be acted on; find-in-page regions are revealed without one."""
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
            self._info[cid] = dict(item)
            self._classify(rec, item)

    def _classify(self, rec: ControlRecord, item: dict[str, Any]) -> None:  # noqa: C901
        """Decide the initial status of a newly discovered control."""
        href = str(item.get("href") or "")
        real_link = item.get("tag") == "a" and href and not href.startswith(("#", "javascript:"))
        if item.get("chrome"):
            rec.status, rec.reason = "excluded", "third_party_chrome"
            return
        if item.get("utility") or _UTILITY_LABEL.match(rec.label.strip()):
            rec.status, rec.reason = "excluded", "site_utility_control"
            return
        if rec.kind == "accordion" and item.get("inNav"):
            rec.kind = "menu"
            rec.status, rec.reason = "excluded", "site_navigation_menu"
            return
        if rec.kind == "hover" and item.get("inNav") and "hover_info" not in self._allowed:
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
        if rec.kind in ("pagination", "load_more") and real_link:
            # A separate URL: the frontier crawls it as its own page with its own record.
            rec.status, rec.reason = "excluded", "separate_url_followup"
            if item.get("abs"):
                self._followups.append(str(item["abs"]))
            return
        if rec.kind in _ITERATIVE and item.get("disabled"):
            rec.status, rec.reason = "excluded", "control_disabled"
            return
        if real_link and rec.kind != "hover":
            rec.status, rec.reason = "unvisited", "navigates_away"
            return
        if item.get("type") == "submit":
            rec.status, rec.reason = "unvisited", "submit_control"
            return
        action = _ACTION_FOR_KIND.get(rec.kind)
        if action is None or action not in self._allowed:
            rec.status, rec.reason = "unvisited", f"policy_disallows:{action}"

    async def _act(self, page: Any, rec: ControlRecord) -> bool:
        locator = page.locator(f'[data-cp-ctl="{rec.control_id}"]').first
        try:
            if rec.kind == "find_reveal":
                return bool(await page.evaluate(REVEAL_UNTIL_FOUND_JS, rec.control_id))
            if rec.kind == "carousel" and rec.control_id not in self._carousel_seen:
                box = await page.evaluate(CAROUSEL_TEXT_JS, rec.control_id)
                self._carousel_seen[rec.control_id] = {_digest(str(box or ""))}
            if rec.kind == "hover":
                await locator.hover(timeout=self._click_timeout_ms)
            else:
                try:
                    await locator.click(timeout=self._click_timeout_ms)
                except PlaywrightError as exc:
                    if _click_cause(exc) != "intercepted":
                        raise
                    # Something left open (a menu, a dialog) covers the control:
                    # dismiss it and try once more.
                    await page.keyboard.press("Escape")
                    await page.evaluate(CLOSE_DIALOGS_JS)
                    await self._settle(page)
                    await locator.click(timeout=self._click_timeout_ms)
        except PlaywrightError as exc:
            self._last_error = _click_cause(exc)
            _logger.info(
                "explore_click_failed", extra={"control": rec.label, "error": str(exc)[:120]}
            )
            return False
        return True

    async def _restore(self, page: Any, rec: ControlRecord) -> None:
        """Undo a reversible reveal so later controls are reachable."""
        try:
            if rec.kind == "hover":
                await page.mouse.move(0, 0)
            elif rec.kind == "dialog":
                await page.keyboard.press("Escape")
                await asyncio.sleep(self._settle_poll_s)
                if int(await page.evaluate(CLOSE_DIALOGS_JS)) > 0:
                    _note(rec, "dialog_left_open")
                await self._settle(page)
        except PlaywrightError:
            _note(rec, "restore_failed")

    async def _exhausted_before_click(self, page: Any, rec: ControlRecord) -> bool:
        """An iterative control that vanished or was disabled is done."""
        state = dict(await page.evaluate(CONTROL_STATE_JS, rec.control_id))
        if state.get("present") and state.get("visible") and not state.get("disabled"):
            return False
        done = self._iterations.get(rec.control_id, 0)
        rec.status = "visited" if done else "excluded"
        rec.reason = f"exhausted_after:{done}" if done else "control_disabled"
        return True

    async def _after_iteration(self, page: Any, rec: ControlRecord, fresh: bool) -> None:
        """Decide whether a carousel or pagination control needs another click."""
        n = self._iterations[rec.control_id] = self._iterations.get(rec.control_id, 0) + 1
        if rec.kind == "carousel":
            box = await page.evaluate(CAROUSEL_TEXT_JS, rec.control_id)
            seen = self._carousel_seen.setdefault(rec.control_id, set())
            total = int(self._info.get(rec.control_id, {}).get("total") or 0)
            if box is None:
                rec.status, rec.reason = "unvisited", "carousel_disappeared"
                return
            if _digest(str(box)) in seen:
                rec.status, rec.reason = "visited", f"cycled_after:{n}"
                return
            seen.add(_digest(str(box)))
            if total and n >= total - 1:
                rec.status, rec.reason = "visited", f"known_total:{total}"
                return
        else:
            state = dict(await page.evaluate(CONTROL_STATE_JS, rec.control_id))
            if not state.get("present") or not state.get("visible") or state.get("disabled"):
                rec.status, rec.reason = "visited", f"exhausted_after:{n}"
                return
            if not fresh:
                rec.status, rec.reason = "visited", f"no_new_content_after:{n}"
                return
        if n >= MAX_ITERATIONS:
            rec.status, rec.reason = "unvisited", f"iteration_budget_exhausted:{n}"

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
            point = (int(probe.get("text", 0)), int(probe.get("hash", probe.get("nodes", 0))))
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
        composed = await compose_page(page, self._closed)
        if composed is not None:
            comp = self._composition
            comp["open_roots"] = max(comp.get("open_roots", 0), composed.open_roots)
            comp["closed_roots"] = max(comp.get("closed_roots", 0), composed.closed_roots)
            comp["closed_roots_read"] = max(
                comp.get("closed_roots_read", 0), composed.closed_roots_read
            )
            return composed.text, composed.html.encode("utf-8")
        text = str(await page.evaluate(_BODY_TEXT))
        dom = (await page.content()).encode("utf-8")
        return text, dom


def _click_cause(exc: Exception) -> str:
    """Short, stable cause of a failed click, for the control's reason."""
    text = str(exc)
    for marker, cause in (
        ("intercepts pointer events", "intercepted"),
        ("not visible", "not_visible"),
        ("outside of the viewport", "outside_viewport"),
        ("not enabled", "disabled"),
        ("detached", "detached"),
        ("Timeout", "timeout"),
    ):
        if marker in text:
            return cause
    return "error"


def _is_telemetry(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in TELEMETRY_HOSTS)


def _endpoint(url: str) -> str:
    """Host and path of a request, without query or fragment."""
    return url.split("#", 1)[0].split("?", 1)[0]


def _note(rec: ControlRecord, note: str) -> None:
    """Add a note to a control's reason without hiding the reason already there."""
    rec.reason = f"{rec.reason};{note}" if rec.reason else note


def _digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
