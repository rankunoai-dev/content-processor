"""What the run has learned about controls that repeat across a site's pages.

A site's header menu is on every page, so exploring it again on each page repeats the
same clicks (and the same clicks that fail) thousands of times. The memory lets the
explorer leave such a control alone once it has been seen to be the same thing.

A control counts as site-wide only when all of this holds:

- the same kind, label and surrounding text (or panel text) were found on at least
  `SITE_WIDE_AFTER_PAGES` different pages, so a "Details" accordion whose answer differs
  from page to page is a different control on each page;
- it behaved the same way on each of them: a click revealed the same new text, or
  failed, or it could not be clicked at all. One that ever behaved differently (a panel
  filled in per page after the click) is marked as varying and is always explored.

The file lives in the run's journal folder, so the batches of a run share it and a
continued run keeps it. Losing it only means the first pages are explored in full again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from src.core.logger import get_logger
from src.modules.content_processor.scheduler.capture_journal import write_durably

__all__ = ["SITE_CONTROLS_FILE", "SITE_WIDE_AFTER_PAGES", "SiteControlMemory"]

_logger = get_logger("content_processor.scheduler.site_controls")

SITE_CONTROLS_FILE = "site-controls.state"
SITE_WIDE_AFTER_PAGES = 2
"""Different pages a control must be seen on before later pages leave it alone."""
_MAX_ENTRIES = 50_000
_VARIES = "\u0000varies"


class SiteControlMemory:
    """Per-run memory of controls, by fingerprint: what they do and on which pages."""

    def __init__(self, path: Path | None = None, *, max_entries: int = _MAX_ENTRIES) -> None:
        """Load what earlier batches learned; `path=None` keeps it in memory only."""
        self._path = path
        self._max_entries = max_entries
        # fingerprint -> [outcome, [page ids, up to SITE_WIDE_AFTER_PAGES]]
        self._entries: dict[str, list[object]] = self._load()

    def _load(self) -> dict[str, list[object]]:
        if self._path is None or not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _logger.warning("site_controls_unreadable", extra={"path": str(self._path)})
            return {}
        return data if isinstance(data, dict) else {}

    def is_site_wide(self, fingerprint: str) -> bool:
        """True when this control repeated, unchanged, on enough different pages."""
        entry = self._entries.get(fingerprint)
        if entry is None or entry[0] == _VARIES:
            return False
        pages = entry[1]
        return isinstance(pages, list) and len(pages) >= SITE_WIDE_AFTER_PAGES

    def record(self, page_url: str, outcomes: Mapping[str, str]) -> None:
        """Remember what each control did on one page. Never raises."""
        if not outcomes:
            return
        page = hashlib.sha256(page_url.encode("utf-8")).hexdigest()[:12]
        for fingerprint, outcome in outcomes.items():
            entry = self._entries.setdefault(fingerprint, [outcome, []])
            if entry[0] != outcome:
                entry[0] = _VARIES
            pages = entry[1]
            if isinstance(pages, list) and page not in pages and len(pages) < SITE_WIDE_AFTER_PAGES:
                pages.append(page)
        self._trim()
        self._save()

    def _trim(self) -> None:
        """Keep the file small: controls seen once (per-page ones) go first, oldest first."""
        excess = len(self._entries) - self._max_entries
        if excess <= 0:
            return
        once = [k for k, v in self._entries.items() if isinstance(v[1], list) and len(v[1]) <= 1]
        for key in once[:excess]:
            del self._entries[key]

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            write_durably(self._path, json.dumps(self._entries).encode("utf-8"))
        except Exception:  # noqa: BLE001 - losing this only costs some repeated clicks
            _logger.exception("site_controls_save_failed", extra={"path": str(self._path)})
