"""Deterministic verification rules (ARCHITECTURE.md section 10).

Each rule is a pure function over ledger facts that returns typed gaps with
stable ids, so re-running a rule updates its gaps instead of duplicating them.
Rules never raise a status; `verify.record` applies the gaps to the badge.

Implemented here: R1 inventory accounting, R2 acquisition, R3 frame outcomes,
R10 payload sanity. R4-R9 and R11-R13 follow in later items (V-3..V-6).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from src.core.schemas import StrictModel
from src.modules.content_processor.contracts import (
    ContentBlock,
    DiscoveryState,
    Gap,
    Resource,
    RunEvent,
)
from src.modules.content_processor.enums import (
    TERMINAL_ENTITY_STATUSES,
    BlockKind,
    EntityStatus,
    GapType,
)
from src.modules.content_processor.verify.gaps import gap_id_for

__all__ = [
    "CONTENT_STATUSES",
    "FrameRef",
    "expected_mime_for",
    "rule_r1_inventory",
    "rule_r2_acquisition",
    "rule_r3_frames",
    "rule_r10_payload",
]

# Statuses that claim content was obtained (so content-level rules apply).
CONTENT_STATUSES: frozenset[EntityStatus] = frozenset(
    {
        EntityStatus.VERIFIED_EXTRACTED,
        EntityStatus.EXTRACTED_UNVERIFIED,
        EntityStatus.PARTIAL,
        EntityStatus.VERIFIED_NONCONTENT,
    }
)
# Block kinds a higher tier may get past (retry is worth it after approval).
_ESCALATABLE = frozenset(
    {
        BlockKind.FINGERPRINT,
        BlockKind.IP_REPUTATION,
        BlockKind.JS_CHALLENGE,
        BlockKind.INTERACTIVE_CAPTCHA,
    }
)
# Verdicts that are retry states, not statements about the payload.
_TRANSIENT = frozenset({BlockKind.NONE, BlockKind.RATE, BlockKind.TRANSPORT, BlockKind.SOFT_404})
_EXTENSION_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "png": "image/*",
    "jpg": "image/*",
    "jpeg": "image/*",
    "gif": "image/*",
    "webp": "image/*",
    "avif": "image/*",
}
_HTML = ("text/html", "application/xhtml")


class FrameRef(StrictModel):
    """One frame found in a page's document tree."""

    url: str
    hidden: bool = False


def _gap(
    run_id: str,
    resource: Resource,
    key: str,
    gap_type: GapType,
    status: EntityStatus,
    reason: str,
    *,
    retry: bool = False,
) -> Gap:
    return Gap(
        gap_id=gap_id_for(run_id, resource.resource_id, key),
        run_id=run_id,
        resource_id=resource.resource_id,
        gap_type=gap_type,
        status=status,
        reason=reason,
        evidence_ids=[f"evidence:{resource.payload_sha256}"] if resource.payload_sha256 else [],
        retry_eligible=retry,
        tier_reached=resource.acquired_at_tier,
    )


def _mime_for_name(name: str | None) -> str | None:
    if not name:
        return None
    leaf = urlsplit(name).path.rsplit("/", 1)[-1] if "/" in name else name
    if "." not in leaf:
        return None
    return _EXTENSION_MIME.get(leaf.rsplit(".", 1)[-1].lower())


def expected_mime_for(resource: Resource, explicit: str | None = None) -> str | None:
    """What the resource was supposed to be, strongest evidence first.

    Order: an explicit expectation (the discovering link, the manifest, or
    `Resource.expected_mime` once CR-004 lands), then the Content-Disposition
    file name, then the requested and final URL extensions. None = no claim.
    """
    persisted = getattr(resource, "expected_mime", None)  # CR-004, additive
    for candidate in (
        explicit,
        persisted if isinstance(persisted, str) else None,
        _mime_for_name(resource.content_disposition_name),
        _mime_for_name(resource.requested_url),
        _mime_for_name(resource.final_url),
    ):
        if candidate:
            return candidate
    return None


def _reason_tokens(resource: Resource) -> list[str]:
    return [t for t in (resource.status_reason or "").split(";") if t]


# -- R1 ----------------------------------------------------------------------


def rule_r1_inventory(
    run_id: str,
    target: Resource,
    *,
    discovery: DiscoveryState,
    children: Sequence[Resource] = (),
    regions: Sequence[ContentBlock] = (),
) -> list[Gap]:
    """R1: every discovered resource, state and region has a terminal status.

    Emits DISCOVERY_OPEN while bounded discovery has not closed, and while any
    discovered child or region is still in flight. Applies only to targets
    that claim content; a blocked or failed page has nothing to account for.
    """
    if target.status not in CONTENT_STATUSES:
        return []
    gaps: list[Gap] = []
    if not discovery.closed_within_scope:
        detail = discovery.stop_reason or "unknown"
        gaps.append(
            _gap(
                run_id,
                target,
                "R1:discovery_open",
                GapType.DISCOVERY_OPEN,
                EntityStatus.PARTIAL,
                f"discovery_open:{detail}:remaining_controls={discovery.remaining_controls}",
                retry=True,
            )
        )
    open_entities = [c for c in children if c.status not in TERMINAL_ENTITY_STATUSES]
    open_regions = [r for r in regions if r.status not in TERMINAL_ENTITY_STATUSES]
    if open_entities or open_regions:
        gaps.append(
            _gap(
                run_id,
                target,
                "R1:non_terminal",
                GapType.DISCOVERY_OPEN,
                EntityStatus.PARTIAL,
                f"non_terminal_entities:resources={len(open_entities)}:regions={len(open_regions)}",
                retry=True,
            )
        )
    return gaps


# -- R2 ----------------------------------------------------------------------


def rule_r2_acquisition(
    run_id: str,
    target: Resource,
    *,
    children: Sequence[Resource] = (),
    recorded: Iterable[Gap] = (),
) -> list[Gap]:
    """R2: required resources acquired vs discovered; blocked and failed remain.

    One gap per terminal resource that was not acquired. A resource that
    already carries an ACCESS_BLOCKED gap (a gate, or R10) gets no second one.
    """
    already_blocked = {g.resource_id for g in recorded if g.gap_type is GapType.ACCESS_BLOCKED}
    gaps: list[Gap] = []
    for res in (target, *children):
        reason = res.status_reason or res.status.value.lower()
        status = res.status
        if status is EntityStatus.EXCLUDED_BY_POLICY:
            gaps.append(_gap(run_id, res, "R2:excluded", GapType.POLICY_EXCLUDED, status, reason))
        elif status is EntityStatus.BLOCKED and res.resource_id not in already_blocked:
            verdict = res.block_verdict
            retry = verdict is not None and verdict.kind in _ESCALATABLE
            gaps.append(
                _gap(
                    run_id,
                    res,
                    "R2:blocked",
                    GapType.ACCESS_BLOCKED,
                    status,
                    reason,
                    retry=retry,
                )
            )
        elif status is EntityStatus.FAILED:
            gaps.append(_gap(run_id, res, "R2:failed", GapType.ACQUISITION_FAILED, status, reason))
        elif status is EntityStatus.UNSUPPORTED:
            gaps.append(
                _gap(run_id, res, "R2:unsupported", GapType.UNSUPPORTED_FORMAT, status, reason)
            )
        elif status is EntityStatus.BUDGET_EXHAUSTED:
            gaps.append(_gap(run_id, res, "R2:budget", GapType.BUDGET_STOPPED, status, reason))
        elif status in CONTENT_STATUSES and res.payload_sha256 is None:
            gaps.append(
                _gap(
                    run_id,
                    res,
                    "R2:no_payload",
                    GapType.ACQUISITION_FAILED,
                    EntityStatus.FAILED,
                    f"no_payload_recorded:{reason}",
                )
            )
    return gaps


# -- R3 ----------------------------------------------------------------------


def _frame_child(url: str, children: Sequence[Resource]) -> Resource | None:
    for child in children:
        if url in (child.requested_url, child.final_url, *child.aliases):
            return child
    return None


def rule_r3_frames(
    run_id: str,
    target: Resource,
    *,
    frames: Sequence[FrameRef] | None,
    children: Sequence[Resource] = (),
    blocks: Sequence[ContentBlock] = (),
) -> list[Gap]:
    """R3: every frame in the tree has a load and an extraction outcome.

    `frames=None` means the frame inventory could not be read (no DOM
    evidence); that is a review gap, never a silent pass. Hidden frames
    (tracking pixels, zero-size) are not content and are skipped.
    """
    if target.status not in CONTENT_STATUSES or target.sniffed_mime != "text/html":
        return []
    if frames is None:
        return [
            _gap(
                run_id,
                target,
                "R3:inventory_unavailable",
                GapType.LOW_CONFIDENCE,
                EntityStatus.EXTRACTED_UNVERIFIED,
                "frame_inventory_unavailable",
            )
        ]
    extracted_frames = {b.provenance.frame_path for b in blocks}
    gaps: list[Gap] = []
    for frame in frames:
        if frame.hidden:
            continue
        key = f"R3:frame:{frame.url}"
        child = _frame_child(frame.url, children)
        if child is not None and child.status is EntityStatus.BLOCKED:
            gaps.append(
                _gap(
                    run_id,
                    target,
                    key,
                    GapType.ACCESS_BLOCKED,
                    EntityStatus.PARTIAL,
                    f"frame_blocked:{frame.url}",
                    retry=True,
                )
            )
        elif child is not None and child.status is EntityStatus.FAILED:
            gaps.append(
                _gap(
                    run_id,
                    target,
                    key,
                    GapType.ACQUISITION_FAILED,
                    EntityStatus.PARTIAL,
                    f"frame_failed:{frame.url}",
                )
            )
        elif child is not None and child.status in TERMINAL_ENTITY_STATUSES:
            continue  # the child's own status and gaps carry its outcome
        elif child is not None:
            continue  # still in flight: the URL is RUNNING, not partial
        elif frame.url not in extracted_frames:
            gaps.append(
                _gap(
                    run_id,
                    target,
                    key,
                    GapType.UNSUPPORTED_FORMAT,
                    EntityStatus.PARTIAL,
                    f"frame_not_acquired:{frame.url}",
                    retry=True,
                )
            )
    return gaps


# -- R10 ---------------------------------------------------------------------


def _wrong_payload_history(resource: Resource, history: Iterable[RunEvent]) -> str | None:
    for event in history:
        if (
            event.entity_type == "resource"
            and event.entity_id == resource.resource_id
            and event.new_state == EntityStatus.BLOCKED.value
            and (event.reason or "").startswith("wrong_payload")
        ):
            return event.reason
    return None


def rule_r10_payload(
    run_id: str,
    resource: Resource,
    *,
    expected_mime: str | None = None,
    history: Iterable[RunEvent] = (),
) -> list[Gap]:
    """R10: the payload is what it claims to be.

    Checks, in order: a wrong payload (HTML where a document or image was
    expected, now or in any earlier event for this resource, so a later
    pass cannot launder it); a block page stored as content; a soft 404; a
    soft redirect to the site root; truncation or a size limit; placeholder
    text published live.
    """
    gaps: list[Gap] = []
    expected = expected_mime_for(resource, expected_mime)
    sniffed = resource.sniffed_mime or ""
    declared = (resource.declared_mime or "").lower()
    html_returned = sniffed.startswith(_HTML) or declared.startswith(_HTML)
    verdict = resource.block_verdict
    earlier = _wrong_payload_history(resource, history)

    if (
        resource.payload_sha256 is not None
        and expected
        and not expected.startswith(_HTML)
        and html_returned
    ):
        gaps.append(
            _gap(
                run_id,
                resource,
                "R10:wrong_payload",
                GapType.ACCESS_BLOCKED,
                EntityStatus.BLOCKED,
                f"wrong_payload:html_for:{expected}",
            )
        )
    if not gaps and (earlier or (verdict is not None and verdict.kind is BlockKind.WRONG_PAYLOAD)):
        gaps.append(
            _gap(
                run_id,
                resource,
                "R10:wrong_payload",
                GapType.ACCESS_BLOCKED,
                EntityStatus.BLOCKED,
                earlier or f"wrong_payload:{resource.status_reason or 'classifier'}",
            )
        )
    if (
        verdict is not None
        and verdict.kind not in _TRANSIENT
        and verdict.kind is not BlockKind.WRONG_PAYLOAD
        and resource.status in CONTENT_STATUSES
    ):
        gaps.append(
            _gap(
                run_id,
                resource,
                "R10:block_page_as_content",
                GapType.ACCESS_BLOCKED,
                EntityStatus.BLOCKED,
                f"block_page_stored_as_content:{verdict.kind.value.lower()}:{verdict.vendor}",
                retry=verdict.kind in _ESCALATABLE,
            )
        )
    tokens = _reason_tokens(resource)
    if (verdict is not None and verdict.kind is BlockKind.SOFT_404) or (
        "soft_404_suspected" in tokens
    ):
        gaps.append(
            _gap(
                run_id,
                resource,
                "R10:soft_404",
                GapType.SOFT_404_SUSPECTED,
                EntityStatus.EXTRACTED_UNVERIFIED,
                "soft_404_suspected",
            )
        )
    if resource.final_url and resource.status in CONTENT_STATUSES:
        requested = urlsplit(resource.requested_url)
        final = urlsplit(resource.final_url)
        if (
            requested.path.strip("/")
            and not final.path.strip("/")
            and requested.hostname == final.hostname
        ):
            gaps.append(
                _gap(
                    run_id,
                    resource,
                    "R10:soft_redirect",
                    GapType.SOFT_REDIRECT_SUSPECTED,
                    EntityStatus.EXTRACTED_UNVERIFIED,
                    f"soft_redirect_to_root:{resource.final_url}",
                )
            )
    if "truncated_body" in tokens or "limit_hit" in tokens:
        detail = "truncated_body" if "truncated_body" in tokens else "limit_hit"
        gaps.append(
            _gap(
                run_id,
                resource,
                "R10:truncated",
                GapType.LIMIT_HIT,
                EntityStatus.PARTIAL,
                detail,
                retry=True,
            )
        )
    if "placeholder_content_suspected" in tokens:
        gaps.append(
            _gap(
                run_id,
                resource,
                "R10:placeholder",
                GapType.DEGRADED_VARIANT,
                EntityStatus.EXTRACTED_UNVERIFIED,
                "placeholder_content_suspected",
            )
        )
    return gaps
