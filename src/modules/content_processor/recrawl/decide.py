"""Judge whether a URL changed since the previous capture.

The decision is evidence-led and ordered from strongest to weakest:

1. HTTP 304 Not Modified to our conditional request          -> unchanged
2. Identical payload bytes (sha256)                          -> unchanged
3. Identical visible-text fingerprint (markup or nonces churn) -> unchanged
4. Site says it was modified later than the previous capture -> changed
5. Text differs although the site's date did not move        -> changed (date stale)
6. Anything else that differs                                -> changed

Content wins over dates: a page whose text changed is re-extracted even if its
`dateModified` was not updated, and a page whose date moved but whose text is
identical is reused (the date change is noted).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from src.core.schemas import StrictModel
from src.modules.content_processor.contracts import ContentBlock, UrlRecord
from src.modules.content_processor.enums import ChangeStatus, ContentLayer

__all__ = ["ChangeDecision", "content_fingerprint", "decide_change"]

_FINGERPRINT_LAYERS = (ContentLayer.RENDERED_VISIBLE, ContentLayer.COLLAPSED_PUBLISHED)
_VOLATILE_ROLES = frozenset({"nav", "chrome"})


class ChangeDecision(StrictModel):
    """Outcome of comparing a fetch with the previous capture."""

    status: ChangeStatus
    basis: str
    notes: list[str] = []

    @property
    def reuse(self) -> bool:
        """True when the previous extraction can be reused as is."""
        return self.status is ChangeStatus.UNCHANGED


def content_fingerprint(blocks: Sequence[ContentBlock]) -> str | None:
    """Hash of the visible text in reading order, ignoring navigation churn.

    Returns None when there is no visible text to compare.
    """
    texts = [
        b.normalized_text.casefold()
        for b in sorted(blocks, key=lambda b: b.reading_order)
        if b.layer in _FINGERPRINT_LAYERS and b.role not in _VOLATILE_ROLES and b.normalized_text
    ]
    if not texts:
        return None
    return hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()


def decide_change(
    prior: UrlRecord | None,
    *,
    http_status: int | None,
    payload_sha256: str | None,
    fingerprint: str | None,
    modified_at: datetime | None,
) -> ChangeDecision:
    """Compare a new fetch with the previous capture of the same URL."""
    if prior is None:
        return ChangeDecision(status=ChangeStatus.NEW, basis="no_prior_capture")
    if http_status == 304:
        return ChangeDecision(status=ChangeStatus.UNCHANGED, basis="http_304_not_modified")
    notes: list[str] = []
    date_moved = (
        modified_at is not None
        and prior.page_modified_at is not None
        and modified_at > prior.page_modified_at
    )
    if date_moved:
        notes.append("site_modified_date_moved")
    if payload_sha256 is not None and payload_sha256 == prior.payload_sha256:
        return ChangeDecision(status=ChangeStatus.UNCHANGED, basis="payload_identical", notes=notes)
    if fingerprint is not None and fingerprint == prior.content_fingerprint:
        return ChangeDecision(
            status=ChangeStatus.UNCHANGED,
            basis="visible_text_identical",
            notes=[*notes, "bytes_differ"],
        )
    if date_moved:
        return ChangeDecision(status=ChangeStatus.CHANGED, basis="site_modified_date_newer")
    if (
        fingerprint is not None
        and prior.content_fingerprint is not None
        and modified_at is not None
        and modified_at == prior.page_modified_at
    ):
        return ChangeDecision(
            status=ChangeStatus.CHANGED,
            basis="visible_text_changed",
            notes=["site_modified_date_not_updated"],
        )
    if fingerprint is not None and prior.content_fingerprint is not None:
        return ChangeDecision(status=ChangeStatus.CHANGED, basis="visible_text_changed")
    return ChangeDecision(status=ChangeStatus.CHANGED, basis="payload_changed", notes=notes)
