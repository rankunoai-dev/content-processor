"""Run outputs written next to `--out` (Lane L6).

For `--out runs/x.json` the files are:

- `runs/x.json`                  run report, now with a `url_records` badge summary
- `runs/x.records.jsonl`         one full UrlRecord per requested URL (input of `cli report`)
- `runs/x.content.jsonl`         whole content per URL (report R1)
- `runs/x.review_queue.jsonl`    gated, blocked and excluded URLs for a human (HITL)

Everything is read through the ledger port, so the same writer serves the
in-memory ledger and Postgres.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.export.content import whole_content
from src.modules.content_processor.export.review_queue import write_review_queue
from src.modules.content_processor.verify.status import summarize_run_badge
from src.modules.content_processor.workers.ports import LedgerPort

__all__ = ["RunOutputs", "badge_summary", "records_path_for", "write_run_outputs"]


class RunOutputs(StrictModel):
    """Paths written and what the records say."""

    report: str
    records: str
    content: str
    review_queue: str
    review_items: int = Field(ge=0)
    summary: dict[str, Any]


def records_path_for(out: Path) -> Path:
    """Where the full UrlRecord JSONL for `--out` goes."""
    return out.with_suffix(".records.jsonl")


def badge_summary(records: Sequence[UrlRecord]) -> dict[str, Any]:
    """Run badge plus counts by badge; the denominator is every requested URL."""
    counts = Counter(r.badge.value for r in records)
    return {
        "run_badge": summarize_run_badge(r.badge for r in records).value,
        "urls": len(records),
        "by_badge": dict(sorted(counts.items())),
        "gaps": sum(len(r.gaps) for r in records),
    }


def write_run_outputs(
    out: Path,
    *,
    reader: LedgerPort,
    run_id: str,
    records: Sequence[UrlRecord],
    report: dict[str, Any],
) -> RunOutputs:
    """Write the four output files and return their paths and the badge summary."""
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = badge_summary(records)
    records_path = records_path_for(out)
    with records_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(record.model_dump_json() + "\n")

    resources = reader.list_resources(run_id)
    content_path = out.with_suffix(".content.jsonl")
    with content_path.open("w", encoding="utf-8") as handle:
        for res in resources:
            blocks = reader.get_blocks(res.resource_id)
            body_text = whole_content(blocks)
            row = {
                "url": res.requested_url,
                "status": res.status.value,
                "reason": res.status_reason,
                "word_count": len(body_text.split()),
                "main_only": whole_content(blocks, include_boilerplate=False),
                "whole_content": body_text,
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    queue_path = out.with_suffix(".review_queue.jsonl")
    queued = write_review_queue(queue_path, resources, reader.get_gaps)

    full = {
        **report,
        "url_records": summary,
        "records": {"path": str(records_path), "count": len(records)},
        "review_queue": {"path": str(queue_path), "items": queued},
        "content": {"path": str(content_path)},
    }
    out.write_text(json.dumps(full, indent=2, default=str) + "\n", encoding="utf-8")
    return RunOutputs(
        report=str(out),
        records=str(records_path),
        content=str(content_path),
        review_queue=str(queue_path),
        review_items=queued,
        summary=summary,
    )
