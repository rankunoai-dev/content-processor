"""Document worker (Stage 1c): EXTRACT_DOC and EXTRACT_IMAGE tasks.

Per task: load the acquired payload from the evidence store, run the PDF or
image adapter in a child process under a wall-clock limit, OCR scanned pages
and text-bearing images, write blocks and gaps, and set the resource status.
The worker never raises out of `process()`. A parser crash or hang cannot take
the worker down because the adapter runs in a separate, killable process.

Statuses here are extraction outcomes. Nothing is VERIFIED_EXTRACTED at
resource level: verification belongs to the verification lane.
"""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.logger import get_logger
from src.integrations.evidence_store import EvidenceStore
from src.modules.content_processor.adapters.html_source import normalize_text
from src.modules.content_processor.adapters.image import ImageExtraction, extract_image
from src.modules.content_processor.adapters.ocr import (
    OCR_CONFIDENCE_FLOOR,
    installed_packs,
    route_ocr,
    run_ocr,
)
from src.modules.content_processor.adapters.pdf import PdfExtraction, extract_pdf
from src.modules.content_processor.contracts import (
    ContentBlock,
    Gap,
    Provenance,
    Resource,
    RunEvent,
    Task,
)
from src.modules.content_processor.enums import (
    ContentLayer,
    EntityStatus,
    GapType,
    TaskKind,
)
from src.modules.content_processor.provenance import stamp_blocks
from src.modules.content_processor.workers.ports import LedgerPort

__all__ = ["DocWorker"]

_logger = get_logger("content_processor.doc_worker")

TOOL_VERSION = "doc_worker/1.0"
_KINDS = (TaskKind.EXTRACT_DOC, TaskKind.EXTRACT_IMAGE)
_RETRY_BACKOFF_S = (5, 30)
_MAX_PDF_REGION_OCR = 50


def _child_main(conn: Any, fn: Callable[..., Any], kwargs: dict[str, Any]) -> None:
    """Child-process entry point: run `fn`, send back ("ok", result) or ("err", text)."""
    try:
        conn.send(("ok", fn(**kwargs)))
    except Exception as exc:  # noqa: BLE001 - boundary: report instead of dying silently
        conn.send(("err", f"{type(exc).__name__}:{exc}"))
    finally:
        conn.close()


def run_isolated(fn: Callable[..., Any], kwargs: dict[str, Any], *, timeout_s: float) -> Any:
    """Run `fn(**kwargs)` in a spawned child process and return its result.

    Raises TimeoutError when the child exceeds `timeout_s` (it is killed) and
    RuntimeError when the child fails or dies without a result. `fn` must be a
    module-level callable and its arguments and result must be picklable.
    """
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_main, args=(child, fn, kwargs), daemon=True)
    proc.start()
    child.close()
    try:
        if not parent.poll(timeout_s):
            raise TimeoutError(f"extraction_timeout:{timeout_s:.0f}s")
        try:
            kind, value = parent.recv()
        except EOFError as exc:
            raise RuntimeError("extraction_process_died") from exc
        if kind == "err":
            raise RuntimeError(str(value))
        return value
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(timeout=5)
        parent.close()


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class DocWorker:
    """Leases EXTRACT_DOC / EXTRACT_IMAGE tasks and extracts documents and images."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        evidence: EvidenceStore,
        run_id: str,
        worker_id: str,
        ocr_languages: list[str] | None = None,
        timeout_s: float = 120.0,
        max_payload_bytes: int = 100 * 1024 * 1024,
        isolate: bool = True,
        ocr_runner: Any = None,
        osd_runner: Any = None,
        locale: str | None = None,
    ) -> None:
        """Bind the worker to one run. `isolate=False` runs adapters in-process (tests)."""
        self._ledger = ledger
        self._evidence = evidence
        self._run_id = run_id
        self._worker_id = worker_id
        self._ocr_languages = ocr_languages
        self._timeout_s = timeout_s
        self._max_payload_bytes = max_payload_bytes
        self._isolate = isolate
        self._ocr_runner = ocr_runner
        self._osd_runner = osd_runner
        self._locale = locale

    # -- loop ---------------------------------------------------------------

    async def run_until_idle(
        self,
        *,
        max_tasks: int | None = None,
        idle_polls: int = 3,
        poll_s: float = 1.0,
        max_wait_s: float = 300.0,
    ) -> int:
        """Process tasks until none remain or none become ready within `max_wait_s`."""
        processed = 0
        idle = 0
        waited = 0.0
        while max_tasks is None or processed < max_tasks:
            task = self._ledger.lease(self._worker_id, _KINDS, run_id=self._run_id)
            if task is None:
                if self._ledger.pending_count(_KINDS, run_id=self._run_id) == 0:
                    idle += 1
                    if idle >= idle_polls:
                        break
                elif waited >= max_wait_s:
                    _logger.warning("doc_loop_gave_up_waiting", extra={"waited_s": waited})
                    break
                waited += poll_s
                await asyncio.sleep(poll_s)
                continue
            idle = 0
            waited = 0.0
            await asyncio.to_thread(self.process, task)
            processed += 1
        return processed

    def process(self, task: Task) -> None:
        """Handle one leased task end to end. Never raises."""
        assert task.id is not None  # noqa: S101 - leased tasks always have ids
        try:
            self._process_inner(task)
        except Exception as exc:  # noqa: BLE001 - boundary: convert to ledger outcome
            _logger.exception("doc_task_crashed", extra={"task_id": task.id})
            self._retry_or_fail(task, f"worker_exception:{exc}")

    # -- pipeline -----------------------------------------------------------

    def _process_inner(self, task: Task) -> None:
        resource_id = str(task.payload["resource_id"])
        sha256 = str(task.payload["sha256"])
        resource = self._ledger.get_resource(resource_id)
        if resource is None:
            self._ledger.fail(task.id or 0, self._worker_id, "resource_missing", retry=False)
            return
        if not self._evidence.exists(sha256):
            self._finish(task, resource, EntityStatus.FAILED, "evidence_missing")
            return
        payload = self._evidence.get(sha256)
        if len(payload) > self._max_payload_bytes:
            self._gap(
                resource, GapType.LIMIT_HIT, EntityStatus.PARTIAL, "payload_too_large", sha256
            )
            self._finish(task, resource, EntityStatus.PARTIAL, "payload_too_large")
            return

        self._ledger.heartbeat(task.id or 0, self._worker_id)
        evidence_id = f"evidence:{sha256}"
        try:
            if task.kind is TaskKind.EXTRACT_IMAGE:
                self._do_image(task, resource, payload, evidence_id)
            else:
                self._do_pdf(task, resource, payload, evidence_id)
        except TimeoutError as exc:
            self._gap(resource, GapType.LIMIT_HIT, EntityStatus.PARTIAL, str(exc), sha256)
            self._finish(task, resource, EntityStatus.PARTIAL, str(exc))
        except RuntimeError as exc:
            self._retry_or_fail(task, f"extraction_failed:{exc}", resource)

    def _call(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        if self._isolate:
            return run_isolated(fn, kwargs, timeout_s=self._timeout_s)
        return fn(**kwargs)

    def _do_image(self, task: Task, resource: Resource, payload: bytes, evidence_id: str) -> None:
        extraction: ImageExtraction = self._call(
            extract_image,
            image_bytes=payload,
            resource_id=resource.resource_id,
            run_id=self._run_id,
            evidence_id=evidence_id,
            media_type=resource.sniffed_mime or "image/png",
            ocr_languages=self._ocr_languages,
            ocr_runner=self._ocr_runner if not self._isolate else None,
            locale=self._locale,
        )
        self._ledger.upsert_blocks(resource.resource_id, stamp_blocks(extraction.blocks, resource))
        for gap in extraction.gaps:
            self._ledger.record_gap(gap)
        reason = ";".join(["image_extracted", *extraction.warnings])
        self._finish(task, resource, self._resource_status(extraction.status), reason)

    def _do_pdf(self, task: Task, resource: Resource, payload: bytes, evidence_id: str) -> None:
        extraction: PdfExtraction = self._call(
            extract_pdf,
            pdf_bytes=payload,
            resource_id=resource.resource_id,
            run_id=self._run_id,
            evidence_id=evidence_id,
        )
        if extraction.page_count == 0 and not extraction.blocks:
            reason = ";".join(extraction.warnings) or "pdf_unreadable"
            self._gap(
                resource, GapType.UNSUPPORTED_FORMAT, EntityStatus.FAILED, reason, evidence_id
            )
            self._finish(task, resource, EntityStatus.FAILED, reason)
            return

        blocks = list(extraction.blocks)
        reasons = ["pdf_extracted", *extraction.warnings]
        status = EntityStatus.EXTRACTED_UNVERIFIED

        # Region-level OCR: a scanned page, or a large image region, is OCRed even when
        # other regions have native text (a native header must not hide a scanned body).
        regions = [r for r in extraction.image_regions if extraction.is_scanned or r.is_full_page]
        if len(regions) > _MAX_PDF_REGION_OCR:
            self._gap(
                resource,
                GapType.LIMIT_HIT,
                EntityStatus.PARTIAL,
                f"ocr_region_cap:{len(regions)}>{_MAX_PDF_REGION_OCR}",
                evidence_id,
            )
            regions = regions[:_MAX_PDF_REGION_OCR]
            status = EntityStatus.PARTIAL
        order = max((b.reading_order for b in blocks), default=0) + 1
        ocr_missing = False
        available = installed_packs() if self._ocr_runner is None else None
        for idx, region in enumerate(regions):
            route = route_ocr(
                region.image_bytes,
                script_hint=region.script_hint,
                locale=self._locale,
                allowed_packs=self._ocr_languages,
                available_packs=available,
                osd_runner=self._osd_runner,
            )
            if route.unsupported_reason is not None:
                # Crop is kept as evidence in the PDF itself; the gap names the script.
                self._gap(
                    resource,
                    GapType.UNSUPPORTED_FORMAT,
                    EntityStatus.UNSUPPORTED,
                    f"{route.unsupported_reason};region=p{region.page_index}",
                    evidence_id,
                )
                reasons.append(route.unsupported_reason)
                status = EntityStatus.PARTIAL
                continue
            result = run_ocr(region.image_bytes, packs=route.packs, ocr_runner=self._ocr_runner)
            if not result.engine_available:
                ocr_missing = True
                break
            if result.error or not result.text.strip():
                if result.error:
                    reasons.append(f"region_ocr_error:p{region.page_index}")
                continue
            if result.confidence < OCR_CONFIDENCE_FLOOR:
                self._gap(
                    resource,
                    GapType.LOW_CONFIDENCE,
                    EntityStatus.EXTRACTED_UNVERIFIED,
                    f"ocr_confidence={result.confidence:.2f};region=p{region.page_index}",
                    evidence_id,
                    retry_eligible=True,
                )
                reasons.append("ocr_low_confidence:second_opinion_recommended")
            norm, ops = normalize_text(result.text)
            key = f"{resource.resource_id}|pdf|p{region.page_index}|ocr{idx}"
            blocks.append(
                ContentBlock(
                    block_id=f"blk-{_sha(key, norm)[:12]}",
                    block_key=key,
                    resource_id=resource.resource_id,
                    state_id=f"{self._run_id}|pdf",
                    region_id=f"reg-pdf-p{region.page_index}-ocr{idx}",
                    reading_order=order,
                    page_index=region.page_index,
                    page_label=region.page_label,
                    bbox=region.bbox,
                    layer=ContentLayer.RENDERED_VISIBLE,
                    role="main",
                    raw_text=result.text,
                    normalized_text=norm,
                    normalization_ops=ops,
                    method="tesseract/ocr",
                    tool_version=TOOL_VERSION,
                    confidence=result.confidence,
                    status=EntityStatus.EXTRACTED_UNVERIFIED,
                    evidence_ids=[evidence_id],
                    provenance=Provenance(
                        pdf_object_id=f"page_{region.page_index}_image_{idx}",
                        ocr_params={"packs": result.packs_used},
                        frame_path="pdf",
                    ),
                )
            )
            order += 1

        if (
            status is EntityStatus.PARTIAL
            and extraction.is_scanned
            and not any(b.method == "tesseract/ocr" for b in blocks)
        ):
            status = EntityStatus.UNSUPPORTED  # nothing readable came out of a scanned file

        if ocr_missing:
            self._gap(
                resource,
                GapType.UNSUPPORTED_FORMAT,
                EntityStatus.UNSUPPORTED,
                "ocr_engine_missing:tesseract;scanned_content_unextracted",
                evidence_id,
            )
            reasons.append("ocr_engine_missing")
            status = EntityStatus.UNSUPPORTED if not blocks else EntityStatus.PARTIAL

        self._ledger.upsert_blocks(resource.resource_id, stamp_blocks(blocks, resource))
        self._finish(task, resource, status, ";".join(reasons))

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _resource_status(image_status: EntityStatus) -> EntityStatus:
        """Map an adapter outcome to a resource status; extracted text stays unverified."""
        if image_status is EntityStatus.VERIFIED_EXTRACTED:
            return EntityStatus.EXTRACTED_UNVERIFIED
        return image_status

    def _gap(
        self,
        resource: Resource,
        gap_type: GapType,
        status: EntityStatus,
        reason: str,
        evidence_id: str,
        *,
        retry_eligible: bool = False,
    ) -> None:
        self._ledger.record_gap(
            Gap(
                gap_id=f"gap-{_sha(resource.resource_id, gap_type.value, reason)[:12]}",
                run_id=self._run_id,
                resource_id=resource.resource_id,
                gap_type=gap_type,
                status=status,
                reason=reason,
                evidence_ids=[evidence_id],
                retry_eligible=retry_eligible,
            )
        )

    def _finish(self, task: Task, resource: Resource, status: EntityStatus, reason: str) -> None:
        self._upsert(task, resource, status, reason)
        self._ledger.complete(task.id or 0, self._worker_id)

    def _retry_or_fail(self, task: Task, error: str, resource: Resource | None = None) -> None:
        resource = resource or self._ledger.get_resource(str(task.payload.get("resource_id")))
        attempt_index = min(max(task.attempts - 1, 0), len(_RETRY_BACKOFF_S) - 1)
        when = datetime.now(UTC) + timedelta(seconds=_RETRY_BACKOFF_S[attempt_index])
        state = self._ledger.fail(task.id or 0, self._worker_id, error, retry=True, not_before=when)
        if resource is None:
            return
        if state is not None and state.value == "FAILED":
            self._upsert(task, resource, EntityStatus.FAILED, error)
        else:
            self._upsert(task, resource, EntityStatus.RETRY_SCHEDULED, error)

    def _upsert(self, task: Task, resource: Resource, status: EntityStatus, reason: str) -> None:
        old = resource.status.value
        resource.status = status
        resource.status_reason = reason
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
            )
        )
