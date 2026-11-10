"""Re-score stored records after a verdict rule was corrected, without a new scrape.

A record's badge is derived from its gaps by pure rules (`verify.status`). When a rule
turns out to have raised a false alarm (a hidden tag-manager frame counted as content
that was not captured), the pages already scraped need not be fetched again: the false
gaps are removed from the stored record and the badge, status vector and warnings are
derived again from what is left. Everything else in the record (blocks, evidence ids,
capture time) stays exactly as it was, and the record says it was re-scored.

Only gaps that the corrected rule would no longer raise are removed; nothing is ever
added, and a record without such a gap is left untouched byte for byte.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from src.core.logger import get_logger
from src.integrations.evidence_store import EvidenceStore
from src.modules.content_processor.contracts import Gap, Resource, UrlRecord
from src.modules.content_processor.enums import EntityStatus
from src.modules.content_processor.governor.gates import detect_gates
from src.modules.content_processor.scheduler.capture_journal import write_durably
from src.modules.content_processor.verify.gaps import GATE_STATUS, strictest_status
from src.modules.content_processor.verify.record import VERIFY_VERSION, status_vector_for
from src.modules.content_processor.verify.rules import is_pixel_frame
from src.modules.content_processor.verify.status import derive_url_badge

__all__ = [
    "Recheck",
    "ReverdictResult",
    "false_alarm",
    "gate_recheck",
    "reverdict_file",
    "reverdict_folder",
    "reverdict_record",
]

_logger = get_logger("content_processor.verify.reverdict")
_FRAME_PREFIX = "frame_not_acquired:"
Recheck = Callable[[Gap], bool | None]
"""Says whether a gap still stands under the current rules: False drops it, None keeps it."""


def gate_recheck(evidence: EvidenceStore) -> Recheck:
    """Run the gate detector again on the stored page of each gate gap.

    A gate gap's evidence ids are the rendered DOM, its visible text and the
    screenshot, in that order. The stored DOM is re-read and the current gate rules
    are applied; a gate the rules no longer find is dropped. Anything that cannot
    be re-read is left as it is.
    """

    def still_stands(gap: Gap) -> bool | None:
        kind = (gap.reason or "").split(":")[0]
        if kind not in GATE_STATUS:
            return None
        ids = [e.split(":", 1)[1] for e in gap.evidence_ids if e.startswith("evidence:")]
        if not ids:
            return None
        try:
            dom = evidence.get(ids[0])
            text = evidence.get(ids[1]).decode("utf-8", errors="replace") if len(ids) > 1 else ""
        except (OSError, KeyError, ValueError):
            return None
        if b"<" not in dom[:4096]:
            return None
        return any(f.kind == kind for f in detect_gates(dom, text))

    return still_stands


def _status_before_gates(record: UrlRecord, raw: EntityStatus) -> EntityStatus:
    """The page's own status before a gate finding lowered it (read from the reason).

    The render worker writes "rendered;all_rendered_blocks_verified;..." or
    "...;unverified_rendered_blocks=N+M;..." before it applies gates, so the status
    the page earned on its own can be read back when a gate was a false alarm.
    """
    tokens = (record.resource_status_reason or "").split(";")
    if raw is not EntityStatus.PARTIAL or not any(t.split(":")[0] in GATE_STATUS for t in tokens):
        return raw
    if "rendered" not in tokens:
        return raw
    if "all_rendered_blocks_verified" in tokens and not any(
        t.startswith("collapsed_unrevealed") for t in tokens
    ):
        return EntityStatus.VERIFIED_EXTRACTED
    return EntityStatus.EXTRACTED_UNVERIFIED


@dataclass
class ReverdictResult:
    """What re-scoring changed."""

    files: int = 0
    records: int = 0
    changed: int = 0
    gaps_dropped: int = 0
    badges_before: dict[str, int] | None = None
    badges_after: dict[str, int] | None = None
    damaged_lines: int = 0

    def as_dict(self) -> dict[str, object]:
        """A JSON-ready summary."""
        return {
            "files": self.files,
            "records": self.records,
            "changed": self.changed,
            "gaps_dropped": self.gaps_dropped,
            "badges_before": self.badges_before or {},
            "badges_after": self.badges_after or {},
            "damaged_lines": self.damaged_lines,
        }


def false_alarm(gap: Gap) -> bool:
    """True for a gap the corrected rules would no longer raise."""
    reason = gap.reason or ""
    return reason.startswith(_FRAME_PREFIX) and is_pixel_frame(reason[len(_FRAME_PREFIX) :])


def reverdict_record(record: UrlRecord, recheck: Recheck | None = None) -> tuple[UrlRecord, int]:
    """The record with its false-alarm gaps removed and its verdict derived again.

    `recheck` may look at a gap's stored evidence under the current rules (see
    `gate_recheck`). Returns the record unchanged (same object) and 0 when nothing
    applies.
    """
    dropped = [
        g for g in record.gaps if false_alarm(g) or (recheck is not None and recheck(g) is False)
    ]
    raw = record.resource_status
    if not dropped or raw is None:
        return record, 0
    original = raw
    gone = {g.gap_id for g in dropped}
    kept = [g for g in record.gaps if g.gap_id not in gone]
    target_id = dropped[0].resource_id
    if not any(g.resource_id == target_id and g.gap_type in _GATE_TYPES for g in kept):
        raw = _status_before_gates(record, raw)
    target_status = strictest_status(raw, [g.status for g in kept if g.resource_id == target_id])
    target = Resource(
        resource_id=target_id,
        run_id=record.run_id,
        requested_url=record.requested_url,
        final_url=record.final_url,
        schedule_key=record.schedule_key or record.requested_url,
        sniffed_mime=record.sniffed_mime,
        payload_sha256=record.payload_sha256,
        status=raw,
        status_reason=record.resource_status_reason,
    )
    badge = derive_url_badge(
        target_status=target_status,
        child_statuses=[b.status for b in record.blocks],
        gaps=kept,
        discovery_closed=record.discovery.closed_within_scope,
        work_outstanding=False,
    )
    vector = status_vector_for([target], record.blocks, kept, target_status, record.discovery)
    warnings = [
        w for w in record.warnings if not w.startswith(("status_lowered_by_gaps:", "reverdicted:"))
    ]
    if target_status is not raw:
        warnings.append(f"status_lowered_by_gaps:{raw.value}->{target_status.value}")
    if raw is not original:
        warnings.append(f"gate_withdrawn:{original.value}->{raw.value}")
    warnings.append(f"reverdicted:{VERIFY_VERSION}:dropped={len(dropped)}")
    updated = record.model_copy(
        update={
            "gaps": kept,
            "badge": badge,
            "status_vector": vector,
            "warnings": warnings,
            "resource_status": raw,
        }
    )
    return updated, len(dropped)


_GATE_TYPES = frozenset(v[0] for v in GATE_STATUS.values())


def reverdict_file(
    path: Path, result: ReverdictResult | None = None, *, recheck: Recheck | None = None
) -> ReverdictResult:
    """Re-score one `.records.jsonl` in place (written whole, then swapped in)."""
    out = result or ReverdictResult(badges_before={}, badges_after={})
    before = out.badges_before if out.badges_before is not None else {}
    after = out.badges_after if out.badges_after is not None else {}
    out.badges_before, out.badges_after = before, after
    lines = path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    touched = False
    for line in lines:
        if not line.strip():
            continue
        try:
            record = UrlRecord.model_validate(json.loads(line))
        except ValueError:
            out.damaged_lines += 1
            rewritten.append(line)
            continue
        out.records += 1
        before[str(record.badge)] = before.get(str(record.badge), 0) + 1
        updated, dropped = reverdict_record(record, recheck)
        after[str(updated.badge)] = after.get(str(updated.badge), 0) + 1
        if dropped:
            out.changed += 1
            out.gaps_dropped += dropped
            touched = True
            rewritten.append(updated.model_dump_json())
        else:
            rewritten.append(line)
    out.files += 1
    if touched:
        # Written whole and swapped in, with the retry a reader that holds the file open
        # for a moment on Windows needs: the app reads these files while it runs.
        write_durably(path, ("\n".join(rewritten) + "\n").encode("utf-8"))
        _logger.info("records_reverdicted", extra={"path": str(path), "changed": out.changed})
    return out


def reverdict_folder(
    folder: Path, pattern: str = "*.records.jsonl", *, recheck: Recheck | None = None
) -> ReverdictResult:
    """Re-score every records file in a run folder."""
    result = ReverdictResult(badges_before={}, badges_after={})
    for path in _ordered(folder.glob(pattern)):
        reverdict_file(path, result, recheck=recheck)
    return result


def _ordered(paths: Iterable[Path]) -> list[Path]:
    return sorted(paths, key=lambda p: p.name)
