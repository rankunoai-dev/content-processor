"""Per-URL verdict assembly (V-1, ARCHITECTURE.md sections 9 and 10).

One `UrlRecord` per scheduled URL and per excluded input, so the run's
denominator equals what was requested. Each record carries the status vector,
inventory counts over observed entities only, the discovery state, every gap
(recorded by workers plus those emitted by rules R1, R2, R3 and R10) and the
corpus blocks. The badge is derived from the gaps, never from a worker's
status alone: a gate recorded by the fetch worker lowers the badge even when
no render ran (K14), and a wrong payload seen once stays seen (K15).
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from src.integrations.evidence_store import EvidenceStore
from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.adapters.ocr import is_ocr_available
from src.modules.content_processor.contracts import (
    ContentBlock,
    DiscoveryState,
    FollowupRef,
    Gap,
    InventoryCounts,
    Resource,
    RunEvent,
    StatusVector,
    UrlRecord,
)
from src.modules.content_processor.enums import (
    TERMINAL_ENTITY_STATUSES,
    ContentLayer,
    EntityStatus,
    GapType,
    UrlBadge,
)
from src.modules.content_processor.scope.frontier import ExcludedInput, FrontierPlan
from src.modules.content_processor.verify.detectors import (
    DetectedRegion,
    EvidenceProblem,
    PdfRegionRef,
    TextRegionDetector,
    evidence_problems,
    pdf_regions_from_evidence,
    screenshot_sha,
)
from src.modules.content_processor.verify.gaps import gap_id_for, strictest_status
from src.modules.content_processor.verify.rules import (
    CONTENT_STATUSES,
    FrameRef,
    rule_r1_inventory,
    rule_r2_acquisition,
    rule_r3_frames,
    rule_r10_payload,
)
from src.modules.content_processor.verify.rules_extended import (
    main_word_count,
    rule_evidence_integrity,
    rule_r6_visual_regions,
    rule_r7_channel_agreement,
    rule_r8_pdf_regions,
    rule_r11_degradation,
    rule_r12_limits,
    rule_r13_layer_hygiene,
    rule_volatility,
    uncovered_pdf_regions,
)
from src.modules.content_processor.verify.status import derive_status_vector, derive_url_badge

__all__ = [
    "COUNTERFACTUAL_WARNING",
    "CORPUS_LAYERS",
    "VERIFY_VERSION",
    "RunReader",
    "assemble_url_record",
    "corpus_blocks",
    "derive_discovery",
    "excluded_url_record",
    "frames_from_evidence",
    "url_records_for_run",
]

VERIFY_VERSION = "v1-r1r2r3r10"
COUNTERFACTUAL_WARNING = "counterfactual:discovery_assumed_closed"
CORPUS_LAYERS: frozenset[ContentLayer] = frozenset(
    {ContentLayer.RENDERED_VISIBLE, ContentLayer.COLLAPSED_PUBLISHED}
)
_ACQUISITION_GAPS = frozenset(
    {
        GapType.ACCESS_BLOCKED,
        GapType.ACQUISITION_FAILED,
        GapType.LIMIT_HIT,
        GapType.UNSUPPORTED_FORMAT,
        GapType.BUDGET_STOPPED,
    }
)


class RunReader(Protocol):
    """Read side of a ledger that records can be assembled from.

    Implemented by `MemoryLedger` and `TaskLedger` (CR-005).
    """

    def list_resources(self, run_id: str) -> list[Resource]:
        """Latest resource rows for a run."""
        ...

    def get_summaries(self, resource_id: str) -> dict[str, dict[str, Any]]:
        """Named summaries per resource id (render, explore, followups...)."""
        ...

    def get_events(self, run_id: str, entity_id: str | None = None) -> list[RunEvent]:
        """Append-only event history."""
        ...

    def get_gaps(self, resource_id: str) -> list[Gap]:
        """Gaps recorded for a resource."""
        ...

    def get_blocks(self, resource_id: str) -> list[ContentBlock]:
        """All blocks of a resource."""
        ...


# -- building blocks ----------------------------------------------------------


def corpus_blocks(blocks: Sequence[ContentBlock]) -> list[ContentBlock]:
    """The blocks that make up the URL's visible corpus, from its best channel.

    Rendered (and explored-state) blocks supersede the source channel when a
    render ran; otherwise the source channel stands. Hidden, source-only,
    metadata and other non-corpus layers are never counted as regions.
    """
    content = [b for b in blocks if b.layer in CORPUS_LAYERS]
    rendered = [b for b in content if not b.state_id.endswith("|source")]
    return rendered or content


def _tokens(resource: Resource) -> list[str]:
    return [t for t in (resource.status_reason or "").split(";") if t]


def _nested(summary: Mapping[str, Any] | None, key: str) -> Any:
    """Find `key` at the top level of a summary or one level down."""
    if not summary:
        return None
    if key in summary:
        return summary[key]
    for value in summary.values():
        if isinstance(value, Mapping) and key in value:
            return value[key]
    return None


def derive_discovery(
    target: Resource, summaries: Mapping[str, Mapping[str, Any]], gaps: Sequence[Gap]
) -> DiscoveryState:
    """Whether bounded discovery closed for this URL, read from what workers recorded.

    Closed only when the render worker said `discovery_closed_within_scope`,
    said nothing contradicting it, and no UNVISITED_STATE gap is open. Until
    the state explorer lands (K5), every rendered page stays open.
    """
    tokens = _tokens(target)
    unvisited = [g for g in gaps if g.gap_type is GapType.UNVISITED_STATE]
    explore = summaries.get("explore")
    opened = [t for t in tokens if t.startswith("discovery_open")]
    if (
        target.status in CONTENT_STATUSES
        and "discovery_closed_within_scope" in tokens
        and not opened
        and not unvisited
    ):
        stop = _nested(explore, "stop_reason")
        return DiscoveryState(
            closed_within_scope=True,
            remaining_controls=0,
            unknown_state_space=False,
            stop_reason=str(stop) if stop else "closed_within_scope",
        )
    if target.status not in CONTENT_STATUSES:
        stop_reason = "not_acquired"
    elif "render" not in summaries:
        stop_reason = "not_rendered"
    elif opened and ":" in opened[0]:
        stop_reason = opened[0].split(":", 1)[1]
    else:
        stop_reason = "not_explored"
    return DiscoveryState(
        closed_within_scope=False,
        remaining_controls=len(unvisited),
        unknown_state_space=True,
        stop_reason=stop_reason,
    )


def _acquired(resource: Resource) -> bool:
    return resource.payload_sha256 is not None and resource.status in CONTENT_STATUSES


def _inventory(
    resources: Sequence[Resource],
    regions: Sequence[ContentBlock],
    gaps: Sequence[Gap],
    revision: int,
) -> InventoryCounts:
    by_region: dict[str, EntityStatus] = {}
    for block in regions:
        by_region[block.region_id] = block.status
    unvisited = sum(1 for g in gaps if g.gap_type is GapType.UNVISITED_STATE)
    statuses = list(by_region.values())
    verified = sum(1 for s in statuses if s is EntityStatus.VERIFIED_EXTRACTED)
    blocked = sum(1 for s in statuses if s is EntityStatus.BLOCKED)
    return InventoryCounts(
        inventory_revision=revision,
        resources_discovered=len(resources),
        resources_acquired=sum(1 for r in resources if _acquired(r)),
        resources_blocked=sum(1 for r in resources if r.status is EntityStatus.BLOCKED),
        resources_failed=sum(1 for r in resources if r.status is EntityStatus.FAILED),
        regions_discovered=len(statuses) + unvisited,
        regions_verified=verified,
        regions_needs_review=len(statuses) - verified - blocked,
        regions_blocked=blocked,
        regions_unvisited=unvisited,
    )


def _dedupe(gaps: Sequence[Gap]) -> list[Gap]:
    seen: dict[str, Gap] = {}
    for gap in gaps:
        seen.setdefault(gap.gap_id, gap)
    return list(seen.values())


def _effective(resource: Resource, gaps: Sequence[Gap]) -> EntityStatus:
    """The resource status after every gap recorded against it is applied (K14)."""
    return strictest_status(
        resource.status, [g.status for g in gaps if g.resource_id == resource.resource_id]
    )


def _status_vector(
    resources: Sequence[Resource],
    regions: Sequence[ContentBlock],
    gaps: Sequence[Gap],
    target_status: EntityStatus,
    discovery: DiscoveryState,
) -> StatusVector:
    acquisition: list[EntityStatus] = []
    for res in resources:
        base = EntityStatus.VERIFIED_EXTRACTED if _acquired(res) else res.status
        acquisition.append(
            strictest_status(
                base,
                [
                    g.status
                    for g in gaps
                    if g.resource_id == res.resource_id and g.gap_type in _ACQUISITION_GAPS
                ],
            )
        )
    fidelity = [b.status for b in regions] or [target_status]
    if target_status not in CONTENT_STATUSES:
        exploration = [target_status]
    elif discovery.closed_within_scope:
        exploration = [EntityStatus.VERIFIED_EXTRACTED]
    else:
        exploration = [EntityStatus.PARTIAL]
    vector = derive_status_vector(
        acquisition_statuses=acquisition,
        region_statuses=fidelity,
        state_statuses=exploration,
        discovery_closed=discovery.closed_within_scope,
    )
    if discovery.stop_reason and not discovery.closed_within_scope:
        vector.reasons.append(f"discovery_stop:{discovery.stop_reason}")
    return vector


# -- assembly -----------------------------------------------------------------


def assemble_url_record(
    *,
    run_id: str,
    target: Resource,
    recorded_gaps: Sequence[Gap],
    blocks: Sequence[ContentBlock],
    summaries: Mapping[str, Mapping[str, Any]],
    frames: Sequence[FrameRef] | None,
    children: Sequence[Resource] = (),
    expected_mime: str | None = None,
    history: Sequence[RunEvent] = (),
    work_outstanding: bool = False,
    tool_versions: Mapping[str, str] | None = None,
    captured_at: datetime | None = None,
    assume_discovery_closed: bool = False,
    visual_regions: Sequence[DetectedRegion] | None = None,
    detector_name: str | None = None,
    pdf_regions: Sequence[PdfRegionRef] | None = None,
    ocr_available: bool = False,
    evidence_issues: Sequence[EvidenceProblem] = (),
    host_baseline: tuple[float, int] | None = None,
) -> UrlRecord:
    """Assemble the honest verdict for one URL.

    Args:
        run_id: Run the URL belongs to.
        target: The URL's own resource.
        recorded_gaps: Gaps workers recorded for the target and its children.
        blocks: Every block of the target (all channels, all layers).
        summaries: Named summaries recorded for the target.
        frames: Frames in the target's document; None when unreadable.
        children: Required child resources (embedded assets, frames, downloads).
        expected_mime: What the discovering link or manifest promised, if known.
        history: Events for the target and children (wrong payload is sticky).
        work_outstanding: A task for this URL is still pending or leased.
        tool_versions: Versions to record; the verifier's own is added.
        captured_at: Capture time; defaults to the target's acquisition time.
        assume_discovery_closed: Counterfactual for the release gate only: treat
            discovery as closed to prove traps fail on their own merits. The
            record says so in its warnings.
        visual_regions: Text regions on the rendered screenshot (R6); None when
            no detector ran.
        detector_name: The detector that produced `visual_regions`; None when no
            detector is installed (the record warns `r6:detector_unavailable`).
        pdf_regions: Image regions of a PDF target (R8); None when not a PDF.
        ocr_available: Whether an OCR engine could have run (R8).
        evidence_issues: Missing or changed evidence the record cites.
        host_baseline: (median main-region words, pages) for the target's host (R11).

    Returns:
        The `UrlRecord`. Its badge never claims more than its gaps allow.
    """
    warnings: list[str] = []
    discovery = derive_discovery(target, summaries, recorded_gaps)
    if assume_discovery_closed and not discovery.closed_within_scope:
        discovery = DiscoveryState(
            closed_within_scope=True,
            remaining_controls=discovery.remaining_controls,
            unknown_state_space=False,
            stop_reason=COUNTERFACTUAL_WARNING,
        )
        warnings.append(COUNTERFACTUAL_WARNING)
    regions = corpus_blocks(blocks)
    resources = [target, *children]

    rule_gaps: list[Gap] = []
    for res in resources:
        rule_gaps += rule_r10_payload(
            run_id,
            res,
            expected_mime=expected_mime if res is target else None,
            history=history,
        )
    rule_gaps += rule_r2_acquisition(
        run_id, target, children=children, recorded=[*recorded_gaps, *rule_gaps]
    )
    rule_gaps += rule_r3_frames(run_id, target, frames=frames, children=children, blocks=blocks)
    rule_gaps += rule_r1_inventory(
        run_id, target, discovery=discovery, children=children, regions=regions
    )
    rule_gaps += _extended_rules(
        run_id,
        target,
        blocks=blocks,
        summaries=summaries,
        visual_regions=visual_regions,
        pdf_regions=pdf_regions,
        ocr_available=ocr_available,
        evidence_issues=evidence_issues,
        host_baseline=host_baseline,
        already=[*recorded_gaps, *rule_gaps],
    )
    gaps = _dedupe([*recorded_gaps, *rule_gaps])

    target_status = _effective(target, gaps)
    child_statuses = [_effective(c, gaps) for c in children] + [b.status for b in regions]
    outstanding = work_outstanding or any(
        c.status not in TERMINAL_ENTITY_STATUSES for c in children
    )
    badge = derive_url_badge(
        target_status=target_status,
        child_statuses=child_statuses,
        gaps=gaps,
        discovery_closed=discovery.closed_within_scope,
        work_outstanding=outstanding,
    )

    if target.status in CONTENT_STATUSES and target.sniffed_mime == "text/html":
        if "render" not in summaries:
            warnings.append("render_not_run:source_channel_only")
        if frames is not None:
            hidden = sum(1 for f in frames if f.hidden)
            if hidden:
                warnings.append(f"hidden_frames_skipped={hidden}")
            warnings.append("frames_from_source_dom_only")
    warnings += _ocr_warnings(
        target,
        summaries=summaries,
        visual_regions=visual_regions,
        detector_name=detector_name,
        pdf_regions=pdf_regions,
        ocr_available=ocr_available,
        blocks=blocks,
    )
    if target_status is not target.status:
        warnings.append(f"status_lowered_by_gaps:{target.status.value}->{target_status.value}")

    revision = sum(
        1 for e in history if e.entity_type == "resource" and e.entity_id == target.resource_id
    )
    versions = dict(tool_versions or {})
    versions["verify"] = VERIFY_VERSION
    return UrlRecord(
        run_id=run_id,
        requested_url=target.requested_url,
        final_url=target.final_url,
        aliases=list(target.aliases),
        captured_at=captured_at or target.acquired_at or datetime.now(UTC),
        tool_versions=versions,
        badge=badge,
        status_vector=_status_vector(resources, regions, gaps, target_status, discovery),
        inventory=_inventory(resources, regions, gaps, revision),
        discovery=discovery,
        gaps=gaps,
        blocks=list(regions),
        tier_reached=target.acquired_at_tier,
        warnings=warnings,
        **_freshness(target, summaries),
    )


def _extended_rules(
    run_id: str,
    target: Resource,
    *,
    blocks: Sequence[ContentBlock],
    summaries: Mapping[str, Mapping[str, Any]],
    visual_regions: Sequence[DetectedRegion] | None,
    pdf_regions: Sequence[PdfRegionRef] | None,
    ocr_available: bool,
    evidence_issues: Sequence[EvidenceProblem],
    host_baseline: tuple[float, int] | None,
    already: Sequence[Gap],
) -> list[Gap]:
    """R6, R7, R8, R11, R12, R13, volatility and evidence integrity for one target."""
    gaps = rule_evidence_integrity(run_id, target, problems=evidence_issues)
    if target.status not in CONTENT_STATUSES:
        return gaps
    gaps += rule_r6_visual_regions(run_id, target, regions=visual_regions, blocks=blocks)
    gaps += rule_r7_channel_agreement(run_id, target, blocks=blocks)
    gaps += rule_r8_pdf_regions(
        run_id, target, regions=pdf_regions, blocks=blocks, ocr_available=ocr_available
    )
    gaps += rule_r12_limits(run_id, target, summaries=summaries)
    gaps += rule_r13_layer_hygiene(run_id, target, blocks=blocks)
    gaps += rule_volatility(run_id, target, summaries=summaries)
    explained = any(g.resource_id == target.resource_id for g in [*already, *gaps])
    if host_baseline is not None and not explained:
        median, pages = host_baseline
        gaps += rule_r11_degradation(
            run_id,
            target,
            main_words=main_word_count(blocks),
            host_median=median,
            host_pages=pages,
        )
    return gaps


def _ocr_warnings(
    target: Resource,
    *,
    summaries: Mapping[str, Mapping[str, Any]],
    visual_regions: Sequence[DetectedRegion] | None,
    detector_name: str | None,
    pdf_regions: Sequence[PdfRegionRef] | None,
    ocr_available: bool,
    blocks: Sequence[ContentBlock],
) -> list[str]:
    """Say when R6 or R8 could not judge for lack of an engine (record, not block)."""
    if target.status not in CONTENT_STATUSES:
        return []
    warnings: list[str] = []
    if "render" in summaries and visual_regions is None:
        warnings.append(
            "r6:detector_unavailable" if detector_name is None else "r6:screenshot_unavailable"
        )
    if pdf_regions is not None and not ocr_available:
        uncovered = uncovered_pdf_regions(pdf_regions, blocks)
        if uncovered:
            warnings.append(f"r8:ocr_unavailable:regions={len(uncovered)}")
    return warnings


def _freshness(target: Resource, summaries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Identity, validators, page dates and change status: the next run's baseline."""
    followed = _nested(summaries.get("followups"), "followed") or []
    refs = [
        FollowupRef(
            url=str(item["url"]),
            edge_kind=item.get("edge"),
            expected_mime=item.get("expected_mime"),
        )
        for item in followed
        if isinstance(item, dict) and item.get("url")
    ]
    return {
        "schedule_key": target.schedule_key,
        "resource_status": target.status,
        "resource_status_reason": target.status_reason,
        "sniffed_mime": target.sniffed_mime,
        "payload_sha256": target.payload_sha256,
        "http_etag": target.http_etag,
        "http_last_modified": target.http_last_modified,
        "page_modified_at": target.page_modified_at,
        "page_modified_source": target.page_modified_source,
        "page_published_at": target.page_published_at,
        "content_fingerprint": target.content_fingerprint,
        "change_status": target.change_status,
        "change_basis": target.change_basis,
        "previous_run_id": target.previous_run_id,
        "reused_from_run_id": target.reused_from_run_id,
        "followups": refs,
        "summaries": {k: dict(v) for k, v in summaries.items()},
    }


def excluded_url_record(
    run_id: str,
    excluded: ExcludedInput,
    *,
    tool_versions: Mapping[str, str] | None = None,
    captured_at: datetime | None = None,
) -> UrlRecord:
    """Record for a requested URL the planner refused, so it stays in the denominator."""
    resource_id = "unscheduled-" + hashlib.sha256(excluded.requested_url.encode()).hexdigest()[:16]
    gap = Gap(
        gap_id=gap_id_for(run_id, resource_id, "plan:excluded"),
        run_id=run_id,
        resource_id=resource_id,
        gap_type=GapType.POLICY_EXCLUDED,
        status=EntityStatus.EXCLUDED_BY_POLICY,
        reason=excluded.reason,
    )
    versions = dict(tool_versions or {})
    versions["verify"] = VERIFY_VERSION
    return UrlRecord(
        run_id=run_id,
        requested_url=excluded.requested_url,
        final_url=None,
        captured_at=captured_at or datetime.now(UTC),
        tool_versions=versions,
        badge=UrlBadge.EXCLUDED,
        status_vector=StatusVector(
            discovery_closed=False,
            acquisition=EntityStatus.EXCLUDED_BY_POLICY,
            fidelity=EntityStatus.EXCLUDED_BY_POLICY,
            state_exploration=EntityStatus.EXCLUDED_BY_POLICY,
            reasons=[f"excluded_at_plan:{excluded.reason}"],
        ),
        inventory=InventoryCounts(
            inventory_revision=0,
            resources_discovered=1,
            resources_acquired=0,
            resources_blocked=0,
            resources_failed=0,
            regions_discovered=0,
            regions_verified=0,
            regions_needs_review=0,
            regions_blocked=0,
            regions_unvisited=0,
        ),
        discovery=DiscoveryState(
            closed_within_scope=False,
            remaining_controls=0,
            unknown_state_space=True,
            stop_reason="excluded_at_plan",
        ),
        gaps=[gap],
    )


def frames_from_evidence(store: EvidenceStore) -> Callable[[Resource], list[FrameRef] | None]:
    """Frame inventory read from a resource's stored source payload.

    Returns None (inventory unavailable) when the payload is missing or not
    HTML. Frames injected by script appear only in the rendered DOM and are
    not seen here; records say `frames_from_source_dom_only`.
    """

    def read(resource: Resource) -> list[FrameRef] | None:
        sha = resource.payload_sha256
        if sha is None or resource.sniffed_mime != "text/html" or not store.exists(sha):
            return None
        extraction = extract_html_source(
            store.get(sha),
            base_url=resource.final_url or resource.requested_url,
            evidence_id=f"evidence:{sha}",
            resource_id=resource.resource_id,
            state_id="verify|frames",
        )
        return [
            FrameRef(url=a.url, hidden=a.hidden) for a in extraction.assets if a.kind == "frame"
        ]

    return read


def _host(resource: Resource) -> str:
    return (urlsplit(resource.final_url or resource.requested_url).hostname or "").lower()


def _host_baselines(
    resources: Iterable[Resource], blocks_by_id: Mapping[str, Sequence[ContentBlock]]
) -> dict[str, tuple[float, int]]:
    """(median main-region words, page count) per host over pages that claim content (R11)."""
    words: dict[str, list[int]] = {}
    for res in resources:
        if res.status in (EntityStatus.VERIFIED_EXTRACTED, EntityStatus.EXTRACTED_UNVERIFIED):
            words.setdefault(_host(res), []).append(
                main_word_count(blocks_by_id.get(res.resource_id, []))
            )
    return {host: (float(statistics.median(ws)), len(ws)) for host, ws in words.items()}


def url_records_for_run(
    reader: RunReader,
    *,
    run_id: str,
    plan: FrontierPlan | None = None,
    frames_for: Callable[[Resource], Sequence[FrameRef] | None] | None = None,
    expectations: Mapping[str, str] | None = None,
    outstanding: Collection[str] = (),
    tool_versions: Mapping[str, str] | None = None,
    assume_discovery_closed: bool = False,
    evidence: EvidenceStore | None = None,
    detector: TextRegionDetector | None = None,
    ocr_available: bool | None = None,
) -> list[UrlRecord]:
    """One record per scheduled URL plus one per excluded input.

    Args:
        reader: Ledger read side.
        run_id: Run to assemble.
        plan: The frontier plan; scheduled entries without a resource yet are
            RUNNING, and excluded inputs get EXCLUDED records. Without a plan,
            only resources in the ledger are reported.
        frames_for: Frame inventory per resource (see `frames_from_evidence`);
            None means no inventory, which R3 reports as a review gap.
        expectations: Expected media type by requested URL or schedule key.
        outstanding: Resource ids with a pending or leased task that will run.
            A run that skipped rendering passes an empty set: a RENDER task
            that will never run is not outstanding work.
        tool_versions: Versions recorded on every record.
        assume_discovery_closed: Release-gate counterfactual; see
            `assemble_url_record`.
        evidence: Evidence store; enables evidence integrity, R8 PDF regions and
            (with a detector) R6 on the rendered screenshot.
        detector: Text-region detector for R6 (`default_detector()`); None when
            no engine is installed, which records warn about.
        ocr_available: Whether OCR could run (R8); None detects it.
    """
    expected = dict(expectations or {})
    ocr = is_ocr_available() if ocr_available is None else ocr_available
    pdf_regions_for = pdf_regions_from_evidence(evidence) if evidence is not None else None
    resources: dict[str, Resource] = {r.schedule_key: r for r in reader.list_resources(run_id)}
    if plan is not None:
        for entry in plan.entries:
            resources.setdefault(
                entry.schedule_key,
                Resource(
                    resource_id=f"pending-{hashlib.sha256(entry.schedule_key.encode()).hexdigest()[:16]}",
                    run_id=run_id,
                    requested_url=entry.requested_url,
                    schedule_key=entry.schedule_key,
                    aliases=list(entry.aliases),
                    mandatory=entry.mandatory,
                    status=EntityStatus.DISCOVERED,
                    status_reason="scheduled_not_started",
                ),
            )
    blocks_by_id = {r.resource_id: reader.get_blocks(r.resource_id) for r in resources.values()}
    baselines = _host_baselines(resources.values(), blocks_by_id)
    records: list[UrlRecord] = []
    for key, res in resources.items():
        history = reader.get_events(run_id, res.resource_id)
        blocks = blocks_by_id[res.resource_id]
        summaries = reader.get_summaries(res.resource_id)
        regions: list[DetectedRegion] | None = None
        if detector is not None and evidence is not None and "render" in summaries:
            shot = screenshot_sha(history, res.resource_id)
            if shot is not None and evidence.exists(shot):
                regions = detector.detect(evidence.get(shot))
        records.append(
            assemble_url_record(
                run_id=run_id,
                target=res,
                recorded_gaps=reader.get_gaps(res.resource_id),
                blocks=blocks,
                summaries=summaries,
                frames=frames_for(res) if frames_for is not None else None,
                expected_mime=expected.get(res.requested_url) or expected.get(key),
                history=history,
                work_outstanding=res.resource_id in outstanding,
                tool_versions=tool_versions,
                assume_discovery_closed=assume_discovery_closed,
                visual_regions=regions,
                detector_name=detector.name if detector is not None else None,
                pdf_regions=pdf_regions_for(res) if pdf_regions_for is not None else None,
                ocr_available=ocr,
                evidence_issues=(
                    evidence_problems(evidence, res, blocks) if evidence is not None else ()
                ),
                host_baseline=baselines.get(_host(res)),
            )
        )
    if plan is not None:
        records += [
            excluded_url_record(run_id, x, tool_versions=tool_versions) for x in plan.excluded
        ]
    return records
