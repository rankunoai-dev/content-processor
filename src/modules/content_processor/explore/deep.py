"""Shadow-piercing queries for the explorer's page scripts.

`document.querySelectorAll` and `Element.closest` stop at shadow roots, so a
control inside a web component (a `<content-accordion>` whose button lives in
its open shadow root) was never discovered, and the page wrongly reported
discovery as closed. `deep()` rewrites a page script so every document query
covers the document and all open shadow roots, and `closest` climbs through
shadow hosts. Closed roots stay unreachable from page scripts; their text is
read through CDP (see `compose.py`) and their controls are not clickable.
"""

from __future__ import annotations

import re

__all__ = ["DEEP_PRELUDE", "deep"]

DEEP_PRELUDE = """
  const __cpRootsOf = () => {
    const out = [document];
    const walk = (r) => {
      for (const el of r.querySelectorAll('*')) {
        if (el.shadowRoot) { out.push(el.shadowRoot); walk(el.shadowRoot); }
      }
    };
    walk(document);
    return out;
  };
  const __cpRoots = __cpRootsOf();
  const __cpAll = (sel) => {
    const res = [];
    for (const r of __cpRoots) { res.push(...r.querySelectorAll(sel)); }
    return res;
  };
  const __cpOne = (sel) => {
    for (const r of __cpRoots) { const e = r.querySelector(sel); if (e) { return e; } }
    return null;
  };
  const __cpClosest = (el, sel) => {
    let node = el;
    while (node) {
      const hit = node.closest ? node.closest(sel) : null;
      if (hit) { return hit; }
      const root = node.getRootNode ? node.getRootNode() : null;
      node = root && root.host ? root.host : null;
    }
    return null;
  };
"""

_ARROW_BODY = re.compile(r"=>\s*\{")
# The whole receiver chain: `c.parentElement.closest(` -> `__cpClosest(c.parentElement, `.
_CLOSEST = re.compile(r"(\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\.closest\(")


def deep(script: str) -> str:
    """Make a `(...) => { ... }` page script query across open shadow roots."""
    match = _ARROW_BODY.search(script)
    if match is None:
        raise ValueError("deep() expects an arrow function with a block body")
    body = script[match.end() :]
    body = body.replace("document.querySelectorAll(", "__cpAll(")
    body = body.replace("document.querySelector(", "__cpOne(")
    body = _CLOSEST.sub(lambda m: f"__cpClosest({m.group(1)}, ", body)
    return script[: match.end()] + DEEP_PRELUDE + body
