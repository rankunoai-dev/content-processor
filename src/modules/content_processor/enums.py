"""Enumerations shared by every content-processor contract.

These are the vocabularies fixed by ADR-0005 (two-level status taxonomy) and
the gap audit Part D. Adding a member is a contract change: update the
architecture document and the fixture expectations in the same commit.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

__all__ = [
    "BADGE_PRECEDENCE",
    "PAID_TIERS",
    "TERMINAL_ENTITY_STATUSES",
    "TIER_ORDER",
    "AccessTier",
    "AuthorizationKind",
    "BlockKind",
    "ChangeStatus",
    "ContentLayer",
    "EntityStatus",
    "EnvironmentProfile",
    "GapType",
    "PriorityClass",
    "RedirectKind",
    "RobotsPolicy",
    "ScopeMode",
    "SourceFormat",
    "TaskKind",
    "TaskState",
    "UiRegion",
    "UrlBadge",
    "tier_rank",
]


class EntityStatus(StrEnum):
    """Status of one resource, state, region or extraction."""

    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    VERIFIED_EXTRACTED = "VERIFIED_EXTRACTED"
    EXTRACTED_UNVERIFIED = "EXTRACTED_UNVERIFIED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    EXCLUDED_BY_POLICY = "EXCLUDED_BY_POLICY"
    VERIFIED_NONCONTENT = "VERIFIED_NONCONTENT"


TERMINAL_ENTITY_STATUSES: frozenset[EntityStatus] = frozenset(
    {
        EntityStatus.VERIFIED_EXTRACTED,
        EntityStatus.EXTRACTED_UNVERIFIED,
        EntityStatus.PARTIAL,
        EntityStatus.BLOCKED,
        EntityStatus.FAILED,
        EntityStatus.UNSUPPORTED,
        EntityStatus.BUDGET_EXHAUSTED,
        EntityStatus.EXCLUDED_BY_POLICY,
        EntityStatus.VERIFIED_NONCONTENT,
    }
)


class UrlBadge(StrEnum):
    """Single badge derived for a URL or a run. Never hides the status vector."""

    RUNNING = "RUNNING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PARTIAL = "PARTIAL"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    VERIFIED_WITHIN_SCOPE = "VERIFIED_WITHIN_SCOPE"
    EXCLUDED = "EXCLUDED"


BADGE_PRECEDENCE: tuple[UrlBadge, ...] = (
    UrlBadge.RUNNING,
    UrlBadge.FAILED,
    UrlBadge.BLOCKED,
    UrlBadge.PARTIAL,
    UrlBadge.NEEDS_REVIEW,
    UrlBadge.VERIFIED_WITHIN_SCOPE,
    UrlBadge.EXCLUDED,
)


class GapType(StrEnum):
    """Why a required item is not verified."""

    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    ACQUISITION_FAILED = "ACQUISITION_FAILED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    BUDGET_STOPPED = "BUDGET_STOPPED"
    DISCOVERY_OPEN = "DISCOVERY_OPEN"
    FIDELITY_DISAGREEMENT = "FIDELITY_DISAGREEMENT"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNVISITED_STATE = "UNVISITED_STATE"
    LIMIT_HIT = "LIMIT_HIT"
    POLICY_EXCLUDED = "POLICY_EXCLUDED"
    SHADOW_BLOCK_SUSPECTED = "SHADOW_BLOCK_SUSPECTED"
    DEGRADED_VARIANT = "DEGRADED_VARIANT"
    VOLATILE = "VOLATILE"
    SOFT_REDIRECT_SUSPECTED = "SOFT_REDIRECT_SUSPECTED"
    SOFT_404_SUSPECTED = "SOFT_404_SUSPECTED"


class ContentLayer(StrEnum):
    """Truth layer of a content block. Layers are never merged silently."""

    RENDERED_VISIBLE = "rendered_visible"
    COLLAPSED_PUBLISHED = "collapsed_published"
    ACCESSIBILITY_ONLY = "accessibility_only"
    HIDDEN = "hidden"
    METADATA = "metadata"
    SOURCE_ONLY = "source_only"
    UNCERTAIN = "uncertain"
    THIRD_PARTY_CHROME = "third_party_chrome"
    UGC = "ugc"
    ICON_ARTIFACT = "icon_artifact"
    PAYWALLED_IN_SOURCE = "paywalled_in_source"
    HIDDEN_REVISION = "hidden_revision"
    DESIGN_EVIDENCE = "design_evidence"


class AccessTier(StrEnum):
    """Rung of the access ladder (playbook section 2)."""

    T0_AUTHORIZED = "T0_AUTHORIZED"
    T1_POLITE_HTTP = "T1_POLITE_HTTP"
    T2_IMPERSONATED_HTTP = "T2_IMPERSONATED_HTTP"
    T3_FIDELITY_BROWSER = "T3_FIDELITY_BROWSER"
    T4_PROXIED = "T4_PROXIED"
    T5_MANAGED_UNBLOCKER = "T5_MANAGED_UNBLOCKER"
    T6_POLICY_GATED = "T6_POLICY_GATED"


TIER_ORDER: tuple[AccessTier, ...] = (
    AccessTier.T0_AUTHORIZED,
    AccessTier.T1_POLITE_HTTP,
    AccessTier.T2_IMPERSONATED_HTTP,
    AccessTier.T3_FIDELITY_BROWSER,
    AccessTier.T4_PROXIED,
    AccessTier.T5_MANAGED_UNBLOCKER,
    AccessTier.T6_POLICY_GATED,
)

PAID_TIERS: frozenset[AccessTier] = frozenset(
    {AccessTier.T4_PROXIED, AccessTier.T5_MANAGED_UNBLOCKER, AccessTier.T6_POLICY_GATED}
)


def tier_rank(tier: AccessTier) -> int:
    """Position of a tier on the ladder; T0 is 0."""
    return TIER_ORDER.index(tier)


class BlockKind(StrEnum):
    """Outcome of the block classifier for one response."""

    NONE = "NONE"
    RATE = "RATE"
    TRANSPORT = "TRANSPORT"
    FINGERPRINT = "FINGERPRINT"
    IP_REPUTATION = "IP_REPUTATION"
    JS_CHALLENGE = "JS_CHALLENGE"
    INTERACTIVE_CAPTCHA = "INTERACTIVE_CAPTCHA"
    AUTH_WALL = "AUTH_WALL"
    PAYWALL = "PAYWALL"
    GEO_GATE = "GEO_GATE"
    LEGAL = "LEGAL"
    PAY_PER_CRAWL = "PAY_PER_CRAWL"
    AI_OPTOUT = "AI_OPTOUT"
    SHADOW_BLOCK_SUSPECTED = "SHADOW_BLOCK_SUSPECTED"
    SOFT_404 = "SOFT_404"
    WRONG_PAYLOAD = "WRONG_PAYLOAD"


class TaskKind(StrEnum):
    """Unit of work in the ledger. Some kinds consume a host slot (see ledger.sql)."""

    FETCH = "FETCH"
    RENDER = "RENDER"
    UNLOCK = "UNLOCK"
    EXTRACT_DOC = "EXTRACT_DOC"
    EXTRACT_IMAGE = "EXTRACT_IMAGE"
    OCR = "OCR"
    VERIFY = "VERIFY"
    ANNOTATE = "ANNOTATE"
    EXPORT = "EXPORT"


class TaskState(StrEnum):
    """Lifecycle of a ledger task."""

    PENDING = "PENDING"
    LEASED = "LEASED"
    DONE = "DONE"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"
    CANCELLED = "CANCELLED"


class PriorityClass(IntEnum):
    """Lower runs first."""

    MANDATORY = 0
    HTML_PAGE = 1
    DOCUMENT = 2
    EMBEDDED_ASSET = 3
    OCR_OR_REMEDIATION = 4
    ANNOTATION = 5


class ScopeMode(StrEnum):
    """What a run is allowed to discover."""

    SUPPLIED_URLS = "SUPPLIED_URLS"
    SUPPLIED_URLS_PLUS_EMBEDDED = "SUPPLIED_URLS_PLUS_EMBEDDED"
    RECURSIVE_SITE = "RECURSIVE_SITE"


class AuthorizationKind(StrEnum):
    """Relationship with the target host; decides the default ceiling."""

    OWNER_AUTHORIZED = "OWNER_AUTHORIZED"
    PUBLIC_THIRD_PARTY = "PUBLIC_THIRD_PARTY"
    LICENSED_SOURCE = "LICENSED_SOURCE"


class RobotsPolicy(StrEnum):
    """How robots.txt directives are applied."""

    RESPECT = "respect"
    RESPECT_WITH_EXCEPTIONS = "respect_with_exceptions"
    INFORMATIONAL = "informational"


class EnvironmentProfile(StrEnum):
    """Operating profile (playbook section 2)."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class RedirectKind(StrEnum):
    """Type of hop in a redirect chain (case 97)."""

    HTTP = "http"
    META_REFRESH = "meta"
    JAVASCRIPT = "js"
    CANONICAL_HINT = "canonical_hint"


class SourceFormat(StrEnum):
    """The kind of artefact a piece of content was extracted from."""

    HTML = "html"
    PDF = "pdf"
    IMAGE = "image"
    OFFICE_DOC = "office_doc"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    TEXT = "text"
    JSON = "json"
    XML = "xml"
    AUDIO = "audio"
    VIDEO = "video"
    OTHER = "other"


class UiRegion(StrEnum):
    """Where on the page (or in the document) a piece of content sits.

    The most specific region wins: a link inside a hamburger menu inside the
    header is `hamburger_menu`. Detection uses tags, ARIA roles and common
    id/class vocabularies, so it is a strong hint, not a guarantee.
    """

    MAIN_CONTENT = "main_content"
    HEADER = "header"
    HERO = "hero"
    NAV_MENU = "nav_menu"
    HAMBURGER_MENU = "hamburger_menu"
    MEGA_MENU = "mega_menu"
    BREADCRUMB = "breadcrumb"
    SIDEBAR = "sidebar"
    FOOTER = "footer"
    MODAL = "modal"
    POPOVER = "popover"
    TOOLTIP = "tooltip"
    ACCORDION_PANEL = "accordion_panel"
    TAB_PANEL = "tab_panel"
    CAROUSEL = "carousel"
    CARD = "card"
    SNIPPET = "snippet"
    PALETTE = "palette"
    SEARCH = "search"
    FORM = "form"
    TABLE = "table"
    COOKIE_BANNER = "cookie_banner"
    CHAT_WIDGET = "chat_widget"
    AD_SLOT = "ad_slot"
    DOCUMENT_METADATA = "document_metadata"
    PAGE_METADATA = "page_metadata"
    PDF_PAGE = "pdf_page"
    PDF_TABLE = "pdf_table"
    PDF_FORM = "pdf_form"
    PDF_ANNOTATION = "pdf_annotation"
    IMAGE_TEXT = "image_text"
    UNKNOWN = "unknown"


class ChangeStatus(StrEnum):
    """Whether a URL changed since the previous capture (recrawl)."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    UNKNOWN = "unknown"
