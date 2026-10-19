"""Write every Lane 5 output for one run into a directory.

Files (all rows carry `run_id`, evidence ids and block ids):

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
from src.modules.content_processor.export.reports import (
    r1_rows,
    r1_table,
    review_rows,
    review_table,
)
from src.modules.content_processor.export.tabular import write_csv, write_jsonl, write_xlsx
from src.modules.content_processor.ui.explorer import write_explorer

__all__ = ["BUNDLE_FILES", "REPORT_VERSION", "write_run_reports"]

REPORT_VERSION = "l5-v2"
BUNDLE_FILES: tuple[str, ...] = (
    "explorer.html",
    "manifest.json",
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
"""Every file `write_run_reports` writes (other files in the directory are left alone)."""


def write_run_reports(
    records: Sequence[UrlRecord],
    out_dir: Path,
    *,
    run_id: str | None = None,
    overlap: OverlapConfig | None = None,
    include_boilerplate: bool = True,
    contradictions: ContradictionConfig | None = None,
    judge: NliJudge | None = None,
    decisions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Write R1, the review queue, R2a/R2b, R4 and the explorer; return the manifest.

    Args:
        records: One `UrlRecord` per requested URL (`url_records_for_run`).
        out_dir: Created if missing; existing files of the same name are replaced.
        run_id: Defaults to the records' run id.
        overlap: Overlap settings; defaults follow the Stage 4 spec.
        include_boilerplate: R1's `whole_content` includes nav/chrome/footer roles
            (the spec default). The overlap analysis excludes them unless its
            own config says otherwise.
        contradictions: Contradiction settings; defaults follow the Stage 4 spec.
        judge: NLI judge for R4; None runs deterministic checks only and the
            manifest records `judge_status: disabled(...)`.
        decisions: Reviewer decisions by `pair_id` from an earlier R4, carried forward.
    """
    run = run_id or (records[0].run_id if records else "unknown-run")
    out_dir.mkdir(parents=True, exist_ok=True)
    replaced: dict[str, int] = {}

    r1 = r1_rows(records, include_boilerplate=include_boilerplate)
    write_jsonl(out_dir / "r1_url_content.jsonl", r1)
    r1_sheet = r1_table(r1)
    write_csv(out_dir / "r1_url_content.csv", r1_sheet)
    replaced["r1_url_content.xlsx"] = write_xlsx(out_dir / "r1_url_content.xlsx", [r1_sheet])

    review = review_rows(records)
    write_jsonl(out_dir / "review_queue.jsonl", review)
    review_sheet = review_table(review)
    write_csv(out_dir / "review_queue.csv", review_sheet)
    replaced["review_queue.xlsx"] = write_xlsx(out_dir / "review_queue.xlsx", [review_sheet])

    result = find_overlaps(records, overlap)
    groups, pairs = r2a_table(result), r2b_table(result)
    write_csv(out_dir / "r2a_overlap_groups.csv", groups)
    write_csv(out_dir / "r2b_overlap_pairs.csv", pairs)
    replaced["r2_overlap.xlsx"] = write_xlsx(out_dir / "r2_overlap.xlsx", [groups, pairs])

    found = find_contradictions(records, contradictions, judge=judge)
    r4 = r4_table(found, decisions)
    write_jsonl(out_dir / "r4_contradictions.jsonl", r4.rows)
    write_csv(out_dir / "r4_contradictions.csv", r4)
    replaced["r4_contradictions.xlsx"] = write_xlsx(out_dir / "r4_contradictions.xlsx", [r4])

    downloads = {
        "R1 (XLSX)": "r1_url_content.xlsx",
        "R1 (CSV)": "r1_url_content.csv",
        "Review queue (XLSX)": "review_queue.xlsx",
        "Overlap R2 (XLSX)": "r2_overlap.xlsx",
        "Contradictions R4 (XLSX)": "r4_contradictions.xlsx",
    }
    write_explorer(out_dir / "explorer.html", records, run_id=run, downloads=downloads)

    badges: dict[str, int] = {}
    for record in records:
        badges[record.badge.value] = badges.get(record.badge.value, 0) + 1
    manifest: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "run_id": run,
        "generated_at": datetime.now(UTC).isoformat(),
        "requested_urls": len(records),
        "badges": badges,
        "r1_rows": len(r1),
        "r1_split_rows": sum(1 for r in r1_sheet.rows if r.get("whole_content_split")),
        "review_rows": len(review),
        "overlap": result.summary(),
        "contradictions": found.summary(),
        "xlsx_characters_replaced": replaced,
        "files": list(BUNDLE_FILES),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest
