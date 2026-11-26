"""Technical SEO checks over what a scrape already stored (no new requests).

Each check lists the pages (or links) it found, from the run's records (answer status,
final address, redirect hops), its page stamps (titles, H1s, why a page left search) and
its SEO inventory (links, images, robots tags, hreflang). Answer status codes and redirect
hops are kept on records from this version on; older scrapes only have the final address
and the engine's verdict, and the check says so.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit

from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.knowledge.inventory_models import PageInventory
from src.modules.content_processor.knowledge.models import PageStamp
from src.modules.content_processor.recrawl.baseline import url_key

__all__ = ["CHECKS", "Finding", "TechnicalReport", "technical_checks"]

CHECKS: Final[dict[str, str]] = {
    "http_errors": "Pages that answered with an error (4xx/5xx), failed or were blocked",
    "soft_404": "Pages that answer 200 but say the page was not found",
    "redirects": "Addresses that end at another address (with the hops when kept)",
    "redirect_chains": "Redirects with two or more hops, or that loop",
    "duplicate_titles": "Searchable pages that share a title",
    "title_equals_h1": "Pages whose H1 repeats the title word for word",
    "parameter_urls": "Addresses with ?parameters (scraped, and linked internally)",
    "insecure_links": "http:// links and images on https:// pages (mixed content)",
    "broken_internal_links": "Internal links to scraped pages that failed, errored or are gone",
    "robots_meta": "Pages with noindex or nofollow in a robots meta tag",
    "hreflang": "Pages with hreflang alternates, and alternates without a return link",
}
_SUFFIX: Final = re.compile(r"\s*[|–—-]\s*[^|–—-]{2,60}$")
_BAD_REASONS: Final = {"error_page", "soft_404", "blocked"}


@dataclass
class Finding:
    """One page (or link) a check found."""

    url: str
    detail: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TechnicalReport:
    """Every check's findings, and notes on what the scrape could not tell."""

    findings: dict[str, list[Finding]]
    notes: list[str]
    pages: int


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def _short_title(title: str | None) -> str:
    return _norm(_SUFFIX.sub("", title or ""))


def _http(record: UrlRecord) -> dict[str, Any]:
    found = record.summaries.get("http")
    return dict(found) if isinstance(found, Mapping) else {}


def _bad(record: UrlRecord, stamp: PageStamp | None) -> str | None:
    """Why a scraped page counts as broken, or None."""
    status = _http(record).get("status")
    if isinstance(status, int) and status >= 400:
        return f"HTTP {status}"
    if stamp is not None and stamp.exclusion_reason in _BAD_REASONS:
        return stamp.exclusion_reason
    badge = str(record.badge)
    if badge in ("BLOCKED", "FAILED"):
        return badge.lower()
    return None


def _same(a: str | None, b: str | None) -> bool:
    return url_key(a) == url_key(b)


def technical_checks(
    records: Sequence[UrlRecord],
    stamps: Iterable[PageStamp],
    inventory: Sequence[PageInventory],
) -> TechnicalReport:
    """Run every check (see CHECKS)."""
    by_url = {s.url: s for s in stamps}
    stamp_of = {k: s for s in by_url.values() for u in (s.url, s.final_url) if (k := url_key(u))}
    findings: dict[str, list[Finding]] = {name: [] for name in CHECKS}
    notes: list[str] = []
    with_status = sum(1 for r in records if "status" in _http(r))
    if not with_status:
        notes.append(
            "this scrape predates kept answer statuses: errors come from the engine's verdict "
            "(error page, soft 404, blocked, failed) and redirects show only the final address"
        )
    record_of: dict[str, UrlRecord] = {}
    for record in records:
        for u in (record.requested_url, record.final_url, *record.aliases):
            if (k := url_key(u)) is not None:
                record_of.setdefault(k, record)
        stamp = stamp_of.get(url_key(record.requested_url) or "")
        why = _bad(record, stamp)
        if why == "soft_404":
            findings["soft_404"].append(Finding(record.requested_url, "says the page is missing"))
        elif why is not None:
            findings["http_errors"].append(Finding(record.requested_url, why))
        hops = _http(record).get("redirects") or []
        if record.final_url and not _same(record.final_url, record.requested_url):
            findings["redirects"].append(
                Finding(
                    record.requested_url,
                    f"ends at {record.final_url}",
                    {"hops": hops} if hops else {},
                )
            )
        targets = [h.get("to") for h in hops if isinstance(h, Mapping)]
        keys = [url_key(t) for t in targets]
        if len(hops) >= 2 or len(set(keys)) < len(keys):
            loop = len(set(keys)) < len(keys)
            findings["redirect_chains"].append(
                Finding(
                    record.requested_url,
                    ("loops: " if loop else f"{len(hops)} hops: ") + " > ".join(map(str, targets)),
                )
            )
    titles: dict[str, list[PageStamp]] = defaultdict(list)
    for stamp in by_url.values():
        if not stamp.knowledge_eligible or stamp.is_asset:
            continue
        if stamp.title:
            titles[_norm(stamp.title)].append(stamp)
        if stamp.title and stamp.h1 and _short_title(stamp.title) == _norm(stamp.h1):
            findings["title_equals_h1"].append(Finding(stamp.url, stamp.h1))
    for same in titles.values():
        if len(same) > 1:
            for stamp in same:
                others = [s.url for s in same if s is not stamp]
                findings["duplicate_titles"].append(
                    Finding(stamp.url, stamp.title or "", {"also_on": others[:10]})
                )
    inventory_of = {k: p for p in inventory if (k := url_key(p.url))}
    linked_params: Counter[str] = Counter()
    unchecked = 0
    for record in records:
        if "?" in record.requested_url and (record.sniffed_mime or "").startswith("text/html"):
            names = sorted({k for k, _ in parse_qsl(urlsplit(record.requested_url).query)})
            findings["parameter_urls"].append(
                Finding(record.requested_url, "scraped address", {"parameters": names})
            )
    for page in inventory:
        https = page.url.startswith("https://")
        for link in page.links:
            if https and link.insecure:
                findings["insecure_links"].append(Finding(page.url, f"link to {link.url}"))
            if link.kind != "internal":
                continue
            if "?" in link.url:
                linked_params[link.url] += 1
            target = record_of.get(url_key(link.url) or "")
            if target is None:
                unchecked += 1
                continue
            why = _bad(target, stamp_of.get(url_key(target.requested_url) or ""))
            if why is not None:
                findings["broken_internal_links"].append(
                    Finding(page.url, f"links to {link.url} ({why})")
                )
        if https:
            for image in page.images:
                if image.url.startswith("http://"):
                    findings["insecure_links"].append(Finding(page.url, f"image {image.url}"))
        robots = {v for values in page.head.robots.values() for v in values}
        flags = sorted(robots & {"noindex", "nofollow", "none"})
        if flags:
            findings["robots_meta"].append(
                Finding(page.url, ", ".join(flags), {"tags": dict(page.head.robots)})
            )
        if page.head.hreflang:
            missing = []
            for lang, href in page.head.hreflang.items():
                other = inventory_of.get(url_key(href) or "")
                if other is not None and not any(
                    _same(h, page.url) for h in other.head.hreflang.values()
                ):
                    missing.append(lang)
            findings["hreflang"].append(
                Finding(
                    page.url,
                    f"{len(page.head.hreflang)} alternates"
                    + (f"; no return link from {', '.join(missing)}" if missing else ""),
                    {"alternates": dict(page.head.hreflang)},
                )
            )
    for url, count in linked_params.most_common():
        findings["parameter_urls"].append(Finding(url, f"linked internally {count} time(s)"))
    if unchecked:
        notes.append(
            f"{unchecked} internal links point to pages this scrape did not fetch; they are "
            "not checked (scrape them to check them)"
        )
    notes.append(
        "X-Robots-Tag headers are not kept by the scraper: robots_meta reads meta tags only "
        "(PDFs and other files have none)"
    )
    return TechnicalReport(findings=findings, notes=notes, pages=len(records))
