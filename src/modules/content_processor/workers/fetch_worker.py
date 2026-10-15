"""Fetch worker (Stage 1a): T1 acquisition with honest outcomes.

Per task: robots policy, egress check, fetch, evidence write, block
classification, host governor update, resource upsert, events, then the task
is completed or re-queued. The worker never raises out of `process()`; any
unexpected exception becomes a retried task and, at max attempts, a FAILED
resource with the error preserved.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.core.logger import get_logger
from src.integrations.evidence_store import EvidenceStore
from src.integrations.http_client import FetchResult, HttpFetcher
from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.contracts import (
    BlockVerdict,
    RedirectHop,
    Resource,
    RunEvent,
    RunScope,
    Task,
)
from src.modules.content_processor.enums import (
    AccessTier,
    AuthorizationKind,
    BlockKind,
    EntityStatus,
    RedirectKind,
    RobotsPolicy,
    TaskKind,
)
from src.modules.content_processor.governor.block_classifier import classify_response
from src.modules.content_processor.governor.escalation import ActionKind
from src.modules.content_processor.governor.gates import detect_gates
from src.modules.content_processor.governor.robots import evaluate_robots
from src.modules.content_processor.ledger.keys import task_key
from src.modules.content_processor.verify.gaps import gap_for_gate
from src.modules.content_processor.workers.ports import LedgerPort, RobotsCache

__all__ = ["FetchWorker", "user_agent_for"]

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


def _resource_id(run_id: str, schedule_key: str) -> str:
    return "res-" + hashlib.sha256(f"{run_id}\x1f{schedule_key}".encode()).hexdigest()[:16]


class FetchWorker:
    """Leases FETCH tasks and acquires them at tier T1."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        fetcher: HttpFetcher,
        evidence: EvidenceStore,
        scope: RunScope,
        run_id: str,
        worker_id: str,
    ) -> None:
        """Bind the worker to one run."""
        self._ledger = ledger
        self._fetcher = fetcher
        self._evidence = evidence
        self._scope = scope
        self._run_id = run_id
        self._worker_id = worker_id

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
            task = self._ledger.lease(self._worker_id, [TaskKind.FETCH])
            if task is None:
                if self._ledger.pending_count([TaskKind.FETCH]) == 0:
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

        robots = await self._robots_for(host, requested_url)
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
        result = await self._fetcher.fetch(requested_url)
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

        evidence_ref = self._evidence.put(result.body, result.sniffed_mime)
        self._fill_acquisition(resource, result)
        resource.payload_sha256 = evidence_ref.sha256
        block = classify_response(
            status=result.http_status or 0,
            headers=result.headers,
            body_prefix=result.body[:65536],
            requested_mime_family=self._expected_mime(requested_url),
            final_url=result.final_url,
        )
        resource.block_verdict = block
        resource.acquired_at = datetime.now(UTC)
        # The tier that produced these bytes. The host may already *want* a higher
        # tier; until a fetcher for it exists, that is a pending escalation, not a fact.
        resource.acquired_at_tier = AccessTier(self._fetcher.tier_name)

        if block.kind is BlockKind.NONE:
            if result.truncated or result.limit_hit:
                reason = "limit_hit" if result.limit_hit else "truncated_body"
                host_state.record_success()
                self._ledger.save_host_state(tenant, host_state)
                self._upsert(task, resource, EntityStatus.PARTIAL, reason)
            else:
                host_state.record_success()
                self._ledger.save_host_state(tenant, host_state)
                if result.sniffed_mime == "text/html":
                    self._extract_source(task, resource, result, evidence_ref.sha256)
                else:
                    self._upsert(
                        task, resource, EntityStatus.IN_PROGRESS, "acquired:awaiting_extraction"
                    )
            self._ledger.complete(task.id or 0, self._worker_id)
            return

        self._handle_block(task, resource, block, result, host_state)

    def _extract_source(
        self, task: Task, resource: Resource, result: FetchResult, sha256: str
    ) -> None:
        """Run the source channel on an acquired HTML payload (Stage 1b)."""
        extraction = extract_html_source(
            result.body,
            base_url=result.final_url,
            evidence_id=f"evidence:{sha256}",
            resource_id=resource.resource_id,
            state_id=f"{self._run_id}|source",
        )
        self._ledger.upsert_blocks(resource.resource_id, extraction.blocks)
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
        self._ledger.enqueue_followups(
            [
                Task(
                    run_id=self._run_id,
                    kind=TaskKind.RENDER,
                    idempotency_key=task_key(self._run_id, TaskKind.RENDER, resource.schedule_key),
                    host=task.host,
                    priority=task.priority,
                    max_attempts=2,
                    payload={
                        "resource_id": resource.resource_id,
                        "requested_url": result.final_url,
                        "schedule_key": resource.schedule_key,
                    },
                )
            ]
        )

    def _handle_block(
        self,
        task: Task,
        resource: Resource,
        block: BlockVerdict,
        result: FetchResult,
        host_state: Any,
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
            f":fetched_at={self._fetcher.tier_name}:host_tier={host_state.tier.value}"
        )
        if action.kind is ActionKind.EXCLUDED:
            self._upsert(task, resource, EntityStatus.EXCLUDED_BY_POLICY, tier_note)
        elif block.kind is BlockKind.SOFT_404:
            self._upsert(task, resource, EntityStatus.EXTRACTED_UNVERIFIED, "soft_404_suspected")
        elif block.kind is BlockKind.WRONG_PAYLOAD:
            self._upsert(task, resource, EntityStatus.BLOCKED, "wrong_payload:" + tier_note)
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

    # -- helpers ------------------------------------------------------------

    async def _robots_for(self, host: str, url: str) -> RobotsCache:
        cached = self._ledger.get_robots(self._scope.tenant_id, host)
        if cached is not None:
            return cached
        scheme = urlsplit(url).scheme or "https"
        robots_url = urlunsplit((scheme, urlsplit(url).netloc, "/robots.txt", "", ""))
        result = await self._fetcher.fetch(robots_url)
        status = result.http_status if result.transport_error is None else None
        body = (
            result.body.decode("utf-8", errors="replace")
            if status is not None and status < 400 and len(result.body) < 512_000
            else None
        )
        cache = RobotsCache(body=body, status=status, fetched_at=datetime.now(UTC))
        if status is not None and status < 500:
            # Only definitive answers are cached; transient failures are retried.
            self._ledger.set_robots(self._scope.tenant_id, host, cache)
        return cache

    def _expected_mime(self, url: str) -> str:
        path = urlsplit(url).path.lower()
        if path.endswith(".pdf"):
            return "application/pdf"
        if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            return "image/*"
        return "text/html"

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
