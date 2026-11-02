"""`analyze`: list, inspect, preflight and run Senior Analyst Action Cards.

    python -m src.modules.content_processor.cli analyze cards
    python -m src.modules.content_processor.cli analyze show eeat-trust-audit
    python -m src.modules.content_processor.cli analyze preflight eeat-trust-audit --project p1
    python -m src.modules.content_processor.cli analyze run eeat-trust-audit --project p1 \
        --set topic="term life insurance" [--backend claude]
    python -m src.modules.content_processor.cli analyze runs
    python -m src.modules.content_processor.cli analyze policy --project p1 \
        --data-class client_confidential
    python -m src.modules.content_processor.cli analyze spend

`--backend pack` (the default) costs nothing and sends nothing anywhere: it writes
the card's context pack. `--backend claude` sends it to Claude and needs
`CP_CLAUDE_ENABLED=true` and a monthly cap in `.env`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from src.modules.analyst.library import CARDS, card

if TYPE_CHECKING:
    from src.modules.analyst.runs import CardRunner

__all__ = ["cmd_analyze", "register"]


def _write(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _root(args: argparse.Namespace) -> Path:
    from src.core.config import get_settings

    return Path(args.app_data) if args.app_data else get_settings().cp_app_data_root


def _runner(args: argparse.Namespace) -> CardRunner:
    from src.modules.analyst.runs import CardRunner
    from src.modules.content_processor.projects.store import ProjectStore
    from src.modules.content_processor.webapi.__main__ import knowledge_access

    root = _root(args)
    return CardRunner(ProjectStore(root), knowledge_access(), root / "analyst")


def _inputs(args: argparse.Namespace) -> dict[str, Any]:
    raw: dict[str, Any] = {"project": args.project}
    for item in args.set or []:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            msg = f"--set expects name=value, got {item!r}"
            raise ValueError(msg)
        raw[name.strip()] = value.strip()
    return raw


def _card_row(c: Any) -> dict[str, Any]:
    from src.modules.analyst.tools import TOOLS

    missing = [t for t in c.required_tools if not TOOLS[t].built]
    return {
        "id": c.id,
        "title": c.title,
        "module": c.module,
        "deliverable": c.deliverable,
        "available": not missing,
        "needs": sorted({TOOLS[t].module for t in missing}),
        "summary": c.summary,
    }


def cmd_analyze(args: argparse.Namespace) -> int:
    """Dispatch the `analyze` actions."""
    action = args.action
    try:
        if action == "cards":
            rows = [_card_row(c) for c in CARDS if not args.module or c.module == args.module]
            _write(rows)
            return 0
        if action == "show":
            _write(card(args.card).model_dump())
            return 0
        if action in ("preflight", "run"):
            chosen = card(args.card)
            if not args.project:
                sys.stderr.write("--project is required\n")
                return 2
            runner = _runner(args)
            if action == "preflight":
                check, _play = runner.preflight(chosen, _inputs(args), args.backend)
                _write(check.model_dump())
                return 0 if check.runnable else 1
            record = runner.start(chosen, _inputs(args), args.backend)
            if record.status == "queued":
                record = runner.execute(chosen, record)
            folder = runner.runs.folder(record.run_id)
            _write(
                {
                    **record.model_dump(exclude={"inputs"}),
                    "folder": str(folder),
                    "open": str(folder / record.files["deliverable"])
                    if "deliverable" in record.files
                    else str(folder / record.files["context_pack"])
                    if "context_pack" in record.files
                    else None,
                }
            )
            return 0 if record.status == "finished" else 1
        runner_root = _root(args) / "analyst"
        if action == "runs":
            from src.modules.analyst.runs import RunStore

            _write(
                [
                    r.model_dump(
                        include={
                            "run_id",
                            "card_id",
                            "backend",
                            "status",
                            "created_at",
                            "cost_usd",
                            "message",
                        }
                    )
                    for r in RunStore(runner_root).all()[: args.limit]
                ]
            )
            return 0
        if action == "policy":
            from src.modules.analyst.policy import DataClass, DataPolicy

            policy = DataPolicy(runner_root)
            if not args.project:
                sys.stderr.write("--project is required\n")
                return 2
            if args.data_class:
                policy.set(args.project, cast(DataClass, args.data_class))
            _write({"project": args.project, "data_class": policy.data_class(args.project)})
            return 0
        if action == "spend":
            from src.modules.analyst.policy import SpendLog
            from src.modules.analyst.runs import AnalystSettings

            log = SpendLog(runner_root)
            now = datetime.now(UTC)
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            settings = AnalystSettings.from_env()
            _write(
                {
                    "claude_enabled": settings.enabled,
                    "model": settings.model,
                    "today_usd": log.spent(since=day),
                    "this_month_usd": log.spent(since=day.replace(day=1)),
                    "caps_usd": {
                        "run": settings.run_cap_usd,
                        "day": settings.daily_cap_usd,
                        "month": settings.monthly_cap_usd or "unset (Claude runs refused)",
                    },
                    "calls": len(log.entries()),
                }
            )
            return 0
    except (KeyError, ValueError) as exc:
        sys.stderr.write(f"{exc.args[0] if exc.args else exc}\n")
        return 2
    sys.stderr.write(f"unknown action {action!r}\n")
    return 2


def register(subparsers: Any) -> None:
    """Add `analyze` to the CLI."""
    parser = subparsers.add_parser(
        "analyze", help="Senior Analyst Action Cards: list, preflight and run them"
    )
    parser.add_argument(
        "action", choices=["cards", "show", "preflight", "run", "runs", "policy", "spend"]
    )
    parser.add_argument("card", nargs="?", default=None, help="card id (show, preflight, run)")
    parser.add_argument("--project", default=None, help="project id")
    parser.add_argument(
        "--set",
        action="append",
        default=None,
        metavar="NAME=VALUE",
        help="a card input; lists are comma-separated (repeatable)",
    )
    parser.add_argument("--backend", choices=["pack", "claude"], default="pack")
    parser.add_argument("--module", default=None, help="cards: only this module")
    parser.add_argument("--limit", type=int, default=20, help="runs: how many")
    parser.add_argument(
        "--data-class",
        choices=["public", "client_confidential"],
        default=None,
        help="policy: set the project's data class",
    )
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_analyze)
