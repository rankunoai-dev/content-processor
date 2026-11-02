"""`research`: pull or import search data, read it, compare snapshots, see unit use.

    python -m src.modules.content_processor.cli research estimate --project p1
    python -m src.modules.content_processor.cli research pull --project p1 \
        [--keyword "term life insurance" ...]
    python -m src.modules.content_processor.cli research import --project p1 \
        --website example.com --file positions.csv
    python -m src.modules.content_processor.cli research show --project p1
    python -m src.modules.content_processor.cli research movers --project p1
    python -m src.modules.content_processor.cli research units

`pull` spends Semrush API units and is refused unless CP_SEMRUSH_ENABLED, SEMRUSH_API_KEY
and a monthly cap are set; `estimate` shows its worst case first. `import` reads an
Organic Positions export from the Semrush website and costs nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["cmd_research", "register"]


def _write(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _summary(snapshot: Any) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "created_at": snapshot.created_at,
        "database": snapshot.database,
        "units_spent": snapshot.units_spent,
        "websites": [
            {
                "website": w.website,
                "role": w.role,
                "source": w.source,
                "keywords": len(w.keywords),
                "top10": sum(1 for r in w.keywords if r.position <= 10),
                "organic_traffic": w.overview.organic_traffic if w.overview else None,
            }
            for w in snapshot.websites
        ],
        "demand": [
            {"theme": d.label, "volume": d.volume, "keywords": d.keywords, "top10": d.top10}
            for d in snapshot.demand[:20]
        ],
        "notes": snapshot.notes,
    }


def cmd_research(args: argparse.Namespace) -> int:  # noqa: PLR0911 - one branch per action
    """Dispatch the `research` actions."""
    from src.core.config import get_settings
    from src.modules.content_processor.projects.store import ProjectStore
    from src.modules.research.build import movers
    from src.modules.research.importer import ImportError_
    from src.modules.research.semrush import SemrushError
    from src.modules.research.service import (
        ResearchSettings,
        estimate_units,
        import_csv,
        pull_project,
        research_store,
        unit_log,
    )

    root = Path(args.app_data) if args.app_data else get_settings().cp_app_data_root
    projects = ProjectStore(root)
    settings = ResearchSettings.from_env()
    if args.action == "units":
        now = datetime.now(UTC)
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        _write(
            {
                "enabled": settings.enabled,
                "api_key_set": bool(settings.api_key),
                "used_this_month": unit_log(projects).used(month),
                "monthly_cap": settings.monthly_units or "unset (pulls refused)",
                "pull_cap": settings.pull_units,
                "keyword_limit": settings.keyword_limit,
                "database": settings.database,
            }
        )
        return 0
    if not args.project:
        sys.stderr.write("--project is required\n")
        return 2
    try:
        projects.get(args.project)
    except LookupError:
        sys.stderr.write(f"unknown project: {args.project}\n")
        return 2
    store = research_store(projects)
    if args.action in ("estimate", "pull", "import"):
        from src.modules.analyst.playground import Playground
        from src.modules.content_processor.webapi.__main__ import knowledge_access

        access = knowledge_access()
        if args.action == "estimate":
            websites = len(Playground(projects, access, [args.project]).websites())
            _write(
                {
                    "websites": websites,
                    "keyword_limit": settings.keyword_limit,
                    "chosen_keywords": len(args.keyword or []),
                    "worst_case_units": estimate_units(websites, settings, len(args.keyword or [])),
                    "allowed_now": settings.pull_units,
                }
            )
            return 0
        try:
            if args.action == "pull":
                snapshot, files = pull_project(
                    projects, access, args.project, settings, serp_keywords=args.keyword or []
                )
            else:
                if not args.website or not args.file:
                    sys.stderr.write("import needs --website and --file\n")
                    return 2
                snapshot, files = import_csv(
                    projects,
                    access,
                    args.project,
                    args.website,
                    Path(args.file).read_bytes(),
                    database=settings.database,
                )
        except (SemrushError, ImportError_, ValueError, OSError) as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
        _write({**_summary(snapshot), "files": files})
        return 0
    latest = store.latest(args.project)
    if latest is None:
        sys.stderr.write("no research yet: run `research import` or `research pull` first\n")
        return 1
    if args.action == "show":
        _write(_summary(latest))
        return 0
    previous = store.previous(args.project)
    if previous is None:
        sys.stderr.write("only one snapshot: pull or import again later to compare\n")
        return 1
    _write([m.model_dump() for m in movers(previous, latest)])
    return 0


def register(subparsers: Any) -> None:
    """Add `research` to the CLI."""
    parser = subparsers.add_parser(
        "research", help="keyword rankings and demand per theme (Semrush API or export)"
    )
    parser.add_argument("action", choices=["estimate", "pull", "import", "show", "movers", "units"])
    parser.add_argument("--project", default=None, help="project id")
    parser.add_argument(
        "--keyword",
        action="append",
        default=None,
        help="pull: also get demand and top results of this keyword (repeatable)",
    )
    parser.add_argument("--website", default=None, help="import: the website the export is of")
    parser.add_argument("--file", default=None, help="import: the Organic Positions CSV")
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_research)
