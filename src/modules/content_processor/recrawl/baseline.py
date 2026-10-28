"""The previous run's records, indexed for the next visit.

A baseline is simply the `.records.jsonl` a previous run wrote (dry or durable),
or the records assembled from a previous durable run in Postgres, or the tenant's
global history (`history.py`). Lookup is by schedule key first, then by requested
and final URL, so a URL supplied with a tracking parameter last time still matches.

`reuse_since` limits reuse to captures whose content was extracted at or after a
moment (for example the day an extraction fix landed, or N days ago). Older
captures are still compared, so the change status stays truthful, but the page
is extracted again instead of inheriting an old extraction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.enums import EntityStatus, UrlBadge
from src.modules.content_processor.scope.normalize import InvalidInputUrl, normalize_url

__all__ = ["Baseline", "conditional_headers", "extraction_time", "url_key"]

_REUSABLE_BADGES = frozenset(
    {UrlBadge.VERIFIED_WITHIN_SCOPE, UrlBadge.PARTIAL, UrlBadge.NEEDS_REVIEW}
)
_UNUSABLE_STATUSES = frozenset({EntityStatus.FAILED, EntityStatus.BLOCKED})


def url_key(url: str | None) -> str | None:
    """The normalized key a URL is stored under (any spelling of one page matches)."""
    if not url:
        return None
    try:
        return normalize_url(url).schedule_key
    except InvalidInputUrl:
        return None


class Baseline:
    """Previous captures by URL. Only captures worth reusing are indexed."""

    def __init__(
        self, records: Iterable[UrlRecord], *, reuse_since: datetime | None = None
    ) -> None:
        """Index records; a later record for the same key replaces an earlier one."""
        self._by_key: dict[str, UrlRecord] = {}
        self.skipped = 0
        self.reuse_since = reuse_since
        for record in records:
            if not self.reusable(record):
                self.skipped += 1
                continue
            for key in {
                record.schedule_key,
                url_key(record.requested_url),
                url_key(record.final_url),
            }:
                if key:
                    self._by_key[key] = record

    @staticmethod
    def reusable(record: UrlRecord) -> bool:
        """A failed, blocked or never-acquired capture is nothing to compare with."""
        if record.badge not in _REUSABLE_BADGES or record.payload_sha256 is None:
            return False
        return record.resource_status not in _UNUSABLE_STATUSES

    @classmethod
    def from_jsonl(cls, path: Path) -> Baseline:
        """Load a `.records.jsonl` written by a previous run."""
        records = [
            UrlRecord.model_validate(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return cls(records)

    def reuse_allowed(self, prior: UrlRecord) -> bool:
        """Whether a matching capture may be reused as is (it is recent enough)."""
        return self.reuse_since is None or extraction_time(prior) >= self.reuse_since

    def __len__(self) -> int:
        """Number of distinct URLs that can be compared."""
        return len({id(r) for r in self._by_key.values()})

    def lookup(self, *urls: str | None) -> UrlRecord | None:
        """The previous capture for any of these URLs or schedule keys."""
        for url in urls:
            key = url if url in self._by_key else url_key(url)
            if key and key in self._by_key:
                return self._by_key[key]
        return None


def extraction_time(record: UrlRecord) -> datetime:
    """When the record's content was extracted.

    A reused capture keeps the time of the extraction it reused (its blocks carry
    it), so a page reused run after run still ages and is eventually re-extracted.
    """
    if record.reused_from_run_id:
        times = [b.extracted_at for b in record.blocks if b.extracted_at is not None]
        if times:
            return min(times)
    return record.captured_at


def conditional_headers(prior: UrlRecord | None) -> dict[str, str]:
    """`If-None-Match` / `If-Modified-Since` from the previous capture's validators."""
    if prior is None:
        return {}
    headers: dict[str, str] = {}
    if prior.http_etag:
        headers["If-None-Match"] = prior.http_etag
    if prior.http_last_modified:
        headers["If-Modified-Since"] = prior.http_last_modified
    return headers
