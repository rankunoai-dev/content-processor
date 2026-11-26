"""`analytics`: GA4 imports, pulls and the report; `analytics-standin`: dummy GA4 data.

    python -m src.modules.content_processor.cli analytics import --project p1 --file ga4.csv
    python -m src.modules.content_processor.cli analytics settings --project p1 --property 123456789
    python -m src.modules.content_processor.cli analytics pull --project p1
    python -m src.modules.content_processor.cli analytics show --project p1
    python -m src.modules.content_processor.cli analytics-standin make --project p1 --out dummy
    python -m src.modules.content_processor.cli analytics-standin serve --out dummy

`import` reads GA4's landing page CSV ($0). `pull` calls the GA4 Data API with a service
account (`GA4_CREDENTIALS_FILE`) and is refused unless `CP_GA4_ENABLED=true` and the
project's property is set; it keeps the latest period and the one before.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["cmd_analytics", "cmd_standin", "register", "register_standin"]


def _write(data: Any) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def _projects(args: argparse.Namespace) -> Any:
    from src.core.config import get_settings
    from src.modules.content_processor.projects.store import ProjectStore

    return ProjectStore(Path(args.app_data) if args.app_data else get_settings().cp_app_data_root)


def cmd_analytics(args: argparse.Namespace) -> int:
    """Dispatch the `analytics` actions."""
    from src.modules.analytics.ga4 import Ga4Error
    from src.modules.analytics.importer import Ga4ImportError
    from src.modules.analytics.service import (
        build_analytics_report,
        import_ga4_csv,
        pull_ga4,
        set_property,
    )
    from src.modules.content_processor.webapi.__main__ import knowledge_access

    projects = _projects(args)
    try:
        projects.get(args.project)
    except LookupError:
        sys.stderr.write(f"unknown project: {args.project}\n")
        return 2
    try:
        if args.action == "import":
            if not args.file:
                sys.stderr.write("import needs --file\n")
                return 2
            s = import_ga4_csv(projects, args.project, args.website, Path(args.file).read_bytes())
            _write({"snapshot": s.snapshot_id, "pages": len(s.rows), "sessions": s.sessions,
                    "start": s.start_date, "end": s.end_date, "notes": s.notes})  # fmt: skip
            return 0
        if args.action == "settings":
            _write(set_property(projects, args.project, args.property, args.website).model_dump())
            return 0
        if args.action == "pull":
            saved = pull_ga4(projects, args.project)
            _write([{"snapshot": s.snapshot_id, "start": s.start_date, "end": s.end_date,
                     "pages": len(s.rows), "sessions": s.sessions} for s in saved])  # fmt: skip
            return 0
    except (Ga4Error, Ga4ImportError, ValueError, OSError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    built = build_analytics_report(projects, knowledge_access(), args.project)
    if built is None:
        sys.stderr.write("no GA4 data yet: run `analytics import` or `analytics pull` first\n")
        return 1
    report, files = built
    _write(
        {
            "website": report.website,
            "period": report.period,
            "totals": report.totals,
            "channels": report.channels,
            "themes": [
                t.model_dump() | {"conversion_rate": round(t.conversion_rate, 4)}
                for t in report.themes[:10]
            ],  # fmt: skip
            "leaky_pages": len(report.leaky),
            "decays": len(report.decays),
            "ctas_without_conversions": len(report.cta_gaps),
            "ai_referral_pages": len(report.ai_referrals),
            "after_edits": len(report.post_edit),
            "notes": report.notes,
            "files": files,
        }
    )
    return 0


def register(subparsers: Any) -> None:
    """Add `analytics` to the CLI."""
    parser = subparsers.add_parser(
        "analytics", help="GA4: sessions, engagement and conversions per landing page"
    )
    parser.add_argument("action", choices=["import", "settings", "pull", "show"])
    parser.add_argument("--project", required=True, help="project id")
    parser.add_argument("--file", default=None, help="import: GA4's landing page CSV")
    parser.add_argument("--website", default=None, help="the website (default: the client's)")
    parser.add_argument("--property", default=None, help="settings: the GA4 property id")
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_analytics)


def cmd_standin(args: argparse.Namespace) -> int:
    """`analytics-standin make|serve`: dummy GA4 data for trying the module ($0)."""
    from src.modules.analytics.standin import (
        ga4_csv,
        make_dataset,
        serve,
        write_service_account,
    )

    out = Path(args.out)
    if args.action == "serve":
        spec = json.loads((out / "pages.json").read_text(encoding="utf-8"))
        datasets = [
            make_dataset(spec["website"], spec["pages"], seed=spec["website"], period=n)
            for n in (0, 1)
        ]
        server = serve(datasets, args.port, slow_down_every=args.slow_down_every)
        sys.stdout.write(
            f"GA4 stand-in (dummy data) on http://127.0.0.1:{args.port}/ ; set "
            f"CP_GA4_BASE_URL=http://127.0.0.1:{args.port}/ and "
            f"CP_GA4_TOKEN_URL=http://127.0.0.1:{args.port}/token. Ctrl+C stops.\n"
        )
        sys.stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    if not args.project:
        sys.stderr.write("make needs --project\n")
        return 2
    from src.modules.analytics.service import client_website
    from src.modules.theming.service import theme_store

    projects = _projects(args)
    website = client_website(projects, args.project)
    if website is None:
        sys.stderr.write("the project has no client website\n")
        return 2
    theme_map = theme_store(projects).latest(args.project)
    pages = sorted(
        {p.url for p in theme_map.pages if p.website.removeprefix("www.") == website}
        if theme_map
        else {f"https://{website}/"}
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "pages.json").write_text(
        json.dumps({"website": website, "pages": pages}, indent=1), encoding="utf-8"
    )
    files = {}
    for n, name in ((0, "ga4-latest.csv"), (1, "ga4-previous.csv")):
        dataset = make_dataset(website, pages, seed=website, period=n)
        (out / name).write_bytes(ga4_csv(dataset))
        files[name] = str(out / name)
    key = write_service_account(out / "dummy-service-account.json")
    _write(
        {
            "note": "DUMMY DATA: invented GA4 figures for trying the module",
            "website": website,
            "pages": len(pages),
            "exports": files,
            "service_account_for_the_standin": str(key),
        }
    )
    return 0


def register_standin(subparsers: Any) -> None:
    """Add `analytics-standin` to the CLI."""
    parser = subparsers.add_parser(
        "analytics-standin", help="dummy GA4 data (CSV exports and a local Data API)"
    )
    parser.add_argument("action", choices=["make", "serve"])
    parser.add_argument("--project", default=None, help="make: project id")
    parser.add_argument("--out", required=True, help="folder for the exports and the dataset")
    parser.add_argument("--port", type=int, default=8792, help="serve: port on 127.0.0.1")
    parser.add_argument("--slow-down-every", type=int, default=0, help="serve: every n-th 429")
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_standin)
