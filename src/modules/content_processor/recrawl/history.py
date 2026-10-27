"""URL history: the latest capture of every URL from every run, the automatic baseline.

A run given any list of URLs looks each one up here. A URL captured before is
fetched conditionally and, when unchanged, its previous extraction is reused (see
`decide`); only new or changed pages are extracted again. After the run, its
records update the history, so the next run starts from the newest captures.

Layout under `<root>/<tenant>/`:

- `index.jsonl`: one `HistoryEntry` per URL (rewritten atomically after each run);
- `records/<aa>/<sha256 of key>.json.gz`: that URL's latest full `UrlRecord`.

Only captures worth comparing with are kept (the same rule as `Baseline`): a
failed or blocked capture never replaces a good one.
"""

from __future__ import annotations

import gzip
import hashlib
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from src.core.schemas import StrictModel
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.recrawl.baseline import Baseline, url_key

__all__ = ["HistoryEntry", "UrlHistory", "describe"]


class HistoryEntry(StrictModel):
    """What the history knows about one URL without opening its record."""

    key: str
    url: str
    last_scraped_at: datetime
    content_captured_at: datetime | None = None
    run_id: str
    run_name: str | None = None
    badge: str
    page_modified_at: datetime | None = None
    page_modified_source: str | None = None
    content_fingerprint: str | None = None


def _content_captured_at(record: UrlRecord) -> datetime | None:
    """When the text was really extracted (a reused page keeps its first date)."""
    dates = [b.extracted_at for b in record.blocks if b.extracted_at is not None]
    return min(dates) if dates else None


class UrlHistory:
    """File-backed history of the latest capture per URL for one tenant."""

    def __init__(self, root: Path, tenant_id: str) -> None:
        """Bind to `<root>/<tenant_id>/`; nothing is created until `remember`."""
        self._base = Path(root) / tenant_id
        self._index_path = self._base / "index.jsonl"
        self._entries: dict[str, HistoryEntry] | None = None

    def _record_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._base / "records" / digest[:2] / f"{digest}.json.gz"

    def entries(self) -> dict[str, HistoryEntry]:
        """Every known URL by key (a damaged index line is skipped)."""
        if self._entries is None:
            found: dict[str, HistoryEntry] = {}
            if self._index_path.exists():
                for line in self._index_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = HistoryEntry.model_validate_json(line)
                    except ValueError:
                        continue
                    found[entry.key] = entry
            self._entries = found
        return self._entries

    def __len__(self) -> int:
        """How many URLs the history knows."""
        return len(self.entries())

    def lookup(self, url: str) -> HistoryEntry | None:
        """The entry for a URL in any spelling that normalizes to the same key."""
        key = url_key(url)
        return self.entries().get(key) if key else None

    def baseline_for(self, urls: Iterable[str]) -> Baseline:
        """A `Baseline` of the stored records for these URLs (unknown ones are absent)."""
        records: list[UrlRecord] = []
        for url in urls:
            entry = self.lookup(url)
            if entry is None:
                continue
            path = self._record_path(entry.key)
            if not path.exists():
                continue
            try:
                records.append(UrlRecord.model_validate_json(gzip.decompress(path.read_bytes())))
            except (OSError, ValueError):
                continue
        return Baseline(records)

    def remember(self, records: Iterable[UrlRecord], *, run_name: str | None = None) -> int:
        """Store each comparable record that is newer than what is known; return how many."""
        entries = dict(self.entries())
        stored = 0
        for record in records:
            if not Baseline.reusable(record):
                continue
            key = url_key(record.requested_url) or record.schedule_key
            if not key:
                continue
            known = entries.get(key)
            if known is not None and known.last_scraped_at > record.captured_at:
                continue
            path = self._record_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(path, gzip.compress(record.model_dump_json().encode("utf-8")))
            entries[key] = HistoryEntry(
                key=key,
                url=record.requested_url,
                last_scraped_at=record.captured_at,
                content_captured_at=_content_captured_at(record),
                run_id=record.run_id,
                run_name=run_name,
                badge=record.badge.value,
                page_modified_at=record.page_modified_at,
                page_modified_source=record.page_modified_source,
                content_fingerprint=record.content_fingerprint,
            )
            stored += 1
        if stored:
            self._base.mkdir(parents=True, exist_ok=True)
            lines = (e.model_dump_json() for e in sorted(entries.values(), key=lambda e: e.key))
            _write_atomic(self._index_path, ("\n".join(lines) + "\n").encode("utf-8"))
            self._entries = entries
        return stored


def _write_atomic(target: Path, data: bytes) -> None:
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def describe(entry: HistoryEntry | None) -> dict[str, str]:
    """Plain columns for the pre-scrape check (`cli history`)."""
    if entry is None:
        return {"Scraped before": "No", "Last scraped": "", "Site last updated": "", "Run": ""}
    updated = entry.page_modified_at.date().isoformat() if entry.page_modified_at else ""
    return {
        "Scraped before": "Yes",
        "Last scraped": entry.last_scraped_at.date().isoformat(),
        "Site last updated": updated,
        "Run": entry.run_name or entry.run_id,
    }
