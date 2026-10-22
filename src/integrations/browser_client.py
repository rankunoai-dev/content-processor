"""T3 fidelity browser rendering (playbook section 2, tier T3; ADR-0002).

One `BrowserRenderer` owns one browser. Every `render()` gets a fresh context
with the scope's locale, timezone, viewport and user agent, routes every
subrequest through the same egress check as the HTTP fetcher, waits for the
page to settle (bounded delta-stability, never a fixed sleep), annotates the
DOM with computed visibility, and returns the DOM, visible text, accessibility
snapshot, screenshot and the content-bearing network responses it saw.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from pydantic import Field

from src.core.logger import get_logger
from src.core.schemas import StrictModel

__all__ = [
    "AssetStore",
    "BrowserRenderer",
    "CapturedResponse",
    "PageHook",
    "RenderLimits",
    "RenderResult",
]

_STATIC_RESOURCE_TYPES = frozenset({"stylesheet", "script", "font", "image"})


class AssetStore(Protocol):
    """Group-scoped store for static sub-resources (implemented by grouping.AssetCache).

    Declared here, not imported, because integrations must not depend on modules.
    """

    def lookup(self, url: str, resource_type: str, method: str) -> Any:
        """Return a stored response (with status, headers and body) or None."""
        ...

    def record_origin(
        self,
        url: str,
        resource_type: str,
        method: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        """Account for an origin response and keep it when cacheable."""
        ...


_logger = get_logger("integrations.browser_client")

HostChecker = Callable[[str], tuple[bool, str]]
PageHook = Callable[[Any], Awaitable[None]]
"""Called with the live page after the base state is captured (e.g. state exploration)."""

ANALYTICS_HOSTS: tuple[str, ...] = (
    "googletagmanager.com",
    "google-analytics.com",
    "analytics.google.com",
    "doubleclick.net",
    "hotjar.com",
    "clarity.ms",
    "segment.io",
    "segment.com",
    "optimizely.com",
    "vwo.com",
    "mixpanel.com",
    "fullstory.com",
)

# Marks every element with computed visibility and icon-font hints so the same
# source walker can classify the rendered DOM. Returns counts for the report.
ANNOTATE_JS = """
() => {
  const iconFonts = new RegExp(['material icons', 'material symbols', 'font ?awesome',
    'glyphicon', 'icomoon', 'bootstrap-icons', 'feather', 'ionicons',
    'simple-line-icons'].join('|'), 'i');
  let hidden = 0, icons = 0, total = 0, blocks = 0;
  const inlineTags = new Set(['span', 'a', 'em', 'strong', 'b', 'i', 'u', 'small', 'abbr',
    'cite', 'code', 'time', 'mark', 'font', 'label', 'q', 's', 'del', 'ins', 'bdi', 'bdo',
    'data', 'var', 'kbd', 'samp', 'sup', 'sub', 'img', 'big', 'tt']);
  const blockDisplay = new Set(['block', 'flex', 'grid', 'table', 'table-row', 'table-cell',
    'table-caption', 'table-row-group', 'table-header-group', 'table-footer-group',
    'list-item', 'flow-root']);
  const root = document.documentElement;
  const docRect = { w: root.scrollWidth, h: root.scrollHeight };
  const inlineBox = (node, cs) => {
    if (!(node.textContent || '').trim()) { return false; }
    if (['inline-block', 'inline-flex', 'inline-grid', 'inline-table'].includes(cs.display)) {
      return true;
    }
    if (cs.display !== 'inline') { return false; }
    const side = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight)
      + parseFloat(cs.marginLeft) + parseFloat(cs.marginRight);
    return side >= 4;
  };
  const ownTextLaidOut = (node) => {
    for (const c of node.childNodes) {
      if (c.nodeType !== 3 || !c.nodeValue.trim()) { continue; }
      const range = document.createRange();
      range.selectNodeContents(c);
      if (range.getClientRects().length > 0) { return true; }
    }
    return false;
  };
  // Walk the composed tree: elements inside open shadow roots are marked too.
  const all = [];
  const collect = (root) => {
    for (const el of root.querySelectorAll('*')) {
      all.push(el);
      if (el.shadowRoot) { collect(el.shadowRoot); }
    }
  };
  if (document.body) { collect(document.body); }
  for (const el of all) {
    total += 1;
    const tag = el.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'template' || tag === 'noscript') continue;
    const cs = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const hasBox = rect.width > 0 && rect.height > 0;
    const outside = rect.right < 0 || rect.bottom < 0
      || rect.left > docRect.w || rect.top > docRect.h;
    const offscreen = hasBox && outside;
    const voidTags = ['br', 'wbr', 'area', 'map', 'col', 'colgroup'];
    // A box-less element with text is hidden only when none of its own text is laid
    // out: a web component host (display: contents, or text drawn through a shadow
    // root slot) has no box but its text is on screen (live John Hancock cards).
    const zeroBoxWithText = rect.width === 0 && rect.height === 0 && !voidTags.includes(tag)
      && el.childElementCount === 0 && (el.textContent || '').trim().length > 0
      && !ownTextLaidOut(el);
    const invisible = cs.display === 'none' || cs.visibility === 'hidden'
      || parseFloat(cs.opacity) === 0 || zeroBoxWithText;
    if (invisible || offscreen) { el.setAttribute('data-cp-hidden', '1'); hidden += 1; }
    if (iconFonts.test(cs.fontFamily || '')) { el.setAttribute('data-cp-icon', '1'); icons += 1; }
    if (tag === 'details' && !el.hasAttribute('open')) {
      el.setAttribute('data-cp-collapsed', '1');
    }
    // An inline element the page lays out as a block is a line of its own in the
    // browser ("com<span style=display:block>(opens in a new tab)</span>."): mark
    // it so the extractor ends a block there too (K22). Only inline-by-default tags
    // are marked (block tags already end blocks), and inline-block/flex/grid render
    // inline, so they are not.
    if (inlineTags.has(tag) && blockDisplay.has(cs.display)) {
      el.setAttribute('data-cp-block', '1');
      blocks += 1;
    } else if (inlineBox(el, cs)) {
      // Side-by-side boxes (menu links, buttons, chips) read as separate words on
      // screen even when the markup has no space between them; innerText glues them.
      el.setAttribute('data-cp-inline-box', '1');
    }
  }
  return { total, hidden, icons, blocks };
}
"""

# B. Only elements that can show text count: tag managers keep injecting <script>,
#    tracking pixels and the like long after the visible page is final.
# Settling is judged on the visible text only: its length and a cheap fingerprint, so a
# same-length change still counts. Elements without text (tag-manager scripts, empty
# widget containers, tracking pixels) keep being added on live pages long after the
# text is final and must not keep a page "changing".
SETTLE_PROBE_JS = """
() => {
  const text = document.body ? document.body.innerText : '';
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return { text: text.length, hash: hash >>> 0, ready: document.readyState };
}
"""

# DOM and visible text read in one call, so nothing can change between the two reads.
SNAPSHOT_JS = """
() => {
  const dt = document.doctype;
  const doctype = dt ? '<!DOCTYPE ' + dt.name + '>' : '';
  return { html: doctype + document.documentElement.outerHTML,
           text: document.body ? document.body.innerText : '' };
}
"""


async def serve_through_asset_store(route: Any, request: Any, asset_store: Any) -> None:
    """Answer a static request from the group's asset store, or fetch it and record it.

    A failure on the way (the page timed out or closed while the asset was in
    flight: "Request context disposed") falls back to a normal request, and if
    even that is impossible it is dropped: a missed cache entry must never fail
    the page itself.
    """
    hit = asset_store.lookup(request.url, request.resource_type, request.method)
    try:
        if hit is not None:
            await route.fulfill(status=hit.status, headers=hit.headers, body=hit.body)
            return
        if request.method.upper() != "GET":
            await route.continue_()
            return
        response = await route.fetch()
        body = await response.body()
        asset_store.record_origin(
            request.url,
            request.resource_type,
            request.method,
            response.status,
            dict(response.headers),
            body,
        )
        await route.fulfill(response=response)
    except PlaywrightError:
        with contextlib.suppress(PlaywrightError):
            await route.continue_()


class RenderLimits(StrictModel):
    """Caps applied to every render."""

    navigation_timeout_s: float = Field(default=30.0, gt=0)
    settle_window_s: float = Field(default=12.0, gt=0)
    settle_stable_polls: int = Field(default=3, ge=1, le=20)
    settle_min_quiet_s: float = Field(
        default=3.0,
        ge=0.0,
        description="Text and node counts must stay unchanged this long before "
        "the page counts as settled; late hydration shorter than this is caught.",
    )
    poll_interval_s: float = Field(default=0.5, gt=0)
    max_page_time_s: float = Field(
        default=150.0, gt=0, description="Whole render incl. any page hook (exploration)"
    )
    max_response_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1)
    max_captured_responses: int = Field(default=50, ge=0)
    screenshot_quality: int = Field(default=70, ge=10, le=100)
    full_page_screenshot: bool = True


class CapturedResponse(StrictModel):
    """A content-bearing subresponse observed during the render."""

    url: str
    status: int
    content_type: str
    body: bytes = b""
    truncated: bool = False


class RenderResult(StrictModel):
    """Everything observed while rendering one URL."""

    requested_url: str
    final_url: str
    http_status: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    hops: list[str] = Field(default_factory=list)
    dom_html: bytes = b""
    inner_text: str = ""
    aria_snapshot: str = ""
    screenshot_jpeg: bytes = b""
    console_errors: list[str] = Field(default_factory=list)
    failed_requests: list[str] = Field(default_factory=list)
    blocked_requests: list[str] = Field(default_factory=list)
    captured_responses: list[CapturedResponse] = Field(default_factory=list)
    settle: dict[str, Any] = Field(default_factory=dict)
    annotation: dict[str, int] = Field(default_factory=dict)
    elapsed_ms: float = 0.0
    transport_error: str | None = None

    @property
    def rendered(self) -> bool:
        """True when a document was obtained and serialized."""
        return self.transport_error is None and bool(self.dom_html)


class BrowserRenderer:
    """One browser, one context per render, crash-contained (ADR-0002)."""

    tier_name = "T3_FIDELITY_BROWSER"

    def __init__(
        self,
        *,
        user_agent: str,
        host_checker: HostChecker,
        locale: str = "en-US",
        timezone: str = "UTC",
        viewport: tuple[int, int] = (1440, 900),
        analytics_suppression: bool = False,
        limits: RenderLimits | None = None,
        headless: bool = True,
        channel: str | None = "chrome",
        max_pages_per_browser: int = 50,
    ) -> None:
        """Configure; call `start()` before rendering."""
        self._ua = user_agent
        self._check = host_checker
        self._locale = locale
        self._tz = timezone
        self._viewport = viewport
        self._suppress = analytics_suppression
        self._limits = limits or RenderLimits()
        self.limits = self._limits
        self._headless = headless
        self._channel = channel
        self._recycle_after = max_pages_per_browser
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._pages_served = 0
        self.browser_version = ""

    async def start(self) -> None:
        """Launch the browser (real Chrome channel when available, else Chromium)."""
        self._pw = await async_playwright().start()
        try:
            if self._channel:
                self._browser = await self._pw.chromium.launch(
                    channel=self._channel, headless=self._headless
                )
            else:
                raise PlaywrightError("no channel requested")
        except PlaywrightError:
            self._browser = await self._pw.chromium.launch(headless=self._headless)
        self.browser_version = self._browser.version
        self._pages_served = 0

    async def stop(self) -> None:
        """Close the browser and Playwright."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def _recycle_if_needed(self) -> None:
        if self._browser is None or self._pages_served >= self._recycle_after:
            _logger.info("browser_recycle", extra={"pages_served": self._pages_served})
            await self.stop()
            await self.start()

    async def render(
        self,
        url: str,
        on_page: PageHook | None = None,
        asset_store: AssetStore | None = None,
    ) -> RenderResult:
        """Render `url` with a fresh context. Never raises.

        `on_page` runs on the live page after the base DOM, text and screenshot are
        captured and before the context closes. Its failures are recorded as console
        errors; they never discard the base capture.
        """
        started = time.perf_counter()
        await self._recycle_if_needed()
        try:
            async with asyncio.timeout(self._limits.max_page_time_s):
                result = await self._render_inner(url, on_page, asset_store)
        except TimeoutError:
            result = RenderResult(
                requested_url=url, final_url=url, transport_error="render_timeout"
            )
        except PlaywrightError as exc:
            result = RenderResult(
                requested_url=url,
                final_url=url,
                transport_error=f"PlaywrightError:{str(exc).splitlines()[0][:200]}",
            )
        self._pages_served += 1
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result

    async def _render_inner(  # noqa: C901 - one pipeline
        self,
        url: str,
        on_page: PageHook | None = None,
        asset_store: AssetStore | None = None,
    ) -> RenderResult:
        assert self._browser is not None  # noqa: S101 - start() precondition
        context = await self._browser.new_context(
            user_agent=self._ua,
            locale=self._locale,
            timezone_id=self._tz,
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            ignore_https_errors=False,
        )
        console_errors: list[str] = []
        failed: list[str] = []
        blocked: list[str] = []
        captured: list[CapturedResponse] = []

        async def on_route(route: Route, request: Request) -> None:
            host = urlsplit(request.url).hostname or ""
            allowed, reason = self._check(host)
            if not allowed:
                blocked.append(f"{request.url} ({reason})")
                await route.abort("blockedbyclient")
                return
            if self._suppress and any(host == h or host.endswith("." + h) for h in ANALYTICS_HOSTS):
                blocked.append(f"{request.url} (analytics_suppressed)")
                await route.abort("blockedbyclient")
                return
            # Host checks above run first: the store can only replace a request the policy allowed.
            if asset_store is not None and request.resource_type in _STATIC_RESOURCE_TYPES:
                await serve_through_asset_store(route, request, asset_store)
                return
            await route.continue_()

        async def on_response(response: Response) -> None:
            if len(captured) >= self._limits.max_captured_responses:
                return
            ctype = (response.headers.get("content-type") or "").lower()
            rtype = response.request.resource_type
            content_bearing = any(t in ctype for t in ("json", "html", "xml", "text/plain"))
            if rtype not in ("xhr", "fetch") or not content_bearing:
                return
            try:
                body = await response.body()
            except PlaywrightError:
                return
            cap = self._limits.max_response_body_bytes
            captured.append(
                CapturedResponse(
                    url=response.url,
                    status=response.status,
                    content_type=ctype[:100],
                    body=body[:cap],
                    truncated=len(body) > cap,
                )
            )

        await context.route("**/*", on_route)
        page = await context.new_page()
        page.on(
            "console", lambda m: console_errors.append(m.text[:300]) if m.type == "error" else None
        )
        page.on("pageerror", lambda e: console_errors.append(f"pageerror:{str(e)[:300]}"))
        page.on("requestfailed", lambda r: failed.append(f"{r.url} ({r.failure or 'failed'})"))
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._limits.navigation_timeout_s * 1000,
            )
            settle = await self._settle(page)
            annotation: dict[str, int] = {}
            try:
                annotation = dict(await page.evaluate(ANNOTATE_JS))
            except PlaywrightError as exc:
                console_errors.append(f"annotate_failed:{str(exc)[:120]}")
            snapshot = dict(await page.evaluate(SNAPSHOT_JS))
            dom_html = str(snapshot.get("html") or "").encode("utf-8")
            inner_text = str(snapshot.get("text") or "")
            try:
                aria = await page.locator("body").aria_snapshot()
            except PlaywrightError:
                aria = ""
            try:
                screenshot = await page.screenshot(
                    full_page=self._limits.full_page_screenshot,
                    type="jpeg",
                    quality=self._limits.screenshot_quality,
                    timeout=self._limits.navigation_timeout_s * 1000,
                )
            except PlaywrightError as exc:
                screenshot = b""
                console_errors.append(f"screenshot_failed:{str(exc)[:120]}")
            final_url = page.url
            if on_page is not None:
                try:
                    await on_page(page)
                except Exception as exc:  # noqa: BLE001 - a hook must not lose the base capture
                    console_errors.append(f"page_hook_failed:{type(exc).__name__}:{str(exc)[:120]}")

            hops: list[str] = []
            headers: dict[str, str] = {}
            status: int | None = None
            if response is not None:
                status = response.status
                headers = {k.lower(): v for k, v in (await response.all_headers()).items()}
                req = response.request
                chain: list[str] = []
                prev = req.redirected_from
                while prev is not None:
                    chain.append(prev.url)
                    prev = prev.redirected_from
                hops = list(reversed(chain))
            return RenderResult(
                requested_url=url,
                final_url=final_url,
                http_status=status,
                headers=headers,
                hops=hops,
                dom_html=dom_html,
                inner_text=inner_text,
                aria_snapshot=aria,
                screenshot_jpeg=screenshot,
                console_errors=console_errors,
                failed_requests=failed,
                blocked_requests=blocked,
                captured_responses=captured,
                settle=settle,
                annotation=annotation,
            )
        finally:
            await context.close()

    async def _settle(self, page: Any) -> dict[str, Any]:
        """Wait until text length and node count are unchanged for the quiet period.

        Stability is evidence, not proof (ARCHITECTURE.md section 4): the result
        records whether the quiet period was reached inside the window.
        """
        series: list[tuple[int, int]] = []
        stable_polls = 0
        last_change = time.perf_counter()
        deadline = last_change + self._limits.settle_window_s
        extended = False
        while True:
            if time.perf_counter() >= deadline:
                # The window closed while the page was already quiet (late hydration,
                # unchanged since): allow once just enough time to confirm the quiet
                # period. A page that keeps changing still runs out.
                if extended or stable_polls == 0:
                    break
                extended = True
                deadline = last_change + self._limits.settle_min_quiet_s
                deadline += self._limits.poll_interval_s * 2
                continue
            try:
                probe = await page.evaluate(SETTLE_PROBE_JS)
            except PlaywrightError:
                break
            point = (int(probe.get("text", 0)), int(probe.get("hash", probe.get("nodes", 0))))
            now = time.perf_counter()
            if series and point == series[-1]:
                stable_polls += 1
                quiet_for = now - last_change
                if (
                    stable_polls >= self._limits.settle_stable_polls
                    and quiet_for >= self._limits.settle_min_quiet_s
                ):
                    series.append(point)
                    return {
                        "stable": True,
                        "polls": len(series),
                        "quiet_s": round(quiet_for, 2),
                        "series": series[-6:],
                        "window_extended": extended,
                    }
            else:
                stable_polls = 0
                last_change = now
            series.append(point)
            await asyncio.sleep(self._limits.poll_interval_s)
        return {
            "stable": False,
            "polls": len(series),
            "series": series[-6:],
            "window_extended": extended,
        }
