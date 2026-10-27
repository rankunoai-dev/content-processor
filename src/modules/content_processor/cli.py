"""Command-line entry points.

No database:
    python -m src.modules.content_processor.cli plan   --scope <scope.json>
    python -m src.modules.content_processor.cli dryrun --scope <scope.json> --out exports/x.json

Durable (Postgres via CP_DATABASE_URL in .env):
    ... run    --scope <scope.json> [--run <id>] --out exports/x.json   create + drive + report
    ... start  --scope <scope.json> [--run <id>]                         create run, enqueue only
    ... work   --scope <scope.json> --run <id> [--stages fetch,render]   drive or resume a run
    ... records --scope <scope.json> --run <id> --out exports/x.json     UrlRecords from the ledger

Reports (no database):
    ... report --records exports/x.records.jsonl --out exports/x_reports  R1, R2, review, explorer

Every `--out` also writes the Stage 4 bundle to `<out stem>_reports/` (or `--reports-dir`).

Recrawl: `--baseline <previous .records.jsonl>` (any run) or `--baseline-run <run id>`
(durable) makes fetches conditional; a URL whose bytes or visible text did not change
reuses the previous extraction instead of rendering, exploring and OCR-ing it again.
    ... fetch  --scope <scope.json> --run <id>                           work --stages fetch
    ... status --run <id>                                                task and resource counts

`run` and `work` are resumable: re-running them with the same `--run` id picks
up where a crashed or interrupted run stopped (idempotent tasks, lease reaper).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from src.core.config import get_settings
from src.core.logger import get_logger
from src.integrations.browser_client import BrowserRenderer, RenderLimits
from src.integrations.evidence_store import EvidenceStore, LocalEvidenceStore
from src.integrations.http_client import FetchLimits, HttpFetcher
from src.integrations.impersonated_client import ImpersonatedFetcher, impersonation_available
from src.modules.content_processor.contracts import RunScope
from src.modules.content_processor.enums import AccessTier, tier_rank
from src.modules.content_processor.export.bundle import write_run_reports
from src.modules.content_processor.export.commands import register as register_report_command
from src.modules.content_processor.export.decisions import (
    contradiction_decisions,
    decisions_path,
    read_decisions,
    review_decisions,
)
from src.modules.content_processor.governor.paid import (
    build_paid_fetchers,
    paid_tier_status,
    spend_store,
)
from src.modules.content_processor.governor.paid import register as register_paid_commands
from src.modules.content_processor.governor.spend import approvals_lookup
from src.modules.content_processor.knowledge.commands import register as register_stamp_command
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.ledger.tasks import TaskLedger, new_run_id
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.recrawl import Baseline
from src.modules.content_processor.recrawl.history import UrlHistory, describe
from src.modules.content_processor.scheduler import (
    DurableLedger,
    LazyRenderStage,
    RunExecution,
    RunOptions,
    Stage,
    StageName,
    assemble_run_records,
    execute_run,
    foreign_open_tasks,
    write_run_outputs,
)
from src.modules.content_processor.scheduler.budget import (
    BudgetMeter,
    MeteredEvidenceStore,
    MeteredFetcher,
    MeteredRenderer,
)
from src.modules.content_processor.scheduler.grouping import RunGroups
from src.modules.content_processor.scheduler.reports import badge_summary, records_path_for
from src.modules.content_processor.scope.frontier import FrontierPlan, build_frontier
from src.modules.content_processor.scope.ssrf import EgressDecision, IpClass, check_host
from src.modules.content_processor.workers.doc_worker import DocWorker
from src.modules.content_processor.workers.fetch_worker import FetchWorker, user_agent_for
from src.modules.content_processor.workers.ports import LedgerPort
from src.modules.content_processor.workers.render_worker import RenderWorker

__all__ = ["main"]

_logger = get_logger("content_processor.cli")
TOOL_VERSIONS = {"engine": "0.2.0", "cli": "L6-1"}
ALL_STAGES: tuple[StageName, ...] = ("fetch", "render", "docs")


def load_scope(path: str) -> RunScope:
    """Parse and validate a scope file."""
    return RunScope.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


class EgressGuard:
    """Egress checker that remembers the addresses each check approved (case 93).

    Called like the plain checker (`guard(host) -> (allowed, reason)`); the T1
    fetcher reads `pinned(host)` right after a successful check and connects to
    those addresses, so a DNS answer that changes after the check is never used.

    The browser checks every request, hundreds per page. A lookup that fails
    outright (no answer) for a host approved within `DNS_BLIP_GRACE_S` reuses
    that approval and its pinned addresses: a failure returns no address, so it
    cannot point us anywhere new. Without this, one resolver hiccup aborted a
    page load, opened the host's circuit and held every remaining page of the
    host (live John Hancock run, 2026-10-21). An answer that resolves to a
    non-public address is still denied at once.
    """

    DNS_BLIP_GRACE_S = 300.0

    def __init__(
        self,
        scope: RunScope,
        *,
        check: Callable[[str], EgressDecision] = check_host,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the scope's development-only private allowance."""
        self._allow_private = scope.egress_allow_private
        self._approved: dict[str, list[str]] = {}
        self._approved_at: dict[str, float] = {}
        self._check = check
        self._clock = clock

    def __call__(self, host: str) -> tuple[bool, str]:
        """Check `host`; remember the resolved addresses when it is allowed."""
        key = host.lower()
        decision = self._check(host)
        if decision.allowed:
            self._approve(key, decision.resolved)
            return True, decision.reason
        if self._allow_private and all(
            c in (IpClass.LOOPBACK, IpClass.PRIVATE) for c in decision.classes
        ):
            self._approve(key, decision.resolved)
            return True, "allowed_private_fixture"
        approved_at = self._approved_at.get(key)
        if (
            decision.reason.startswith("dns_failure")
            and key in self._approved
            and approved_at is not None
            and self._clock() - approved_at <= self.DNS_BLIP_GRACE_S
        ):
            return True, "dns_failure_recent_approval"
        self._approved.pop(key, None)
        self._approved_at.pop(key, None)
        return False, decision.reason

    def _approve(self, key: str, resolved: Sequence[str]) -> None:
        self._approved[key] = list(resolved)
        self._approved_at[key] = self._clock()

    def pinned(self, host: str) -> list[str]:
        """Addresses the last check approved for `host` (empty when denied or unchecked)."""
        return list(self._approved.get(host.lower(), []))


def host_checker_for(scope: RunScope) -> EgressGuard:
    """Egress checker honouring the development-only private allowance."""
    return EgressGuard(scope)


def _database_url() -> str:
    url = get_settings().cp_database_url
    if url is None:
        _logger.error("missing_setting", extra={"setting": "CP_DATABASE_URL"})
        sys.exit(2)
    return url.get_secret_value()


def _open_durable_ledger() -> DurableLedger:
    """Postgres ledger; tests replace this factory."""
    return TaskLedger(_database_url())


def _accept_language(scope: RunScope) -> str:
    return f"{scope.locale},{scope.locale.split('-')[0]};q=0.9"


def build_fetchers(scope: RunScope) -> tuple[Any, list[Any]]:
    """T1 fetcher plus T2 when the scope's ceiling allows it and curl_cffi is installed."""
    limits = FetchLimits(max_bytes=min(scope.budgets.max_bytes, 50 * 1024 * 1024))
    guard = host_checker_for(scope)
    t1 = HttpFetcher(
        host_checker=guard,
        pinner=guard.pinned,
        user_agent=user_agent_for(scope),
        accept_language=_accept_language(scope),
        limits=limits,
    )
    escalation: list[Any] = []
    if tier_rank(scope.access_tier_ceiling) >= tier_rank(AccessTier.T2_IMPERSONATED_HTTP) and (
        impersonation_available()
    ):
        escalation.append(
            ImpersonatedFetcher(
                host_checker=host_checker_for(scope),
                accept_language=_accept_language(scope),
                limits=limits,
            )
        )
    return t1, escalation


def build_renderer(scope: RunScope) -> Any:
    """T3 browser configured from the scope (not started)."""
    return BrowserRenderer(
        user_agent=user_agent_for(scope),
        host_checker=host_checker_for(scope),
        locale=scope.locale,
        timezone=scope.timezone,
        viewport=scope.viewport,
        analytics_suppression=scope.analytics_suppression,
        limits=RenderLimits(),
    )


def _parse_stages(raw: str | None, *, no_render: bool, no_docs: bool) -> list[StageName]:
    stages = list(ALL_STAGES)
    if raw:
        wanted = [s.strip() for s in raw.split(",") if s.strip()]
        unknown = sorted(set(wanted) - set(ALL_STAGES))
        if unknown:
            msg = f"unknown stage(s): {', '.join(unknown)}; choose from {', '.join(ALL_STAGES)}"
            raise SystemExit(msg)
        stages = [s for s in ALL_STAGES if s in wanted]
    dropped: set[str] = set()
    if no_render:
        dropped.add("render")
    if no_docs:
        dropped.add("docs")
    kept: list[StageName] = [s for s in stages if s not in dropped]
    return kept


async def _drive(
    ledger: LedgerPort,
    *,
    scope: RunScope,
    run_id: str,
    evidence: EvidenceStore,
    stage_names: Sequence[StageName],
    options: RunOptions,
    worker_prefix: str,
    reap: Any = None,
    baseline: Baseline | None = None,
    group_assets: bool = False,
) -> RunExecution:
    """Build the workers for the requested stages, drive them, close everything.

    Every fetcher, the renderer and the evidence store go through the run's
    budget meter (K9), seeded with the bytes the ledger already recorded for
    this run (a resumed durable run).
    """
    seed = sum(r.payload_bytes or 0 for r in ledger.list_resources(run_id))
    meter = BudgetMeter(scope.budgets, seed_bytes=seed)
    # Grouping reads page bodies for analysis; it uses the raw store so it does not draw on the
    # run's evidence budget.
    grouping = RunGroups(ledger, evidence, run_id=run_id) if group_assets else None
    evidence = MeteredEvidenceStore(evidence, meter)
    raw_t1, raw_escalation = build_fetchers(scope)
    # Paid tiers (T4/T5): only when every switch allows it, and per host only after
    # `approve`; each request also passes the spend guard (governor/spend.py).
    paid = build_paid_fetchers(
        scope,
        run_id=run_id,
        host_checker=host_checker_for(scope),
        user_agent=user_agent_for(scope),
        accept_language=_accept_language(scope),
    )
    raw_escalation = [*raw_escalation, *paid]
    approvals = approvals_lookup(spend_store(), scope.tenant_id) if paid else None
    t1 = MeteredFetcher(raw_t1, meter)
    escalation = [MeteredFetcher(f, meter) for f in raw_escalation]
    stages: dict[StageName, Stage] = {}
    render_stage: LazyRenderStage | None = None
    if "fetch" in stage_names:
        stages["fetch"] = FetchWorker(
            ledger=ledger,
            fetcher=t1,
            evidence=evidence,
            scope=scope,
            run_id=run_id,
            worker_id=f"{worker_prefix}-fetch",
            escalation_fetchers=escalation,
            baseline=baseline,
            approvals=approvals,
            browser_handoff="render" in stage_names,
        )
    if "render" in stage_names:
        renderer = MeteredRenderer(build_renderer(scope), meter)

        def make_render_worker() -> Stage:
            return RenderWorker(
                ledger=ledger,
                renderer=renderer,
                evidence=evidence,
                scope=scope,
                run_id=run_id,
                worker_id=f"{worker_prefix}-render",
                asset_store_for=grouping.asset_store_for if grouping is not None else None,
            )

        render_stage = LazyRenderStage(ledger, renderer, make_render_worker, run_id=run_id)
        stages["render"] = render_stage
    if "docs" in stage_names:
        stages["docs"] = DocWorker(
            ledger=ledger,
            evidence=evidence,
            run_id=run_id,
            worker_id=f"{worker_prefix}-docs",
            ocr_languages=scope.ocr_languages,
            locale=scope.locale,
        )
    try:
        return await execute_run(
            ledger,
            run_id=run_id,
            stages=stages,
            options=options,
            reap=reap,
            meter=meter,
            grouping=grouping,
        )
    finally:
        if render_stage is not None:
            await render_stage.close()
        for fetcher in (t1, *escalation):
            await fetcher.aclose()


def _baseline_from_file(args: argparse.Namespace) -> Baseline | None:
    path = getattr(args, "baseline", None)
    return Baseline.from_jsonl(Path(path)) if path else None


def _history_for(scope: RunScope, args: argparse.Namespace) -> UrlHistory | None:
    """The tenant's URL history, unless `--no-history`."""
    if getattr(args, "no_history", False):
        return None
    return UrlHistory(get_settings().cp_url_history_root, scope.tenant_id)


def _baseline_from_history(
    history: UrlHistory | None, plan: FrontierPlan
) -> tuple[Baseline | None, str | None]:
    """Earlier captures of this run's URLs from the history (any earlier run)."""
    if history is None:
        return None, None
    baseline = history.baseline_for(e.requested_url for e in plan.entries)
    return (baseline, "history") if len(baseline) else (None, None)


def _baseline_info(baseline: Baseline | None, source: str | None) -> dict[str, Any] | None:
    if baseline is None:
        return None
    return {"source": source, "urls": len(baseline), "not_reusable": baseline.skipped}


def _options(args: argparse.Namespace, stages: list[StageName]) -> RunOptions:
    return RunOptions(
        stages=stages,
        max_tasks_per_stage=getattr(args, "max_tasks", None),
        max_rounds=getattr(args, "max_rounds", 10),
        max_wait_s=getattr(args, "max_wait", 360.0),
    )


def _finish(
    *,
    reader: LedgerPort,
    run_id: str,
    plan: FrontierPlan,
    evidence: EvidenceStore,
    stages: Sequence[StageName],
    report: dict[str, Any],
    out: str | None,
    reports_dir: str | None = None,
    history: UrlHistory | None = None,
) -> dict[str, Any]:
    """Assemble UrlRecords, write outputs and the Stage 4 bundle, return the report.

    `--out` writes the run report, full records, content and review queue next
    to it, plus the Stage 4 bundle in `<out stem>_reports/` unless
    `--reports-dir` names another directory. `--reports-dir` alone writes only
    the bundle. With a URL history, the run's captures are remembered in it.
    """
    records = assemble_run_records(
        reader,
        run_id=run_id,
        plan=plan,
        evidence=evidence,
        stages_that_will_run=stages,
        tool_versions=TOOL_VERSIONS,
    )
    full: dict[str, Any] = {
        **report,
        "verdicts": dict(Counter(r.badge.value for r in records)),
        "changes": dict(
            Counter(r.change_status.value if r.change_status else "not_compared" for r in records)
        ),
        "url_records": badge_summary(records),
    }
    bundle_dir: Path | None = Path(reports_dir) if reports_dir else None
    if bundle_dir is None and out:
        bundle_dir = Path(out).parent / f"{Path(out).stem}_reports"
    if bundle_dir is not None:
        run_name = Path(out).stem if out else None
        # Decisions saved in the analyst web app for this run name are carried forward.
        saved = (
            read_decisions(decisions_path(Path(out).parent, run_name)) if out and run_name else []
        )
        manifest = write_run_reports(
            records,
            bundle_dir,
            run_id=run_id,
            run_name=run_name,
            decisions=contradiction_decisions(saved),
            review_decisions=review_decisions(saved),
        )
        full["reports"] = {"dir": str(bundle_dir), "manifest": manifest}
    if out:
        full["records"] = {"path": str(records_path_for(Path(out))), "count": len(records)}
        outputs = write_run_outputs(
            Path(out), reader=reader, run_id=run_id, records=records, report=full
        )
        full["outputs"] = outputs.model_dump()
    if history is not None:
        remembered = history.remember(records, run_name=Path(out).stem if out else None)
        full["history"] = {"remembered": remembered, "known_urls": len(history)}
    return full


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")


# -- commands -----------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    """Print the frontier plan without touching a database."""
    scope = load_scope(args.scope)
    plan = build_frontier(scope)
    _emit(
        {
            "requested": plan.requested_count,
            "scheduled": len(plan.entries),
            "excluded": len(plan.excluded),
            "hosts": plan.hosts,
            "priorities": dict(Counter(int(e.priority) for e in plan.entries)),
            "excluded_reasons": dict(Counter(x.reason for x in plan.excluded)),
            "scope_hash": scope.scope_hash(),
        }
    )
    return 0


async def _dryrun(args: argparse.Namespace) -> int:
    scope = load_scope(args.scope)
    plan = build_frontier(scope)
    run_id = f"dry-{new_run_id()}"
    ledger = MemoryLedger(ceiling=scope.access_tier_ceiling)
    ledger.enqueue(fetch_tasks_for(run_id, plan))
    evidence = LocalEvidenceStore(get_settings().cp_evidence_root, scope.tenant_id)
    stages = _parse_stages(args.stages, no_render=args.no_render, no_docs=args.no_docs)
    history = _history_for(scope, args)
    baseline = _baseline_from_file(args)
    baseline_source = getattr(args, "baseline", None)
    if baseline is None:
        baseline, baseline_source = _baseline_from_history(history, plan)
    execution = await _drive(
        ledger,
        scope=scope,
        run_id=run_id,
        evidence=evidence,
        stage_names=stages,
        options=_options(args, stages),
        worker_prefix="dry",
        baseline=baseline,
        group_assets=getattr(args, "group_assets", False),
    )
    report = {
        "run_id": run_id,
        "mode": "dryrun_memory_ledger",
        "paid_tiers": paid_tier_status(scope),
        "baseline": _baseline_info(baseline, baseline_source),
        "scheduled": len(plan.entries),
        "excluded_at_plan": [x.model_dump() for x in plan.excluded],
        "execution": execution.model_dump(),
        **ledger.report(),
    }
    _emit(
        _finish(
            reader=ledger,
            run_id=run_id,
            plan=plan,
            evidence=evidence,
            stages=stages,
            report=report,
            out=args.out,
            reports_dir=args.reports_dir,
            history=history,
        )
    )
    return 0


def cmd_dryrun(args: argparse.Namespace) -> int:
    """Run a scope end to end with an in-memory ledger and write the reports."""
    return asyncio.run(_dryrun(args))


def cmd_start(args: argparse.Namespace) -> int:
    """Create a run, build the frontier and enqueue FETCH tasks (no workers)."""
    scope = load_scope(args.scope)
    ledger = _open_durable_ledger()
    try:
        ledger.create_schema()
        run_id = args.run or new_run_id()
        ledger.create_run(run_id, scope, TOOL_VERSIONS)
        plan = build_frontier(scope)
        inserted = ledger.enqueue(scope.tenant_id, fetch_tasks_for(run_id, plan))
        _emit(
            {
                "run_id": run_id,
                "scheduled": len(plan.entries),
                "enqueued": inserted,
                "excluded": [x.model_dump() for x in plan.excluded],
            }
        )
    finally:
        ledger.close()
    return 0


async def _durable(args: argparse.Namespace, *, create: bool) -> int:
    scope = load_scope(args.scope)
    plan = build_frontier(scope)
    ledger = _open_durable_ledger()
    try:
        if create:
            ledger.create_schema()
        run_id = args.run or new_run_id()
        # Leases are run-scoped (CR-007): other open runs are reported, not refused.
        concurrent = foreign_open_tasks(ledger, run_id)
        enqueued = 0
        if create:
            ledger.create_run(run_id, scope, TOOL_VERSIONS)
            enqueued = ledger.enqueue(scope.tenant_id, fetch_tasks_for(run_id, plan))
        evidence = LocalEvidenceStore(get_settings().cp_evidence_root, scope.tenant_id)
        stages = _parse_stages(
            getattr(args, "stages", None),
            no_render=getattr(args, "no_render", False),
            no_docs=getattr(args, "no_docs", False),
        )
        baseline = _baseline_from_file(args)
        baseline_source = getattr(args, "baseline", None)
        prior_run = getattr(args, "baseline_run", None) or scope.recrawl_of_run_id
        if baseline is None and prior_run:
            baseline = Baseline(
                assemble_run_records(
                    ledger,
                    run_id=prior_run,
                    plan=None,
                    evidence=evidence,
                    stages_that_will_run=list(ALL_STAGES),
                )
            )
            baseline_source = f"run:{prior_run}"
        history = _history_for(scope, args)
        if baseline is None:
            baseline, baseline_source = _baseline_from_history(history, plan)
        execution = await _drive(
            ledger,
            scope=scope,
            run_id=run_id,
            evidence=evidence,
            stage_names=stages,
            options=_options(args, stages),
            worker_prefix=args.worker_id,
            reap=ledger.reap,
            baseline=baseline,
            group_assets=getattr(args, "group_assets", False),
        )
        report = {
            "run_id": run_id,
            "mode": "durable_postgres",
            "paid_tiers": paid_tier_status(scope),
            "baseline": _baseline_info(baseline, baseline_source),
            "scheduled": len(plan.entries),
            "enqueued": enqueued,
            "execution": execution.model_dump(),
            "tasks": ledger.task_counts(run_id),
            "concurrent_open_tasks": concurrent,
        }
        _emit(
            _finish(
                reader=ledger,
                run_id=run_id,
                plan=plan,
                evidence=evidence,
                stages=stages,
                report=report,
                out=getattr(args, "out", None),
                reports_dir=getattr(args, "reports_dir", None),
                history=history,
            )
        )
    finally:
        ledger.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Create (or resume) a durable run, drive it to completion and write reports."""
    return asyncio.run(_durable(args, create=True))


def cmd_work(args: argparse.Namespace) -> int:
    """Drive an existing durable run (resume after a crash or interruption)."""
    if args.run is None:
        raise SystemExit("work needs --run")
    return asyncio.run(_durable(args, create=False))


def cmd_fetch(args: argparse.Namespace) -> int:
    """Compatibility alias: work --stages fetch."""
    args.stages = "fetch"
    return cmd_work(args)


def cmd_records(args: argparse.Namespace) -> int:
    """Assemble UrlRecords for a durable run from the ledger, without running workers."""
    scope = load_scope(args.scope)
    plan = build_frontier(scope)
    ledger = _open_durable_ledger()
    try:
        evidence = LocalEvidenceStore(get_settings().cp_evidence_root, scope.tenant_id)
        report = {
            "run_id": args.run,
            "mode": "report_only",
            "tasks": ledger.task_counts(args.run),
        }
        _emit(
            _finish(
                reader=ledger,
                run_id=args.run,
                plan=plan,
                evidence=evidence,
                stages=list(ALL_STAGES),
                report=report,
                out=args.out,
                reports_dir=args.reports_dir,
            )
        )
    finally:
        ledger.close()
    return 0


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
    _emit(payload)
    return 0


def _add_drive_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stages", default=None, help="comma list of fetch,render,docs")
    p.add_argument("--no-render", action="store_true", help="skip the T3 render stage")
    p.add_argument("--no-docs", action="store_true", help="skip the document/OCR stage")
    p.add_argument(
        "--group-assets",
        action="store_true",
        help="group pages by template and share static assets within each group",
    )
    p.add_argument("--max-tasks", type=int, default=None, help="per stage, single round")
    p.add_argument("--max-rounds", type=int, default=10)
    p.add_argument(
        "--max-wait",
        type=float,
        default=360.0,
        help="seconds to wait for held tasks (default covers the render retry schedule)",
    )
    p.add_argument("--reports-dir", default=None, help="write the Stage 4 bundle here")
    p.add_argument(
        "--baseline", default=None, help="previous run's .records.jsonl: reuse unchanged pages"
    )
    p.add_argument(
        "--no-history",
        action="store_true",
        help="neither compare with nor update the URL history (every page scraped afresh)",
    )


def cmd_history(args: argparse.Namespace) -> int:
    """Before scraping: which of these URLs were scraped before, when, and site dates."""
    history = UrlHistory(get_settings().cp_url_history_root, args.tenant)
    urls = [
        u.strip() for u in Path(args.urls).read_text(encoding="utf-8").splitlines() if u.strip()
    ]
    rows = [{"URL": u, **describe(history.lookup(u))} for u in dict.fromkeys(urls)]
    if args.out:
        with Path(args.out).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["URL"])
            writer.writeheader()
            writer.writerows(rows)
    _emit(
        {
            "urls": len(rows),
            "scraped_before": sum(1 for r in rows if r["Scraped before"] == "Yes"),
            "new": sum(1 for r in rows if r["Scraped before"] == "No"),
            "known_urls": len(history),
            "out": args.out,
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch a CLI command."""
    parser = argparse.ArgumentParser(prog="content-processor")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="plan the frontier (no database)")
    p_plan.add_argument("--scope", required=True)
    p_plan.set_defaults(func=cmd_plan)

    p_dry = sub.add_parser("dryrun", help="run end to end with an in-memory ledger")
    p_dry.add_argument("--scope", required=True)
    p_dry.add_argument("--out", default=None)
    _add_drive_options(p_dry)
    p_dry.set_defaults(func=cmd_dryrun)

    p_run = sub.add_parser("run", help="durable: create or resume a run, drive it, report")
    p_run.add_argument("--scope", required=True)
    p_run.add_argument("--run", default=None)
    p_run.add_argument("--out", default=None)
    p_run.add_argument("--worker-id", default="w1")
    p_run.add_argument("--baseline-run", default=None, help="durable run id to compare with")
    _add_drive_options(p_run)
    p_run.set_defaults(func=cmd_run)

    p_hist = sub.add_parser("history", help="which URLs were scraped before, and when")
    p_hist.add_argument("--urls", required=True, help="text file, one URL per line")
    p_hist.add_argument("--tenant", default="rankuno-dev")
    p_hist.add_argument("--out", default=None, help="write a CSV with one row per URL")
    p_hist.set_defaults(func=cmd_history)

    p_start = sub.add_parser("start", help="durable: create a run and enqueue fetches only")
    p_start.add_argument("--scope", required=True)
    p_start.add_argument("--run", default=None)
    p_start.set_defaults(func=cmd_start)

    p_work = sub.add_parser("work", help="durable: drive or resume an existing run")
    p_work.add_argument("--scope", required=True)
    p_work.add_argument("--run", required=True)
    p_work.add_argument("--out", default=None)
    p_work.add_argument("--worker-id", default="w1")
    p_work.add_argument("--baseline-run", default=None, help="durable run id to compare with")
    _add_drive_options(p_work)
    p_work.set_defaults(func=cmd_work)

    p_fetch = sub.add_parser("fetch", help="durable: work --stages fetch")
    p_fetch.add_argument("--scope", required=True)
    p_fetch.add_argument("--run", required=True)
    p_fetch.add_argument("--worker-id", default="fetch-1")
    p_fetch.add_argument("--max-tasks", type=int, default=None)
    p_fetch.set_defaults(func=cmd_fetch, no_render=True, no_docs=True, out=None, reports_dir=None)

    p_records = sub.add_parser("records", help="durable: UrlRecords for a run, no workers")
    p_records.add_argument("--scope", required=True)
    p_records.add_argument("--run", required=True)
    p_records.add_argument("--out", default=None)
    p_records.add_argument("--reports-dir", default=None, help="write the Stage 4 bundle here")
    p_records.set_defaults(func=cmd_records)

    register_report_command(sub)
    register_paid_commands(sub)
    register_stamp_command(sub)

    p_status = sub.add_parser("status", help="durable: task and resource counts")
    p_status.add_argument("--run", required=True)
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
