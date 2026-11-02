"""`gaps`: build and read a project's gap analysis, and record decisions on findings.

    python -m src.modules.content_processor.cli gaps build --project p1
    python -m src.modules.content_processor.cli gaps show --project p1
    python -m src.modules.content_processor.cli gaps status --project p1 \
        --finding gap-0123456789ab --set accepted [--note "brief written"]

Offline and free: it reads the theme map, the latest research snapshot and monitoring
check, and the offering catalogs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

__all__ = ["cmd_gaps", "register"]


def _write(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _summary(report: Any) -> dict[str, Any]:
    return {
        "report_id": report.report_id,
        "created_at": report.created_at,
        "stats": report.stats,
        "missing_inputs": report.missing_inputs,
        "findings": [
            {
                "id": f.finding_id,
                "priority": f.priority,
                "severity": f.severity,
                "title": f.title,
                "action": f.action,
                "status": f.status,
            }
            for f in report.findings
        ],
        "resolved": report.resolved,
    }


def cmd_gaps(args: argparse.Namespace) -> int:
    """Dispatch the `gaps` actions."""
    from src.core.config import get_settings
    from src.modules.content_processor.projects.store import ProjectStore
    from src.modules.gaps.models import FindingStatus
    from src.modules.gaps.service import build_project_gaps, gap_store

    root = Path(args.app_data) if args.app_data else get_settings().cp_app_data_root
    projects = ProjectStore(root)
    try:
        projects.get(args.project)
    except LookupError:
        sys.stderr.write(f"unknown project: {args.project}\n")
        return 2
    store = gap_store(projects)
    if args.action == "build":
        from src.modules.content_processor.webapi.__main__ import knowledge_access

        report, files = build_project_gaps(projects, knowledge_access(), args.project)
        _write({**_summary(report), "files": files})
        return 0
    if store.latest(args.project) is None:
        sys.stderr.write("no gap analysis yet: run `gaps build --project ...` first\n")
        return 1
    if args.action == "show":
        _write(_summary(store.latest(args.project)))
        return 0
    if not args.finding or not args.set:
        sys.stderr.write("status needs --finding and --set\n")
        return 2
    try:
        entry = store.set_status(
            args.project, args.finding, cast(FindingStatus, args.set), args.note or ""
        )
    except KeyError:
        sys.stderr.write(f"no finding {args.finding} in the latest gap analysis\n")
        return 2
    _write({"finding": args.finding, **entry.model_dump()})
    return 0


def register(subparsers: Any) -> None:
    """Add `gaps` to the CLI."""
    parser = subparsers.add_parser(
        "gaps", help="prioritised content, theme, keyword and offering gaps against competitors"
    )
    parser.add_argument("action", choices=["build", "show", "status"])
    parser.add_argument("--project", required=True, help="project id")
    parser.add_argument("--finding", default=None, help="status: the finding id (gap-...)")
    parser.add_argument("--set", choices=["open", "accepted", "done", "dismissed"], default=None)
    parser.add_argument("--note", default=None, help="status: a note for the team")
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_gaps)
