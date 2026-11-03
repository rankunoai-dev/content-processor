"""Project reports: duplication and contradictions across all of the client's websites.

A website's scrape writes its own report bundle. This module combines the
**latest finished scrape of every client website** (the main website and its
brands) into one project bundle, from the records files already on disk, so
nothing is scraped again:

- `<project>_contradictions_by_topic.{xlsx,csv,jsonl}`: one row per topic stated
  in two or more ways (`analysis.topics`), with sheets for differences by
  condition and for time-bound figures (news and press pages, dated figures).
- `<project>_duplication.xlsx`: matching content between every pair of pages, the
  shared text line by line, and the R2 overlap pairs and groups, each row with
  its website(s), so the same text on two brands shows up too.
- `<project>_competitors.xlsx` (only when asked): each competitor's own
  contradictions and duplicate pages, on separate sheets. A competitor never
  enters the client's contradictions: a competitor saying something else is not
  a contradiction.
- `manifest.json`: which scrape of which website was used, and the counts.

Bundles live in `<project>/reports/<stamp>/`; every build is kept.
"""

from __future__ import annotations

import json
import re
import threading
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.modules.content_processor.analysis.overlap import find_overlaps, r2a_table, r2b_table
from src.modules.content_processor.analysis.topics import (
    TopicResult,
    facts_from_records,
    find_topics,
    topic_rows,
    topic_tables,
)
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.export.commands import load_records
from src.modules.content_processor.export.project_report import MATCH_COLUMNS, match_rows
from src.modules.content_processor.export.tabular import (
    Cell,
    Table,
    write_csv,
    write_jsonl,
    write_xlsx,
)
from src.modules.content_processor.projects.models import ProjectRecord, RunState, SiteRecord
from src.modules.content_processor.projects.store import ProjectStore

__all__ = [
    "ProjectReport",
    "ReportSource",
    "build_project_reports",
    "project_report_files",
    "project_reports",
    "report_sources",
    "zip_project_report",
]

MATCH_MIN_PCT = 50.0
"""Matching content lists page pairs sharing at least this share of either page's text."""
PREVIEW_CHARS = 1500
"""Shared text is shown up to this length per row, so the workbook stays openable."""
_DUP_NOTES: tuple[tuple[str, str], ...] = (
    (
        "Duplicate pages (80%+)",
        "Page pairs whose text is at least 80% the same (runs of five words; menus, headers "
        "and footers left out). across_websites = yes: the same text on two of the "
        "client's websites (for example the main site and a brand).",
    ),
    ("Duplicate groups (80%+)", "Pages that are all near-copies of one another."),
    (
        "Matching content (50%+)",
        "Every pair of pages where at least half of one page's text is also on the other. "
        "The two percentages are directional: a short page can be 100% inside a long one.",
    ),
    (
        "Shared text",
        "The text both pages carry, shortened to its first 1,500 characters. The website's "
        "own report (Reports tab of that website) has the full text and every pair from 10%.",
    ),
)
_STAMP = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ReportSource:
    """One website and the scrape the project report reads for it."""

    site_id: str
    site_name: str
    role: str
    main: bool
    run_id: str | None
    finished_at: datetime | None
    pages: int
    reason: str
    """Why the website has no usable scrape ('' when it has one)."""
    records: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        """For the manifest and the API."""
        return {
            "site_id": self.site_id,
            "site_name": self.site_name,
            "role": self.role,
            "main": self.main,
            "run_id": self.run_id,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "pages": self.pages,
            "reason": self.reason,
        }


@dataclass
class ProjectReport:
    """One built project bundle."""

    stamp: str
    folder: Path
    manifest: dict[str, Any] = field(default_factory=dict)


def _latest_finished(runs: Sequence[RunState]) -> RunState | None:
    done = [r for r in runs if r.status == "finished"]
    return max(done, key=lambda r: r.finished_at or r.created_at) if done else None


def report_sources(store: ProjectStore, project: ProjectRecord) -> list[ReportSource]:
    """Every website with its latest finished scrape, client websites first."""
    main = project.main_site()
    out: list[ReportSource] = []
    ordered = sorted(project.sites, key=lambda s: (s.role != "client", s is not main))
    for site in ordered:
        run = _latest_finished(store.runs(project.project_id, site.site_id))
        is_main = main is not None and site.site_id == main.site_id
        if run is None:
            out.append(_source(site, is_main, None, None, "No finished scrape yet."))
            continue
        records = store.run_dir(project.project_id, site.site_id, run.run_id) / (
            f"{run.run_id}.records.jsonl"
        )
        if not records.is_file():
            out.append(_source(site, is_main, run, None, "The scrape's page records are missing."))
            continue
        out.append(_source(site, is_main, run, records, ""))
    return out


def _source(
    site: SiteRecord, main: bool, run: RunState | None, records: Path | None, reason: str
) -> ReportSource:
    return ReportSource(
        site_id=site.site_id,
        site_name=site.name,
        role=site.role,
        main=main,
        run_id=run.run_id if run else None,
        finished_at=(run.finished_at or run.created_at) if run else None,
        pages=run.pages_done if run else 0,
        reason=reason,
        records=records,
    )


def _reports_root(store: ProjectStore, project_id: str) -> Path:
    return store.project_dir(project_id) / "reports"


def _lock(project_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project_id, threading.Lock())


def _prefix(project: ProjectRecord) -> str:
    return _UNSAFE.sub("_", project.name).strip("._-")[:60] or project.project_id


def _new_stamp(root: Path, now: datetime) -> str:
    base = now.strftime("%Y%m%d-%H%M%S")
    stamp, n = base, 2
    while (root / stamp).exists():
        stamp, n = f"{base}-{n}", n + 1
    return stamp


def _with_websites(table: Table, websites: dict[str, str], pairs: bool) -> Table:
    """R2 rows with the website of each URL; pairs also say whether they cross websites."""
    rows: list[dict[str, Cell]] = []
    for row in table.rows:
        extra: dict[str, Cell]
        if pairs:
            a = websites.get(str(row.get("url_a", "")), "")
            b = websites.get(str(row.get("url_b", "")), "")
            extra = {"website_a": a, "website_b": b, "across_websites": "yes" if a != b else "no"}
        else:
            extra = {"website": websites.get(str(row.get("url", "")), "")}
        rows.append({**row, **extra})
    if pairs:
        rows.sort(
            key=lambda r: (r["across_websites"] != "yes", -float(str(r.get("jaccard_pct") or 0)))
        )
    added = ["website_a", "website_b", "across_websites"] if pairs else ["website"]
    return Table(name=table.name, columns=[*table.columns, *added], rows=rows)


def _sources_table(sources: Sequence[ReportSource]) -> Table:
    rows: list[dict[str, Cell]] = [
        {
            "Website": s.site_name,
            "Role": ("client (main website)" if s.main else "client (brand)")
            if s.role == "client"
            else "competitor",
            "Scrape used": s.run_id or "",
            "Scrape finished": s.finished_at.isoformat() if s.finished_at else "",
            "Pages": s.pages,
            "Note": s.reason,
        }
        for s in sources
    ]
    return Table(
        name="Websites used",
        columns=["Website", "Role", "Scrape used", "Scrape finished", "Pages", "Note"],
        rows=rows,
        widths={"Website": 30, "Role": 22, "Scrape used": 30, "Scrape finished": 26, "Note": 50},
    )


def _load(sources: Sequence[ReportSource]) -> tuple[list[UrlRecord], dict[str, str]]:
    records: list[UrlRecord] = []
    websites: dict[str, str] = {}
    for source in sources:
        if source.records is None:
            continue
        for record in load_records(source.records):
            records.append(record)
            websites[record.requested_url] = source.site_name
    return records, websites


def _duplication_tables(
    records: Sequence[UrlRecord], websites: dict[str, str]
) -> tuple[list[Table], dict[str, object]]:
    overlap = find_overlaps(records)
    pairs = _with_websites(r2b_table(overlap), websites, pairs=True)
    for row in pairs.rows:
        row["shared_spans"] = _preview(str(row.get("shared_spans") or ""))
    groups = _with_websites(r2a_table(overlap), websites, pairs=False)
    pairs.name, groups.name = "Duplicate pages (80%+)", "Duplicate groups (80%+)"
    across = sum(1 for r in pairs.rows if r["across_websites"] == "yes")
    summary = {**overlap.summary(), "pairs_across_websites": across}
    summary.pop("skipped", None)
    matches = match_rows(records, min_match_pct=MATCH_MIN_PCT)
    for row in matches:
        row["Shared text"] = _preview(str(row.get("Shared text") or ""))
        a = websites.get(str(row.get("URL (A)", "")), "")
        b = websites.get(str(row.get("Matching URL (B)", "")), "")
        row["Websites"] = a if a == b else f"{a} | {b}"
    matching = Table(
        name=f"Matching content ({MATCH_MIN_PCT:g}%+)",
        columns=[*MATCH_COLUMNS, "Websites"],
        rows=matches,
        wrap_columns=frozenset({"Shared text"}),
        widths={"URL (A)": 60, "Matching URL (B)": 60, "Shared text": 100, "Websites": 30},
    )
    notes = Table(
        name="How to read",
        columns=["Item", "Meaning"],
        rows=[{"Item": k, "Meaning": v} for k, v in _DUP_NOTES],
        wrap_columns=frozenset({"Meaning"}),
        widths={"Item": 30, "Meaning": 110},
    )
    summary["matching_pairs"] = sum(1 for r in matches if r.get("Matching URL (B)"))
    return [pairs, groups, matching, notes], summary


def _preview(text: str) -> str:
    """The start of a long shared text; the website's own report holds all of it."""
    if len(text) <= PREVIEW_CHARS:
        return text
    return (
        text[:PREVIEW_CHARS].rstrip()
        + " … [shortened: the full text is in the website's own report]"
    )


def build_project_reports(
    store: ProjectStore,
    project_id: str,
    *,
    include_competitors: bool = False,
    built_by: str = "",
    now: datetime | None = None,
) -> ProjectReport:
    """Build a project bundle from the latest finished scrape of each website.

    Raises `ValueError` when no client website has a finished scrape.
    """
    project = store.get(project_id)
    with _lock(project_id):
        sources = report_sources(store, project)
        clients = [s for s in sources if s.role == "client" and s.records is not None]
        if not clients:
            msg = "None of the client's websites has a finished scrape yet."
            raise ValueError(msg)
        competitors = [s for s in sources if s.role == "competitor" and s.records is not None]
        moment = now or datetime.now(UTC)
        root = _reports_root(store, project_id)
        stamp = _new_stamp(root, moment)
        folder = root / stamp
        tmp = root / f".{stamp}.tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        prefix = _prefix(project)
        used = [s for s in sources if s.role == "client" or include_competitors]

        records, websites = _load(clients)
        topics = find_topics(facts_from_records(records, websites))
        _write_topics(tmp, prefix, topics, used)
        dup_sheets, duplication = _duplication_tables(records, websites)
        write_xlsx(tmp / f"{prefix}_duplication.xlsx", [*dup_sheets, _sources_table(used)])

        competitor_counts: dict[str, object] = {}
        if include_competitors and competitors:
            sheets: list[Table] = []
            for source in competitors:
                own, own_sites = _load([source])
                found = find_topics(facts_from_records(own, own_sites))
                label = source.site_name[:14]
                sheets.extend(topic_tables(found, name_prefix=f"{label} – ")[:1])
                pairs = _with_websites(r2b_table(find_overlaps(own)), own_sites, pairs=True)
                pairs.name = f"{label} – Duplicates"
                sheets.append(pairs)
                competitor_counts[source.site_name] = {
                    "contradictions": len(found.on("contradiction")),
                    "duplicate_pairs": len(pairs.rows),
                }
            write_xlsx(tmp / f"{prefix}_competitors.xlsx", [*sheets, _sources_table(competitors)])

        manifest: dict[str, Any] = {
            "kind": "project_reports",
            "version": 1,
            "project_id": project_id,
            "project_name": project.name,
            "stamp": stamp,
            "built_at": moment.isoformat(),
            "built_by": built_by,
            "include_competitors": include_competitors,
            "websites": [s.as_dict() for s in sources],
            "pages": len(records),
            "contradictions": topics.summary(),
            "duplication": duplication,
            "competitors": competitor_counts,
        }
        manifest["files"] = sorted(p.name for p in tmp.iterdir()) + ["manifest.json"]
        (tmp / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(folder)
    return ProjectReport(stamp=stamp, folder=folder, manifest=manifest)


def _write_topics(
    folder: Path, prefix: str, topics: TopicResult, used: Sequence[ReportSource]
) -> None:
    sheets = topic_tables(topics)
    write_xlsx(folder / f"{prefix}_contradictions_by_topic.xlsx", [*sheets, _sources_table(used)])
    write_csv(folder / f"{prefix}_contradictions_by_topic.csv", sheets[0])
    rows: list[dict[str, object]] = []
    for sheet in ("contradiction", "condition", "time_bound"):
        for topic, row in zip(topics.on(sheet), topic_rows(topics.on(sheet)), strict=True):
            rows.append(
                {
                    "sheet": sheet,
                    **row,
                    "versions": [
                        {
                            "value": v.value_text,
                            "facts": [
                                {
                                    "url": f.url,
                                    "website": f.website,
                                    "quote": f.quote,
                                    "section": f.section,
                                    "block_id": f.block_id,
                                    "evidence_ids": list(f.evidence_ids),
                                }
                                for f in v.facts
                            ],
                        }
                        for v in topic.versions
                    ],
                }
            )
    write_jsonl(folder / f"{prefix}_contradictions_by_topic.jsonl", rows)


def project_reports(store: ProjectStore, project_id: str) -> list[ProjectReport]:
    """Built bundles, newest first."""
    root = _reports_root(store, project_id)
    if not root.is_dir():
        return []
    out: list[ProjectReport] = []
    for folder in root.iterdir():
        manifest_path = folder / "manifest.json"
        if not _STAMP.match(folder.name) or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(ProjectReport(stamp=folder.name, folder=folder, manifest=manifest))
    return sorted(out, key=lambda r: r.stamp, reverse=True)


def project_report_files(store: ProjectStore, project_id: str, stamp: str) -> dict[str, Path]:
    """A bundle's files by name (the stamp is checked; names come from the listing)."""
    if not _STAMP.match(stamp):
        return {}
    folder = _reports_root(store, project_id) / stamp
    if not folder.is_dir():
        return {}
    return {
        p.name: p for p in sorted(folder.iterdir()) if p.is_file() and not p.name.endswith(".zip")
    }


def zip_project_report(store: ProjectStore, project_id: str, stamp: str) -> Path | None:
    """The bundle as one zip, made once and kept next to it."""
    files = project_report_files(store, project_id, stamp)
    if not files:
        return None
    folder = _reports_root(store, project_id) / stamp
    archive = folder / f"{_UNSAFE.sub('_', project_id)}_{stamp}_reports.zip"
    if not archive.is_file():
        tmp = archive.with_name(archive.name + ".tmp")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, path in files.items():
                bundle.write(path, arcname=name)
        tmp.replace(archive)
    return archive
