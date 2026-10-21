"""Write every Lane 5 output for one run into a directory.

Every file name starts with the run's name, `<run>_<report>`: for a dry run
written with `--out exports/jh2.json` the folder is `exports/jh2_reports/` and
the files are `jh2_r1_url_content.xlsx`, `jh2_explorer.html` and so on, so
reports from different runs never share a name, on disk or once downloaded.
`report_file()` finds a report under its run-named file or, for folders written
before run names were added, under the plain name.

Files, by report (all rows carry `run_id`, evidence ids and block ids):

| File | Content |
|---|---|
| `r1_url_content.{csv,xlsx,jsonl}` | Report R1, one row per requested URL |
| `review_queue.{csv,xlsx,jsonl}` | URLs needing a decision, blank operator columns |
| `r2_overlap.xlsx`, `r2a_overlap_groups.csv`, `r2b_overlap_pairs.csv` | Reports R2a and R2b |
| `r4_contradictions.{csv,xlsx,jsonl}` | Report R4, contradiction candidates, reviewer column |
| `explorer.html` | I1 URL Content Explorer, offline, links to the files above |
| `manifest.json` | Counts, parameters, files and XLSX character replacements |

JSONL is verbatim and unsplit. CSV and XLSX split long cells into numbered
part columns and flag them (see `export/tabular.py`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.modules.content_processor.analysis.contradictions import (
    ContradictionConfig,
    find_contradictions,
    r4_table,
)
from src.modules.content_processor.analysis.nli import NliJudge
from src.modules.content_processor.analysis.overlap import (
    OverlapConfig,
    find_overlaps,
    r2a_table,
    r2b_table,
)
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.export.project_report import project_report_tables
from src.modules.content_processor.export.reports import (
    r1_rows,
    r1_table,
    review_rows,
    review_table,
)
from src.modules.content_processor.export.tabular import write_csv, write_jsonl, write_xlsx
from src.modules.content_processor.ui.explorer import write_explorer

__all__ = [
    "BUNDLE_FILES",
    "REPORT_VERSION",
    "REPORTS_DIR_SUFFIX",
    "bundle_file_name",
    "report_file",
    "run_file_prefix",
    "write_run_reports",
]

REPORT_VERSION = "l5-v3"
REPORTS_DIR_SUFFIX = "_reports"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
BUNDLE_FILES: tuple[str, ...] = (
    "explorer.html",
    "manifest.json",
    "project_report.xlsx",
    "r1_url_content.csv",
    "r1_url_content.jsonl",
    "r1_url_content.xlsx",
    "r2_overlap.xlsx",
    "r2a_overlap_groups.csv",
    "r2b_overlap_pairs.csv",
    "r4_contradictions.csv",
    "r4_contradictions.jsonl",
    "r4_contradictions.xlsx",
    "review_queue.csv",
    "review_queue.jsonl",
    "review_queue.xlsx",
)
"""The reports `write_run_reports` writes, by plain name; on disk each is `<run>_<name>`."""


def run_file_prefix(name: str) -> str:
    """A run name made safe for file names (letters, digits, `.`, `_`, `-`)."""
    cleaned = _UNSAFE.sub("_", name).strip("._-")
    return cleaned or "run"


def bundle_file_name(prefix: str, base: str) -> str:
    """`<run>_<report>` for one of `BUNDLE_FILES`."""
    return f"{run_file_prefix(prefix)}_{base}"


def report_file(folder: Path, run_name: str, base: str) -> Path | None:
    """A run's report file: the run-named one, else a plain-named one from older runs."""
    for candidate in (folder / bundle_file_name(run_name, base), folder / base):
        if candidate.is_file():
            return candidate
    return None


def _default_prefix(out_dir: Path, run_id: str) -> str:
    name = out_dir.name
    if name.endswith(REPORTS_DIR_SUFFIX) and len(name) > len(REPORTS_DIR_SUFFIX):
        return name[: -len(REPORTS_DIR_SUFFIX)]
    return run_id


def write_run_reports(
    records: Sequence[UrlRecord],
    out_dir: Path,
    *,
    run_id: str | None = None,
    run_name: str | None = None,
    overlap: OverlapConfig | None = None,
    include_boilerplate: bool = True,
    contradictions: ContradictionConfig | None = None,
    judge: NliJudge | None = None,
    decisions: Mapping[str, str] | None = None,
    review_decisions: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Write R1, the review queue, R2a/R2b, R4 and the explorer; return the manifest.

    Args:
        records: One `UrlRecord` per requested URL (`url_records_for_run`).
        out_dir: Created if missing; existing files of the same name are replaced.
        run_id: Defaults to the records' run id.
        run_name: Prefix of every file name. Defaults to the folder name without
            `_reports` (so `jh2_reports/` holds `jh2_*` files), else the run id.
        overlap: Overlap settings; defaults follow the Stage 4 spec.
        include_boilerplate: R1's `whole_content` includes nav/chrome/footer roles
            (the spec default). The overlap analysis excludes them unless its
            own config says otherwise.
        contradictions: Contradiction settings; defaults follow the Stage 4 spec.
        judge: NLI judge for R4; None runs deterministic checks only and the
            manifest records `judge_status: disabled(...)`.
        decisions: Reviewer decisions by `pair_id` from an earlier R4, carried forward.
        review_decisions: Review-queue decisions by URL (`action`, `note`), saved in the
            web app; they pre-fill the operator columns.
    """
    run = run_id or (records[0].run_id if records else "unknown-run")
    prefix = run_file_prefix(run_name or _default_prefix(out_dir, run))

    def path(base: str) -> Path:
        return out_dir / bundle_file_name(prefix, base)

    out_dir.mkdir(parents=True, exist_ok=True)
    replaced: dict[str, int] = {}

    r1 = r1_rows(records, include_boilerplate=include_boilerplate)
    write_jsonl(path("r1_url_content.jsonl"), r1)
    r1_sheet = r1_table(r1)
    write_csv(path("r1_url_content.csv"), r1_sheet)
    replaced[path("r1_url_content.xlsx").name] = write_xlsx(path("r1_url_content.xlsx"), [r1_sheet])

    review = review_rows(records, review_decisions)
    write_jsonl(path("review_queue.jsonl"), review)
    review_sheet = review_table(review)
    write_csv(path("review_queue.csv"), review_sheet)
    replaced[path("review_queue.xlsx").name] = write_xlsx(path("review_queue.xlsx"), [review_sheet])

    result = find_overlaps(records, overlap)
    groups, pairs = r2a_table(result), r2b_table(result)
    write_csv(path("r2a_overlap_groups.csv"), groups)
    write_csv(path("r2b_overlap_pairs.csv"), pairs)
    replaced[path("r2_overlap.xlsx").name] = write_xlsx(path("r2_overlap.xlsx"), [groups, pairs])

    found = find_contradictions(records, contradictions, judge=judge)
    r4 = r4_table(found, decisions)
    write_jsonl(path("r4_contradictions.jsonl"), r4.rows)
    write_csv(path("r4_contradictions.csv"), r4)
    replaced[path("r4_contradictions.xlsx").name] = write_xlsx(path("r4_contradictions.xlsx"), [r4])

    replaced[path("project_report.xlsx").name] = write_xlsx(
        path("project_report.xlsx"), project_report_tables(records)
    )

    downloads = {
        "Project report (XLSX)": path("project_report.xlsx").name,
        "R1 (XLSX)": path("r1_url_content.xlsx").name,
        "R1 (CSV)": path("r1_url_content.csv").name,
        "Review queue (XLSX)": path("review_queue.xlsx").name,
        "Overlap R2 (XLSX)": path("r2_overlap.xlsx").name,
        "Contradictions R4 (XLSX)": path("r4_contradictions.xlsx").name,
    }
    write_explorer(path("explorer.html"), records, run_id=run, downloads=downloads)

    badges: dict[str, int] = {}
    for record in records:
        badges[record.badge.value] = badges.get(record.badge.value, 0) + 1
    manifest: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "run_id": run,
        "run_name": prefix,
        "generated_at": datetime.now(UTC).isoformat(),
        "requested_urls": len(records),
        "badges": badges,
        "r1_rows": len(r1),
        "r1_split_rows": sum(1 for r in r1_sheet.rows if r.get("whole_content_split")),
        "review_rows": len(review),
        "overlap": result.summary(),
        "contradictions": found.summary(),
        "xlsx_characters_replaced": replaced,
        "files": [bundle_file_name(prefix, base) for base in BUNDLE_FILES],
    }
    path("manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest
