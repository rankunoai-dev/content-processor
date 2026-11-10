"""Live progress of one scrape: its batches, the pages of a batch, and a batch's log.

Everything here is read from the run's folder as the runner leaves it
(`projects/runner.py`): `state.json` and `urls.json`, one `b<N>_a<A>_scope.json`
per attempt of a batch (written when the attempt starts), `b<N>_a<A>.log` (the
attempt's live log, JSON lines), `b<N>_a<A>.records.jsonl` (one record per page,
written when the attempt ends) and `captures/` (every download and render saved
the moment it arrives, see `scheduler/capture_journal.py`). Nothing is written.

The routes are read-only and open like the other GET routes of the project API.
They tolerate files that are missing or half-written: a records line that does
not parse is skipped, a log line that is not finished yet is left for the next
call. Records files are big (the full content of every page), so each one is
summarised once and the summary kept until the file changes.
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.enums import AccessTier
from src.modules.content_processor.projects.models import RUN_ACTIVE, RunState
from src.modules.content_processor.projects.runner import FINISHED_BADGES, RETRIED_ON_RESUME
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.scheduler.capture_journal import capture_path

__all__ = [
    "BATCHES_PER_PAGE",
    "LOG_LINES_PER_CALL",
    "URLS_PER_PAGE",
    "BatchOut",
    "BatchesOut",
    "LogLineOut",
    "LogOut",
    "PageSummary",
    "RecordsCache",
    "UrlRowOut",
    "UrlsOut",
    "batch_progress",
    "batch_urls",
    "read_log",
    "register_run_progress_routes",
    "summarize_record",
]

URLS_PER_PAGE = 100
BATCHES_PER_PAGE = 100
LOG_LINES_PER_CALL = 200
LOG_BYTES_PER_CALL = 1_000_000
LOG_LINE_MAX_CHARS = 2_000
CAPTURES_DIR = "captures"
TIERS = tuple(t.value for t in AccessTier)
"""A capture is filed under the access tier that made it; any tier counts as captured."""

BatchState = Literal["waiting", "running", "done", "retrying"]
Stage = Literal["queued", "downloaded", "rendered", "done"]

_ATTEMPT_FILE = re.compile(r"^b(\d+)_a(\d+)_scope\.json$")
# A line of the pretty-printed report the batch command prints at its end, or of
# any other JSON document: starts like a JSON value, not like prose or a traceback.
_JSON_FRAGMENT = re.compile(r'^\s*(["{}\[\],]|-?\d|true\b|false\b|null\b)')


# -- shapes -----------------------------------------------------------------------


class InProgressOut(StrictModel):
    """How far the attempt that is running now has come, from the saved captures."""

    pages: int = Field(description="pages this attempt works on (the batch's unfinished ones)")
    fetched: int = Field(description="of those, pages whose download is already saved")
    rendered: int = Field(description="of those, pages whose browser render is already saved")


class BatchOut(StrictModel):
    """One batch of a scrape."""

    number: int
    state: BatchState
    attempts: int = Field(description="attempts started so far")
    attempt: int | None = Field(description="the latest attempt's number (None before the first)")
    pages: int
    pages_finished: int = Field(description="pages with a final result")
    by_badge: dict[str, int]
    started_at: datetime | None
    finished_at: datetime | None
    in_progress: InProgressOut | None = Field(
        description="only for the attempt running now", default=None
    )


class BatchesOut(StrictModel):
    """A page of the run's batches, in order."""

    run_id: str
    status: str
    batch_size: int
    total: int = Field(description="batches in the run")
    offset: int
    current_batch: int | None
    items: list[BatchOut]


class UrlRowOut(StrictModel):
    """One page of a batch and how far it has come."""

    url: str
    stage: Stage
    badge: str | None
    reason: str = Field(description="the first clause of the record's reason, '' before the end")
    completeness: int | None = Field(
        description="percent of main-content blocks whose text was verified; None without a "
        "finished record or without main content"
    )
    gaps: int
    attempt: int | None = Field(description="the attempt that produced the shown result")


class UrlsOut(StrictModel):
    """A page of a batch's URLs."""

    batch: int
    total: int
    offset: int
    items: list[UrlRowOut]


class LogLineOut(StrictModel):
    """One line of a batch's log, flattened for display."""

    time: str = Field(description="ISO time, '' for a line without one")
    level: str = Field(description="INFO, WARNING, ERROR ...; TEXT for a plain-text line")
    message: str
    extra: dict[str, Any] = Field(default_factory=dict)


class LogOut(StrictModel):
    """Up to `LOG_LINES_PER_CALL` lines after a byte offset, and where to continue."""

    batch: int | None
    attempt: int | None
    exists: bool
    offset: int = Field(description="pass back as `after` for the next lines")
    size: int = Field(description="the log's size now, in bytes")
    lines: list[LogLineOut]


# -- records, summarised once per file ------------------------------------------


class PageSummary(StrictModel):
    """What the progress view keeps of one page record (a record is the whole page)."""

    badge: str | None
    reason: str
    completeness: int | None
    gaps: int
    final: bool = Field(description="True when the badge is a final result")


def _first_clause(reason: object) -> str:
    text = str(reason or "")
    return text.split(";", 1)[0].strip()[:200]


def summarize_record(record: dict[str, Any]) -> PageSummary:
    """Reduce a page record to its badge, reason, content completeness and gap count.

    Completeness is the share of main-content blocks (`role == "main"`) whose status is
    VERIFIED_EXTRACTED, as a whole percentage. A page without main-content blocks has
    no completeness (None), whatever its badge.
    """
    badge = record.get("badge")
    badge_text = str(badge) if badge else None
    blocks = record.get("blocks")
    main = 0
    verified = 0
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) or block.get("role") != "main":
                continue
            main += 1
            if block.get("status") == "VERIFIED_EXTRACTED":
                verified += 1
    gaps = record.get("gaps")
    return PageSummary(
        badge=badge_text,
        reason=_first_clause(record.get("resource_status_reason")),
        completeness=round(100 * verified / main) if main else None,
        gaps=len(gaps) if isinstance(gaps, list) else 0,
        final=badge_text in FINISHED_BADGES,
    )


class RecordsCache:
    """Per-URL summaries of each records file, re-read only when the file changes."""

    def __init__(self) -> None:
        """Start empty; summaries are kept by file path with the file's size and time."""
        self._lock = threading.Lock()
        self._cache: dict[Path, tuple[tuple[int, int], dict[str, PageSummary]]] = {}

    def summaries(self, path: Path) -> dict[str, PageSummary]:
        """Requested URL to summary; an empty dict for a missing file."""
        try:
            stat = path.stat()
        except OSError:
            return {}
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        summaries = _read_records(path)
        with self._lock:
            self._cache[path] = (stamp, summaries)
        return summaries


def _read_records(path: Path) -> dict[str, PageSummary]:
    out: dict[str, PageSummary] = {}
    try:
        with path.open("rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue  # half-written or damaged: the page counts as not done
                if not isinstance(record, dict) or "requested_url" not in record:
                    continue
                out.setdefault(str(record["requested_url"]), summarize_record(record))
    except OSError:
        return {}
    return out


def _rank(summary: PageSummary | None) -> int:
    """Which of two results for a page to show: captured, then ended badly, then nothing."""
    if summary is None or not summary.final:
        return 0
    return 1 if summary.badge in RETRIED_ON_RESUME else 2


# -- the run folder -----------------------------------------------------------------


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def _attempts_of(folder: Path, number: int) -> list[int]:
    """Attempt numbers of a batch that have started, in order."""
    found: list[int] = []
    for path in folder.glob(f"b{number}_a*_scope.json"):
        match = _ATTEMPT_FILE.match(path.name)
        if match and int(match.group(1)) == number:
            found.append(int(match.group(2)))
    return sorted(found)


def _all_urls(folder: Path) -> list[str]:
    try:
        urls = json.loads((folder / "urls.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(u) for u in urls] if isinstance(urls, list) else []


def _batch_urls(folder: Path, state: RunState, number: int) -> list[str]:
    size = max(state.batch_size, 1)
    return _all_urls(folder)[(number - 1) * size : number * size]


def _scope_urls(folder: Path, number: int, attempt: int) -> list[str]:
    try:
        scope = json.loads((folder / f"b{number}_a{attempt}_scope.json").read_text("utf-8"))
        urls = scope.get("input_urls", []) if isinstance(scope, dict) else []
    except (OSError, ValueError):
        return []
    return [str(u) for u in urls] if isinstance(urls, list) else []


def _captured(folder: Path, kind: str, url: str) -> bool:
    root = folder / CAPTURES_DIR
    return any(capture_path(root, kind, url, tier).is_file() for tier in TIERS)


def _best_results(
    cache: RecordsCache, folder: Path, number: int, attempts: list[int]
) -> dict[str, tuple[int, PageSummary]]:
    """Best result per URL across the batch's attempts, with the attempt it came from."""
    best: dict[str, tuple[int, PageSummary]] = {}
    for attempt in attempts:
        for url, summary in cache.summaries(folder / f"b{number}_a{attempt}.records.jsonl").items():
            current = best.get(url)
            if current is None or _rank(summary) > _rank(current[1]):
                best[url] = (attempt, summary)
    return best


def _running_attempt(
    state: RunState, folder: Path, number: int, attempts: list[int]
) -> tuple[int, list[str]] | None:
    """The attempt of batch `number` running now (started, no records yet) and its pages.

    A page in that attempt's scope is being done (or redone, after a person continued
    the scrape): it does not count as finished until the attempt ends.
    """
    if state.status != "running" or state.current_batch != number or not attempts:
        return None
    latest = attempts[-1]
    if (folder / f"b{number}_a{latest}.records.jsonl").exists():
        return None
    return latest, _scope_urls(folder, number, latest)


def batch_progress(
    cache: RecordsCache, folder: Path, state: RunState, number: int, urls: list[str]
) -> BatchOut:
    """The state of one batch from the files in the run folder."""
    attempts = _attempts_of(folder, number)
    best = _best_results(cache, folder, number, attempts)
    running = _running_attempt(state, folder, number, attempts)
    redoing = set(running[1]) if running else set()
    finished = {
        u: r for u, r in ((u, best.get(u)) for u in urls) if r and r[1].final and u not in redoing
    }
    by_badge: Counter[str] = Counter(str(r[1].badge) for r in finished.values())

    if number <= state.batches_done or (urls and len(finished) == len(urls)):
        batch_state: BatchState = "done"
    elif running is not None:
        batch_state = "retrying" if len(attempts) > 1 else "running"
    else:
        batch_state = "waiting"

    in_progress = None
    if running is not None:
        scope = running[1]
        in_progress = InProgressOut(
            pages=len(scope),
            fetched=sum(1 for u in scope if _captured(folder, "fetch", u)),
            rendered=sum(1 for u in scope if _captured(folder, "render", u)),
        )

    started = _mtime(folder / f"b{number}_a{attempts[0]}_scope.json") if attempts else None
    finished_at = None
    if batch_state == "done" and attempts:
        finished_at = _mtime(folder / f"b{number}_a{attempts[-1]}.records.jsonl") or _mtime(
            folder / f"b{number}_a{attempts[-1]}.json"
        )
    return BatchOut(
        number=number,
        state=batch_state,
        attempts=len(attempts),
        attempt=attempts[-1] if attempts else None,
        pages=len(urls),
        pages_finished=len(finished),
        by_badge=dict(by_badge),
        started_at=started,
        finished_at=finished_at,
        in_progress=in_progress,
    )


def batch_urls(
    cache: RecordsCache, folder: Path, state: RunState, number: int, offset: int, limit: int
) -> UrlsOut:
    """One row per URL of a batch (a page of them): stage reached and result so far."""
    urls = _batch_urls(folder, state, number)
    attempts = _attempts_of(folder, number)
    best = _best_results(cache, folder, number, attempts)
    running = _running_attempt(state, folder, number, attempts)
    redoing = set(running[1]) if running else set()
    rows: list[UrlRowOut] = []
    for url in urls[offset : offset + limit]:
        got = best.get(url)
        if got is not None and got[1].final and url not in redoing:
            attempt, summary = got
            rows.append(
                UrlRowOut(
                    url=url,
                    stage="done",
                    badge=summary.badge,
                    reason=summary.reason,
                    completeness=summary.completeness,
                    gaps=summary.gaps,
                    attempt=attempt,
                )
            )
            continue
        stage: Stage = "queued"
        if _captured(folder, "render", url):
            stage = "rendered"
        elif _captured(folder, "fetch", url):
            stage = "downloaded"
        rows.append(
            UrlRowOut(
                url=url,
                stage=stage,
                badge=got[1].badge if got else None,
                reason=got[1].reason if got else "",
                completeness=None,
                gaps=got[1].gaps if got else 0,
                attempt=got[0] if got else None,
            )
        )
    return UrlsOut(batch=number, total=len(urls), offset=offset, items=rows)


# -- the log --------------------------------------------------------------------------


def _parse_log_line(raw: bytes) -> LogLineOut | None:
    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
    if not text.strip():
        return None
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict) and "message" in document:
        extra = {
            k: v
            for k, v in document.items()
            if k not in ("ts", "level", "logger", "message", "trace_id")
        }
        return LogLineOut(
            time=str(document.get("ts") or ""),
            level=str(document.get("level") or "INFO"),
            message=str(document.get("message"))[:LOG_LINE_MAX_CHARS],
            extra=extra,
        )
    if _JSON_FRAGMENT.match(text):
        return None  # part of a printed JSON document, not a log line
    return LogLineOut(time="", level="TEXT", message=text[:LOG_LINE_MAX_CHARS])


def read_log(path: Path, after: int, *, max_lines: int = LOG_LINES_PER_CALL) -> LogOut:
    """The next log lines after byte `after`, without reading the rest of the file.

    Only complete lines are returned; an unfinished last line (still being written)
    waits for the next call. An offset past the end (the file was replaced) starts over.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return LogOut(batch=None, attempt=None, exists=False, offset=0, size=0, lines=[])
    offset = after if 0 <= after <= size else 0
    lines: list[LogLineOut] = []
    consumed = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        while len(lines) < max_lines and consumed < LOG_BYTES_PER_CALL:
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            consumed += len(raw)
            parsed = _parse_log_line(raw)
            if parsed is not None:
                lines.append(parsed)
    return LogOut(
        batch=None, attempt=None, exists=True, offset=offset + consumed, size=size, lines=lines
    )


def _latest_log(folder: Path) -> tuple[int, int] | None:
    """(batch, attempt) of the newest log in the folder by number, if any."""
    found: list[tuple[int, int]] = []
    for path in folder.glob("b*_a*.log"):
        match = re.match(r"^b(\d+)_a(\d+)\.log$", path.name)
        if match:
            found.append((int(match.group(1)), int(match.group(2))))
    return max(found) if found else None


# -- routes -----------------------------------------------------------------------


def register_run_progress_routes(app: FastAPI, *, store: ProjectStore) -> None:
    """Add the read-only progress routes of a run to the app."""
    cache = RecordsCache()

    def run_and_folder(project_id: str, site_id: str, run_id: str) -> tuple[RunState, Path]:
        try:
            state = store.run(project_id, site_id, run_id)
            return state, store.run_dir(project_id, site_id, run_id)
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="This website has no such run.") from None

    @app.get("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}/batches")
    def batches(
        project_id: str,
        site_id: str,
        run_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(BATCHES_PER_PAGE, ge=1, le=500),
    ) -> BatchesOut:
        state, folder = run_and_folder(project_id, site_id, run_id)
        urls = _all_urls(folder)
        size = max(state.batch_size, 1)
        total = state.batches_total or -(-len(urls) // size)
        numbers = range(offset + 1, min(offset + limit, total) + 1)
        items = [
            batch_progress(cache, folder, state, n, urls[(n - 1) * size : n * size])
            for n in numbers
        ]
        return BatchesOut(
            run_id=state.run_id,
            status=state.status,
            batch_size=size,
            total=total,
            offset=offset,
            current_batch=state.current_batch if state.status in RUN_ACTIVE else None,
            items=items,
        )

    @app.get("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}/batches/{number}/urls")
    def urls_of_batch(
        project_id: str,
        site_id: str,
        run_id: str,
        number: int,
        offset: int = Query(0, ge=0),
        limit: int = Query(URLS_PER_PAGE, ge=1, le=URLS_PER_PAGE),
    ) -> UrlsOut:
        state, folder = run_and_folder(project_id, site_id, run_id)
        if number < 1 or number > max(state.batches_total, 1):
            raise HTTPException(status_code=404, detail="This run has no such batch.")
        return batch_urls(cache, folder, state, number, offset, limit)

    @app.get("/api/projects/{project_id}/sites/{site_id}/runs/{run_id}/log")
    def log(
        project_id: str,
        site_id: str,
        run_id: str,
        batch: int | None = Query(None, ge=1),
        attempt: int | None = Query(None, ge=1),
        after: int = Query(0, ge=0),
    ) -> LogOut:
        state, folder = run_and_folder(project_id, site_id, run_id)
        if batch is None or attempt is None:
            if state.status == "running" and state.current_batch and state.current_attempt:
                batch, attempt = state.current_batch, state.current_attempt
            else:
                latest = _latest_log(folder)
                if latest is None:
                    return LogOut(
                        batch=None, attempt=None, exists=False, offset=0, size=0, lines=[]
                    )
                batch, attempt = latest
        out = read_log(folder / f"b{batch}_a{attempt}.log", after)
        return out.model_copy(update={"batch": batch, "attempt": attempt})
