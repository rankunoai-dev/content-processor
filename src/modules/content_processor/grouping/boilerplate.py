"""Template-aware boilerplate reuse (Bit 3).

Within one template group, some blocks appear on most pages: navigation,
footers, the same help text. Their normalized text is identical on every page,
so the work done on it (verification, classification) can be computed once per
group and reused. Each page still keeps its own block records and provenance;
only the computation is shared.

Matching is exact on normalized text. Two disclaimers that differ by one date
are different blocks and are not shared.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar, cast

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "DEFAULT_MIN_CHARS",
    "DEFAULT_MIN_SHARE",
    "BoilerplateReport",
    "ReuseCache",
    "SharedBlock",
    "find_shared_blocks",
    "text_key",
]

DEFAULT_MIN_SHARE = 0.8
DEFAULT_MIN_CHARS = 20  # trivial strings such as "Home" are not worth sharing

T = TypeVar("T")


def text_key(normalized_text: str) -> str:
    """Stable key for one normalized block text."""
    return hashlib.sha256(normalized_text.encode()).hexdigest()[:16]


class SharedBlock(StrictModel):
    """A block text present on enough pages of the group to be reused."""

    text_key: str
    sample: str = Field(description="First 80 characters, for review")
    pages_covered: int = Field(ge=2)
    share: float = Field(ge=0.0, le=1.0, description="Pages covered over group size")
    chars: int = Field(ge=1)


class BoilerplateReport(StrictModel):
    """Reuse plan for one group."""

    group_id: str
    pages: int = Field(ge=1)
    shared_blocks: list[SharedBlock] = Field(default_factory=list)
    reused_by_page: dict[str, list[str]] = Field(
        default_factory=dict, description="page URL -> text keys it reuses"
    )
    total_units: int = Field(ge=0, description="Block occurrences across all pages")
    reused_units: int = Field(ge=0, description="Occurrences whose work is reused, not recomputed")
    total_chars: int = Field(ge=0)
    reused_chars: int = Field(ge=0)

    @property
    def unit_reuse_ratio(self) -> float:
        """Share of block occurrences that reuse a computed result."""
        return self.reused_units / self.total_units if self.total_units else 0.0


def find_shared_blocks(
    group_id: str,
    page_blocks: Mapping[str, Sequence[str]],
    *,
    min_share: float = DEFAULT_MIN_SHARE,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> BoilerplateReport:
    """Find block texts that most pages of one group share.

    `page_blocks` maps each page URL to its normalized block texts. A text is
    shared when at least `min_share` of the group's pages contain it, and at least
    two pages do. Texts shorter than `min_chars` are ignored.
    """
    n = len(page_blocks)
    if n == 0:
        return BoilerplateReport(
            group_id=group_id, pages=1, total_units=0, reused_units=0, total_chars=0, reused_chars=0
        )
    per_page = {
        url: [t for t in texts if len(t) >= min_chars] for url, texts in page_blocks.items()
    }
    presence: Counter[str] = Counter()
    for texts in per_page.values():
        presence.update(set(texts))
    needed = max(2, math.ceil(min_share * n - 1e-9))

    shared_text = {t for t, c in presence.items() if c >= needed}
    shared: list[SharedBlock] = []
    for text in sorted(shared_text, key=lambda t: (-presence[t], t)):
        shared.append(
            SharedBlock(
                text_key=text_key(text),
                sample=text[:80],
                pages_covered=presence[text],
                share=round(presence[text] / n, 4),
                chars=len(text),
            )
        )

    reused_by_page: dict[str, list[str]] = {}
    total_units = total_chars = reused_units = reused_chars = 0
    for url, texts in per_page.items():
        keys: list[str] = []
        for text in texts:
            total_units += 1
            total_chars += len(text)
            if text in shared_text:
                reused_units += 1
                reused_chars += len(text)
                keys.append(text_key(text))
        reused_by_page[url] = keys

    return BoilerplateReport(
        group_id=group_id,
        pages=n,
        shared_blocks=shared,
        reused_by_page=reused_by_page,
        total_units=total_units,
        reused_units=reused_units,
        total_chars=total_chars,
        reused_chars=reused_chars,
    )


class ReuseCache:
    """Computes the result for each distinct block text once per group."""

    def __init__(self) -> None:
        """Start with no computed results."""
        self._results: dict[str, Any] = {}
        self.computed = 0
        self.reused = 0

    def get_or_compute(self, normalized_text: str, compute: Callable[[str], T]) -> T:
        """Return the stored result for this text, computing it on first sight."""
        key = text_key(normalized_text)
        if key in self._results:
            self.reused += 1
            return cast(T, self._results[key])
        result = compute(normalized_text)
        self._results[key] = result
        self.computed += 1
        return result
