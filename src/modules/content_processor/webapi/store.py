"""Runs in an exports folder, parsed once and cached by file size and mtime.

A run is `<name>.records.jsonl` (one `UrlRecord` per line), with an optional
`<name>.json` manifest from the dry run and an optional `<name>_reports/`
folder written by the `report` command. A growing records file (a run still
in progress) is re-read when its size or mtime changes; a line that is not a
valid record (for example a half-written last line) is skipped and counted.

Memory: a parsed run holds every page's records plus a search haystack, so
only the `max_entries` most recently used runs stay in memory (least recently
used first out). The run list needs only headline numbers; those are computed
by streaming the file once, record by record, and kept in a separate small
cache per run, so listing fifty runs never loads fifty runs.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from src.modules.content_processor.contracts import ContentBlock, UrlRecord
from src.modules.content_processor.enums import (
    BADGE_PRECEDENCE,
    ContentLayer,
    EntityStatus,
    GapType,
    UiRegion,
    UrlBadge,
)
from src.modules.content_processor.export.content import drop_duplicates, whole_content
from src.modules.content_processor.webapi.models import (
    Block,
    Count,
    Facets,
    Finding,
    Followup,
    GapOut,
    IssueStat,
    Overview,
    PageDetail,
    PageRow,
    PagesResponse,
    RunFacts,
    RunSummary,
)

__all__ = [
    "DEFAULT_CACHE_ENTRIES",
    "RECORDS_SUFFIX",
    "UNCONFIRMED_TEXT",
    "PageEntry",
    "PageQuery",
    "RunData",
    "RunNotFoundError",
    "RunStore",
    "page_key_for",
]

RECORDS_SUFFIX = ".records.jsonl"
DEFAULT_CACHE_ENTRIES = 4
"""Fully parsed runs kept in memory when no cap is given (`CP_RUN_CACHE_ENTRIES`)."""
UNCONFIRMED_TEXT = "UNCONFIRMED_TEXT"
"""Derived issue: the page has text that could not be matched on the live page, no gap says so."""

_RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_EXPLAINS_UNCONFIRMED = frozenset({GapType.LOW_CONFIDENCE, GapType.FIDELITY_DISAGREEMENT})
_TITLE_REGIONS = frozenset({UiRegion.MAIN_CONTENT, UiRegion.HERO})
_MIME_FORMATS = (
    ("text/html", "html"),
    ("application/xhtml", "html"),
    ("application/pdf", "pdf"),
    ("image/", "image"),
    ("audio/", "audio"),
    ("video/", "video"),
    ("application/json", "json"),
    ("text/plain", "text"),
    ("application/xml", "xml"),
    ("text/xml", "xml"),
    ("application/msword", "office_doc"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml", "office_doc"),
    ("application/vnd.ms-excel", "spreadsheet"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml", "spreadsheet"),
    ("text/csv", "spreadsheet"),
    ("application/vnd.ms-powerpoint", "presentation"),
    ("application/vnd.openxmlformats-officedocument.presentationml", "presentation"),
)
_EXT_FORMATS = {
    ".pdf": "pdf",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".gif": "image",
    ".webp": "image",
    ".svg": "image",
    ".docx": "office_doc",
    ".doc": "office_doc",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".csv": "spreadsheet",
    ".pptx": "presentation",
    ".ppt": "presentation",
}

SortField = Literal["status", "title", "url", "words", "issues", "modified", "captured", "type"]


class RunNotFoundError(LookupError):
    """No run of that name in the exports folder."""


def page_key_for(record: UrlRecord) -> str:
    """Stable, URL-safe key for a page: 16 hex chars of sha256(schedule key or URL)."""
    basis = record.schedule_key or record.requested_url
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _reports_dir(records_path: Path, name: str) -> Path:
    """Where the `report` command writes the run's files."""
    return records_path.parent / f"{name}_reports"


def _slug_title(url: str) -> str:
    path = unquote(urlsplit(url).path).rstrip("/")
    last = PurePosixPath(path).name
    if not last:
        return _host(url) or url
    stem = last.rsplit(".", 1)[0] if "." in last and len(last.rsplit(".", 1)[1]) <= 5 else last
    words = re.sub(r"[-_]+", " ", stem).strip()
    if not words:
        return last
    return words[0].upper() + words[1:]


def _title(record: UrlRecord) -> str:
    """A readable name: the first main-content heading, else the URL's last segment."""
    for block in sorted(record.blocks, key=lambda b: b.reading_order):
        if not block.heading_path or block.layer is not ContentLayer.RENDERED_VISIBLE:
            continue
        if block.role != "main" and block.ui_region not in _TITLE_REGIONS:
            continue
        heading = " ".join(block.heading_path[0].split())
        if 3 <= len(heading) <= 200:
            return heading
    return _slug_title(record.final_url or record.requested_url)


def _content_type(record: UrlRecord) -> str | None:
    formats = Counter(b.source_format.value for b in record.blocks if b.source_format)
    if formats:
        return formats.most_common(1)[0][0]
    mime = (record.sniffed_mime or "").lower()
    for prefix, kind in _MIME_FORMATS:
        if mime.startswith(prefix):
            return kind
    suffix = PurePosixPath(urlsplit(record.requested_url).path).suffix.lower()
    return _EXT_FORMATS.get(suffix)


def _findings(record: UrlRecord) -> list[Finding]:
    """Issues the badge implies but no gap records (NEEDS_REVIEW with unconfirmed text)."""
    if record.badge not in (UrlBadge.NEEDS_REVIEW, UrlBadge.PARTIAL):
        return []
    if any(g.gap_type in _EXPLAINS_UNCONFIRMED for g in record.gaps):
        return []
    inv = record.inventory
    unconfirmed = inv.regions_needs_review
    if unconfirmed == 0 and record.status_vector.fidelity is not EntityStatus.EXTRACTED_UNVERIFIED:
        return []
    if unconfirmed == 0:
        unconfirmed = sum(1 for b in record.blocks if b.status is EntityStatus.EXTRACTED_UNVERIFIED)
    return [
        Finding(
            key=UNCONFIRMED_TEXT,
            unconfirmed=unconfirmed,
            total=max(inv.regions_discovered, len(record.blocks)),
        )
    ]


def _severity(badge: str) -> int:
    try:
        return BADGE_PRECEDENCE.index(UrlBadge(badge))
    except ValueError:
        return len(BADGE_PRECEDENCE)


def _block(b: ContentBlock) -> Block:
    return Block(
        id=b.block_id,
        text=b.normalized_text,
        heading_path=list(b.heading_path),
        order=b.reading_order,
        layer=b.layer.value,
        role=b.role,
        region=b.ui_region.value if b.ui_region else None,
        format=b.source_format.value if b.source_format else None,
        verified=b.status is EntityStatus.VERIFIED_EXTRACTED,
        status=b.status.value,
        confidence=b.confidence,
        page_label=b.page_label,
        frame=b.provenance.frame_path,
    )


@dataclass
class PageEntry:
    """One record plus everything the table needs, computed once at load."""

    key: str
    record: UrlRecord
    row: PageRow
    findings: list[Finding]
    haystack: str
    severity: int


@dataclass
class RunData:
    """A parsed run and its derived indexes."""

    name: str
    path: Path
    signature: tuple[int, int]
    entries: list[PageEntry]
    by_key: dict[str, PageEntry]
    by_url: dict[str, PageEntry]
    unreadable_lines: int
    file_modified_at: datetime
    cache: dict[str, Any] = field(default_factory=dict)

    @property
    def records(self) -> list[UrlRecord]:
        """The run's records in file order."""
        return [e.record for e in self.entries]

    @property
    def reports_dir(self) -> Path:
        """Where the `report` command writes this run's files."""
        return _reports_dir(self.path, self.name)

    def key_for_url(self, url: str) -> str | None:
        """Page key of a URL of this run (requested, final or alias), if any."""
        entry = self.by_url.get(url)
        return entry.key if entry else None

    def title_for_url(self, url: str) -> str:
        """Page title of a URL of this run, or a title made from the URL."""
        entry = self.by_url.get(url)
        return entry.row.title if entry else _slug_title(url)


@dataclass
class PageQuery:
    """Filters, sort and paging for the pages table."""

    status: list[str] = field(default_factory=list)
    host: list[str] = field(default_factory=list)
    content_type: list[str] = field(default_factory=list)
    issue: list[str] = field(default_factory=list)
    has_issues: bool | None = None
    q: str = ""
    sort: SortField = "status"
    order: Literal["asc", "desc"] = "asc"
    page: int = 1
    size: int = 50


def _iter_records(path: Path) -> Iterator[UrlRecord | None]:
    """The file's records one at a time; None for a line that is not a record."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                yield UrlRecord.model_validate_json(line)
            except (ValidationError, ValueError):
                yield None


def _parse(path: Path) -> tuple[list[UrlRecord], int]:
    records: list[UrlRecord] = []
    bad = 0
    for record in _iter_records(path):
        if record is None:
            bad += 1
        else:
            records.append(record)
    return records, bad


@dataclass
class _Tally:
    """Headline numbers of a run, gathered one record at a time (nothing kept)."""

    badges: dict[str, int] = field(default_factory=lambda: {b.value: 0 for b in BADGE_PRECEDENCE})
    hosts: set[str] = field(default_factory=set)
    total: int = 0
    issues: int = 0
    pages_with_issues: int = 0
    captured_from: datetime | None = None
    captured_to: datetime | None = None
    run_id: str | None = None
    unreadable: int = 0

    def add(self, record: UrlRecord) -> None:
        badge = record.badge.value
        self.badges[badge] = self.badges.get(badge, 0) + 1
        host = _host(record.final_url or record.requested_url)
        if host:
            self.hosts.add(host)
        if self.total == 0:
            self.run_id = record.run_id
        self.total += 1
        issues = len(record.gaps) + len(_findings(record))
        self.issues += issues
        if issues:
            self.pages_with_issues += 1
        when = record.captured_at
        if self.captured_from is None or when < self.captured_from:
            self.captured_from = when
        if self.captured_to is None or when > self.captured_to:
            self.captured_to = when

    def summary(self, name: str, path: Path, modified_at: datetime) -> RunSummary:
        verified = self.badges.get(UrlBadge.VERIFIED_WITHIN_SCOPE.value, 0)
        total = self.total
        return RunSummary(
            name=name,
            run_id=self.run_id,
            records_file=path.name,
            page_count=total,
            hosts=sorted(self.hosts),
            badges=self.badges,
            fully_captured_pct=round(100.0 * verified / total, 1) if total else 0.0,
            issue_count=self.issues,
            pages_with_issues=self.pages_with_issues,
            captured_from=self.captured_from,
            captured_to=self.captured_to,
            file_modified_at=modified_at,
            has_reports=_reports_dir(path, name).is_dir(),
            running=self.badges.get(UrlBadge.RUNNING.value, 0),
            unreadable_lines=self.unreadable,
        )


def _entries(records: Sequence[UrlRecord]) -> list[PageEntry]:
    entries: list[PageEntry] = []
    seen: Counter[str] = Counter()
    for record in records:
        base = page_key_for(record)
        seen[base] += 1
        key = base if seen[base] == 1 else f"{base}-{seen[base]}"
        text = whole_content(record.blocks)
        findings = _findings(record)
        issue_keys = list(dict.fromkeys([g.gap_type.value for g in record.gaps]))
        issue_keys += [f.key for f in findings]
        url = record.requested_url
        row = PageRow(
            page_key=key,
            url=url,
            final_url=record.final_url,
            title=_title(record),
            host=_host(record.final_url or url),
            badge=record.badge.value,
            words=len(text.split()),
            issues=len(record.gaps) + len(findings),
            issue_keys=issue_keys,
            content_type=_content_type(record),
            page_modified_at=record.page_modified_at,
            captured_at=record.captured_at,
            change_status=record.change_status.value if record.change_status else None,
            retryable=any(g.retry_eligible for g in record.gaps),
        )
        haystack = f"{url}\n{record.final_url or ''}\n{row.title}\n{text}".casefold()
        entries.append(
            PageEntry(
                key=key,
                record=record,
                row=row,
                findings=findings,
                haystack=haystack,
                severity=_severity(row.badge),
            )
        )
    return entries


def _counts(values: Iterable[str | None]) -> list[Count]:
    counter = Counter(v for v in values if v)
    return [
        Count(value=v, count=n) for v, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


class RunStore:
    """Finds runs in an exports folder and caches each parsed run by file signature."""

    def __init__(self, exports_dir: Path, *, max_entries: int = DEFAULT_CACHE_ENTRIES) -> None:
        """Remember the folder; nothing is read until a run is asked for.

        `max_entries` fully parsed runs stay in memory (the most recently used ones);
        run summaries are cached separately and are tiny, so they are all kept.
        """
        self.exports_dir = exports_dir
        self.max_entries = max(1, max_entries)
        self._cache: OrderedDict[str, RunData] = OrderedDict()
        self._summaries: dict[str, tuple[tuple[int, int], RunSummary]] = {}
        self._lock = threading.Lock()

    def cached_names(self) -> list[str]:
        """Runs held fully parsed right now, least recently used first (for tests)."""
        with self._lock:
            return list(self._cache)

    def names(self) -> list[str]:
        """Run names in the folder, newest records file first."""
        if not self.exports_dir.is_dir():
            return []
        files = [
            p for p in self.exports_dir.iterdir() if p.is_file() and p.name.endswith(RECORDS_SUFFIX)
        ]
        files.sort(key=lambda p: (-p.stat().st_mtime_ns, p.name))
        names = [p.name[: -len(RECORDS_SUFFIX)] for p in files]
        return [n for n in names if _RUN_NAME.match(n)]

    def get(self, name: str) -> RunData:
        """The parsed run, re-read when the file changed since the last read."""
        if not _RUN_NAME.match(name):
            raise RunNotFoundError(name)
        path = self.exports_dir / f"{name}{RECORDS_SUFFIX}"
        if not path.is_file():
            raise RunNotFoundError(name)
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._cache.get(name)
            if cached is not None and cached.signature == signature:
                self._cache.move_to_end(name)
                return cached
            records, bad = _parse(path)
            entries = _entries(records)
            by_url: dict[str, PageEntry] = {}
            for entry in entries:
                rec = entry.record
                for url in (rec.requested_url, rec.final_url, *rec.aliases):
                    if url and url not in by_url:
                        by_url[url] = entry
            data = RunData(
                name=name,
                path=path,
                signature=signature,
                entries=entries,
                by_key={e.key: e for e in entries},
                by_url=by_url,
                unreadable_lines=bad,
                file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            )
            self._cache[name] = data
            self._cache.move_to_end(name)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
            return data

    # -- views -----------------------------------------------------------------

    def summary(self, run: RunData) -> RunSummary:
        """Headline numbers for one run (also remembered for the run list)."""
        tally = _Tally(unreadable=run.unreadable_lines)
        for entry in run.entries:
            tally.add(entry.record)
        summary = tally.summary(run.name, run.path, run.file_modified_at)
        with self._lock:
            self._summaries[run.name] = (run.signature, summary)
        return summary

    def summary_for(self, name: str) -> RunSummary:
        """Headline numbers for a run by name, without keeping its pages in memory.

        A summary made earlier for the same file (same size and mtime) is reused; a
        run already fully parsed is summarised from memory; otherwise the file is
        streamed once, one record at a time, and only the numbers are kept.
        """
        if not _RUN_NAME.match(name):
            raise RunNotFoundError(name)
        path = self.exports_dir / f"{name}{RECORDS_SUFFIX}"
        if not path.is_file():
            raise RunNotFoundError(name)
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            known = self._summaries.get(name)
            if known is not None and known[0] == signature:
                # The reports folder can appear after the file was last written.
                has_reports = _reports_dir(path, name).is_dir()
                return known[1].model_copy(update={"has_reports": has_reports})
            loaded = self._cache.get(name)
        if loaded is not None and loaded.signature == signature:
            return self.summary(loaded)
        tally = _Tally()
        for record in _iter_records(path):
            if record is None:
                tally.unreadable += 1
            else:
                tally.add(record)
        summary = tally.summary(name, path, datetime.fromtimestamp(stat.st_mtime, tz=UTC))
        with self._lock:
            self._summaries[name] = (signature, summary)
        return summary

    def summaries(self) -> list[RunSummary]:
        """Every run's headline numbers, newest records file first."""
        out: list[RunSummary] = []
        for name in self.names():
            try:
                out.append(self.summary_for(name))
            except RunNotFoundError:  # removed between the listing and the read
                continue
        return out

    def overview(self, run: RunData) -> Overview:
        """Everything the run overview screen needs."""
        pages: Counter[str] = Counter()
        occurrences: Counter[str] = Counter()
        retryable: Counter[str] = Counter()
        for entry in run.entries:
            for key in entry.row.issue_keys:
                pages[key] += 1
            for gap in entry.record.gaps:
                occurrences[gap.gap_type.value] += 1
            for finding in entry.findings:
                occurrences[finding.key] += 1
            for key in {g.gap_type.value for g in entry.record.gaps if g.retry_eligible}:
                retryable[key] += 1
        issues = [
            IssueStat(key=k, pages=n, occurrences=occurrences[k], retryable_pages=retryable[k])
            for k, n in sorted(pages.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        tiers = [
            e.record.tier_reached.value if e.record.tier_reached else None for e in run.entries
        ]
        return Overview(
            summary=self.summary(run),
            issues=issues,
            formats=_counts(e.row.content_type for e in run.entries),
            hosts=_counts(e.row.host for e in run.entries),
            changes=_counts(e.row.change_status for e in run.entries),
            tiers=_counts(tiers),
            words_total=sum(e.row.words for e in run.entries),
            facts=self._facts(run),
        )

    def _facts(self, run: RunData) -> RunFacts | None:
        manifest = run.path.parent / f"{run.name}.json"
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        execution = data.get("execution")
        execution = execution if isinstance(execution, dict) else {}

        def _num(value: object) -> float | None:
            return float(value) if isinstance(value, int | float) else None

        scheduled = data.get("scheduled")
        return RunFacts(
            mode=str(data["mode"]) if isinstance(data.get("mode"), str) else None,
            stop_reason=(
                str(execution["stop_reason"])
                if isinstance(execution.get("stop_reason"), str)
                else None
            ),
            elapsed_s=_num(execution.get("elapsed_s")),
            scheduled=scheduled if isinstance(scheduled, int) else None,
        )

    def pages(self, run: RunData, query: PageQuery) -> PagesResponse:
        """Filter, sort and page the run's pages."""
        needle = query.q.strip().casefold()

        def keep(entry: PageEntry, *, use_status: bool) -> bool:
            row = entry.row
            if use_status and query.status and row.badge not in query.status:
                return False
            if query.host and row.host not in query.host:
                return False
            if query.content_type and (row.content_type or "") not in query.content_type:
                return False
            if query.issue and not set(query.issue) & set(row.issue_keys):
                return False
            if query.has_issues is not None and (row.issues > 0) != query.has_issues:
                return False
            return not needle or needle in entry.haystack

        without_status = [e for e in run.entries if keep(e, use_status=False)]
        status_counts = {b.value: 0 for b in BADGE_PRECEDENCE}
        for entry in without_status:
            status_counts[entry.row.badge] = status_counts.get(entry.row.badge, 0) + 1
        chosen = [e for e in without_status if not query.status or e.row.badge in query.status]
        chosen = _sorted(chosen, query.sort, query.order)
        start = (query.page - 1) * query.size
        issue_counter: Counter[str] = Counter()
        for entry in run.entries:
            issue_counter.update(entry.row.issue_keys)
        facets = Facets(
            status=status_counts,
            hosts=_counts(e.row.host for e in run.entries),
            formats=_counts(e.row.content_type for e in run.entries),
            issues=[
                Count(value=k, count=n)
                for k, n in sorted(issue_counter.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        )
        return PagesResponse(
            total=len(chosen),
            page=query.page,
            size=query.size,
            items=[e.row for e in chosen[start : start + query.size]],
            facets=facets,
            running=status_counts.get(UrlBadge.RUNNING.value, 0),
        )

    def detail(self, run: RunData, key: str) -> PageDetail | None:
        """One page with its blocks, gaps and facts; None for an unknown key."""
        entry = run.by_key.get(key)
        if entry is None:
            return None
        rec = entry.record
        blocks = sorted(rec.blocks, key=lambda b: b.reading_order)
        texts = [b for b in blocks if b.normalized_text.strip()]
        kept = drop_duplicates(texts)
        repeated = len(texts) - len(kept)
        return PageDetail(
            row=entry.row,
            run_name=run.name,
            requested_url=rec.requested_url,
            aliases=list(rec.aliases),
            page_modified_source=rec.page_modified_source,
            page_published_at=rec.page_published_at,
            change_basis=rec.change_basis,
            previous_run_id=rec.previous_run_id,
            reused_from_run_id=rec.reused_from_run_id,
            tier_reached=rec.tier_reached.value if rec.tier_reached else None,
            sniffed_mime=rec.sniffed_mime,
            http_last_modified=rec.http_last_modified,
            followups=[
                Followup(
                    url=f.url,
                    edge_kind=f.edge_kind,
                    expected_mime=f.expected_mime,
                    page_key=run.key_for_url(f.url),
                )
                for f in rec.followups
            ],
            status_vector=rec.status_vector.model_dump(mode="json"),
            inventory=rec.inventory.model_dump(mode="json"),
            discovery=rec.discovery.model_dump(mode="json"),
            warnings=list(rec.warnings),
            gaps=[
                GapOut(
                    gap_id=g.gap_id,
                    gap_type=g.gap_type.value,
                    reason=g.reason,
                    retry_eligible=g.retry_eligible,
                    status=g.status.value,
                    tier_reached=g.tier_reached.value if g.tier_reached else None,
                )
                for g in rec.gaps
            ],
            findings=list(entry.findings),
            blocks=[_block(b) for b in kept],
            repeated_blocks=repeated,
        )

    def raw(self, run: RunData, key: str) -> dict[str, Any] | None:
        """The full record as JSON, for the technical tab; None for an unknown key."""
        entry = run.by_key.get(key)
        return entry.record.model_dump(mode="json") if entry else None


def _sorted(entries: list[PageEntry], sort: SortField, order: str) -> list[PageEntry]:
    reverse = order == "desc"
    if sort in ("modified", "captured", "type"):
        # Missing values always sort last, whatever the direction.
        def value(e: PageEntry) -> str:
            row = e.row
            if sort == "modified":
                return row.page_modified_at.isoformat() if row.page_modified_at else ""
            if sort == "captured":
                return row.captured_at.isoformat()
            return row.content_type or ""

        present = [e for e in entries if value(e)]
        missing = [e for e in entries if not value(e)]
        present.sort(key=lambda e: (value(e), e.row.url), reverse=reverse)
        return present + sorted(missing, key=lambda e: e.row.url)

    def key(e: PageEntry) -> tuple[int | str, str]:
        row = e.row
        if sort == "status":
            return (e.severity, row.url)
        if sort == "title":
            return (row.title.casefold(), row.url)
        if sort == "words":
            return (row.words, row.url)
        if sort == "issues":
            return (row.issues, row.url)
        return (row.url, "")

    return sorted(entries, key=key, reverse=reverse)
