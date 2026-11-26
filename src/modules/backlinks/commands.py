"""`backlinks`: pull or import backlink data, read it, see unit use.

    python -m src.modules.content_processor.cli backlinks estimate --project p1
    python -m src.modules.content_processor.cli backlinks pull --project p1
    python -m src.modules.content_processor.cli backlinks import --project p1 \
        --website example.com --kind refdomains --file "example.com-refdomains.csv"
    python -m src.modules.content_processor.cli backlinks show --project p1
    python -m src.modules.content_processor.cli backlinks units
    python -m src.modules.content_processor.cli backlinks-standin make --project p1 --out dummy
    python -m src.modules.content_processor.cli backlinks-standin serve --out dummy

`pull` spends Ahrefs API units and is refused unless CP_AHREFS_ENABLED, AHREFS_API_KEY
and a monthly cap are set; `estimate` shows its worst case first. `import` reads a
Referring domains or Best by links export from the Ahrefs website and costs nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["cmd_backlinks", "cmd_standin", "register", "register_standin"]


def _write(data: Any) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
    # UTF-8 whatever the console's code page (a redirected Windows console is cp1252)
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def _summary(snapshot: Any) -> dict[str, Any]:
    insights = snapshot.insights
    return {
        "snapshot_id": snapshot.snapshot_id,
        "created_at": snapshot.created_at,
        "units_spent": snapshot.units_spent,
        "websites": [
            {
                "website": w.website,
                "role": w.role,
                "source": w.source,
                "domain_rating": w.domain_rating,
                "live_refdomains": w.stats.live_refdomains if w.stats else None,
                "refdomains": len(w.refdomains),
                "top_pages": len(w.top_pages),
                "lost_since_last": len(w.lost_domains) if w.new_domains is not None else None,
            }
            for w in snapshot.websites
        ],
        "link_intersect": len(insights.link_intersect),
        "top_intersect": [
            {"domain": i.domain, "domain_rating": i.domain_rating, "competitors": i.competitors}
            for i in insights.link_intersect[:10]
        ],
        "authority_gap": [g.model_dump() for g in insights.authority],
        "notes": [*snapshot.notes, *insights.notes],
    }


def cmd_backlinks(args: argparse.Namespace) -> int:  # noqa: PLR0911 - one branch per action
    """Dispatch the `backlinks` actions."""
    from src.core.config import get_settings
    from src.modules.backlinks.ahrefs import AhrefsError
    from src.modules.backlinks.importer import BacklinksImportError
    from src.modules.backlinks.service import (
        BacklinksSettings,
        backlinks_store,
        estimate_units,
        import_csv,
        pull_project,
        unit_log,
    )
    from src.modules.content_processor.projects.store import ProjectStore

    root = Path(args.app_data) if args.app_data else get_settings().cp_app_data_root
    projects = ProjectStore(root)
    settings = BacklinksSettings.from_env()
    if args.action == "units":
        month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        _write(
            {
                "enabled": settings.enabled,
                "api_key_set": bool(settings.api_key),
                "used_this_month": unit_log(projects).used(month),
                "monthly_cap": settings.monthly_units or "unset (pulls refused)",
                "pull_cap": settings.pull_units,
                "refdomains_limit": settings.refdomains_limit,
                "pages_limit": settings.pages_limit,
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
    if args.action == "show":
        latest = backlinks_store(projects).latest(args.project)
        if latest is None:
            sys.stderr.write("no backlink data yet: run `backlinks import` or `backlinks pull`\n")
            return 1
        _write(_summary(latest))
        return 0
    from src.modules.analyst.playground import Playground
    from src.modules.content_processor.webapi.__main__ import knowledge_access

    access = knowledge_access()
    if args.action == "estimate":
        websites = len(Playground(projects, access, [args.project]).websites())
        _write(
            {
                "websites": websites,
                "refdomains_limit": settings.refdomains_limit,
                "pages_limit": settings.pages_limit,
                "worst_case_units": estimate_units(websites, settings),
                "allowed_now": settings.pull_units,
            }
        )
        return 0
    try:
        if args.action == "pull":
            snapshot, files = pull_project(projects, access, args.project, settings)
        else:
            if not args.website or not args.file:
                sys.stderr.write("import needs --website, --kind and --file\n")
                return 2
            snapshot, files = import_csv(
                projects,
                access,
                args.project,
                args.website,
                args.kind,
                Path(args.file).read_bytes(),
            )
    except (AhrefsError, BacklinksImportError, ValueError, OSError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    _write({**_summary(snapshot), "files": files})
    return 0


def register(subparsers: Any) -> None:
    """Add `backlinks` to the CLI."""
    parser = subparsers.add_parser(
        "backlinks",
        help="authority, referring domains and link intersect (Ahrefs API or export)",
    )
    parser.add_argument("action", choices=["estimate", "pull", "import", "show", "units"])
    parser.add_argument("--project", default=None, help="project id")
    parser.add_argument("--website", default=None, help="import: the website the export is of")
    parser.add_argument(
        "--kind",
        default="refdomains",
        choices=["refdomains", "pages"],
        help="import: a Referring domains export or a Best by links (top pages) export",
    )
    parser.add_argument("--file", default=None, help="import: the Ahrefs export (CSV)")
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_backlinks)


def cmd_standin(args: argparse.Namespace) -> int:
    """`backlinks-standin make|serve`: dummy Ahrefs data for trying the module ($0)."""
    from src.modules.backlinks.standin import Dataset, make_dataset, serve

    out = Path(args.out)
    if args.action == "serve":
        dataset = Dataset.load(out / "backlinks-dataset.json")
        server = serve(dataset, args.port, slow_down_every=args.slow_down_every)
        sys.stdout.write(
            f"Ahrefs stand-in (dummy data) on http://127.0.0.1:{args.port}/v3/ for "
            f"{len(dataset.websites)} websites; set CP_AHREFS_BASE_URL to it. Ctrl+C stops.\n"
        )
        sys.stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    from src.core.config import get_settings
    from src.modules.content_processor.projects.store import ProjectStore

    if not args.project:
        sys.stderr.write("make needs --project\n")
        return 2
    root = Path(args.app_data) if args.app_data else get_settings().cp_app_data_root
    project = ProjectStore(root).get(args.project)
    websites = [
        (
            min((d.removeprefix("www.") for d in site.domains), key=len),
            "competitor" if site.role == "competitor" else "client",
        )
        for site in project.sites
        if site.domains
    ]
    dataset = make_dataset(args.project, websites, period=args.period)
    dataset.save(out / "backlinks-dataset.json")
    files: dict[str, dict[str, str]] = {}
    for site in dataset.websites:
        refdomains = out / f"{site.domain}-ahrefs-refdomains.csv"
        refdomains.write_bytes(dataset.refdomains_csv(site.domain))
        pages = out / f"{site.domain}-ahrefs-best-by-links.csv"
        pages.write_bytes(dataset.pages_csv(site.domain))
        files[site.domain] = {"refdomains": str(refdomains), "pages": str(pages)}
    _write(
        {
            "note": dataset.note,
            "dataset": str(out / "backlinks-dataset.json"),
            "websites": {
                s.domain: {
                    "role": s.role,
                    "domain_rating": s.domain_rating,
                    "refdomains": len(s.refdomains),
                }
                for s in dataset.websites
            },
            "exports": files,
        }
    )
    return 0


def register_standin(subparsers: Any) -> None:
    """Add `backlinks-standin` to the CLI."""
    parser = subparsers.add_parser(
        "backlinks-standin",
        help="dummy Ahrefs data (CSV exports and a local API) for trying the Backlinks module",
    )
    parser.add_argument("action", choices=["make", "serve"])
    parser.add_argument("--project", default=None, help="make: project id")
    parser.add_argument("--out", required=True, help="folder for the dataset and the CSV files")
    parser.add_argument(
        "--period", type=int, default=0, help="make: a later month (some links lost and gained)"
    )
    parser.add_argument("--port", type=int, default=8791, help="serve: port on 127.0.0.1")
    parser.add_argument(
        "--slow-down-every",
        type=int,
        default=0,
        help="serve: answer every n-th call with HTTP 429 (to see pacing and retries)",
    )
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_standin)
