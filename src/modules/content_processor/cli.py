"""Command-line entry points for Stage 1.

    python -m src.modules.content_processor.cli plan   --scope fixtures/scopes/local_fixtures.json
    python -m src.modules.content_processor.cli start  --scope fixtures/scopes/johnhancock.json
    python -m src.modules.content_processor.cli fetch  --run <run_id> --scope <scope.json>
    python -m src.modules.content_processor.cli status --run <run_id>
    python -m src.modules.content_processor.cli dryrun --scope <scope.json> --out <out.json>
    python -m src.modules.content_processor.cli report --records <run.jsonl> --out <dir>

`plan`, `dryrun`, and `report` need no database. The others need `CP_DATABASE_URL` in `.env`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from src.core.config import get_settings
from src.core.logger import get_logger
from src.integrations.browser_client import BrowserRenderer, RenderLimits
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, HttpFetcher
from src.modules.content_processor.contracts import RunScope
from src.modules.content_processor.enums import TaskKind
from src.modules.content_processor.export.bundle import write_run_reports
from src.modules.content_processor.export.commands import register as register_report
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.ledger.tasks import TaskLedger, new_run_id
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.scope.frontier import build_frontier
from src.modules.content_processor.scope.ssrf import IpClass, check_host
from src.modules.content_processor.verify.record import frames_from_evidence, url_records_for_run
from src.modules.content_processor.workers.fetch_worker import FetchWorker, user_agent_for
from src.modules.content_processor.workers.render_worker import RenderWorker

__all__ = [
    "cmd_dryrun",
    "cmd_fetch",
    "cmd_plan",
    "cmd_start",
    "cmd_status",
    "host_checker_for",
    "load_scope",
    "main",
]

_logger = get_logger("content_processor.cli")
TOOL_VERSIONS = {"engine": "0.1.0", "fetch_worker": "1a"}


def load_scope(path: str) -> RunScope:
    """Parse and validate a scope file."""
    return RunScope.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def host_checker_for(scope: RunScope) -> Callable[[str], tuple[bool, str]]:
    """Egress checker honouring the development-only private allowance."""

    def check(host: str) -> tuple[bool, str]:
        decision = check_host(host)
        if decision.allowed:
            return True, decision.reason
        if scope.egress_allow_private and all(
            c in (IpClass.LOOPBACK, IpClass.PRIVATE) for c in decision.classes
        ):
            return True, "allowed_private_fixture"
        return False, decision.reason

    return check


def _database_url() -> str:
    url = get_settings().cp_database_url
    if url is None:
        _logger.error("missing_setting", extra={"setting": "CP_DATABASE_URL"})
        sys.exit(2)
    return url.get_secret_value()


def cmd_plan(args: argparse.Namespace) -> int:
    """Print the frontier plan without touching a database."""
    scope = load_scope(args.scope)
    plan = build_frontier(scope)
    summary = {
        "requested": plan.requested_count,
        "scheduled": len(plan.entries),
        "excluded": len(plan.excluded),
        "hosts": plan.hosts,
        "priorities": dict(Counter(int(e.priority) for e in plan.entries)),
        "excluded_reasons": dict(Counter(x.reason for x in plan.excluded)),
        "scope_hash": scope.scope_hash(),
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Create a run, build the frontier and enqueue FETCH tasks."""
    scope = load_scope(args.scope)
    ledger = TaskLedger(_database_url())
    try:
        ledger.create_schema()
        run_id = args.run or new_run_id()
        ledger.create_run(run_id, scope, TOOL_VERSIONS)
        plan = build_frontier(scope)
        inserted = ledger.enqueue(scope.tenant_id, fetch_tasks_for(run_id, plan))
        sys.stdout.write(
            json.dumps(
                {
                    "run_id": run_id,
                    "scheduled": len(plan.entries),
                    "enqueued": inserted,
                    "excluded": [x.model_dump() for x in plan.excluded],
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        ledger.close()
    return 0


async def _fetch_loop(args: argparse.Namespace) -> int:
    scope = load_scope(args.scope)
    settings = get_settings()
    ledger = TaskLedger(_database_url())
    fetcher = HttpFetcher(
        host_checker=host_checker_for(scope),
        user_agent=user_agent_for(scope),
        accept_language=f"{scope.locale},{scope.locale.split('-')[0]};q=0.9",
        limits=FetchLimits(max_bytes=min(scope.budgets.max_bytes, 50 * 1024 * 1024)),
    )
    evidence = LocalEvidenceStore(settings.cp_evidence_root, scope.tenant_id)
    worker = FetchWorker(
        ledger=ledger,
        fetcher=fetcher,
        evidence=evidence,
        scope=scope,
        run_id=args.run,
        worker_id=args.worker_id,
    )
    try:
        processed = await worker.run_until_idle(max_tasks=args.max_tasks)
        sys.stdout.write(json.dumps({"processed": processed}) + "\n")
    finally:
        await fetcher.aclose()
        ledger.close()
    return 0


async def _dryrun(args: argparse.Namespace) -> int:
    scope = load_scope(args.scope)
    settings = get_settings()
    plan = build_frontier(scope)
    run_id = f"dry-{new_run_id()}"
    ledger = MemoryLedger(ceiling=scope.access_tier_ceiling)
    ledger.enqueue(fetch_tasks_for(run_id, plan))
    fetcher = HttpFetcher(
        host_checker=host_checker_for(scope),
        user_agent=user_agent_for(scope),
        accept_language=f"{scope.locale},{scope.locale.split('-')[0]};q=0.9",
        limits=FetchLimits(max_bytes=min(scope.budgets.max_bytes, 50 * 1024 * 1024)),
    )
    evidence = LocalEvidenceStore(settings.cp_evidence_root, scope.tenant_id)
    worker = FetchWorker(
        ledger=ledger,
        fetcher=fetcher,
        evidence=evidence,
        scope=scope,
        run_id=run_id,
        worker_id="dry-1",
    )
    try:
        processed = await worker.run_until_idle(max_tasks=args.max_tasks, idle_polls=5, poll_s=0.5)
    finally:
        await fetcher.aclose()
    rendered = 0
    if not args.no_render:
        renderer = BrowserRenderer(
            user_agent=user_agent_for(scope),
            host_checker=host_checker_for(scope),
            locale=scope.locale,
            timezone=scope.timezone,
            viewport=scope.viewport,
            analytics_suppression=scope.analytics_suppression,
            limits=RenderLimits(),
        )
        await renderer.start()
        render_worker = RenderWorker(
            ledger=ledger,
            renderer=renderer,
            evidence=evidence,
            scope=scope,
            run_id=run_id,
            worker_id="dry-render-1",
        )
        try:
            rendered = await render_worker.run_until_idle(
                max_tasks=args.max_tasks, idle_polls=5, poll_s=0.5, max_wait_s=60.0
            )
        finally:
            await renderer.stop()
    outstanding_kinds = [TaskKind.FETCH] if args.no_render else [TaskKind.FETCH, TaskKind.RENDER]
    records = url_records_for_run(
        ledger,
        run_id=run_id,
        plan=plan,
        frames_for=frames_from_evidence(evidence),
        outstanding=ledger.outstanding_resource_ids(run_id, outstanding_kinds),
        tool_versions=TOOL_VERSIONS,
    )
    report = {
        "run_id": run_id,
        "mode": "dryrun_memory_ledger",
        "scheduled": len(plan.entries),
        "excluded_at_plan": [x.model_dump() for x in plan.excluded],
        "processed": processed,
        "rendered": rendered,
        "verdicts": dict(Counter(r.badge.value for r in records)),
        **ledger.report(),
    }
    reports_dir = Path(args.reports_dir) if getattr(args, "reports_dir", None) else None
    if reports_dir is not None:
        manifest = write_run_reports(records, reports_dir, run_id=run_id)
        report["reports"] = {"dir": str(reports_dir), "manifest": manifest}
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        records_path = out_path.with_suffix(".records.jsonl")
        with records_path.open("w", encoding="utf-8") as handle:
            for rec in records:
                handle.write(rec.model_dump_json() + "\n")
        report["records"] = {"path": str(records_path), "count": len(records)}
        if reports_dir is None:
            reports_dir = out_path.parent / f"{out_path.stem}_reports"
            manifest = write_run_reports(records, reports_dir, run_id=run_id)
            report["reports"] = {"dir": str(reports_dir), "manifest": manifest}
        out_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    return 0


def cmd_dryrun(args: argparse.Namespace) -> int:
    """Fetch a scope with an in-memory ledger and print a report (no database)."""
    return asyncio.run(_dryrun(args))


def cmd_fetch(args: argparse.Namespace) -> int:
    """Run one fetch worker until the queue is idle."""
    return asyncio.run(_fetch_loop(args))


def cmd_status(args: argparse.Namespace) -> int:
    """Print task and resource counts for a run."""
    ledger = TaskLedger(_database_url())
    try:
        ledger.reap()
        payload = {
            "run_id": args.run,
            "tasks": ledger.task_counts(args.run),
            "resources": ledger.resource_counts(args.run),
        }
    finally:
        ledger.close()
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch a CLI command."""
    parser = argparse.ArgumentParser(prog="content-processor")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="plan the frontier (no database)")
    p_plan.add_argument("--scope", required=True)
    p_plan.set_defaults(func=cmd_plan)

    p_start = sub.add_parser("start", help="create a run and enqueue fetches")
    p_start.add_argument("--scope", required=True)
    p_start.add_argument("--run", default=None)
    p_start.set_defaults(func=cmd_start)

    p_fetch = sub.add_parser("fetch", help="run a fetch worker until idle")
    p_fetch.add_argument("--scope", required=True)
    p_fetch.add_argument("--run", required=True)
    p_fetch.add_argument("--worker-id", default="fetch-1")
    p_fetch.add_argument("--max-tasks", type=int, default=None)
    p_fetch.set_defaults(func=cmd_fetch)

    p_dry = sub.add_parser("dryrun", help="fetch with an in-memory ledger; no database")
    p_dry.add_argument("--scope", required=True)
    p_dry.add_argument("--max-tasks", type=int, default=None)
    p_dry.add_argument("--out", default=None, help="write JSON summary and .records.jsonl")
    p_dry.add_argument("--reports-dir", default=None, help="write Stage 4 bundle to this directory")
    p_dry.add_argument("--no-render", action="store_true", help="skip the T3 render pass")
    p_dry.set_defaults(func=cmd_dryrun)

    p_status = sub.add_parser("status", help="counts for a run")
    p_status.add_argument("--run", required=True)
    p_status.set_defaults(func=cmd_status)

    register_report(sub)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
