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
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from src.integrations.evidence_store import EvidenceStore
from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.contracts import (
    ContentBlock,
    DiscoveryState,
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
from src.modules.content_processor.verify.gaps import gap_id_for, strictest_status
from src.modules.content_processor.verify.rules import (
    CONTENT_STATUSES,
    FrameRef,
    rule_r1_inventory,
    rule_r2_acquisition,
    rule_r3_frames,
    rule_r10_payload,
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

    `MemoryLedger` satisfies it today; the durable ledger needs CR-005.
    `resources` is keyed by schedule key.
    """

    @property
    def resources(self) -> Mapping[str, Resource]:
        """Latest resource per schedule key."""
        ...

    @property
    def summaries(self) -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
        """Named summaries per resource id (render, explore, followups...)."""
        ...

    @property
    def events(self) -> Sequence[RunEvent]:
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
    )


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
    """
    expected = dict(expectations or {})
    resources: dict[str, Resource] = dict(reader.resources)
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
    records: list[UrlRecord] = []
    for key, res in resources.items():
        history = [e for e in reader.events if e.entity_id == res.resource_id]
        records.append(
            assemble_url_record(
                run_id=run_id,
                target=res,
                recorded_gaps=reader.get_gaps(res.resource_id),
                blocks=reader.get_blocks(res.resource_id),
                summaries=reader.summaries.get(res.resource_id, {}),
                frames=frames_for(res) if frames_for is not None else None,
                expected_mime=expected.get(res.requested_url) or expected.get(key),
                history=history,
                work_outstanding=res.resource_id in outstanding,
                tool_versions=tool_versions,
                assume_discovery_closed=assume_discovery_closed,
            )
        )
    if plan is not None:
        records += [
            excluded_url_record(run_id, x, tool_versions=tool_versions) for x in plan.excluded
        ]
    return records
