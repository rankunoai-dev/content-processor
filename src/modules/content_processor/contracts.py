"""Typed contracts for the content processor (ARCHITECTURE.md §9).

Every object crossing a module boundary is one of these `StrictModel`s. Rules
that the gap audit and playbook fix are enforced here as validators so a scope
that violates policy fails at admission, not halfway through a run.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from src.core.schemas import StrictModel
from src.modules.content_processor.enums import (
    PAID_TIERS,
    AccessTier,
    AuthorizationKind,
    BlockKind,
    ChangeStatus,
    ContentLayer,
    EntityStatus,
    EnvironmentProfile,
    GapType,
    PriorityClass,
    RedirectKind,
    RobotsPolicy,
    ScopeMode,
    SourceFormat,
    TaskKind,
    TaskState,
    UiRegion,
    UrlBadge,
)

__all__ = [
    "Authorization",
    "BlockVerdict",
    "Budgets",
    "ClaimContext",
    "ContentBlock",
    "DiscoveryState",
    "FormPersona",
    "FollowupRef",
    "Gap",
    "InteractionPolicy",
    "InventoryCounts",
    "Provenance",
    "RedirectHop",
    "Region",
    "Resource",
    "RunEvent",
    "RunScope",
    "State",
    "StatusVector",
    "T6Approval",
    "Task",
    "UrlRecord",
]

_T6_TOKEN = re.compile(
    r"t6:(?P<ticket>[A-Za-z0-9_-]+):(?P<a1>[A-Za-z0-9._@-]+):(?P<a2>[A-Za-z0-9._@-]+)"
    r":(?P<expires>\d{4}-\d{2}-\d{2})"
)

AllowedAction = Literal[
    "scroll",
    "expand_disclosure",
    "select_tab",
    "hover_info",
    "open_informational_dialog",
    "keyboard_traverse",
    "paginate_get",
]


class T6Approval(StrictModel):
    """Parsed dual sign-off token for the policy-gated tier."""

    ticket: str
    approver_1: str
    approver_2: str
    expires_on: date

    def is_valid_on(self, day: date) -> bool:
        """True when two distinct approvers signed and the token has not expired."""
        return self.approver_1 != self.approver_2 and day <= self.expires_on


class Authorization(StrictModel):
    """Who permitted this run and on what basis."""

    kind: AuthorizationKind
    reference: str = Field(min_length=1, description="Contract/ticket id, 'public', or a t6 token")
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    hosts: list[str] = Field(min_length=1)
    robots_exceptions: list[str] = Field(
        default_factory=list, description="Path prefixes exempt under respect_with_exceptions"
    )

    def t6_approval(self) -> T6Approval | None:
        """Extract the T6 token from `reference`, if present and well formed."""
        match = _T6_TOKEN.search(self.reference)
        if match is None:
            return None
        return T6Approval(
            ticket=match["ticket"],
            approver_1=match["a1"],
            approver_2=match["a2"],
            expires_on=date.fromisoformat(match["expires"]),
        )


def _default_actions() -> list[AllowedAction]:
    return ["scroll", "expand_disclosure", "select_tab"]


class InteractionPolicy(StrictModel):
    """Which read-only interactions the state explorer may perform."""

    allowed: list[AllowedAction] = Field(default_factory=_default_actions)
    max_actions_per_url: int = Field(default=50, ge=0, le=500)
    max_scroll_passes: int = Field(default=20, ge=0, le=200)
    side_effect_rule: Literal["get_only_no_navigation"] = "get_only_no_navigation"


class Budgets(StrictModel):
    """Hard stops for a run. Exhaustion ends the run as PARTIAL, never silently."""

    max_runtime_s: int = Field(default=6 * 3600, ge=60, le=7 * 24 * 3600)
    max_requests: int = Field(default=200_000, ge=1)
    max_bytes: int = Field(default=50 * 1024**3, ge=1)
    max_evidence_bytes: int = Field(default=20 * 1024**3, ge=1)
    max_remediation_rounds: int = Field(default=2, ge=0, le=5)
    cost_ceiling_usd: float = Field(default=0.0, ge=0.0)


def _default_robots(kind: AuthorizationKind) -> RobotsPolicy:
    if kind is AuthorizationKind.OWNER_AUTHORIZED:
        return RobotsPolicy.INFORMATIONAL
    return RobotsPolicy.RESPECT


_FREEMAIL = frozenset(
    {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me", "aol.com"}
)


class FormPersona(StrictModel):
    """A real, consenting business identity for lead forms (feasibility doc, Path A)."""

    full_name: str = Field(min_length=3)
    business_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    company: str = Field(min_length=2)
    job_title: str = Field(min_length=2)
    consent_reference: str = Field(min_length=3, description="Where the person consented")
    approval_reference: str = Field(min_length=3, description="AI Lead approval for this use")
    max_submissions_per_host_per_day: int = Field(default=1, ge=0, le=5)

    @model_validator(mode="after")
    def _no_freemail(self) -> FormPersona:
        """Business identities only; invented or free-mail personas are refused."""
        domain = self.business_email.rsplit("@", 1)[-1].lower()
        if domain in _FREEMAIL:
            msg = "persona.business_email must be a business domain, not free mail"
            raise ValueError(msg)
        return self


class RunScope(StrictModel):
    """Declared scope of a run. Hashed into `scope_hash` for recrawl identity."""

    schema_version: Literal["1.0"] = "1.0"
    tenant_id: str = Field(min_length=1)
    profile: EnvironmentProfile = EnvironmentProfile.DEVELOPMENT
    mode: ScopeMode = ScopeMode.SUPPLIED_URLS_PLUS_EMBEDDED
    input_urls: list[str] = Field(min_length=1, max_length=100_000)
    mandatory_urls: list[str] = Field(default_factory=list)
    hosts_in_scope: list[str] = Field(min_length=1)
    linked_download_depth: int = Field(default=1, ge=0, le=3)
    allowed_mime_families: list[str] = Field(
        default_factory=lambda: ["text/html", "application/pdf", "image/*"]
    )
    interaction_policy: InteractionPolicy = Field(default_factory=InteractionPolicy)
    viewport: tuple[int, int] = (1440, 900)
    locale: str = "en-US"
    timezone: str = "UTC"
    geography: str = "US"
    consent_state: Literal["ordinary_public_choices_applied", "none"] = (
        "ordinary_public_choices_applied"
    )
    access_context: Literal["public_anonymous", "authenticated_client"] = "public_anonymous"
    authorization: Authorization
    access_tier_ceiling: AccessTier = AccessTier.T3_FIDELITY_BROWSER
    robots_policy: RobotsPolicy | None = None
    analytics_suppression: bool = False
    content_includes: list[str] = Field(default_factory=lambda: ["visual", "accessibility"])
    content_excludes: list[str] = Field(
        default_factory=lambda: ["video_frames", "hidden_revisions"]
    )
    budgets: Budgets = Field(default_factory=Budgets)
    retention_class: Literal["standard_90d", "extended_1y", "legal_hold"] = "standard_90d"
    pii_policy: Literal["tag_and_redact_exports", "tag_only"] = "tag_and_redact_exports"
    ocr_languages: list[str] | None = Field(
        default=None, description="Restrict OCR packs; None = full multilingual routing"
    )
    recrawl_of_run_id: str | None = None
    gated_content_policy: Literal["report_gap", "persona", "hitl"] = Field(
        default="report_gap",
        description="report_gap always runs; persona adds authorized form filling; "
        "hitl adds the review-queue export",
    )
    persona: FormPersona | None = None
    egress_allow_private: bool = Field(
        default=False,
        description="Development only: allow loopback/private hosts for the local fixture server.",
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_robots_default(cls, data: Any) -> Any:
        """Fill `robots_policy` from the authorization kind when not given."""
        if not isinstance(data, dict) or data.get("robots_policy") is not None:
            return data
        auth = data.get("authorization")
        kind_raw = auth.get("kind") if isinstance(auth, dict) else getattr(auth, "kind", None)
        if kind_raw is None:
            return data
        data = dict(data)
        data["robots_policy"] = _default_robots(AuthorizationKind(kind_raw))
        return data

    @model_validator(mode="after")
    def _enforce_policy(self) -> RunScope:
        """Reject scopes that the playbook forbids."""
        if (
            self.robots_policy is RobotsPolicy.RESPECT_WITH_EXCEPTIONS
            and not self.authorization.robots_exceptions
        ):
            msg = "respect_with_exceptions requires authorization.robots_exceptions"
            raise ValueError(msg)
        if self.access_tier_ceiling in PAID_TIERS and self.budgets.cost_ceiling_usd <= 0.0:
            msg = f"{self.access_tier_ceiling} needs budgets.cost_ceiling_usd > 0"
            raise ValueError(msg)
        if self.access_tier_ceiling is AccessTier.T6_POLICY_GATED:
            approval = self.authorization.t6_approval()
            today = datetime.now(UTC).date()
            if approval is None or not approval.is_valid_on(today):
                msg = (
                    "T6 requires a valid, unexpired dual sign-off token in authorization.reference"
                )
                raise ValueError(msg)
        if self.egress_allow_private and self.profile is not EnvironmentProfile.DEVELOPMENT:
            msg = "egress_allow_private is permitted only in the development profile"
            raise ValueError(msg)
        if self.gated_content_policy == "persona" and self.persona is None:
            msg = "gated_content_policy=persona requires scope.persona"
            raise ValueError(msg)
        if (
            self.persona is not None
            and self.authorization.kind is AuthorizationKind.PUBLIC_THIRD_PARTY
            and not self.persona.approval_reference.startswith("persona:")
        ):
            msg = (
                "persona use on public third-party hosts needs an AI Lead approval reference "
                "of the form persona:<ticket>"
            )
            raise ValueError(msg)
        if self.access_context == "authenticated_client" and (
            self.authorization.kind is not AuthorizationKind.OWNER_AUTHORIZED
        ):
            msg = "authenticated_client access requires OWNER_AUTHORIZED authorization"
            raise ValueError(msg)
        return self

    def scope_hash(self) -> str:
        """Deterministic identity of the declared scope (used for recrawl matching)."""
        payload = self.model_dump_json(exclude={"recrawl_of_run_id"})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RedirectHop(StrictModel):
    """One hop of a typed redirect chain (case 97)."""

    kind: RedirectKind
    from_url: str
    to_url: str
    http_status: int | None = Field(default=None, ge=100, le=599)


class BlockVerdict(StrictModel):
    """Output of the block classifier for one response."""

    kind: BlockKind
    vendor: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)

    @property
    def is_block(self) -> bool:
        """True for anything other than a clean, expected payload."""
        return self.kind is not BlockKind.NONE


class Resource(StrictModel):
    """A fetchable thing: page, document, image, frame source, download."""

    resource_id: str
    run_id: str
    requested_url: str
    final_url: str | None = None
    schedule_key: str
    aliases: list[str] = Field(default_factory=list)
    redirect_chain: list[RedirectHop] = Field(default_factory=list)
    declared_mime: str | None = None
    sniffed_mime: str | None = None
    extension_hint: str | None = None
    content_disposition_name: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    transport_error: str | None = None
    payload_sha256: str | None = None
    payload_bytes: int | None = Field(default=None, ge=0)
    acquired_at: datetime | None = None
    acquired_at_tier: AccessTier | None = None
    block_verdict: BlockVerdict | None = None
    scope_decision: Literal["in_scope", "excluded_by_policy", "out_of_scope_asset"] = "in_scope"
    mandatory: bool = False
    status: EntityStatus = EntityStatus.DISCOVERED
    status_reason: str | None = None
    attempt: int = Field(default=1, ge=1)
    expected_mime: str | None = None
    expected_mime_source: str | None = None
    # -- freshness and recrawl ---------------------------------------------------
    http_etag: str | None = None
    http_last_modified: str | None = None
    page_modified_at: datetime | None = None
    page_modified_source: str | None = None
    page_published_at: datetime | None = None
    content_fingerprint: str | None = Field(
        default=None, description="Hash of the normalised visible text (ignores markup churn)"
    )
    change_status: ChangeStatus | None = None
    change_basis: str | None = None
    previous_run_id: str | None = None
    reused_from_run_id: str | None = None


class State(StrictModel):
    """A dynamic state discovered on a page (e.g. tab selected, disclosure opened)."""

    state_id: str
    run_id: str
    resource_id: str
    state_key: str
    parent_state_id: str | None = None
    status: EntityStatus = EntityStatus.DISCOVERED
    evidence_ids: list[str] = Field(default_factory=list)
    action: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Region(StrictModel):
    """A bounded area or container on a page/document in a specific state."""

    region_id: str
    run_id: str
    resource_id: str
    region_key: str
    state_id: str | None = None
    status: EntityStatus = EntityStatus.DISCOVERED
    bbox: tuple[float, float, float, float] | None = None
    role: str = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Gap(StrictModel):
    """A required item that is not verified, with the evidence for why."""

    gap_id: str
    run_id: str
    resource_id: str
    state_id: str | None = None
    region_id: str | None = None
    gap_type: GapType
    status: EntityStatus
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    retry_eligible: bool = False
    tier_reached: AccessTier | None = None
    inventory_revision: int = Field(default=0, ge=0)


class Provenance(StrictModel):
    """Where a block came from, precisely enough to reproduce it."""

    selector: str | None = None
    xpath: str | None = None
    pdf_object_id: str | None = None
    ocr_params: dict[str, Any] | None = None
    frame_path: str = "main"


class ClaimContext(StrictModel):
    """Context needed before two claims may be compared (Stage 4)."""

    entity: str | None = None
    product_or_variant: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    audience: str | None = None
    geography: str | None = None
    conditions: list[str] = Field(default_factory=list)


class ContentBlock(StrictModel):
    """One extracted unit of content with full provenance."""

    block_id: str
    block_key: str
    resource_id: str
    state_id: str
    region_id: str
    heading_path: list[str] = Field(default_factory=list)
    reading_order: int = Field(ge=0)
    page_index: int | None = Field(default=None, ge=0)
    page_label: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    layer: ContentLayer
    role: Literal["main", "nav", "footer", "aside", "legal", "chrome", "unknown"] = "unknown"
    language: str | None = None
    raw_text: str
    normalized_text: str
    normalization_ops: list[str] = Field(default_factory=list)
    method: str
    tool_version: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: EntityStatus
    evidence_ids: list[str] = Field(min_length=1)
    provenance: Provenance = Field(default_factory=Provenance)
    claim_context: ClaimContext | None = None
    dedupe_of: str | None = None
    # -- source tagging (where the content came from, and when) ------------
    source_format: SourceFormat | None = Field(
        default=None, description="Artefact type: html, pdf, image, office_doc, ..."
    )
    ui_region: UiRegion | None = Field(
        default=None, description="Place on the page: hamburger_menu, snippet, palette, ..."
    )
    source_url: str | None = Field(default=None, description="URL the content was taken from")
    extracted_at: datetime | None = Field(default=None, description="When we extracted it (UTC)")
    page_modified_at: datetime | None = Field(
        default=None, description="When the site says the page or file was last updated"
    )
    page_modified_source: str | None = Field(
        default=None, description="Which signal gave page_modified_at (jsonld, meta, http, ...)"
    )
    reused_from_run_id: str | None = Field(
        default=None, description="Set when the page was unchanged and this block was reused"
    )


class StatusVector(StrictModel):
    """The four completeness questions, answered separately."""

    discovery_closed: bool
    acquisition: EntityStatus
    fidelity: EntityStatus
    state_exploration: EntityStatus
    reasons: list[str] = Field(default_factory=list)


class InventoryCounts(StrictModel):
    """Counts with their denominators made explicit."""

    inventory_revision: int = Field(ge=0)
    resources_discovered: int = Field(ge=0)
    resources_acquired: int = Field(ge=0)
    resources_blocked: int = Field(ge=0)
    resources_failed: int = Field(ge=0)
    regions_discovered: int = Field(ge=0)
    regions_verified: int = Field(ge=0)
    regions_needs_review: int = Field(ge=0)
    regions_blocked: int = Field(ge=0)
    regions_unvisited: int = Field(ge=0)
    denominator_basis: Literal["observed_inventory_only"] = "observed_inventory_only"


class DiscoveryState(StrictModel):
    """Whether bounded discovery closed, and if not, why."""

    closed_within_scope: bool
    remaining_controls: int = Field(ge=0)
    unknown_state_space: bool
    stop_reason: str | None = None


class FollowupRef(StrictModel):
    """A depth-1 child a page pointed to (kept so an unchanged page can re-queue it)."""

    url: str
    edge_kind: str | None = None
    expected_mime: str | None = None


class UrlRecord(StrictModel):
    """Per-URL export record (ARCHITECTURE.md §9)."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    requested_url: str
    final_url: str | None
    aliases: list[str] = Field(default_factory=list)
    captured_at: datetime
    tool_versions: dict[str, str]
    badge: UrlBadge
    status_vector: StatusVector
    inventory: InventoryCounts
    discovery: DiscoveryState
    gaps: list[Gap] = Field(default_factory=list)
    blocks: list[ContentBlock] = Field(default_factory=list)
    template_fingerprint: str | None = None
    design_evidence_id: str | None = None
    tier_reached: AccessTier | None = None
    warnings: list[str] = Field(default_factory=list)
    # -- identity, freshness and recrawl baseline ---------------------------------
    schedule_key: str | None = None
    resource_status: EntityStatus | None = None
    resource_status_reason: str | None = None
    sniffed_mime: str | None = None
    payload_sha256: str | None = None
    http_etag: str | None = None
    http_last_modified: str | None = None
    page_modified_at: datetime | None = None
    page_modified_source: str | None = None
    page_published_at: datetime | None = None
    content_fingerprint: str | None = None
    change_status: ChangeStatus | None = None
    change_basis: str | None = None
    previous_run_id: str | None = None
    reused_from_run_id: str | None = None
    followups: list[FollowupRef] = Field(default_factory=list)
    summaries: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Task(StrictModel):
    """A ledger task row."""

    id: int | None = None
    run_id: str
    kind: TaskKind
    idempotency_key: str
    host: str
    priority: PriorityClass
    state: TaskState = TaskState.PENDING
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    not_before: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    last_error: str | None = None


class RunEvent(StrictModel):
    """Append-only event; the live status stream is built from these."""

    seq: int | None = None
    run_id: str
    entity_type: Literal["run", "resource", "state", "region", "task", "host"]
    entity_id: str
    old_state: str | None
    new_state: str
    inventory_revision: int = Field(default=0, ge=0)
    reason: str | None = None
    attempt: int = Field(default=0, ge=0)
    evidence_ids: list[str] = Field(default_factory=list)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
