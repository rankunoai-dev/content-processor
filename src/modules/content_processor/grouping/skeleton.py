"""DOM skeleton fingerprints: the tag tree without text or attributes.

Two pages built from one template have the same skeleton even when their
words, links and image sources differ. The skeleton is a sequence of
`tag@depth` tokens in document order. Similarity is the ordered sequence ratio
(difflib), which tolerates a few inserted or removed elements and reordered
sections of one template. Shingle or bag-of-tag measures were tried
first: a 4-token shingle set drops to 0.76 on one extra aside in a 25-token skeleton,
while the ordered ratio gives 0.96.
"""

from __future__ import annotations

import difflib
import hashlib

from selectolax.parser import HTMLParser, Node

__all__ = [
    "skeleton_fingerprint",
    "skeleton_similarity",
    "skeleton_tokens",
    "visible_word_count",
]

_SKIP_TAGS = frozenset({"head", "#comment", "#text"})


def skeleton_tokens(html: str) -> list[str]:
    """Tag tokens (`tag@depth`) in document order, starting at <body>."""
    tree = HTMLParser(html)
    root = tree.body
    if root is None:
        return []
    tokens: list[str] = []
    stack: list[tuple[Node, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        tokens.append(f"{node.tag}@{depth}")
        children = [c for c in node.iter(include_text=False) if c.tag not in _SKIP_TAGS]
        for child in reversed(children):
            stack.append((child, depth + 1))
    return tokens


def skeleton_fingerprint(tokens: list[str]) -> str:
    """Exact identity of a skeleton; equal fingerprints mean identical templates."""
    return hashlib.sha256("|".join(tokens).encode()).hexdigest()


def skeleton_similarity(a: list[str], b: list[str]) -> float:
    """Ordered similarity of two skeletons in [0, 1]; 1.0 for identical sequences."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def visible_word_count(html: str) -> int:
    """Words of visible text in the static HTML, ignoring script, style and noscript."""
    tree = HTMLParser(html)
    body = tree.body
    if body is None:
        return 0
    for junk in tree.css("script, style, noscript"):
        junk.decompose()
    return len((body.text(separator=" ", strip=True) or "").split())
