"""Eligibility gate: decides whether a page may join a template group.

Error screens, bot-manager blocks and non-HTML responses must never be grouped
with real content: a 377-byte "Access Denied" page has an empty skeleton that
would match every other empty page. This gate runs before any fingerprinting.

Script-heavy shells (little markup text, client-side rendering) are not
excluded; they are routed to the `spa_shell` path, where skeletons are not
trusted and grouping falls back to the path family alone.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.grouping.model import PageSignal
from src.modules.content_processor.grouping.skeleton import visible_word_count

__all__ = ["Eligibility", "SHELL_MAX_WORDS", "assess"]

# Static visible words below this, with a script tag, mark a client-rendered shell. A ratio of
# text to markup was tried first and misfired: real article pages (145-163 words in 69 KB of
# markup) scored like shells.
SHELL_MAX_WORDS = 20

_BLOCK_MARKERS = (
    "access denied",
    "you don't have permission to access",
    "challenge-platform",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "captcha",
    "are you a robot",
)


class Eligibility(StrictModel):
    """Outcome of the gate for one page."""

    kind: Literal["template", "spa_shell", "excluded"]
    reason: str | None = Field(default=None, description="Set when excluded")


def assess(page: PageSignal) -> Eligibility:
    """Classify one page before it is fingerprinted."""
    status = page.http_status
    if status is None:
        return Eligibility(kind="excluded", reason="no_response")
    if status >= 400:
        return Eligibility(kind="excluded", reason=f"http_{status}")
    if page.media_type is not None and not page.media_type.lower().startswith("text/html"):
        return Eligibility(kind="excluded", reason=f"not_html:{page.media_type}")
    body = page.body
    if not body.strip():
        return Eligibility(kind="excluded", reason="empty_body")
    head = body[:4096].lower()
    if any(marker in head for marker in _BLOCK_MARKERS):
        return Eligibility(kind="excluded", reason="challenge_or_block_page")
    if "<script" in body.lower() and visible_word_count(body) < SHELL_MAX_WORDS:
        return Eligibility(kind="spa_shell")
    return Eligibility(kind="template")
