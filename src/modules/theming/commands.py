"""`themes`: build, show and compare a project's theme map.

    python -m src.modules.content_processor.cli themes build --project p1
    python -m src.modules.content_processor.cli themes show --project p1
    python -m src.modules.content_processor.cli themes compare --project p1

Offline and free: reads the project's scrapes and knowledge spaces; nothing is
fetched and no model is called.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["cmd_themes", "register"]


def _write(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _summary(theme_map: Any) -> dict[str, Any]:
    return {
        "map_id": theme_map.map_id,
        "built_at": theme_map.built_at,
        "websites": [
            {"website": w.website, "name": w.name, "role": w.role, "pages": w.pages}
            for w in theme_map.websites
        ],
        "skipped": theme_map.skipped,
        "stats": theme_map.stats,
        "themes": [
            {
                "theme": t.label,
                "pages": t.pages,
                "ownership": t.ownership,
                "why": t.ownership_reason,
                "per_website": {c.website: c.pages for c in t.coverage},
                "topics": [tp.label for tp in t.topics],
            }
            for t in theme_map.themes
        ],
    }


def cmd_themes(args: argparse.Namespace) -> int:
    """Dispatch the `themes` actions."""
    from src.core.config import get_settings
    from src.modules.content_processor.projects.store import ProjectStore
    from src.modules.theming.service import build_project_themes, theme_store
    from src.modules.theming.store import compare_maps

    root = Path(args.app_data) if args.app_data else get_settings().cp_app_data_root
    projects = ProjectStore(root)
    store = theme_store(projects)
    try:
        projects.get(args.project)
    except LookupError:
        sys.stderr.write(f"unknown project: {args.project}\n")
        return 2
    try:
        if args.action == "build":
            from src.modules.content_processor.webapi.__main__ import knowledge_access

            theme_map, files = build_project_themes(projects, knowledge_access(), args.project)
            _write({**_summary(theme_map), "files": files})
            return 0
        latest = store.latest(args.project)
        if latest is None:
            sys.stderr.write("no theme map yet: run `themes build --project ...` first\n")
            return 1
        if args.action == "show":
            _write(_summary(latest))
            return 0
        history = store.history(args.project)
        if len(history) < 2:
            sys.stderr.write("only one theme map: build it again later to compare\n")
            return 1
        _write(compare_maps(store.get(args.project, history[1]), latest))
        return 0
    except KeyError as exc:
        sys.stderr.write(f"theme map not found: {exc}\n")
        return 2


def register(subparsers: Any) -> None:
    """Add `themes` to the CLI."""
    parser = subparsers.add_parser(
        "themes", help="topic clusters, pillar pages and theme ownership of a project"
    )
    parser.add_argument("action", choices=["build", "show", "compare"])
    parser.add_argument("--project", required=True, help="project id")
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_themes)
