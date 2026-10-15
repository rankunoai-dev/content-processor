"""Render worker (Stage 1c): T3 rendered channel and source-vs-render verification.

Per RENDER task: governor gate, render, evidence (DOM, visible text, screenshot),
block classification on the rendered document, rendered extraction with the
computed-visibility annotations, reconciliation against the source blocks,
block persistence, summary, events. Never raises out of `process()`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from src.core.logger import get_logger
from src.integrations.browser_client import RenderResult
from src.integrations.evidence_store import EvidenceStore
from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.contracts import Resource, RunEvent, RunScope, Task
from src.modules.content_processor.enums import (
    AccessTier,
    BlockKind,
    EntityStatus,
    TaskKind,
)
from src.modules.content_processor.governor.block_classifier import classify_response
from src.modules.content_processor.governor.escalation import ActionKind
from src.modules.content_processor.verify.reconcile import reconcile_source_vs_rendered
from src.modules.content_processor.workers.ports import LedgerPort

__all__ = ["RenderWorker", "Renderer"]

_logger = get_logger("content_processor.render_worker")


class Renderer(Protocol):
    """What the worker needs from a browser (so tests can fake it)."""

    tier_name: str

    async def render(self, url: str) -> RenderResult:
        """Render one URL."""
        ...


class RenderWorker:
    """Leases RENDER tasks and verifies pages at tier T3."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        renderer: Renderer,
        evidence: EvidenceStore,
        scope: RunScope,
        run_id: str,
        worker_id: str,
    ) -> None:
        """Bind the worker to one run."""
        self._ledger = ledger
        self._renderer = renderer
        self._evidence = evidence
        self._scope = scope
        self._run_id = run_id
        self._worker_id = worker_id

    async def run_until_idle(
        self,
        *,
        max_tasks: int | None = None,
        idle_polls: int = 3,
        poll_s: float = 1.0,
        max_wait_s: float = 300.0,
    ) -> int:
        """Process RENDER tasks until none remain or none become ready in time."""
        processed = 0
        idle = 0
        waited = 0.0
        while max_tasks is None or processed < max_tasks:
            task = self._ledger.lease(self._worker_id, [TaskKind.RENDER])
            if task is None:
                if self._ledger.pending_count([TaskKind.RENDER]) == 0:
                    idle += 1
                    if idle >= idle_polls:
                        break
                elif waited >= max_wait_s:
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
        """Handle one leased RENDER task. Never raises."""
        try:
            await self._process_inner(task)
        except Exception as exc:  # noqa: BLE001 - boundary
            _logger.exception("render_task_crashed", extra={"task_id": task.id})
            self._ledger.fail(task.id or 0, self._worker_id, f"worker_exception:{exc}", retry=True)

    async def _process_inner(self, task: Task) -> None:
        resource_id = str(task.payload["resource_id"])
        url = str(task.payload["requested_url"])
        resource = self._ledger.get_resource(resource_id)
        if resource is None:
            self._ledger.fail(task.id or 0, self._worker_id, "resource_missing", retry=False)
            return
        tenant = self._scope.tenant_id
        host_state = self._ledger.load_host_state(tenant, task.host)
        gate = host_state.can_attempt()
        if gate.kind is ActionKind.CIRCUIT_OPEN:
            self._ledger.fail(
                task.id or 0,
                self._worker_id,
                "circuit_open",
                retry=True,
                not_before=gate.not_before,
            )
            return

        self._ledger.heartbeat(task.id or 0, self._worker_id)
        result = await self._renderer.render(url)
        if not result.rendered:
            action = host_state.record_transient_failure()
            self._ledger.save_host_state(tenant, host_state)
            state = self._ledger.fail(
                task.id or 0,
                self._worker_id,
                f"render:{result.transport_error}",
                retry=True,
                not_before=action.not_before,
            )
            if state is not None and state.value == "FAILED":
                self._set_status(
                    task, resource, EntityStatus.PARTIAL, f"render_failed:{result.transport_error}"
                )
            return

        dom_ref = self._evidence.put(result.dom_html, "text/html")
        text_ref = self._evidence.put(result.inner_text.encode("utf-8"), "text/plain")
        shot_ref = (
            self._evidence.put(result.screenshot_jpeg, "image/jpeg")
            if result.screenshot_jpeg
            else None
        )
        evidence_ids = [f"evidence:{dom_ref.sha256}", f"evidence:{text_ref.sha256}"]
        if shot_ref is not None:
            evidence_ids.append(f"evidence:{shot_ref.sha256}")

        block = classify_response(
            status=result.http_status or 200,
            headers=result.headers,
            body_prefix=result.dom_html[:65536],
            requested_mime_family="text/html",
            final_url=result.final_url,
        )
        if block.kind not in (BlockKind.NONE, BlockKind.SOFT_404):
            action = host_state.record_block(block.kind)
            self._ledger.save_host_state(tenant, host_state)
            note = (
                f"{block.kind.value.lower()}:{block.vendor or 'unknown'}"
                f":fetched_at={self._renderer.tier_name}"
            )
            if action.kind is ActionKind.EXCLUDED:
                self._set_status(
                    task, resource, EntityStatus.EXCLUDED_BY_POLICY, note, evidence_ids
                )
            else:
                self._set_status(task, resource, EntityStatus.BLOCKED, note, evidence_ids)
                if action.kind is ActionKind.NEEDS_APPROVAL and action.proposed_tier is not None:
                    self._event(
                        "host",
                        task.host,
                        host_state.tier.value,
                        host_state.tier.value,
                        f"paid_tier_available:{action.proposed_tier.value}",
                    )
            self._ledger.complete(task.id or 0, self._worker_id)
            return

        rendered = extract_html_source(
            result.dom_html,
            base_url=result.final_url,
            evidence_id=f"evidence:{dom_ref.sha256}",
            resource_id=resource_id,
            state_id=f"{self._run_id}|rendered",
        )
        source_blocks = [
            b for b in self._ledger.get_blocks(resource_id) if b.state_id.endswith("|source")
        ]
        rendered_blocks, recon = reconcile_source_vs_rendered(
            source_blocks, rendered.blocks, result.inner_text
        )
        self._ledger.upsert_blocks(resource_id, [*source_blocks, *rendered_blocks])

        summary: dict[str, Any] = {
            **recon.model_dump(
                exclude={
                    "verified_block_ids",
                    "unverified_block_ids",
                    "source_only_block_ids",
                    "rendered_only_block_ids",
                }
            ),
            "console_errors": len(result.console_errors),
            "failed_requests": len(result.failed_requests),
            "blocked_requests": len(result.blocked_requests),
            "captured_responses": len(result.captured_responses),
            "settle": result.settle,
            "annotation": result.annotation,
            "hidden_blocks": sum(1 for b in rendered_blocks if b.layer.value == "hidden"),
            "placeholder_suspected": rendered.placeholder_suspected,
            "hops": result.hops,
            "final_url": result.final_url,
            "elapsed_ms": round(result.elapsed_ms),
            "tier": self._renderer.tier_name,
        }
        self._ledger.record_summary(resource_id, "render", summary)

        host_state.record_success()
        self._ledger.save_host_state(tenant, host_state)
        reasons = ["rendered"]
        if recon.fully_verified:
            status = EntityStatus.VERIFIED_EXTRACTED
            reasons.append("all_rendered_blocks_verified")
        else:
            status = EntityStatus.EXTRACTED_UNVERIFIED
            reasons.append(f"unverified_rendered_blocks={recon.unverified_blocks}")
        if recon.rendered_only_keys:
            reasons.append(f"hydrated_blocks={recon.rendered_only_keys}")
        if recon.source_only_keys:
            reasons.append(f"source_only_not_rendered={recon.source_only_keys}")
        if rendered.placeholder_suspected:
            reasons.append("placeholder_content_suspected")
        if block.kind is BlockKind.SOFT_404:
            reasons.append("soft_404_suspected")
        reasons.append("discovery_open")
        self._set_status(task, resource, status, ";".join(reasons), evidence_ids)
        self._ledger.complete(task.id or 0, self._worker_id)

    # -- helpers --------------------------------------------------------------

    def _set_status(
        self,
        task: Task,
        resource: Resource,
        status: EntityStatus,
        reason: str,
        evidence_ids: list[str] | None = None,
    ) -> None:
        old = resource.status.value
        resource.status = status
        resource.status_reason = reason
        if resource.acquired_at_tier is None:
            resource.acquired_at_tier = AccessTier(self._renderer.tier_name)
        self._ledger.upsert_resource(resource, task.host)
        self._ledger.append_event(
            RunEvent(
                run_id=self._run_id,
                entity_type="resource",
                entity_id=resource.resource_id,
                old_state=old,
                new_state=status.value,
                reason=reason,
                attempt=task.attempts,
                evidence_ids=evidence_ids or [],
            )
        )

    def _event(
        self, entity_type: Any, entity_id: str, old: str | None, new: str, reason: str
    ) -> None:
        self._ledger.append_event(
            RunEvent(
                run_id=self._run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                old_state=old,
                new_state=new,
                reason=reason,
                at=datetime.now(UTC),
            )
        )


def retry_at(seconds: float) -> datetime:
    """Helper for tests and callers scheduling a retry."""
    return datetime.now(UTC) + timedelta(seconds=seconds)
