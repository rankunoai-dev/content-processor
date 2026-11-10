"""Document worker (Stage 1c): EXTRACT_DOC and EXTRACT_IMAGE tasks.

Per task: load the acquired payload from the evidence store, run the PDF or
image adapter in a child process under a wall-clock limit, OCR scanned pages
and text-bearing images, write blocks and gaps, and set the resource status.
The worker never raises out of `process()`. A parser crash or hang cannot take
the worker down because the adapter runs in a separate, killable process.

The child processes are long-lived (`ExtractionPool`): `parallel_tasks`
interpreters are started on first use and fed one task at a time over a pipe,
instead of a fresh interpreter (1-3 s of imports) per document. A child that
exceeds the time limit is killed and replaced (the task ends PARTIAL with a
LIMIT_HIT gap, as before); a child that dies mid-task is replaced and the task
is retried (`extraction_process_died`); every child is recycled after
`recycle_after` tasks so a leaky parser cannot grow without bound.

Statuses here are extraction outcomes. Nothing is VERIFIED_EXTRACTED at
resource level: verification belongs to the verification lane.
"""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

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
from src.modules.content_processor.adapters.office import (
    OfficeExtraction,
    OfficeLimitError,
    OfficeParseError,
    detect_office_kind,
    extract_office,
    is_legacy_ole,
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
from src.modules.content_processor.ledger.serial import SerialLedger
from src.modules.content_processor.provenance import stamp_blocks
from src.modules.content_processor.workers.ports import LedgerPort

__all__ = ["DocWorker", "ExtractionPool", "run_isolated"]

_logger = get_logger("content_processor.doc_worker")

TOOL_VERSION = "doc_worker/1.0"
_KINDS = (TaskKind.EXTRACT_DOC, TaskKind.EXTRACT_IMAGE)
_RETRY_BACKOFF_S = (5, 30)
_MAX_PDF_REGION_OCR = 50
RECYCLE_CHILD_AFTER_TASKS = 50


def _child_main(conn: Any, fn: Callable[..., Any], kwargs: dict[str, Any]) -> None:
    """Child-process entry point: run `fn`, send back ("ok", result) or ("err", text)."""
    try:
        conn.send(("ok", fn(**kwargs)))
    except Exception as exc:  # noqa: BLE001 - boundary: report instead of dying silently
        conn.send(("err", f"{type(exc).__name__}:{exc}"))
    finally:
        conn.close()


def run_isolated(fn: Callable[..., Any], kwargs: dict[str, Any], *, timeout_s: float) -> Any:
    """Run `fn(**kwargs)` in a fresh spawned child process and return its result.

    One interpreter per call; the worker itself uses `ExtractionPool`, which
    keeps its children. Raises TimeoutError when the child exceeds `timeout_s`
    (it is killed) and RuntimeError when the child fails or dies without a
    result. `fn` must be a module-level callable and its arguments and result
    must be picklable.
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


def _pool_child_main(conn: Any) -> None:
    """Long-lived child: answer `(fn, kwargs)` requests until the parent closes the pipe."""
    while True:
        try:
            fn, kwargs = conn.recv()
        except (EOFError, OSError):
            return
        try:
            reply = ("ok", fn(**kwargs))
        except Exception as exc:  # noqa: BLE001 - boundary: report instead of dying silently
            reply = ("err", f"{type(exc).__name__}:{exc}")
        try:
            conn.send(reply)
        except (EOFError, OSError):
            return


class _Child:
    """One spawned interpreter and the parent's end of its pipe."""

    def __init__(self, ctx: Any) -> None:
        self.conn, child_end = ctx.Pipe(duplex=True)
        self.proc = ctx.Process(target=_pool_child_main, args=(child_end,), daemon=True)
        self.proc.start()
        child_end.close()
        self.served = 0

    @property
    def pid(self) -> int | None:
        return int(self.proc.pid) if self.proc.pid is not None else None

    def alive(self) -> bool:
        return bool(self.proc.is_alive())

    def kill(self) -> None:
        """Stop the child now and release the pipe."""
        if self.proc.is_alive():
            self.proc.kill()
        self.proc.join(timeout=5)
        self.conn.close()


class ExtractionPool:
    """`size` long-lived spawn children that run adapter calls one at a time each.

    Children start on first use, so a run without documents starts none. A call
    takes a free child (waiting for one when all are busy), sends the function
    and its arguments, and waits up to `timeout_s` for the answer. A timeout
    kills that child only; a child that dies mid-call is dropped; either way the
    slot spawns a fresh child on its next use. A child is recycled after
    `recycle_after` calls to bound its memory.
    """

    def __init__(self, size: int = 1, *, recycle_after: int = RECYCLE_CHILD_AFTER_TASKS) -> None:
        """Reserve `size` slots; nothing is spawned yet."""
        self.size = max(1, size)
        self.recycle_after = max(1, recycle_after)
        self.spawned = 0
        self.closed = False
        self._ctx = multiprocessing.get_context("spawn")
        self._slots: queue.Queue[_Child | None] = queue.Queue()
        for _ in range(self.size):
            self._slots.put(None)
        self._lock = threading.Lock()

    def _ready(self, child: _Child | None) -> _Child:
        """`child` if it can take another call, else a fresh one (the old one is stopped)."""
        if child is not None and child.alive() and child.served < self.recycle_after:
            return child
        if child is not None:
            child.kill()
        with self._lock:
            self.spawned += 1
        return _Child(self._ctx)

    def run(self, fn: Callable[..., Any], kwargs: dict[str, Any], *, timeout_s: float) -> Any:
        """Run `fn(**kwargs)` in a pool child and return its result.

        Raises TimeoutError when the call exceeds `timeout_s` (that child is
        killed) and RuntimeError when the call fails or the child dies
        (`extraction_process_died`). `fn` must be a module-level callable and its
        arguments and result must be picklable.
        """
        if self.closed:
            msg = "extraction_pool_closed"
            raise RuntimeError(msg)
        keep: _Child | None = None
        child = self._slots.get()
        try:
            child = self._ready(child)
            try:
                child.conn.send((fn, kwargs))
            except (OSError, EOFError, ValueError) as exc:
                child.kill()
                msg = "extraction_process_died"
                raise RuntimeError(msg) from exc
            if not child.conn.poll(timeout_s):
                child.kill()
                raise TimeoutError(f"extraction_timeout:{timeout_s:.0f}s")
            try:
                kind, value = child.conn.recv()
            except (EOFError, OSError) as exc:
                child.kill()
                msg = "extraction_process_died"
                raise RuntimeError(msg) from exc
            child.served += 1
            keep = child
            if kind == "err":
                raise RuntimeError(str(value))
            return value
        finally:
            self._slots.put(keep)

    def close(self) -> None:
        """Stop every child; later calls are refused."""
        self.closed = True
        for _ in range(self.size):
            try:
                child = self._slots.get(timeout=5)
            except queue.Empty:
                break
            if child is not None:
                child.kill()


def _extract_office_safely(
    *, data: bytes, resource_id: str, run_id: str, evidence_id: str
) -> OfficeExtraction | str:
    """Run the OOXML adapter; return the rejection reason as data instead of raising.

    Exception types do not survive the child-process boundary, so refusals are
    returned as strings and mapped to ledger outcomes by the caller.
    """
    try:
        return extract_office(data, resource_id=resource_id, run_id=run_id, evidence_id=evidence_id)
    except OfficeLimitError as exc:
        return f"limit:{exc}"
    except OfficeParseError as exc:
        return f"parse:{exc}"


class _SerialLedger:
    """A ledger whose calls run one at a time, for workers sharing it across threads."""

    def __init__(self, ledger: LedgerPort) -> None:
        self._ledger = ledger
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._ledger, name)
        if not callable(target):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return target(*args, **kwargs)

        return call


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
        parallel_tasks: int = 1,
        pool: ExtractionPool | None = None,
    ) -> None:
        """Bind the worker to one run. `isolate=False` runs adapters in-process (tests).

        `parallel_tasks` is how many documents and images are read at once
        (`CP_DOC_PARALLEL_TASKS`); extraction never contacts a site, so this does
        not touch the per-host pace. A ledger the run already serialised
        (`ledger/serial.py`) is used as it is; any other is wrapped here. With
        `isolate=True` the adapters run in `pool` (one child per parallel task);
        a worker given no pool makes its own and stops it in `close()`.
        """
        self.concurrent_tasks = max(1, parallel_tasks)
        self._ledger: LedgerPort = (
            cast(LedgerPort, _SerialLedger(ledger))
            if self.concurrent_tasks > 1 and not isinstance(ledger, SerialLedger)
            else ledger
        )
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
        self._pool = pool
        self._owns_pool = False
        self._pool_lock = threading.Lock()

    @property
    def pool(self) -> ExtractionPool | None:
        """The extraction children in use (None until the first isolated call)."""
        return self._pool

    def close(self) -> None:
        """Stop the extraction children this worker started."""
        if self._owns_pool and self._pool is not None:
            self._pool.close()

    def _children(self) -> ExtractionPool:
        with self._pool_lock:
            if self._pool is None:
                self._pool = ExtractionPool(size=self.concurrent_tasks)
                self._owns_pool = True
            return self._pool

    # -- loop ---------------------------------------------------------------

    async def run_until_idle(
        self,
        *,
        max_tasks: int | None = None,
        idle_polls: int = 3,
        poll_s: float = 1.0,
        max_wait_s: float = 300.0,
    ) -> int:
        """Process tasks until none remain or none become ready within `max_wait_s`.

        Up to `concurrent_tasks` tasks run at once, each in a worker thread; the
        ledger calls are serialised (see `__init__`), and `MemoryLedger` locks
        itself as well, so fetch and render may use the same ledger meanwhile.
        """
        processed = 0
        idle = 0
        waited = 0.0
        running: set[asyncio.Future[None]] = set()
        while True:
            while len(running) < self.concurrent_tasks and (
                max_tasks is None or processed + len(running) < max_tasks
            ):
                task = self._ledger.lease(self._worker_id, _KINDS, run_id=self._run_id)
                if task is None:
                    break
                idle = 0
                waited = 0.0
                running.add(asyncio.ensure_future(asyncio.to_thread(self.process, task)))
            if running:
                done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                processed += len(done)
                continue
            if max_tasks is not None and processed >= max_tasks:
                break
            if self._ledger.pending_count(_KINDS, run_id=self._run_id) == 0:
                idle += 1
                if idle >= idle_polls:
                    break
            elif waited >= max_wait_s:
                _logger.warning("doc_loop_gave_up_waiting", extra={"waited_s": waited})
                break
            waited += poll_s
            await asyncio.sleep(poll_s)
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
                self._do_document(task, resource, payload, evidence_id)
        except TimeoutError as exc:
            self._gap(resource, GapType.LIMIT_HIT, EntityStatus.PARTIAL, str(exc), sha256)
            self._finish(task, resource, EntityStatus.PARTIAL, str(exc))
        except RuntimeError as exc:
            self._retry_or_fail(task, f"extraction_failed:{exc}", resource)

    def _call(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        if self._isolate:
            return self._children().run(fn, kwargs, timeout_s=self._timeout_s)
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

    def _do_document(
        self, task: Task, resource: Resource, payload: bytes, evidence_id: str
    ) -> None:
        """Dispatch by content: PDF, OOXML (DOCX/XLSX/PPTX), or legacy binary Office."""
        if detect_office_kind(payload) is not None:
            self._do_office(task, resource, payload, evidence_id)
        elif is_legacy_ole(payload):
            # .doc/.xls/.ppt need LibreOffice; not installed by design (C-9 scope).
            ext = resource.extension_hint or "unknown"
            reason = f"legacy_office_requires_libreoffice:{ext}"
            self._gap(
                resource, GapType.UNSUPPORTED_FORMAT, EntityStatus.UNSUPPORTED, reason, evidence_id
            )
            self._finish(task, resource, EntityStatus.UNSUPPORTED, reason)
        else:
            self._do_pdf(task, resource, payload, evidence_id)

    def _do_office(self, task: Task, resource: Resource, payload: bytes, evidence_id: str) -> None:
        result: OfficeExtraction | str = self._call(
            _extract_office_safely,
            data=payload,
            resource_id=resource.resource_id,
            run_id=self._run_id,
            evidence_id=evidence_id,
        )
        if isinstance(result, str):
            if result.startswith("limit:"):
                self._gap(resource, GapType.LIMIT_HIT, EntityStatus.PARTIAL, result, evidence_id)
                self._finish(task, resource, EntityStatus.PARTIAL, result)
            else:
                self._gap(
                    resource, GapType.UNSUPPORTED_FORMAT, EntityStatus.FAILED, result, evidence_id
                )
                self._finish(task, resource, EntityStatus.FAILED, result)
            return
        self._ledger.upsert_blocks(resource.resource_id, stamp_blocks(result.blocks, resource))
        self._finish(task, resource, result.status, ";".join(result.warnings))

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
