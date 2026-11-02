"""`monitor`: run and read monitoring checks, and set how a project is watched.

    python -m src.modules.content_processor.cli monitor check --project p1
    python -m src.modules.content_processor.cli monitor show --project p1
    python -m src.modules.content_processor.cli monitor watch --project p1 \
        [--auto-check on|off] [--rescrape-every-days N] [--push-threshold N]

A check reads the scrapes already made; nothing is fetched. Scheduled re-scraping
(`--rescrape-every-days`, 0 = off) contacts the websites and happens only while the
app server runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["cmd_monitor", "register"]


def _write(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _summary(check: Any) -> dict[str, Any]:
    return {
        "check_id": check.check_id,
        "created_at": check.created_at,
        "stats": check.stats,
        "websites": [
            {
                "website": d.website,
                "role": d.role,
                "new": len(d.new_pages),
                "removed": len(d.removed_pages),
                "changed": len(d.changed_pages),
                "drift": d.drift,
                "note": d.note,
            }
            for d in check.websites
        ],
        "alerts": [
            {"severity": a.severity, "title": a.title, "detail": a.detail} for a in check.alerts
        ],
    }


def cmd_monitor(args: argparse.Namespace) -> int:
    """Dispatch the `monitor` actions."""
    from src.core.config import get_settings
    from src.modules.content_processor.projects.store import ProjectStore
    from src.modules.monitoring.service import monitor_store, run_project_check

    root = Path(args.app_data) if args.app_data else get_settings().cp_app_data_root
    projects = ProjectStore(root)
    try:
        projects.get(args.project)
    except LookupError:
        sys.stderr.write(f"unknown project: {args.project}\n")
        return 2
    store = monitor_store(projects)
    if args.action == "check":
        from src.modules.content_processor.webapi.__main__ import knowledge_access

        check, files = run_project_check(projects, knowledge_access(), args.project)
        _write({**_summary(check), "files": files})
        return 0
    if args.action == "show":
        latest = store.latest(args.project)
        if latest is None:
            sys.stderr.write("no check yet: run `monitor check --project ...` first\n")
            return 1
        _write(_summary(latest))
        return 0
    settings = store.watch(args.project)
    update: dict[str, Any] = {}
    if args.auto_check is not None:
        update["auto_check"] = args.auto_check == "on"
    if args.rescrape_every_days is not None:
        update["rescrape_every_days"] = args.rescrape_every_days
    if args.push_threshold is not None:
        update["push_threshold"] = args.push_threshold
    if update:
        try:
            settings = store.set_watch(
                args.project, type(settings).model_validate({**settings.model_dump(), **update})
            )
        except ValueError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
    _write(settings.model_dump())
    return 0


def register(subparsers: Any) -> None:
    """Add `monitor` to the CLI."""
    parser = subparsers.add_parser(
        "monitor", help="changes between scrapes, price shifts, theme drift and alerts"
    )
    parser.add_argument("action", choices=["check", "show", "watch"])
    parser.add_argument("--project", required=True, help="project id")
    parser.add_argument("--auto-check", choices=["on", "off"], default=None)
    parser.add_argument("--rescrape-every-days", type=int, default=None, help="0 = off")
    parser.add_argument("--push-threshold", type=int, default=None)
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_monitor)
