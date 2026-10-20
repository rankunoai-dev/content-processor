"""Response models of the analyst API (docs/UI_PLAN.md §4).

Field names are engine-neutral where the UI needs them (`badge`, `issue_keys`)
and keep the engine's enum values: the plain-language wording lives in the web
app's `vocabulary.ts`, never here, so one file owns every label.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "Block",
    "Contradiction",
    "ContradictionsResponse",
    "Count",
    "DownloadFile",
    "DownloadsResponse",
    "DuplicateGroup",
    "DuplicateMember",
    "DuplicatePair",
    "DuplicatesResponse",
    "Facets",
    "Finding",
    "Followup",
    "GapOut",
    "IssueStat",
    "Overview",
    "PageDetail",
    "PageRow",
    "PagesResponse",
    "ReportSource",
    "ReviewItem",
    "ReviewResponse",
    "RunFacts",
    "RunSummary",
    "RunsResponse",
    "Statement",
]

ReportSource = Literal["report", "computed"]
"""`report`: read from the run's saved reports folder; `computed`: built now from the records."""


class Count(StrictModel):
    """One value of a facet and how many pages have it."""

    value: str
    count: int = Field(ge=0)


class RunSummary(StrictModel):
    """One run (a `<name>.records.jsonl` file) with its headline numbers."""

    name: str
    run_id: str | None
    records_file: str
    page_count: int = Field(ge=0)
    hosts: list[str]
    badges: dict[str, int]
    fully_captured_pct: float
    issue_count: int = Field(ge=0, description="Issues over all pages (gaps plus derived findings)")
    pages_with_issues: int = Field(ge=0)
    captured_from: datetime | None
    captured_to: datetime | None
    file_modified_at: datetime
    has_reports: bool
    running: int = Field(ge=0)
    unreadable_lines: int = Field(ge=0, description="Lines of the file that are not a UrlRecord")


class RunsResponse(StrictModel):
    """Every run found in the exports folder, newest file first."""

    exports_dir: str
    runs: list[RunSummary]


class IssueStat(StrictModel):
    """How often one issue type occurs in a run."""

    key: str
    pages: int = Field(ge=0)
    occurrences: int = Field(ge=0)
    retryable_pages: int = Field(ge=0)


class RunFacts(StrictModel):
    """Facts from the run's manifest (`<name>.json`), when it exists and is readable."""

    mode: str | None = None
    stop_reason: str | None = None
    elapsed_s: float | None = None
    scheduled: int | None = None


class Overview(StrictModel):
    """Everything the run overview screen shows."""

    summary: RunSummary
    issues: list[IssueStat]
    formats: list[Count]
    hosts: list[Count]
    changes: list[Count]
    tiers: list[Count]
    words_total: int = Field(ge=0)
    facts: RunFacts | None


class PageRow(StrictModel):
    """One row of the pages table (no content blocks)."""

    page_key: str
    url: str
    final_url: str | None
    title: str
    host: str
    badge: str
    words: int = Field(ge=0)
    issues: int = Field(ge=0)
    issue_keys: list[str]
    content_type: str | None
    page_modified_at: datetime | None
    captured_at: datetime
    change_status: str | None
    retryable: bool


class Facets(StrictModel):
    """Counts the pages toolbar needs to label its filters."""

    status: dict[str, int] = Field(description="Badge counts under every filter except status")
    hosts: list[Count]
    formats: list[Count]
    issues: list[Count]


class PagesResponse(StrictModel):
    """One page of the filtered, sorted pages table."""

    total: int = Field(ge=0)
    page: int = Field(ge=1)
    size: int = Field(ge=1)
    items: list[PageRow]
    facets: Facets
    running: int = Field(ge=0)


class GapOut(StrictModel):
    """One gap of a page, as recorded by the engine."""

    gap_id: str
    gap_type: str
    reason: str
    retry_eligible: bool
    status: str
    tier_reached: str | None


class Finding(StrictModel):
    """An issue derived from the status vector when no gap explains the badge."""

    key: str
    unconfirmed: int = Field(ge=0)
    total: int = Field(ge=0)


class Followup(StrictModel):
    """A link or embedded file the page pointed to."""

    url: str
    edge_kind: str | None
    expected_mime: str | None
    page_key: str | None = Field(description="Set when the linked URL is a page of this run")


class Block(StrictModel):
    """One piece of captured content. `text` is third-party text: render as text only."""

    id: str
    text: str
    heading_path: list[str]
    order: int
    layer: str
    role: str
    region: str | None
    format: str | None
    verified: bool
    status: str
    confidence: float
    page_label: str | None
    frame: str


class PageDetail(StrictModel):
    """Everything the page detail screen shows, except the raw record."""

    row: PageRow
    run_name: str
    requested_url: str
    aliases: list[str]
    page_modified_source: str | None
    page_published_at: datetime | None
    change_basis: str | None
    previous_run_id: str | None
    reused_from_run_id: str | None
    tier_reached: str | None
    sniffed_mime: str | None
    http_last_modified: str | None
    followups: list[Followup]
    status_vector: dict[str, Any]
    inventory: dict[str, Any]
    discovery: dict[str, Any]
    warnings: list[str]
    gaps: list[GapOut]
    findings: list[Finding]
    blocks: list[Block]
    repeated_blocks: int = Field(ge=0, description="Blocks left out as exact repeats")


class DuplicateMember(StrictModel):
    """One page in an overlap group."""

    url: str
    page_key: str | None
    title: str
    badge: str
    overlap_pct: float
    shared_blocks: int = Field(ge=0)
    representative: bool


class DuplicateGroup(StrictModel):
    """Pages whose content overlaps above the threshold (report R2a)."""

    group_id: str
    size: int = Field(ge=0)
    members: list[DuplicateMember]


class DuplicatePair(StrictModel):
    """Two pages and how much of their text they share (report R2b)."""

    url_a: str
    url_b: str
    key_a: str | None
    key_b: str | None
    title_a: str
    title_b: str
    jaccard_pct: float
    a_in_b_pct: float
    b_in_a_pct: float
    shared_passages: list[str]
    exact: bool


class DuplicatesResponse(StrictModel):
    """Overlap groups and pairs for a run."""

    source: ReportSource
    generated_at: str | None
    stale: bool
    metric: str
    threshold_pct: float
    groups: list[DuplicateGroup]
    pairs: list[DuplicatePair]


class Statement(StrictModel):
    """One side of a possible contradiction. `quote` is third-party text."""

    url: str
    page_key: str | None
    title: str
    quote: str
    heading: str
    badge: str


class Contradiction(StrictModel):
    """Two statements that may disagree (report R4)."""

    pair_id: str
    bucket: str
    confidence: str
    confidence_reason: str
    flags: list[str]
    context: dict[str, str]
    contradiction_pct: float | None
    a: Statement
    b: Statement
    reviewer_decision: str


class ContradictionsResponse(StrictModel):
    """Contradiction candidates for a run."""

    source: ReportSource
    generated_at: str | None
    stale: bool
    judge_status: str | None
    items: list[Contradiction]


class ReviewItem(StrictModel):
    """One page that needs a person's decision."""

    url: str
    page_key: str | None
    title: str
    badge: str
    issue_keys: list[str]
    gap_types: list[str]
    suggested_action: str
    operator_action: str
    operator_note: str


class ReviewResponse(StrictModel):
    """The review queue for a run."""

    source: ReportSource
    generated_at: str | None
    stale: bool
    items: list[ReviewItem]


class DownloadFile(StrictModel):
    """One file in the run's reports folder."""

    name: str
    size: int = Field(ge=0)
    modified_at: datetime


class DownloadsResponse(StrictModel):
    """Files in the run's reports folder (empty when there is no folder)."""

    folder: str | None
    files: list[DownloadFile]
