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
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.core.logger import get_logger
from src.modules.content_processor.contracts import Gap, Resource, UrlRecord
from src.modules.content_processor.verify.gaps import strictest_status
from src.modules.content_processor.verify.record import VERIFY_VERSION, status_vector_for
from src.modules.content_processor.verify.rules import is_pixel_frame
from src.modules.content_processor.verify.status import derive_url_badge

__all__ = [
    "ReverdictResult",
    "false_alarm",
    "reverdict_file",
    "reverdict_folder",
    "reverdict_record",
]

_logger = get_logger("content_processor.verify.reverdict")
_FRAME_PREFIX = "frame_not_acquired:"


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


def reverdict_record(record: UrlRecord) -> tuple[UrlRecord, int]:
    """The record with its false-alarm gaps removed and its verdict derived again.

    Returns the record unchanged (same object) and 0 when nothing applies.
    """
    dropped = [g for g in record.gaps if false_alarm(g)]
    raw = record.resource_status
    if not dropped or raw is None:
        return record, 0
    gone = {g.gap_id for g in dropped}
    kept = [g for g in record.gaps if g.gap_id not in gone]
    target_id = dropped[0].resource_id
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
    warnings.append(f"reverdicted:{VERIFY_VERSION}:dropped={len(dropped)}")
    updated = record.model_copy(
        update={"gaps": kept, "badge": badge, "status_vector": vector, "warnings": warnings}
    )
    return updated, len(dropped)


def reverdict_file(path: Path, result: ReverdictResult | None = None) -> ReverdictResult:
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
        updated, dropped = reverdict_record(record)
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
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        _logger.info("records_reverdicted", extra={"path": str(path), "changed": out.changed})
    return out


def reverdict_folder(folder: Path, pattern: str = "*.records.jsonl") -> ReverdictResult:
    """Re-score every records file in a run folder."""
    result = ReverdictResult(badges_before={}, badges_after={})
    for path in _ordered(folder.glob(pattern)):
        reverdict_file(path, result)
    return result


def _ordered(paths: Iterable[Path]) -> list[Path]:
    return sorted(paths, key=lambda p: p.name)
