"""Fetch worker (Stage 1a): HTTP acquisition (T1, T2) with honest outcomes.

Per task: robots policy, egress check, fetch, evidence write, block
classification, host governor update, resource upsert, events, then the task
is completed or re-queued. The worker never raises out of `process()`; any
unexpected exception becomes a retried task and, at max attempts, a FAILED
resource with the error preserved.

Tiers: each task runs on the highest configured HTTP fetcher at or below the
host's current tier. When a block escalates the host to a tier that has a
fetcher here (T2), the URL is re-queued for that tier instead of ending
BLOCKED; tiers without an HTTP fetcher (T3 browser, paid tiers) keep the
existing outcome.

Follow-ups (A-1): an extracted page's embedded assets and linked downloads are
queued one level deeper with the media type the page promised, so the block
classifier can fail a wrong payload closed (K15).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.core.logger import get_logger
from src.integrations.evidence_store import EvidenceStore
from src.integrations.http_client import Fetcher, FetchResult
from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.contracts import (
    BlockVerdict,
    FollowupRef,
    RedirectHop,
    Resource,
    RunEvent,
    RunScope,
    Task,
    UrlRecord,
)
from src.modules.content_processor.enums import (
    AccessTier,
    AuthorizationKind,
    BlockKind,
    EntityStatus,
    PriorityClass,
    RedirectKind,
    RobotsPolicy,
    TaskKind,
    tier_rank,
)
from src.modules.content_processor.governor.block_classifier import classify_response
from src.modules.content_processor.governor.escalation import Action, ActionKind, HostAccessState
from src.modules.content_processor.governor.gates import detect_gates
from src.modules.content_processor.governor.robots import crawl_delay_for, evaluate_robots
from src.modules.content_processor.governor.spend import Approval, apply_approval
from src.modules.content_processor.ledger.keys import resource_id_for, task_key
from src.modules.content_processor.provenance import stamp_blocks
from src.modules.content_processor.recrawl import (
    Baseline,
    ChangeDecision,
    conditional_headers,
    content_fingerprint,
    decide_change,
    extract_page_dates,
)
from src.modules.content_processor.scope.followups import (
    FollowupCandidate,
    anchor_hints,
    expected_mime_for_name,
    mime_allowed,
    plan_followups,
)
from src.modules.content_processor.scope.normalize import InvalidInputUrl, normalize_url
from src.modules.content_processor.verify.gaps import gap_for_gate, gap_id_for
from src.modules.content_processor.workers.ports import LedgerPort, RobotsCache
from src.modules.content_processor.workers.render_worker import RENDER_MAX_ATTEMPTS

__all__ = ["FetchWorker", "extraction_kind_for", "user_agent_for"]

_logger = get_logger("content_processor.fetch_worker")

BOT_PRODUCT_NAME = "RankunoContentBot"
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_TRANSIENT_BACKOFF_S = (5, 30, 120)


def user_agent_for(scope: RunScope) -> str:
    """Self-identify on authorized sites; look like an ordinary visitor elsewhere."""
    if scope.authorization.kind is AuthorizationKind.OWNER_AUTHORIZED:
        return f"{BOT_PRODUCT_NAME}/1.0 (+https://rankuno.com/bot; client={scope.tenant_id})"
    return _CHROME_UA


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    if raw.strip().isdigit():
        return float(raw.strip())
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def extraction_kind_for(media_type: str) -> TaskKind | None:
    """The extraction task for a sniffed non-HTML type, or None when no adapter reads it."""
    if media_type == "application/pdf":
        return TaskKind.EXTRACT_DOC
    if media_type.startswith("image/"):
        return TaskKind.EXTRACT_IMAGE
    return None


def _resource_id(run_id: str, schedule_key: str) -> str:
    return resource_id_for(run_id, schedule_key)


Expectation = tuple[str, str | None]
"""(expected media type, where the expectation came from; None = default)."""


class FetchWorker:
    """Leases FETCH tasks and acquires them at the host's HTTP tier."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        fetcher: Fetcher,
        evidence: EvidenceStore,
        scope: RunScope,
        run_id: str,
        worker_id: str,
        escalation_fetchers: Sequence[Fetcher] = (),
        baseline: Baseline | None = None,
        approvals: Callable[[str], Approval | None] | None = None,
        browser_handoff: bool = False,
    ) -> None:
        """Bind the worker to one run.

        Args:
            ledger: Ledger port.
            fetcher: Base fetcher (T1); used whenever no higher one applies.
            evidence: Evidence store.
            scope: Run scope.
            run_id: Run id.
            worker_id: Lease owner name.
            escalation_fetchers: Higher HTTP tiers (e.g. an `ImpersonatedFetcher`
                for T2). A host escalated to one of these tiers is fetched with it.
            baseline: Previous run's records. When a URL is in it, the fetch is
                conditional and an unchanged page reuses the previous extraction.
            browser_handoff: The run renders pages (T3). A page whose download met a
                bot check that the browser tier may pass is handed to it instead of
                ending BLOCKED; the browser reports the block if it is challenged too.
            approvals: `host -> active paid-tier approval` (A-8). An approved
                host's ceiling rises to its approved tier (never past the
                scope's); every other host stays at the free tiers.
        """
        self._ledger = ledger
        self._fetcher = fetcher
        self._browser_handoff = browser_handoff
        self._fetchers: dict[AccessTier, Fetcher] = {AccessTier(fetcher.tier_name): fetcher}
        for extra in escalation_fetchers:
            self._fetchers[AccessTier(extra.tier_name)] = extra
        self._evidence = evidence
        self._scope = scope
        self._run_id = run_id
        self._worker_id = worker_id
        # Per-process caches. Durable versions need contract changes (CR-004).
        self._expected_by_key: dict[str, Expectation] = {}
        self._payload_owner: dict[str, str] = {}
        self._baseline = baseline
        self._approvals = approvals
        self._priors: dict[str, UrlRecord] = {}

    # -- loop ---------------------------------------------------------------

    async def run_until_idle(
        self,
        *,
        max_tasks: int | None = None,
        idle_polls: int = 3,
        poll_s: float = 1.0,
        max_wait_s: float = 300.0,
    ) -> int:
        """Process tasks until none remain, or none become ready within `max_wait_s`.

        A task held back by Retry-After, a host delay or an open circuit is
        pending-but-not-ready; the loop keeps polling for it up to `max_wait_s`
        instead of mistaking the hold for an empty queue.
        """
        processed = 0
        idle = 0
        waited = 0.0
        while max_tasks is None or processed < max_tasks:
            task = self._ledger.lease(self._worker_id, [TaskKind.FETCH], run_id=self._run_id)
            if task is None:
                if self._ledger.pending_count([TaskKind.FETCH], run_id=self._run_id) == 0:
                    idle += 1
                    if idle >= idle_polls:
                        break
                elif waited >= max_wait_s:
                    _logger.warning("fetch_loop_gave_up_waiting", extra={"waited_s": waited})
                    break
                waited += poll_s
                await asyncio.sleep(poll_s)
                continue
            idle = 0
            waited = 0.0
            await self.process(task)
            processed += 1
        return processed

    async def process(self, task: Task) -> None:
        """Handle one leased task end to end. Never raises."""
        assert task.id is not None  # noqa: S101 - leased tasks always have ids
        try:
            await self._process_inner(task)
        except Exception as exc:  # noqa: BLE001 - boundary: convert to ledger outcome
            _logger.exception("fetch_task_crashed", extra={"task_id": task.id})
            state = self._ledger.fail(
                task.id, self._worker_id, f"worker_exception:{exc}", retry=True
            )
            if state is not None and state.value == "FAILED":
                self._upsert(
                    task, self._base_resource(task), EntityStatus.FAILED, f"worker_exception:{exc}"
                )

    # -- pipeline -----------------------------------------------------------

    async def _process_inner(self, task: Task) -> None:
        requested_url = str(task.payload["requested_url"])
        host = task.host
        resource = self._base_resource(task)
        tenant = self._scope.tenant_id
        host_state = self._ledger.load_host_state(tenant, host)
        gate = host_state.can_attempt()  # an expired open circuit becomes HALF_OPEN
        if gate.kind is ActionKind.CIRCUIT_OPEN:
            self._ledger.fail(
                task.id or 0,
                self._worker_id,
                "circuit_open",
                retry=True,
                not_before=gate.not_before,
            )
            return
        approval = self._approvals(host) if self._approvals is not None else None
        apply_approval(host_state, approval, self._scope.access_tier_ceiling)
        fetch_tier = host_state.tier
        approved_tier = task.payload.get("approved_tier")
        if approval is not None and isinstance(approved_tier, str):
            # Re-queued after an approval: go straight to the approved tier.
            wanted = AccessTier(approved_tier)
            if tier_rank(host_state.ceiling) >= tier_rank(wanted) > tier_rank(fetch_tier):
                fetch_tier = wanted
        fetcher = self._fetcher_for(fetch_tier)
        depth = int(task.payload.get("depth", 0))
        self._remember_expectation(task)
        if depth > 0:
            self._ledger.record_summary(
                resource.resource_id,
                "discovered_from",
                {
                    "parent_resource_id": task.payload.get("parent_resource_id"),
                    "edge_kind": task.payload.get("edge_kind"),
                    "depth": depth,
                    "expected_mime": task.payload.get("expected_mime"),
                    "expected_mime_source": task.payload.get("expected_mime_source"),
                },
            )

        robots = await self._robots_for(host, requested_url, fetcher)
        verdict = evaluate_robots(
            url=requested_url,
            user_agent_token=BOT_PRODUCT_NAME,
            policy=self._scope.robots_policy or RobotsPolicy.RESPECT,
            robots_body=robots.body,
            robots_status=robots.status,
            exceptions=self._scope.authorization.robots_exceptions,
        )
        if verdict.source == "unreachable_disallow":
            action = host_state.record_transient_failure()
            self._ledger.save_host_state(tenant, host_state)
            self._retry_or_fail(task, resource, "robots_unreachable", action.not_before)
            return
        if not verdict.allowed:
            resource.scope_decision = "excluded_by_policy"
            self._upsert(task, resource, EntityStatus.EXCLUDED_BY_POLICY, verdict.reason)
            self._ledger.complete(task.id or 0, self._worker_id)
            return

        self._ledger.heartbeat(task.id or 0, self._worker_id)
        prior = (
            self._baseline.lookup(resource.schedule_key, requested_url)
            if self._baseline is not None
            else None
        )
        if prior is not None:
            self._priors[resource.resource_id] = prior
        # A capture too old to reuse is still compared, but fetched in full.
        validators = conditional_headers(prior) if self._may_reuse(prior) else {}
        result = await (
            fetcher.fetch(requested_url, headers=validators)
            if validators
            else fetcher.fetch(requested_url)
        )
        resource.final_url = result.final_url
        resource.redirect_chain = [
            RedirectHop(
                kind=RedirectKind(h.kind),
                from_url=h.from_url,
                to_url=h.to_url,
                http_status=h.http_status,
            )
            for h in result.hops
        ]

        if result.spend_refused:
            # A paid tier refused before sending anything (cap, approval, kill switch).
            reason = f"paid_tier_refused:{result.spend_refused}:fetched_at={fetcher.tier_name}"
            self._upsert(task, resource, EntityStatus.BLOCKED, reason)
            self._event("host", host, host_state.tier.value, host_state.tier.value, reason)
            self._ledger.complete(task.id or 0, self._worker_id)
            return

        if result.egress_denied:
            self._upsert(
                task, resource, EntityStatus.FAILED, f"egress_denied:{result.egress_denied}"
            )
            self._ledger.complete(task.id or 0, self._worker_id)
            return

        if result.transport_error:
            action = host_state.record_transient_failure()
            self._ledger.save_host_state(tenant, host_state)
            self._retry_or_fail(
                task, resource, f"transport:{result.transport_error}", action.not_before
            )
            return

        if prior is not None and result.http_status == 304:
            self._fill_acquisition(resource, result)
            resource.acquired_at = datetime.now(UTC)
            resource.acquired_at_tier = AccessTier(fetcher.tier_name)
            decision = decide_change(
                prior, http_status=304, payload_sha256=None, fingerprint=None, modified_at=None
            )
            self._reuse(task, resource, prior, decision, depth)
            self._ledger.complete(task.id or 0, self._worker_id)
            return

        evidence_ref = self._evidence.put(result.body, result.sniffed_mime)
        self._fill_acquisition(resource, result)
        self._apply_page_dates(resource, result)
        resource.payload_sha256 = evidence_ref.sha256
        expected = self._expectation(task, result)
        block = classify_response(
            status=result.http_status or 0,
            headers=result.headers,
            body_prefix=result.body[:65536],
            requested_mime_family=expected[0],
            final_url=result.final_url,
        )
        resource.block_verdict = block
        resource.acquired_at = datetime.now(UTC)
        # The tier that produced these bytes. The host may already *want* a higher
        # tier; until a fetcher for it exists, that is a pending escalation, not a fact.
        resource.acquired_at_tier = AccessTier(fetcher.tier_name)

        if block.kind is BlockKind.NONE:
            host_state.record_success()
            self._ledger.save_host_state(tenant, host_state)
            self._accept(task, resource, result, evidence_ref.sha256, depth)
            self._ledger.complete(task.id or 0, self._worker_id)
            return

        self._handle_block(task, resource, block, result, host_state, fetcher, expected)

    def _accept(
        self, task: Task, resource: Resource, result: FetchResult, sha256: str, depth: int
    ) -> None:
        """A clean payload: decide partial, policy, duplicate, extract or hand off."""
        owner = self._payload_owner.setdefault(sha256, resource.resource_id)
        if result.truncated or result.limit_hit:
            reason = "limit_hit" if result.limit_hit else "truncated_body"
            self._upsert(task, resource, EntityStatus.PARTIAL, reason)
        elif depth > 0 and not mime_allowed(result.sniffed_mime, self._scope.allowed_mime_families):
            resource.scope_decision = "excluded_by_policy"
            self._upsert(
                task,
                resource,
                EntityStatus.EXCLUDED_BY_POLICY,
                f"mime_not_allowed:{result.sniffed_mime}",
            )
        elif depth > 0 and owner != resource.resource_id:
            # Same bytes already acquired under another URL in this run: keep the
            # record and the edge, do the extraction work once (A-1).
            self._upsert(
                task, resource, EntityStatus.EXTRACTED_UNVERIFIED, f"duplicate_payload_of:{owner}"
            )
        elif self._payload_unchanged(task, resource, result, sha256, depth):
            pass  # previous extraction reused; nothing else to do for this URL
        elif result.sniffed_mime == "text/html":
            self._extract_source(task, resource, result, sha256, depth)
        else:
            self._hand_off(task, resource, result, sha256)

    def _hand_off(self, task: Task, resource: Resource, result: FetchResult, sha256: str) -> None:
        """Queue document or image extraction for a non-HTML payload (K17)."""
        kind = extraction_kind_for(result.sniffed_mime)
        if kind is None:
            # No adapter reads this type; say so instead of leaving it in progress.
            self._upsert(
                task, resource, EntityStatus.UNSUPPORTED, f"no_adapter:{result.sniffed_mime}"
            )
            return
        self._upsert(task, resource, EntityStatus.IN_PROGRESS, "acquired:awaiting_extraction")
        self._ledger.enqueue_followups(
            [
                Task(
                    run_id=self._run_id,
                    kind=kind,
                    idempotency_key=task_key(self._run_id, kind, resource.schedule_key),
                    host=task.host,
                    priority=task.priority,
                    max_attempts=2,
                    payload={
                        "resource_id": resource.resource_id,
                        "sha256": sha256,
                        "requested_url": resource.requested_url,
                        "schedule_key": resource.schedule_key,
                    },
                )
            ]
        )

    def _extract_source(
        self, task: Task, resource: Resource, result: FetchResult, sha256: str, depth: int
    ) -> None:
        """Run the source channel on an acquired HTML payload (Stage 1b)."""
        extraction = extract_html_source(
            result.body,
            base_url=result.final_url,
            evidence_id=f"evidence:{sha256}",
            resource_id=resource.resource_id,
            state_id=f"{self._run_id}|source",
        )
        resource.content_fingerprint = content_fingerprint(extraction.blocks)
        prior = self._priors.get(resource.resource_id)
        if prior is not None:
            decision = decide_change(
                prior,
                http_status=result.http_status,
                payload_sha256=sha256,
                fingerprint=resource.content_fingerprint,
                modified_at=resource.page_modified_at,
            )
            self._note_change(resource, prior, decision)
            if decision.reuse and self._reuse_or_note(resource, prior):
                self._reuse(task, resource, prior, decision, depth)
                return
        self._ledger.upsert_blocks(resource.resource_id, stamp_blocks(extraction.blocks, resource))
        reasons = ["source_extracted"]
        if extraction.placeholder_suspected:
            reasons.append("placeholder_content_suspected")
        if extraction.needs_render_hint:
            reasons.append("needs_render")
        reasons.extend(extraction.warnings)
        for finding in detect_gates(result.body):
            gap, _ = gap_for_gate(
                self._run_id, resource.resource_id, finding, [f"evidence:{sha256}"]
            )
            self._ledger.record_gap(gap)
            reasons.append(f"gate:{finding.kind}")
        self._upsert(task, resource, EntityStatus.EXTRACTED_UNVERIFIED, ";".join(reasons))
        plan = plan_followups(
            scope=self._scope,
            parent_url=result.final_url,
            parent_depth=depth,
            links=extraction.links,
            assets=extraction.assets,
            hints=anchor_hints(result.body, result.final_url),
        )
        if plan.followups or plan.skipped:
            self._ledger.record_summary(resource.resource_id, "followups", plan.summary())
        render = Task(
            run_id=self._run_id,
            kind=TaskKind.RENDER,
            idempotency_key=task_key(self._run_id, TaskKind.RENDER, resource.schedule_key),
            host=task.host,
            priority=task.priority,
            max_attempts=RENDER_MAX_ATTEMPTS,
            payload={
                "resource_id": resource.resource_id,
                "requested_url": result.final_url,
                "schedule_key": resource.schedule_key,
            },
        )
        children = [self._followup_task(task, resource, c, depth) for c in plan.followups]
        self._ledger.enqueue_followups([render, *children])

    def _followup_task(
        self, parent_task: Task, parent: Resource, child: FollowupCandidate, depth: int
    ) -> Task:
        """A FETCH one level down that carries what the parent promised."""
        parts = [child.schedule_key]
        if self._expectation_is_informative(child.url, child.expected_mime):
            # The parent knows something the URL does not (K15). A plain key would
            # collapse onto a supplied URL's task and lose it, so key on it too.
            parts.append(f"expect={child.expected_mime}")
            self._expected_by_key.setdefault(
                child.schedule_key,
                (child.expected_mime or "text/html", child.expected_mime_source),
            )
        return Task(
            run_id=self._run_id,
            kind=TaskKind.FETCH,
            idempotency_key=task_key(self._run_id, TaskKind.FETCH, *parts),
            host=child.host,
            priority=child.priority,
            max_attempts=parent_task.max_attempts,
            payload={
                "schedule_key": child.schedule_key,
                "requested_url": child.url,
                "aliases": [],
                "mandatory": False,
                "depth": depth + 1,
                "parent_resource_id": parent.resource_id,
                "edge_kind": child.edge_kind,
                "expected_mime": child.expected_mime,
                "expected_mime_source": child.expected_mime_source,
            },
        )

    # -- freshness and recrawl ------------------------------------------------

    @staticmethod
    def _apply_page_dates(resource: Resource, result: FetchResult) -> None:
        """Record validators and the dates the site declares for this payload."""
        dates = extract_page_dates(
            body=result.body,
            headers=result.headers,
            sniffed_mime=result.sniffed_mime,
            fetched_at=resource.acquired_at or datetime.now(UTC),
        )
        resource.http_etag = dates.http_etag
        resource.http_last_modified = dates.http_last_modified
        resource.page_modified_at = dates.modified_at
        resource.page_modified_source = dates.modified_source
        resource.page_published_at = dates.published_at

    def _note_change(
        self, resource: Resource, prior: UrlRecord | None, decision: ChangeDecision
    ) -> None:
        resource.change_status = decision.status
        resource.change_basis = ";".join([decision.basis, *decision.notes])
        resource.previous_run_id = prior.run_id if prior is not None else None
        self._ledger.record_summary(
            resource.resource_id,
            "recrawl",
            {
                "status": decision.status.value,
                "basis": decision.basis,
                "notes": decision.notes,
                "previous_run_id": resource.previous_run_id,
                "previous_captured_at": prior.captured_at.isoformat() if prior else None,
                "previous_page_modified_at": (
                    prior.page_modified_at.isoformat()
                    if prior is not None and prior.page_modified_at
                    else None
                ),
                "page_modified_at": (
                    resource.page_modified_at.isoformat() if resource.page_modified_at else None
                ),
            },
        )

    def _payload_unchanged(
        self, task: Task, resource: Resource, result: FetchResult, sha256: str, depth: int
    ) -> bool:
        """Reuse the previous extraction when the bytes are identical (any format).

        HTML whose bytes differ is judged later on its visible text, after the
        cheap source extraction; other formats are judged on bytes and dates here.
        """
        if self._baseline is None:
            return False
        prior = self._priors.get(resource.resource_id)
        if prior is None:
            self._note_change(
                resource,
                None,
                decide_change(
                    None, http_status=None, payload_sha256=None, fingerprint=None, modified_at=None
                ),
            )
            return False
        if result.sniffed_mime == "text/html" and sha256 != prior.payload_sha256:
            return False
        decision = decide_change(
            prior,
            http_status=result.http_status,
            payload_sha256=sha256,
            fingerprint=None,
            modified_at=resource.page_modified_at,
        )
        self._note_change(resource, prior, decision)
        if not decision.reuse or not self._reuse_or_note(resource, prior):
            return False
        self._reuse(task, resource, prior, decision, depth)
        return True

    def _may_reuse(self, prior: UrlRecord | None) -> bool:
        return (
            prior is not None and self._baseline is not None and self._baseline.reuse_allowed(prior)
        )

    def _reuse_or_note(self, resource: Resource, prior: UrlRecord) -> bool:
        """True when an unchanged page may reuse `prior`; otherwise say why it is re-extracted."""
        if self._may_reuse(prior):
            return True
        resource.change_basis = f"{resource.change_basis};reuse_not_allowed:capture_too_old"
        return False

    def _reuse(
        self,
        task: Task,
        resource: Resource,
        prior: UrlRecord,
        decision: ChangeDecision,
        depth: int,
    ) -> None:
        """Carry the previous capture forward instead of extracting again.

        Blocks, gaps and summaries are copied under the new resource id with
        their original `extracted_at`; no RENDER, OCR or exploration is queued.
        Children the page pointed to are re-queued so each gets its own check.
        """
        self._note_change(resource, prior, decision)
        origin = prior.reused_from_run_id or prior.run_id
        resource.reused_from_run_id = origin
        resource.payload_sha256 = resource.payload_sha256 or prior.payload_sha256
        if (
            resource.http_status == 304
            or not resource.sniffed_mime
            or (resource.sniffed_mime == "application/x-empty")
        ):
            resource.sniffed_mime = prior.sniffed_mime
        resource.content_fingerprint = resource.content_fingerprint or prior.content_fingerprint
        resource.http_etag = resource.http_etag or prior.http_etag
        resource.http_last_modified = resource.http_last_modified or prior.http_last_modified
        if resource.page_modified_at is None:
            resource.page_modified_at = prior.page_modified_at
            resource.page_modified_source = prior.page_modified_source
        resource.page_published_at = resource.page_published_at or prior.page_published_at

        blocks = []
        for block in prior.blocks:
            old = block.resource_id
            blocks.append(
                block.model_copy(
                    update={
                        "block_id": block.block_id.replace(old, resource.resource_id, 1),
                        "region_id": block.region_id.replace(old, resource.resource_id, 1),
                        "resource_id": resource.resource_id,
                        "dedupe_of": (
                            block.dedupe_of.replace(old, resource.resource_id, 1)
                            if block.dedupe_of
                            else None
                        ),
                        "reused_from_run_id": block.reused_from_run_id or origin,
                    }
                )
            )
        self._ledger.upsert_blocks(
            resource.resource_id,
            stamp_blocks(blocks, resource, extracted_at=prior.captured_at),
        )
        for gap in prior.gaps:
            self._ledger.record_gap(
                gap.model_copy(
                    update={
                        "gap_id": gap_id_for(
                            self._run_id, resource.resource_id, f"reused:{gap.gap_id}"
                        ),
                        "run_id": self._run_id,
                        "resource_id": resource.resource_id,
                    }
                )
            )
        for name, summary in prior.summaries.items():
            if name != "recrawl":
                self._ledger.record_summary(resource.resource_id, name, dict(summary))
        status = prior.resource_status or EntityStatus.EXTRACTED_UNVERIFIED
        reason = f"unchanged:{decision.basis};reused_from={origin}"
        if prior.resource_status_reason:
            reason += f";{prior.resource_status_reason}"
        self._upsert(task, resource, status, reason)
        children = [
            c for c in (self._child_from_ref(ref) for ref in prior.followups) if c is not None
        ]
        if children:
            self._ledger.enqueue_followups(
                [self._followup_task(task, resource, c, depth) for c in children]
            )

    @staticmethod
    def _child_from_ref(ref: FollowupRef) -> FollowupCandidate | None:
        try:
            norm = normalize_url(ref.url)
        except InvalidInputUrl:
            return None
        image = (ref.expected_mime or "").startswith("image/")
        try:
            return FollowupCandidate.model_validate(
                {
                    "url": ref.url,
                    "schedule_key": norm.schedule_key,
                    "host": norm.host,
                    "edge_kind": ref.edge_kind or "links_to",
                    "priority": (PriorityClass.EMBEDDED_ASSET if image else PriorityClass.DOCUMENT),
                    "expected_mime": ref.expected_mime,
                    "expected_mime_source": "url_extension" if ref.expected_mime else None,
                }
            )
        except ValueError:
            return None

    def _handle_block(
        self,
        task: Task,
        resource: Resource,
        block: BlockVerdict,
        result: FetchResult,
        host_state: HostAccessState,
        fetcher: Fetcher,
        expected: Expectation,
    ) -> None:
        tenant = self._scope.tenant_id
        if block.kind is BlockKind.RATE:
            wait = _retry_after_seconds(result.headers) or 30.0
            until = datetime.now(UTC) + timedelta(seconds=wait)
            host_state.record_block(BlockKind.RATE)
            self._ledger.save_host_state(tenant, host_state, retry_after_until=until)
            self._retry_or_fail(task, resource, "rate_limited", until)
            return
        if block.kind is BlockKind.TRANSPORT:
            action = host_state.record_transient_failure()
            self._ledger.save_host_state(tenant, host_state)
            self._retry_or_fail(task, resource, f"http_{result.http_status}", action.not_before)
            return

        action = host_state.record_block(block.kind)
        self._ledger.save_host_state(tenant, host_state)
        tier_note = (
            f"{block.kind.value.lower()}:{block.vendor or 'unknown'}"
            f":fetched_at={fetcher.tier_name}:host_tier={host_state.tier.value}"
        )
        if action.kind is ActionKind.EXCLUDED:
            self._upsert(task, resource, EntityStatus.EXCLUDED_BY_POLICY, tier_note)
        elif block.kind is BlockKind.SOFT_404:
            self._upsert(task, resource, EntityStatus.EXTRACTED_UNVERIFIED, "soft_404_suspected")
        elif block.kind is BlockKind.WRONG_PAYLOAD:
            mime, source = expected
            self._upsert(
                task,
                resource,
                EntityStatus.BLOCKED,
                f"{tier_note}:expected={mime}:from={source or 'default'}",
            )
        elif action.kind is ActionKind.ESCALATE and action.tier in self._fetchers:
            self._requeue_at_tier(task, resource, action, tier_note)
            self._event("host", task.host, None, host_state.tier.value, action.reason)
        elif (
            self._browser_handoff
            and action.kind is ActionKind.ESCALATE
            and action.tier is AccessTier.T3_FIDELITY_BROWSER
            and expected[0] == "text/html"
        ):
            self._hand_to_browser(task, resource, tier_note)
            self._event("host", task.host, None, host_state.tier.value, action.reason)
        else:
            self._upsert(task, resource, EntityStatus.BLOCKED, tier_note)
            if action.kind is ActionKind.NEEDS_APPROVAL and action.proposed_tier is not None:
                self._event(
                    "host",
                    task.host,
                    host_state.tier.value,
                    host_state.tier.value,
                    f"paid_tier_available:{action.proposed_tier.value}",
                )
            elif action.kind is ActionKind.ESCALATE:
                self._event("host", task.host, None, host_state.tier.value, action.reason)
        self._ledger.complete(task.id or 0, self._worker_id)

    def _hand_to_browser(self, task: Task, resource: Resource, note: str) -> None:
        """A bot check met by the download: let the real browser (T3) try the page.

        The page stays BLOCKED until the browser either gets through (it then
        extracts and verifies the page and records T3 as the tier reached) or is
        challenged too (it then records the block at T3).
        """
        resource.acquired_at_tier = None
        self._upsert(task, resource, EntityStatus.BLOCKED, f"{note}:handed_to_browser")
        self._ledger.enqueue_followups(
            [
                Task(
                    run_id=self._run_id,
                    kind=TaskKind.RENDER,
                    idempotency_key=task_key(self._run_id, TaskKind.RENDER, resource.schedule_key),
                    host=task.host,
                    priority=task.priority,
                    max_attempts=RENDER_MAX_ATTEMPTS,
                    payload={
                        "resource_id": resource.resource_id,
                        "requested_url": resource.final_url or str(task.payload["requested_url"]),
                        "schedule_key": resource.schedule_key,
                    },
                )
            ]
        )

    def _requeue_at_tier(self, task: Task, resource: Resource, action: Action, note: str) -> None:
        """The host moved to a tier this worker can fetch at: try the URL again there (A-2)."""
        tier = action.tier
        self._ledger.enqueue_followups(
            [
                Task(
                    run_id=self._run_id,
                    kind=TaskKind.FETCH,
                    idempotency_key=task_key(
                        self._run_id, TaskKind.FETCH, resource.schedule_key, f"tier={tier.value}"
                    ),
                    host=task.host,
                    priority=task.priority,
                    max_attempts=task.max_attempts,
                    not_before=action.not_before,
                    payload={**task.payload, "escalated_to": tier.value},
                )
            ]
        )
        self._upsert(
            task, resource, EntityStatus.RETRY_SCHEDULED, f"escalated_to:{tier.value}:{note}"
        )

    # -- tiers and expectations ---------------------------------------------

    def _fetcher_for(self, host_tier: AccessTier) -> Fetcher:
        """Highest configured HTTP fetcher at or below the host's tier."""
        usable = [t for t in self._fetchers if tier_rank(t) <= tier_rank(host_tier)]
        if not usable:
            return self._fetcher
        return self._fetchers[max(usable, key=tier_rank)]

    @staticmethod
    def _expectation_is_informative(url: str, mime: str | None) -> bool:
        """True when a parent's expected type says more than the URL and the default."""
        return mime is not None and mime != "text/html" and mime != expected_mime_for_name(url)

    def _remember_expectation(self, task: Task) -> None:
        mime = task.payload.get("expected_mime")
        url = str(task.payload["requested_url"])
        if isinstance(mime, str) and self._expectation_is_informative(url, mime):
            source = task.payload.get("expected_mime_source")
            self._expected_by_key.setdefault(
                str(task.payload["schedule_key"]),
                (mime, str(source) if source is not None else None),
            )

    def _expectation(self, task: Task, result: FetchResult) -> Expectation:
        """What these bytes must be: parent's promise, then names, then HTML (K15)."""
        payload_mime = task.payload.get("expected_mime")
        if isinstance(payload_mime, str) and payload_mime:
            source = task.payload.get("expected_mime_source")
            return payload_mime, str(source) if source is not None else None
        remembered = self._expected_by_key.get(str(task.payload["schedule_key"]))
        if remembered is not None:
            return remembered
        if result.content_disposition_name:
            by_name = expected_mime_for_name(result.content_disposition_name)
            if by_name is not None:
                return by_name, "content_disposition"
        by_url = expected_mime_for_name(str(task.payload["requested_url"]))
        if by_url is not None:
            return by_url, "url_extension"
        return "text/html", None

    # -- helpers ------------------------------------------------------------

    async def _robots_for(self, host: str, url: str, fetcher: Fetcher) -> RobotsCache:
        cached = self._ledger.get_robots(self._scope.tenant_id, host)
        if cached is not None:
            return cached
        scheme = urlsplit(url).scheme or "https"
        robots_url = urlunsplit((scheme, urlsplit(url).netloc, "/robots.txt", "", ""))
        result = await fetcher.fetch(robots_url)
        status = result.http_status if result.transport_error is None else None
        body = (
            result.body.decode("utf-8", errors="replace")
            if status is not None and status < 400 and len(result.body) < 512_000
            else None
        )
        cache = RobotsCache(
            body=body,
            status=status,
            fetched_at=datetime.now(UTC),
            crawl_delay_s=crawl_delay_for(body, BOT_PRODUCT_NAME),
        )
        if status is not None and status < 500:
            # Only definitive answers are cached; transient failures are retried.
            self._ledger.set_robots(self._scope.tenant_id, host, cache)
        return cache

    def _base_resource(self, task: Task) -> Resource:
        schedule_key = str(task.payload["schedule_key"])
        return Resource(
            resource_id=_resource_id(self._run_id, schedule_key),
            run_id=self._run_id,
            requested_url=str(task.payload["requested_url"]),
            schedule_key=schedule_key,
            aliases=[str(a) for a in task.payload.get("aliases", [])],
            mandatory=bool(task.payload.get("mandatory", False)),
            attempt=max(1, task.attempts),
        )

    @staticmethod
    def _fill_acquisition(resource: Resource, result: FetchResult) -> None:
        resource.http_status = result.http_status
        resource.declared_mime = result.declared_mime
        resource.sniffed_mime = result.sniffed_mime
        resource.content_disposition_name = result.content_disposition_name
        resource.payload_bytes = result.received_bytes
        leaf = urlsplit(result.final_url).path.rsplit("/", 1)[-1]
        resource.extension_hint = leaf.rsplit(".", 1)[-1].lower() if "." in leaf else None

    def _retry_or_fail(
        self, task: Task, resource: Resource, error: str, not_before: datetime | None
    ) -> None:
        attempt_index = min(max(task.attempts - 1, 0), len(_TRANSIENT_BACKOFF_S) - 1)
        when = not_before or datetime.now(UTC) + timedelta(
            seconds=_TRANSIENT_BACKOFF_S[attempt_index]
        )
        state = self._ledger.fail(task.id or 0, self._worker_id, error, retry=True, not_before=when)
        if state is not None and state.value == "FAILED":
            self._upsert(task, resource, EntityStatus.FAILED, error)
        else:
            self._upsert(task, resource, EntityStatus.RETRY_SCHEDULED, error)

    def _upsert(self, task: Task, resource: Resource, status: EntityStatus, reason: str) -> None:
        old = resource.status.value
        resource.status = status
        resource.status_reason = reason
        self._ledger.upsert_resource(resource, task.host)
        self._event("resource", resource.resource_id, old, status.value, reason, task.attempts)

    def _event(
        self,
        entity_type: Any,
        entity_id: str,
        old: str | None,
        new: str,
        reason: str,
        attempt: int = 0,
    ) -> None:
        self._ledger.append_event(
            RunEvent(
                run_id=self._run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                old_state=old,
                new_state=new,
                reason=reason,
                attempt=attempt,
            )
        )
