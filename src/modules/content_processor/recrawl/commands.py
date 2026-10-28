"""`history` subcommand: check, fill and inspect the tenant's global URL history.

    python -m src.modules.content_processor.cli history --urls urls.txt --tenant t --out c.csv
    python -m src.modules.content_processor.cli history import --records x.records.jsonl --tenant t
    python -m src.modules.content_processor.cli history show --url <url> --tenant t
    python -m src.modules.content_processor.cli history stats --tenant t

`check` (the default) answers, before scraping, which URLs were scraped before,
when, and what date the site gives. Runs add their records automatically; `import`
is for runs made before the history existed (or with `--no-history`). Importing
the same run twice changes nothing.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from src.core.config import get_settings
from src.modules.content_processor.export.commands import load_records, run_name_for
from src.modules.content_processor.recrawl.history import UrlHistory, describe

__all__ = ["cmd_history", "register"]


def _history(args: argparse.Namespace) -> UrlHistory:
    root = Path(args.history_root) if args.history_root else get_settings().cp_url_history_root
    return UrlHistory(root, args.tenant)


def _check(args: argparse.Namespace, history: UrlHistory, out: dict[str, Any]) -> int:
    if not args.urls:
        sys.stderr.write("history check needs --urls\n")
        return 2
    lines = Path(args.urls).read_text(encoding="utf-8").splitlines()
    urls = [u.strip() for u in lines if u.strip()]
    rows = [{"URL": u, **describe(history.lookup(u))} for u in dict.fromkeys(urls)]
    if args.out:
        with Path(args.out).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["URL"])
            writer.writeheader()
            writer.writerows(rows)
    out.update(
        {
            "urls": len(rows),
            "scraped_before": sum(1 for r in rows if r["Scraped before"] == "Yes"),
            "new": sum(1 for r in rows if r["Scraped before"] == "No"),
            "known_urls": len(history),
            "out": args.out,
        }
    )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Check URLs against the history, import a run, show one URL's captures, or count."""
    with _history(args) as history:
        out: dict[str, Any] = {"history": str(history.path)}
        if args.action == "check":
            code = _check(args, history, out)
            if code:
                return code
        elif args.action == "import":
            if not args.records:
                sys.stderr.write("history import needs --records\n")
                return 2
            for path in args.records:
                result = history.add_records(
                    load_records(Path(path)), run_name=run_name_for(Path(path))
                )
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
                for c in (history.index().captures(args.url) if history.exists() else [])
            ]
        if args.action != "check" and history.exists():
            out["stats"] = history.index().stats()
        if history.migrated:
            out["migrated_from_files"] = history.migrated
    sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return 0


def register(subparsers: Any) -> None:
    """Add the `history` subcommand to the CLI's subparsers."""
    parser = subparsers.add_parser(
        "history", help="which URLs were scraped before, and when; fill or inspect the history"
    )
    parser.add_argument(
        "action", nargs="?", default="check", choices=["check", "import", "show", "stats"]
    )
    parser.add_argument("--tenant", default="rankuno-dev", help="tenant whose history to use")
    parser.add_argument("--urls", default=None, help="check: text file, one URL per line")
    parser.add_argument("--out", default=None, help="check: write a CSV with one row per URL")
    parser.add_argument("--records", nargs="+", default=None, help="import: .records.jsonl files")
    parser.add_argument("--url", default=None, help="show: the URL to look up")
    parser.add_argument(
        "--history-root", default=None, help="history root (default: CP_URL_HISTORY_ROOT)"
    )
    parser.set_defaults(func=cmd_history)
