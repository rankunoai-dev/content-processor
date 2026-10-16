"""`report` subcommand for the CLI (WORK_BREAKDOWN §4.2: L6 wires `register`).

    python -m src.modules.content_processor.cli report --records run.records.jsonl --out reports/

`--records` is a JSONL file with one `UrlRecord` per line (as written by the
dry run once L6 wires `url_records_for_run` into it).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.modules.content_processor.analysis.overlap import OverlapConfig
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.export.bundle import write_run_reports

__all__ = ["cmd_report", "load_records", "register"]


def load_records(path: Path) -> list[UrlRecord]:
    """Read and validate UrlRecords from JSONL; blank lines are ignored."""
    records: list[UrlRecord] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(UrlRecord.model_validate_json(line))
            except ValueError as exc:
                msg = f"{path}:{number}: not a UrlRecord: {exc}"
                raise ValueError(msg) from exc
    return records


def cmd_report(args: argparse.Namespace) -> int:
    """Write the Lane 5 report bundle for a run's records."""
    records = load_records(Path(args.records))
    config = OverlapConfig(
        shingle_size=args.shingle_size,
        threshold=args.threshold / 100.0,
        metric=args.metric,
        strict=args.strict,
        include_boilerplate=args.overlap_boilerplate,
    )
    manifest: dict[str, Any] = write_run_reports(
        records, Path(args.out), run_id=args.run, overlap=config
    )
    sys.stdout.write(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return 0


def register(subparsers: Any) -> None:
    """Add the `report` subcommand to the CLI's subparsers."""
    parser = subparsers.add_parser("report", help="write R1, review queue, R2 and the explorer")
    parser.add_argument("--records", required=True, help="UrlRecord JSONL file")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--run", default=None, help="run id (defaults to the records' run id)")
    parser.add_argument("--threshold", type=float, default=80.0, help="overlap %% (default 80)")
    parser.add_argument("--metric", choices=["jaccard", "containment"], default="jaccard")
    parser.add_argument("--shingle-size", type=int, default=5)
    parser.add_argument("--strict", action="store_true", help="every pair in a group must pass")
    parser.add_argument(
        "--overlap-boilerplate",
        action="store_true",
        help="include nav/chrome/footer text in the overlap analysis",
    )
    parser.set_defaults(func=cmd_report)
