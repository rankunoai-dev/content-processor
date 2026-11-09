"""Every captured response, saved the moment it arrives, for the next attempt of a scrape.

A scrape can stop anywhere: a cancel, a crash, the app or the computer restarting. A
batch's records are only written when the batch ends, so without this journal the
pages already downloaded or rendered in the unfinished batch would be requested again.

One journal belongs to one run (`<run folder>/captures/`) and is shared by all its
batches and attempts:

- each successful download or render is written straight after it arrives, as one small
  file swapped into place, so a stop at any instant leaves either the whole capture or
  none of it; the bytes themselves go to the (content-addressed) evidence store first;
- the next attempt finds the capture and sends no request. The page is still extracted
  and checked by the current code, and keeps the time it was really captured.

What is not kept, so it is asked for again: errors, connection failures, cut-off
downloads and "not modified" answers. A saved response that the checks now reject (a
block or challenge page) is forgotten when that happens. Captures older than
`REPLAY_MAX_AGE` are not reused, and neither is one whose stored bytes are missing or do
not match their hash. Saving never stops a scrape: a failed save is logged and the
scrape goes on. robots.txt is never journaled: it is always read fresh.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.logger import get_logger
from src.integrations.browser_client import RenderResult
from src.integrations.evidence_store import EvidenceStore
from src.integrations.http_client import FetchResult
from src.modules.content_processor.explore.model import ExplorationResult

__all__ = [
    "REPLAY_MAX_AGE",
    "CaptureJournal",
    "SavedFetch",
    "SavedRender",
    "saved_capture_count",
    "write_durably",
]

_logger = get_logger("content_processor.scheduler.capture_journal")

REPLAY_MAX_AGE = timedelta(days=7)
"""A capture older than this is requested again: the page may have changed."""
_SCHEMA = 1
_BLOB = "__evidence_sha256__"
_REPLACE_TRIES = 20
_RENDER_MEDIA = {"dom_html": "text/html", "screenshot_jpeg": "image/jpeg"}
_DEFAULT_MEDIA = "application/octet-stream"


class _DamagedCapture(Exception):
    """A capture's stored bytes are missing or do not match their hash."""


@dataclass(frozen=True)
class SavedFetch:
    """A download found in the journal, and the access tier that made it."""

    tier: str
    result: FetchResult


@dataclass(frozen=True)
class SavedRender:
    """A browser render found in the journal, with what exploring the page found."""

    result: RenderResult
    exploration: ExplorationResult | None


def saved_capture_count(root: Path) -> int:
    """How many captures a journal folder holds."""
    return sum(1 for _ in root.rglob("*.json")) if root.is_dir() else 0


class CaptureJournal:
    """One run's saved responses: one atomically written file per capture."""

    def __init__(
        self,
        root: Path,
        evidence: EvidenceStore,
        *,
        max_age: timedelta = REPLAY_MAX_AGE,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind to a folder and the evidence store that holds the response bytes."""
        self.root = root
        self._evidence = evidence
        self._max_age = max_age
        self._clock = clock
        self.stats: Counter[str] = Counter()

    # -- downloads ------------------------------------------------------------

    def save_fetch(self, result: FetchResult, tier: str) -> None:
        """Keep a complete, successful download. Never raises."""
        complete = not (
            result.transport_error
            or result.egress_denied
            or result.spend_refused
            or result.truncated
            or result.limit_hit
        )
        if not complete or result.http_status is None or not 200 <= result.http_status < 300:
            return
        self._save("fetch", result.requested_url, tier, result, {"body": result.sniffed_mime})

    def find_fetch(self, url: str, tiers: Iterable[str]) -> SavedFetch | None:
        """The saved download of `url`, looked for at each of `tiers` in the given order."""
        for tier in tiers:
            payload, saved_at = self._load("fetch", url, tier)
            if payload is None or saved_at is None:
                continue
            try:
                result = FetchResult.model_validate(
                    {**payload["result"], "captured_earlier_at": saved_at}
                )
            except ValueError as exc:
                self._drop("fetch", url, tier, f"unreadable:{exc!s:.120}")
                continue
            self.stats["fetch_replayed"] += 1
            return SavedFetch(tier=tier, result=result)
        return None

    def forget_fetch(self, url: str, tier: str) -> None:
        """Drop a saved download the checks rejected, so the page is requested again."""
        self._path("fetch", url, tier).unlink(missing_ok=True)

    # -- browser renders ------------------------------------------------------

    def save_render(
        self, result: RenderResult, exploration: ExplorationResult | None, tier: str
    ) -> None:
        """Keep a successful render and what exploring the page found. Never raises."""
        if not result.rendered:
            return
        self._save("render", result.requested_url, tier, result, _RENDER_MEDIA, exploration)

    def find_render(self, url: str, tier: str) -> SavedRender | None:
        """The saved render of `url` at `tier`."""
        payload, saved_at = self._load("render", url, tier)
        if payload is None or saved_at is None:
            return None
        try:
            result = RenderResult.model_validate(
                {**payload["result"], "captured_earlier_at": saved_at}
            )
            explored = payload.get("exploration")
            exploration = None if explored is None else ExplorationResult.model_validate(explored)
        except ValueError as exc:
            self._drop("render", url, tier, f"unreadable:{exc!s:.120}")
            return None
        self.stats["render_replayed"] += 1
        return SavedRender(result=result, exploration=exploration)

    # -- files ----------------------------------------------------------------

    def _path(self, kind: str, url: str, tier: str) -> Path:
        name = hashlib.sha256(f"{tier}|{url}".encode()).hexdigest()
        return self.root / kind / name[:2] / f"{name}.json"

    def _save(
        self,
        kind: str,
        url: str,
        tier: str,
        result: FetchResult | RenderResult,
        media: dict[str, str],
        exploration: ExplorationResult | None = None,
    ) -> None:
        try:
            # The bytes first: a capture file on disk always has its bytes stored.
            payload: dict[str, Any] = {
                "result": self._pack(result.model_dump(exclude={"captured_earlier_at"}), media),
                "exploration": (
                    None if exploration is None else self._pack(exploration.model_dump(), media)
                ),
            }
            document = {
                "schema": _SCHEMA,
                "url": url,
                "tier": tier,
                "saved_at": self._clock().isoformat(),
                **payload,
            }
            write_durably(
                self._path(kind, url, tier), json.dumps(document, default=str).encode("utf-8")
            )
            self.stats[f"{kind}_saved"] += 1
        except Exception:  # noqa: BLE001 - losing a save must never stop the scrape
            _logger.exception("capture_save_failed", extra={"url": url, "kind": kind})
            self.stats["save_failed"] += 1

    def _load(
        self, kind: str, url: str, tier: str
    ) -> tuple[dict[str, Any] | None, datetime | None]:
        path = self._path(kind, url, tier)
        if not path.is_file():
            return None, None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if document["schema"] != _SCHEMA or document["url"] != url:
                raise _DamagedCapture("another format or address")
            saved_at = datetime.fromisoformat(document["saved_at"])
            if self._clock() - saved_at > self._max_age:
                self._drop(kind, url, tier, "too_old")
                return None, None
            payload = {
                "result": self._unpack(document["result"]),
                "exploration": self._unpack(document["exploration"]),
            }
        except (OSError, ValueError, KeyError, TypeError, _DamagedCapture) as exc:
            self._drop(kind, url, tier, f"damaged:{exc!s:.120}")
            return None, None
        return payload, saved_at

    def _drop(self, kind: str, url: str, tier: str, why: str) -> None:
        _logger.warning("capture_not_reused", extra={"url": url, "kind": kind, "why": why})
        self.stats["not_reused"] += 1
        try:
            self._path(kind, url, tier).unlink(missing_ok=True)
        except OSError:
            _logger.warning("capture_drop_failed", extra={"url": url, "kind": kind})

    def _pack(self, value: Any, media: dict[str, str], key: str = "") -> Any:
        """A JSON-ready copy of `value`, its bytes stored as evidence and named by hash."""
        if isinstance(value, bytes):
            if not value:
                return {_BLOB: ""}
            ref = self._evidence.put(value, media.get(key, _DEFAULT_MEDIA))
            return {_BLOB: ref.sha256}
        if isinstance(value, dict):
            return {k: self._pack(v, media, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._pack(v, media, key) for v in value]
        return value

    def _unpack(self, value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {_BLOB}:
                return self._blob(str(value[_BLOB]))
            return {k: self._unpack(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._unpack(v) for v in value]
        return value

    def _blob(self, sha256: str) -> bytes:
        if not sha256:
            return b""
        try:
            data = self._evidence.get(sha256)
        except (OSError, KeyError, ValueError) as exc:
            raise _DamagedCapture(f"bytes missing: {sha256}") from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise _DamagedCapture(f"bytes changed: {sha256}")
        return data


def write_durably(target: Path, data: bytes) -> None:
    """Write to a temporary file, flush it to disk and swap it in: all of it or nothing.

    On Windows the swap can be refused for a moment while a scanner has the file open.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(0.1)
    finally:
        Path(tmp).unlink(missing_ok=True)
