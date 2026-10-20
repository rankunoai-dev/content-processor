"""Composed capture: shadow DOM (B-12) and frames (B-11).

`page.content()` and `document.body.innerText` both stop at shadow roots and
frame boundaries, so text inside web components and iframes would silently be
missing from every state. This module reads them:

- open shadow roots are serialized in the page from the composed tree (slots
  replaced by the nodes assigned to them), with the same visibility marks the
  rest of the document carries;
- closed shadow roots are invisible to page scripts. In Chromium the DevTools
  protocol still sees them (`DOM.getDocument` with `pierce`), so their exact
  text is read there and spliced in at the host. No page global is patched, so
  nothing in the page can notice. Without CDP the roots stay an honest gap;
  screenshot OCR of such hosts belongs to C-8;
- every child frame (same-origin, cross-origin, `srcdoc`, nested) is read on
  its own, annotated inside the frame, and returned with a `frame_path` (the
  iframe's resolved `src`, or `srcdoc:<n>`) so rule R3 can match it.

When a page has no shadow roots the plain capture is used unchanged.
"""

from __future__ import annotations

import contextlib
import html
from collections.abc import Collection, Iterator
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "COMPOSE_JS",
    "ClosedRoot",
    "ComposedCapture",
    "FrameCapture",
    "capture_frames",
    "compose_page",
    "read_closed_roots",
]

_CLOSED_MARK = "\u0000cp-closed:{}\u0000"
_SKIP_TEXT_TAGS = frozenset({"script", "style", "template", "noscript"})
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)

COMPOSE_JS = """
() => {
  const VOID = new Set(['area','base','br','col','embed','hr','img','input','link','meta',
    'source','track','wbr']);
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const escA = (s) => esc(s).replace(/"/g, '&quot;');
  const shown = (el) => (el.checkVisibility ? el.checkVisibility() : !!el.getClientRects().length);
  const any = Array.from(document.querySelectorAll('*')).some(
    (el) => el.shadowRoot || el.hasAttribute('data-cp-closed'));
  if (!any) { return { open: 0, closed: [] }; }
  // `display: contents` hosts have no box of their own; their parent decides.
  const hostShown = (el) => (window.getComputedStyle(el).display === 'contents'
    ? (el.parentElement ? shown(el.parentElement) : true) : shown(el));
  let open = 0;
  const closed = [];
  const texts = [];
  // Text of one shadow root that is actually laid out: a text node counts when it has
  // client rects and its parent is not visibility:hidden. Slotted (light) text is
  // already in body.innerText; nested roots are collected when `ser` reaches them.
  const laidOut = (t) => {
    const p = t.parentElement;
    if (!p || window.getComputedStyle(p).visibility === 'hidden') { return false; }
    const r = document.createRange();
    r.selectNodeContents(t);
    return r.getClientRects().length > 0;
  };
  const collect = (n) => {
    for (const c of n.childNodes) {
      if (c.nodeType === 3) {
        const v = c.nodeValue.trim();
        if (v && laidOut(c)) { texts.push(v); }
      } else if (c.nodeType === 1) {
        const t = c.tagName.toLowerCase();
        if (!['style', 'script', 'template', 'slot', 'noscript', 'link'].includes(t)) {
          collect(c);
        }
      }
    }
  };
  const ser = (node) => {
    if (node.nodeType === 3) { return esc(node.nodeValue); }
    if (node.nodeType !== 1) { return ''; }
    const tag = node.tagName.toLowerCase();
    if (tag === 'slot') {
      const assigned = node.assignedNodes({ flatten: true });
      return (assigned.length ? assigned : Array.from(node.childNodes)).map(ser).join('');
    }
    let attrs = '';
    for (const a of node.attributes) { attrs += ` ${a.name}="${escA(a.value)}"`; }
    let inner;
    if (node.shadowRoot) {
      open += 1;
      attrs += ' data-cp-shadow="open"';
      inner = Array.from(node.shadowRoot.childNodes).map(ser).join('');
      collect(node.shadowRoot);
    } else if (node.hasAttribute('data-cp-closed')) {
      const i = node.getAttribute('data-cp-closed');
      closed.push({ index: Number(i), visible: hostShown(node) });
      attrs += ' data-cp-shadow="closed"';
      inner = '\\u0000cp-closed:' + i + '\\u0000';
    } else if (tag === 'template' || tag === 'script' || tag === 'style') {
      inner = tag === 'template' ? '' : esc(node.textContent || '');
    } else {
      inner = Array.from(node.childNodes).map(ser).join('');
    }
    return VOID.has(tag) ? `<${tag}${attrs}>` : `<${tag}${attrs}>${inner}</${tag}>`;
  };
  const html = '<!DOCTYPE html>' + ser(document.documentElement);
  const light = document.body ? document.body.innerText : '';
  return { html, light, shadowText: texts.join('\\n'), open, closed };
}
"""

MARK_CLOSED_HOST_JS = "function (i) { this.setAttribute('data-cp-closed', String(i)); }"

FRAME_ELEMENT_JS = """
(el) => {
  const r = el.getBoundingClientRect();
  const cs = window.getComputedStyle(el);
  return {
    src: el.getAttribute('src'),
    srcdoc: el.hasAttribute('srcdoc'),
    title: el.getAttribute('title') || el.getAttribute('name') || '',
    hidden: r.width < 2 || r.height < 2 || cs.display === 'none' || cs.visibility === 'hidden',
  };
}
"""


class ClosedRoot(StrictModel):
    """One closed shadow root read through the DevTools protocol."""

    index: int
    html: str
    text: str


class ComposedCapture(StrictModel):
    """One state's DOM and visible text including shadow content."""

    html: str
    text: str
    open_roots: int = 0
    closed_roots: int = 0
    closed_roots_read: int = 0


class FrameCapture(StrictModel):
    """One child frame's outcome and content."""

    frame_path: str
    kind: str = Field(description="same_origin | cross_origin | srcdoc | blank")
    title: str = ""
    hidden: bool = False
    html: str = ""
    text: str = ""
    error: str | None = None


def _children(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from node.get("children", [])


def _cdp_html(node: dict[str, Any], host_light: list[dict[str, Any]]) -> str:
    """Serialize a CDP node; `host_light` fills `<slot>` elements."""
    ntype = node.get("nodeType")
    if ntype == 3:
        return html.escape(str(node.get("nodeValue", "")), quote=False)
    if ntype == 11:  # shadow root / fragment
        return "".join(_cdp_html(c, host_light) for c in _children(node))
    if ntype != 1:
        return ""
    tag = str(node.get("localName") or node.get("nodeName", "")).lower()
    if tag == "slot":
        name = _attrs(node).get("name")
        light = [c for c in host_light if (_attrs(c).get("slot") or None) == (name or None)]
        return "".join(_cdp_html(c, []) for c in (light or list(_children(node))))
    attrs = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in _attrs(node).items())
    roots = node.get("shadowRoots", [])
    if roots:
        inner = "".join(_cdp_html(r, list(_children(node))) for r in roots)
    elif tag == "template":
        inner = ""
    else:
        inner = "".join(_cdp_html(c, host_light) for c in _children(node))
    return f"<{tag}{attrs}>" if tag in _VOID else f"<{tag}{attrs}>{inner}</{tag}>"


def _cdp_text(node: dict[str, Any], out: list[str], skip: bool = False) -> None:
    if node.get("nodeType") == 3 and not skip:
        value = str(node.get("nodeValue", "")).strip()
        if value:
            out.append(value)
        return
    tag = str(node.get("localName") or "").lower()
    skip = skip or tag in _SKIP_TEXT_TAGS
    for root in node.get("shadowRoots", []):
        _cdp_text(root, out, skip)
    for child in _children(node):
        _cdp_text(child, out, skip)


def _attrs(node: dict[str, Any]) -> dict[str, str]:
    flat = list(node.get("attributes", []))
    return {str(flat[i]): str(flat[i + 1]) for i in range(0, len(flat) - 1, 2)}


def _closed_hosts(node: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """(host, root) for every closed shadow root, outermost first; frames are skipped."""
    for root in node.get("shadowRoots", []):
        if root.get("shadowRootType") == "closed":
            yield node, root
            continue  # nested closed roots are serialized with their outer root
        for child in _children(root):
            yield from _closed_hosts(child)
    for child in _children(node):
        yield from _closed_hosts(child)


async def read_closed_roots(page: Any) -> list[ClosedRoot] | None:
    """Mark closed-root hosts in the page and read their content; None without CDP."""
    try:
        cdp = await page.context.new_cdp_session(page)
    except (PlaywrightError, AttributeError):
        return None
    try:
        doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        roots: list[ClosedRoot] = []
        for i, (host, root) in enumerate(_closed_hosts(doc["root"])):
            resolved = await cdp.send("DOM.resolveNode", {"backendNodeId": host["backendNodeId"]})
            await cdp.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": MARK_CLOSED_HOST_JS,
                    "objectId": resolved["object"]["objectId"],
                    "arguments": [{"value": i}],
                },
            )
            texts: list[str] = []
            _cdp_text(root, texts)
            roots.append(
                ClosedRoot(
                    index=i, html=_cdp_html(root, list(_children(host))), text="\n".join(texts)
                )
            )
        return roots
    except PlaywrightError:
        return None
    finally:
        with contextlib.suppress(PlaywrightError):
            await cdp.detach()


async def compose_page(target: Any, closed: list[ClosedRoot] | None) -> ComposedCapture | None:
    """Composed DOM and text of a page or frame; None when it has no shadow roots."""
    raw = await target.evaluate(COMPOSE_JS)
    marked = list(raw.get("closed") or [])
    if not int(raw.get("open") or 0) and not marked:
        return None
    by_index = {r.index: r for r in closed or []}
    page_html = str(raw["html"])
    extra_text: list[str] = []
    read = 0
    for item in marked:
        root = by_index.get(int(item["index"]))
        mark = _CLOSED_MARK.format(item["index"])
        if root is None:
            page_html = page_html.replace(mark, "")
            continue
        read += 1
        page_html = page_html.replace(mark, root.html)
        if item.get("visible"):
            extra_text.append(root.text)
    text = "\n".join(t for t in (str(raw["light"]), str(raw["shadowText"]), *extra_text) if t)
    return ComposedCapture(
        html=page_html,
        text=text,
        open_roots=int(raw.get("open") or 0),
        closed_roots=len(marked),
        closed_roots_read=read,
    )


async def capture_frames(
    page: Any, annotate: list[str], skip: Collection[str] = ()
) -> list[FrameCapture]:
    """Read every child frame on its own; `annotate` scripts run inside each frame first.

    Frames whose path is in `skip` (already read in an earlier pass) are left out.
    """
    out: list[FrameCapture] = []
    main = page.main_frame
    page_origin = _origin(page.url)
    for n, frame in enumerate(page.frames):
        if frame == main:
            continue
        try:
            element = await frame.frame_element()
            info = dict(await element.evaluate(FRAME_ELEMENT_JS))
        except PlaywrightError as exc:
            out.append(
                FrameCapture(frame_path=frame.url or f"frame:{n}", kind="blank", error=_err(exc))
            )
            continue
        parent_url = frame.parent_frame.url if frame.parent_frame is not None else page.url
        if info.get("srcdoc"):
            path, kind = f"srcdoc:{n}", "srcdoc"
        elif info.get("src"):
            path = urljoin(parent_url, str(info["src"]))
            kind = "same_origin" if _origin(frame.url or path) == page_origin else "cross_origin"
        else:
            path, kind = frame.url or f"frame:{n}", "blank"
        if path in skip:
            continue
        capture = FrameCapture(
            frame_path=path,
            kind=kind,
            title=str(info.get("title") or ""),
            hidden=bool(info["hidden"]),
        )
        if capture.hidden:
            out.append(capture)
            continue
        try:
            await frame.wait_for_load_state("domcontentloaded", timeout=5000)
            for script in annotate:
                await frame.evaluate(script)
            composed = await compose_page(frame, None)
            if composed is not None:
                capture.html, capture.text = composed.html, composed.text
            else:
                capture.html = await frame.content()
                capture.text = str(
                    await frame.evaluate("document.body ? document.body.innerText : ''")
                )
        except PlaywrightError as exc:
            capture.error = _err(exc)
        out.append(capture)
    return out


def _origin(url: str) -> str:
    parts = url.split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 else url


def _err(exc: Exception) -> str:
    return str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
