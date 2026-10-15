"""Depth-limited follow-ups: embedded assets and linked downloads (A-1, K6, K15).

A fetched HTML page names other resources: images, frames, embedded objects,
and links to downloadable documents. Under `SUPPLIED_URLS_PLUS_EMBEDDED` each
of these becomes a FETCH task one level deeper, unless scope, depth, MIME or
trap policy says no. Every candidate ends either as a follow-up or as a
skipped entry with a reason, so the parent's summary accounts for all of them.

Each follow-up carries the media type its parent promised (K15): an
`<a type="application/pdf">`, a "PDF" link label or an `<img>` tag all say
what the bytes must be. The fetch worker hands that to the block classifier,
so an HTML login page served where a PDF was promised fails closed as
`WRONG_PAYLOAD` instead of being extracted and verified as a page.

Ordinary page-to-page links are not followed: that is `RECURSIVE_SITE`.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urljoin, urlsplit

from pydantic import Field
from selectolax.parser import HTMLParser

from src.core.schemas import StrictModel
from src.modules.content_processor.adapters.html_source import DiscoveredAsset, DiscoveredLink
from src.modules.content_processor.contracts import RunScope
from src.modules.content_processor.enums import PriorityClass, ScopeMode
from src.modules.content_processor.scope.frontier import host_in_scope
from src.modules.content_processor.scope.normalize import InvalidInputUrl, normalize_url

__all__ = [
    "MAX_FOLLOWUPS_PER_PAGE",
    "AnchorHints",
    "ExpectedMimeSource",
    "FollowupCandidate",
    "FollowupPlan",
    "SkippedFollowup",
    "anchor_hints",
    "expected_mime_for_name",
    "mime_allowed",
    "plan_followups",
]

MAX_FOLLOWUPS_PER_PAGE = 200
"""Hard cap per parent page; the rest are reported as `per_page_limit`."""

EdgeKind = Literal["embeds", "frames", "links_to"]
ExpectedMimeSource = Literal[
    "link_type", "link_text", "url_extension", "embed_kind", "content_disposition"
]

_EXTENSION_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "svg": "image/svg+xml",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "html": "text/html",
    "htm": "text/html",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}
_DOCUMENT_FAMILIES = ("application/",)
_PDF_LABEL = re.compile(r"(^|[\s(\[])pdf($|[\s)\],.:;-])", re.IGNORECASE)
_EMBED_KIND_MIME: dict[str, str] = {"image": "image/*", "frame": "text/html"}
_NOT_CONTENT_KINDS = frozenset({"stylesheet", "script"})


class AnchorHints(StrictModel):
    """Attributes of an `<a>` that the source adapter does not keep."""

    type_attr: str | None = None
    download: bool = False


class FollowupCandidate(StrictModel):
    """One resource to fetch one level below its parent."""

    url: str
    schedule_key: str
    host: str
    edge_kind: EdgeKind
    priority: PriorityClass
    expected_mime: str | None = None
    expected_mime_source: ExpectedMimeSource | None = None


class SkippedFollowup(StrictModel):
    """A discovered reference that will not be fetched, and why."""

    url: str
    reason: str


class FollowupPlan(StrictModel):
    """Everything decided for one parent page."""

    followups: list[FollowupCandidate] = Field(default_factory=list)
    skipped: list[SkippedFollowup] = Field(default_factory=list)
    links_not_downloads: int = 0
    """Ordinary page links seen and left alone (not counted as skipped)."""

    def summary(self) -> dict[str, object]:
        """Compact parent summary for the ledger."""
        reasons: dict[str, int] = {}
        for item in self.skipped:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        return {
            "followed": [
                {"url": f.url, "edge": f.edge_kind, "expected_mime": f.expected_mime}
                for f in self.followups
            ],
            "skipped": [s.model_dump() for s in self.skipped[:100]],
            "skipped_by_reason": reasons,
            "links_not_downloads": self.links_not_downloads,
        }


def expected_mime_for_name(name: str) -> str | None:
    """Media type implied by the extension of a URL path or a file name."""
    leaf = urlsplit(name).path.rsplit("/", 1)[-1] if "/" in name else name
    if "." not in leaf:
        return None
    return _EXTENSION_MIME.get(leaf.rsplit(".", 1)[-1].lower())


def mime_allowed(mime: str, families: list[str]) -> bool:
    """True when `mime` matches one of the scope's families (`image/*` style allowed)."""
    main = mime.split(";", 1)[0].strip().lower()
    for family in families:
        f = family.strip().lower()
        if f.endswith("/*") and main.startswith(f[:-1]):
            return True
        if main == f:
            return True
    # A family wildcard on the expectation side ("image/*") matches a declared family.
    return main.endswith("/*") and any(f.lower().startswith(main[:-1]) for f in families)


def anchor_hints(body: bytes, base_url: str) -> dict[str, AnchorHints]:
    """Map absolute anchor URLs to their `type` and `download` attributes."""
    hints: dict[str, AnchorHints] = {}
    for node in HTMLParser(body).css("a[href]"):
        attrs = node.attributes
        type_attr = (attrs.get("type") or "").split(";", 1)[0].strip().lower() or None
        download = "download" in attrs
        if type_attr is None and not download:
            continue
        url = urljoin(base_url, (attrs.get("href") or "").strip())
        hints[url] = AnchorHints(type_attr=type_attr, download=download)
    return hints


def _link_expectation(
    link: DiscoveredLink, hint: AnchorHints | None
) -> tuple[str | None, ExpectedMimeSource | None, bool]:
    """(expected mime, its source, is a download link)."""
    if hint is not None and hint.type_attr:
        return hint.type_attr, "link_type", True
    by_ext = expected_mime_for_name(link.url)
    if by_ext is not None and by_ext != "text/html":
        return by_ext, "url_extension", True
    if _PDF_LABEL.search(link.text):
        return "application/pdf", "link_text", True
    if hint is not None and hint.download:
        return None, None, True
    return None, None, False


def _priority(mime: str | None, edge: EdgeKind) -> PriorityClass:
    if mime is not None and mime.startswith(_DOCUMENT_FAMILIES):
        return PriorityClass.DOCUMENT
    if edge == "links_to":
        return PriorityClass.DOCUMENT
    return PriorityClass.EMBEDDED_ASSET


def plan_followups(
    *,
    scope: RunScope,
    parent_url: str,
    parent_depth: int,
    links: list[DiscoveredLink],
    assets: list[DiscoveredAsset],
    hints: dict[str, AnchorHints],
) -> FollowupPlan:
    """Decide which embedded assets and linked downloads to fetch next.

    Args:
        scope: Run scope (mode, depth, hosts, MIME families).
        parent_url: Final URL of the parent page.
        parent_depth: 0 for supplied URLs.
        links: Anchors from the source extraction.
        assets: Embedded references from the source extraction.
        hints: `anchor_hints()` of the same body.

    Returns:
        Follow-ups deduplicated by schedule key, plus every skipped reference.
    """
    plan = FollowupPlan()
    if scope.mode is ScopeMode.SUPPLIED_URLS:
        return plan
    if parent_depth + 1 > scope.linked_download_depth:
        return plan
    try:
        parent_key = normalize_url(parent_url).schedule_key
    except InvalidInputUrl:
        parent_key = parent_url
    seen: set[str] = {parent_key}

    raw: list[tuple[str, EdgeKind, str | None, ExpectedMimeSource | None, str | None]] = []
    for asset in assets:
        if asset.kind in _NOT_CONTENT_KINDS:
            continue
        edge: EdgeKind = "frames" if asset.kind == "frame" else "embeds"
        mime = expected_mime_for_name(asset.url) or _EMBED_KIND_MIME.get(asset.kind)
        if asset.kind == "image" and mime is not None and not mime.startswith("image/"):
            mime = "image/*"  # an <img> promises an image whatever the path says
        source: ExpectedMimeSource | None = (
            "embed_kind" if asset.kind in _EMBED_KIND_MIME else "url_extension" if mime else None
        )
        if asset.kind == "media" and mime is None:
            mime, source = "video/*", "embed_kind"
        raw.append((asset.url, edge, mime, source, "hidden" if asset.hidden else None))
    for link in links:
        mime, source, is_download = _link_expectation(link, hints.get(link.url))
        if not is_download:
            plan.links_not_downloads += 1
            continue
        trap = "hidden" if link.hidden else "action_like" if link.action_like else None
        raw.append((link.url, "links_to", mime, source, trap))

    for url, edge, mime, source, trap in raw:
        if trap is not None:
            plan.skipped.append(SkippedFollowup(url=url, reason=trap))
            continue
        try:
            norm = normalize_url(url)
        except InvalidInputUrl as exc:
            plan.skipped.append(SkippedFollowup(url=url, reason=f"invalid:{exc.reason}"))
            continue
        if norm.schedule_key in seen:
            continue
        seen.add(norm.schedule_key)
        if not host_in_scope(norm.host, scope.hosts_in_scope):
            plan.skipped.append(SkippedFollowup(url=url, reason="out_of_scope_host"))
            continue
        if mime is not None and not mime_allowed(mime, scope.allowed_mime_families):
            plan.skipped.append(SkippedFollowup(url=url, reason=f"mime_not_allowed:{mime}"))
            continue
        if len(plan.followups) >= MAX_FOLLOWUPS_PER_PAGE:
            plan.skipped.append(SkippedFollowup(url=url, reason="per_page_limit"))
            continue
        plan.followups.append(
            FollowupCandidate(
                url=url,
                schedule_key=norm.schedule_key,
                host=norm.host,
                edge_kind=edge,
                priority=_priority(mime, edge),
                expected_mime=mime,
                expected_mime_source=source,
            )
        )
    return plan
