"""`history` subcommand: fill and inspect the tenant's global URL history.

    python -m src.modules.content_processor.cli history import --records x.records.jsonl --tenant t
    python -m src.modules.content_processor.cli history show --url <url> --tenant t
    python -m src.modules.content_processor.cli history stats --tenant t

Runs add their records automatically; `import` is for runs made before the history
existed (or with `--no-history`). Importing the same run twice changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.core.config import get_settings
from src.modules.content_processor.export.commands import load_records
from src.modules.content_processor.recrawl.history import HistoryIndex, history_path

__all__ = ["cmd_history", "register"]


def _index(args: argparse.Namespace) -> HistoryIndex:
    root = Path(args.evidence_root) if args.evidence_root else get_settings().cp_evidence_root
    return HistoryIndex(history_path(root, args.tenant))


def cmd_history(args: argparse.Namespace) -> int:
    """Import a run into the history, show one URL's captures, or print counts."""
    with _index(args) as index:
        out: dict[str, Any] = {"history": str(index.path)}
        if args.action == "import":
            if not args.records:
                sys.stderr.write("history import needs --records\n")
                return 2
            for path in args.records:
                result = index.add_records(load_records(Path(path)))
                out.setdefault("imported", {})[path] = vars(result)
        elif args.action == "show":
            if not args.url:
                sys.stderr.write("history show needs --url\n")
                return 2
            out["url"] = args.url
            out["captures"] = [
                {
                    "run_id": c.run_id,
                    "captured_at": c.captured_at.isoformat(),
                    "extracted_at": c.extracted_at.isoformat(),
                    "badge": c.badge,
                    "change": c.change_status,
                    "basis": c.change_basis,
                    "site_modified": c.page_modified_at.isoformat() if c.page_modified_at else None,
                    "reusable": c.reusable,
                    "latest": c.is_latest,
                }
                for c in index.captures(args.url)
            ]
        out["stats"] = index.stats()
    sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return 0


def register(subparsers: Any) -> None:
    """Add the `history` subcommand to the CLI's subparsers."""
    parser = subparsers.add_parser("history", help="global URL history across runs")
    parser.add_argument("action", choices=["import", "show", "stats"])
    parser.add_argument("--tenant", required=True, help="tenant whose history to use")
    parser.add_argument("--records", nargs="+", default=None, help="import: .records.jsonl files")
    parser.add_argument("--url", default=None, help="show: the URL to look up")
    parser.add_argument(
        "--evidence-root", default=None, help="evidence store root (default: CP_EVIDENCE_ROOT)"
    )
    parser.set_defaults(func=cmd_history)
