"""What a backlink snapshot says together, worked out by code (nothing is sent anywhere).

- **Link intersect**: websites that link to two or more of the competitors and not to the
  client (to the one competitor when the project has only one), strongest first. They
  already link to pages like the client's, so they are the likeliest to link to it too.
  Spam domains (as Ahrefs flags them) and the project's own websites are left out.
- **Authority gap**: the client's Domain Rating against each competitor's; when either
  Domain Rating is unknown (an export carries none), their referring domain counts.
- **Top pages**: the client's pages with the most referring domains.
- **Lost domains**: referring domains of the previous snapshot that are gone now. A
  domain is counted lost only when it would have been in the new list (the list was
  complete, or the domain was stronger than the weakest one listed).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from urllib.parse import urlsplit

from src.modules.backlinks.models import (
    AuthorityGap,
    BacklinkInsights,
    IntersectDomain,
    RefDomain,
    WebsiteBacklinks,
)

__all__ = [
    "INTERSECT_LIMIT",
    "authority_gaps",
    "build_insights",
    "domain_key",
    "link_intersect",
    "lost_domains",
]

INTERSECT_LIMIT = 200
_TOP_PAGES = 20


def domain_key(text: str) -> str:
    """A domain as compared here: host only, lower case, without `www.`."""
    raw = text.strip()
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname or raw
    return host.casefold().removeprefix("www.")


def _owns(website: str, domain: str) -> bool:
    """True when `domain` is the website itself or one of its subdomains."""
    site = domain_key(website)
    return domain == site or domain.endswith("." + site)


def link_intersect(
    websites: Sequence[WebsiteBacklinks], *, limit: int = INTERSECT_LIMIT
) -> tuple[list[IntersectDomain], int]:
    """Domains linking to enough competitors and not to the client; and how many is enough."""
    competitors = [w for w in websites if w.role == "competitor" and w.refdomains]
    if not competitors:
        return [], 2
    need = 2 if len(competitors) >= 2 else 1
    client_domains = {r.domain for w in websites if w.role == "client" for r in w.refdomains}
    own = [w.website for w in websites]
    found: dict[str, IntersectDomain] = {}
    for site in competitors:
        for ref in site.refdomains:
            if (
                ref.is_spam
                or ref.domain in client_domains
                or any(_owns(o, ref.domain) for o in own)
            ):
                continue
            item = found.setdefault(
                ref.domain, IntersectDomain(domain=ref.domain, competitors=[], links=0)
            )
            if site.website not in item.competitors:
                item.competitors.append(site.website)
            item.links += ref.links_to_target
            if ref.domain_rating is not None:
                item.domain_rating = max(item.domain_rating or 0.0, ref.domain_rating)
    rows = [r for r in found.values() if len(r.competitors) >= need]
    rows.sort(key=lambda r: (-(r.domain_rating or 0.0), -len(r.competitors), -r.links, r.domain))
    return rows[:limit], need


def authority_gaps(websites: Sequence[WebsiteBacklinks]) -> list[AuthorityGap]:
    """The client's authority against each competitor's, biggest gap first."""
    client = next((w for w in websites if w.role == "client"), None)
    if client is None:
        return []

    def refdomains(w: WebsiteBacklinks) -> int:
        return w.stats.live_refdomains if w.stats else len(w.refdomains)

    out: list[AuthorityGap] = []
    metric: Literal["domain_rating", "referring_domains"]
    for rival in websites:
        if rival.role != "competitor":
            continue
        if client.domain_rating is not None and rival.domain_rating is not None:
            mine, theirs = client.domain_rating, rival.domain_rating
            metric = "domain_rating"
        elif refdomains(client) or refdomains(rival):
            mine, theirs = float(refdomains(client)), float(refdomains(rival))
            metric = "referring_domains"
        else:
            continue
        out.append(
            AuthorityGap(
                competitor=rival.website,
                metric=metric,
                client=mine,
                competitor_value=theirs,
                gap=round(theirs - mine, 1),
                client_refdomains=refdomains(client),
                competitor_refdomains=refdomains(rival),
            )
        )
    return sorted(out, key=lambda g: (-g.gap, g.competitor))


def lost_domains(previous: WebsiteBacklinks | None, current: WebsiteBacklinks) -> list[str]:
    """Referring domains of `previous` that are gone from `current` (see the module notes)."""
    if previous is None or not previous.refdomains or not current.refdomains:
        return []
    now = {r.domain for r in current.refdomains}
    complete = current.refdomains_asked == 0 or len(current.refdomains) < current.refdomains_asked
    weakest = min((r.domain_rating or 0.0) for r in current.refdomains)
    return sorted(
        r.domain
        for r in previous.refdomains
        if r.domain not in now and (complete or (r.domain_rating or 0.0) > weakest)
    )


def with_history(previous: WebsiteBacklinks | None, current: WebsiteBacklinks) -> WebsiteBacklinks:
    """`current` with its lost and new referring domains against `previous`."""
    if previous is None or not previous.refdomains:
        return current.model_copy(update={"lost_domains": [], "new_domains": None})
    before = {r.domain for r in previous.refdomains}
    new = sum(1 for r in current.refdomains if r.domain not in before)
    return current.model_copy(
        update={"lost_domains": lost_domains(previous, current), "new_domains": new}
    )


def build_insights(websites: Sequence[WebsiteBacklinks]) -> BacklinkInsights:
    """Every insight of a snapshot's websites."""
    client = next((w for w in websites if w.role == "client"), None)
    intersect, need = link_intersect(websites)
    notes: list[str] = []
    competitors = [w for w in websites if w.role == "competitor"]
    with_domains = [w for w in competitors if w.refdomains]
    if client is None:
        notes.append("The project has no client website: the link intersect needs one.")
    elif not client.refdomains:
        notes.append(
            "The client's referring domains are not in yet: until they are, the link "
            "intersect cannot leave out the websites already linking to it."
        )
    if competitors and len(with_domains) < len(competitors):
        missing = ", ".join(w.website for w in competitors if not w.refdomains)
        notes.append(f"No referring domains yet for {missing}: they are not in the intersect.")
    if need == 1 and with_domains:
        notes.append(
            "Only one competitor has referring domains: the intersect lists the domains "
            "linking to it and not to the client."
        )
    if any(w.refdomains_asked and len(w.refdomains) >= w.refdomains_asked for w in websites):
        notes.append(
            "Some lists of referring domains were cut at the number asked (strongest "
            "first): weaker linking websites may be missing."
        )
    pages = sorted(
        client.top_pages if client else [], key=lambda p: (-p.referring_domains, -p.links, p.url)
    )
    return BacklinkInsights(
        client=client.website if client else None,
        min_competitors=need,
        link_intersect=intersect,
        authority=authority_gaps(websites),
        client_top_pages=pages[:_TOP_PAGES],
        lost={w.website: len(w.lost_domains) for w in websites if w.new_domains is not None},
        notes=notes,
    )


def strongest(refdomains: Sequence[RefDomain], n: int = 10) -> list[RefDomain]:
    """The `n` strongest referring domains (spam left out)."""
    rows = [r for r in refdomains if not r.is_spam]
    return sorted(rows, key=lambda r: (-(r.domain_rating or 0.0), r.domain))[:n]
