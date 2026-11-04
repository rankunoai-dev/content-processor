"""Smart recrawl: which pages a scheduled re-scrape checks, and which it carries forward.

Re-scraping every page of every website on every pass wastes the websites' bandwidth
and ours, and makes a block more likely. A plan decides, page by page:

1. **Never captured** -> check.
2. **Core page** -> check on every scheduled pass (weekly by default). Core pages are
   the ones competitors change on purpose: the website's pillar pages in the theme
   map, pages linked from its header menu, comparison and decision pages, pages one
   level from the home page, addresses with words such as pricing, plans, compare,
   solutions, products, platform, features or services, and pages pinned by a person
   (up to `core_limit` per website, in that order).
3. **The website's sitemap shows a newer change** (`<lastmod>` later than our last
   capture) -> check, whatever the tier. Most CMSs (WordPress, Webflow, HubSpot, AEM)
   update `<lastmod>` when a page is saved.
4. **Last checked `other_every_days` ago or more** (monthly by default) -> check.
5. **Otherwise** -> skip: no request is sent; the previous capture is carried forward
   (with its capture time, so the page comes due later). The reason is recorded:
   "sitemap: unchanged since the last capture" or "not a core page; next check by ...".

Checked pages still go through the usual change-aware fetch: a conditional request
(`If-None-Match` / `If-Modified-Since`, a 304 ends it), then bytes and visible text
compared, so an unchanged page is never extracted again.

Sitemaps are read from `robots.txt` (`Sitemap:` lines) or `/sitemap.xml` and
`/sitemap_index.xml`, following sitemap indexes, only on the website's own domain,
with limits on files, size and addresses. This is the only traffic of a plan.
"""

from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.knowledge.models import SiteStamp
from src.modules.content_processor.projects.models import RunState, SiteRecord
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.recrawl.baseline import url_key
from src.modules.monitoring.models import PageDecision, RecrawlPlan, SitePlan, WatchSettings
from src.modules.theming.models import ThemeMap

__all__ = [
    "CORE_WORDS",
    "SitemapIndex",
    "core_pages",
    "plan_project",
    "plan_site",
    "read_sitemaps",
]

CORE_WORDS: Final = (
    "pricing", "price", "plans", "plan", "compare", "comparison", "vs", "versus",
    "solutions", "solution", "products", "product", "platform", "features", "services",
)  # fmt: skip
_WORD = re.compile(r"[a-z0-9]+")
_MAX_SITEMAPS: Final = 25
_MAX_BYTES: Final = 15 * 1024 * 1024
_MAX_LOCS: Final = 200_000
_TIMEOUT: Final = 20.0
_CORE_STAGES: Final = frozenset({"compare", "decide"})
# Pages that are never core: error, search and account pages
_NOT_CORE: Final = frozenset(
    {"error", "errors", "404", "search", "login", "signin", "sign-in", "logout", "account", "cart"}
)


# -- sitemaps ----------------------------------------------------------------------------------


@dataclass
class SitemapIndex:
    """What a website's sitemaps say: last modification per address."""

    lastmod: dict[str, datetime]
    files: list[str]
    addresses: int
    note: str = ""

    def of(self, url: str) -> datetime | None:
        """The sitemap's last modification of a page, or None."""
        key = url_key(url)
        return self.lastmod.get(key) if key else None


def _same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    base = domain.split("/")[0].lower()
    return urlsplit(url).scheme in ("http", "https") and (host == base or host.endswith("." + base))


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        try:
            moment = datetime.fromisoformat(raw[:10])
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


_REFUSED: Final = frozenset({401, 403, 429, 503})


def _get(client: httpx.Client, url: str, refused: list[int] | None = None) -> bytes | None:
    """The file's bytes, or None; refusals (401, 403, 429, 503) are noted in `refused`."""
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                if refused is not None and response.status_code in _REFUSED:
                    refused.append(response.status_code)
                return None
            data = b""
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > _MAX_BYTES:
                    return None
    except httpx.HTTPError:
        return None
    if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            return None
    return data[:_MAX_BYTES]


def _parse(data: bytes) -> ElementTree.Element | None:
    """A sitemap's XML; None when broken or when it declares a DTD or entities.

    Sitemaps never need a DTD; refusing one shuts out entity-expansion and external
    entity tricks before the parser sees them.
    """
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        return None
    try:
        return ElementTree.fromstring(data)  # noqa: S314 - no DTD or entities, checked above
    except ElementTree.ParseError:
        return None


def read_sitemaps(domain: str, client: httpx.Client) -> SitemapIndex:
    """Read the website's sitemaps (robots.txt `Sitemap:` lines, else the usual places)."""
    base = f"https://{domain.split('/')[0]}"
    queue: list[str] = []
    refused: list[int] = []
    robots = _get(client, f"{base}/robots.txt", refused)
    if robots:
        for line in robots.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("sitemap:"):
                found = line.split(":", 1)[1].strip()
                if _same_site(found, domain):
                    queue.append(found)
    if not queue:
        queue = [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]
    lastmod: dict[str, datetime] = {}
    read: list[str] = []
    seen: set[str] = set()
    addresses = 0
    while queue and len(read) < _MAX_SITEMAPS and addresses < _MAX_LOCS:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        data = _get(client, url, refused)
        if not data:
            continue
        root = _parse(data)
        if root is None:
            continue
        read.append(url)
        for entry in root:
            kind = _local(entry.tag)
            loc = next((c.text for c in entry if _local(c.tag) == "loc" and c.text), None)
            mod = next((c.text for c in entry if _local(c.tag) == "lastmod"), None)
            if not loc:
                continue
            loc = urljoin(url, loc.strip())
            if kind == "sitemap":
                if _same_site(loc, domain):
                    queue.append(loc)
            elif kind == "url":
                addresses += 1
                key = url_key(loc)
                moment = _when(mod)
                if key and moment:
                    lastmod[key] = max(moment, lastmod.get(key, moment))
    note = ""
    if not read and refused:
        note = (
            f"the website refused the sitemap request (HTTP {refused[0]}); the plan uses "
            "core pages and the regular check interval"
        )
    elif not read:
        note = "no sitemap found"
    elif not lastmod:
        note = "the sitemap gives no last-modified dates"
    elif queue:
        note = f"stopped after {len(read)} sitemap files"
    return SitemapIndex(lastmod=lastmod, files=read, addresses=addresses, note=note)


# -- core pages --------------------------------------------------------------------------------


def _header_links(site: SiteStamp | None) -> set[str]:
    if site is None:
        return set()
    out: set[str] = set()

    def walk(items: Iterable[object]) -> None:
        for item in items:
            href = getattr(item, "href", None)
            if href:
                key = url_key(href)
                if key:
                    out.add(key)
            walk(getattr(item, "children", []))

    for menu in site.menus:
        if menu.kind == "header":
            walk(menu.items)
    return out


def core_pages(
    urls: Iterable[str],
    *,
    website: str,
    theme_map: ThemeMap | None,
    site: SiteStamp | None,
    pinned: Iterable[str] = (),
    limit: int = 50,
) -> dict[str, str]:
    """The website's core pages, by key, with why each is core (at most `limit`)."""
    by_key = {k: u for u in urls if (k := url_key(u))}
    pinned_keys = {k for p in pinned if (k := url_key(p))}
    pillars: set[str] = set()
    stages: set[str] = set()
    for page in theme_map.pages if theme_map else []:
        key = url_key(page.url)
        if key and page.website == website:
            if page.role == "pillar":
                pillars.add(key)
            if page.funnel_stage in _CORE_STAGES:
                stages.add(key)
    header = _header_links(site)
    ranked: list[tuple[int, str, str]] = []
    for key, url in by_key.items():
        path = urlsplit(url).path.strip("/")
        if path.split("/")[0].lower() in _NOT_CORE and key not in pinned_keys:
            continue
        named = sorted(set(_WORD.findall(path.lower())) & set(CORE_WORDS))
        reasons = [
            (0, "pinned by a person", key in pinned_keys),
            (1, "pillar page of a theme", key in pillars),
            (2, "linked from the header menu", key in header),
            (3, "comparison or decision page", key in stages),
            (4, "address names " + ", ".join(named), bool(named)),
            (5, "home page or one level from it", path.count("/") == 0),
        ]
        hit = next(((rank, why) for rank, why, ok in reasons if ok), None)
        if hit is not None:
            ranked.append((hit[0], key, hit[1]))
    ranked.sort(key=lambda r: (r[0], len(r[1]), r[1]))
    return {key: why for _rank, key, why in ranked[:limit]}


# -- the plan ----------------------------------------------------------------------------------


def _captures(folder: Path, run_id: str) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    path = folder / f"{run_id}.records.jsonl"
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = UrlRecord.model_validate_json(line)
            if record.payload_sha256 is None:
                continue  # never really captured (failed, blocked): due now
            for key in {url_key(record.requested_url), url_key(record.final_url)}:
                if key:
                    out[key] = record.captured_at
    return out


def _site_stamp(folder: Path, run_id: str) -> SiteStamp | None:
    path = folder / f"{run_id}.site.json"
    if not path.exists():
        return None
    return SiteStamp.model_validate_json(path.read_text(encoding="utf-8"))


def _day(moment: datetime) -> str:
    return moment.strftime("%d %b %Y").lstrip("0")


def plan_site(
    urls: list[str],
    *,
    captured: Mapping[str, datetime],
    core: Mapping[str, str],
    sitemap: SitemapIndex | None,
    settings: WatchSettings,
    now: datetime,
) -> list[PageDecision]:
    """Decide, page by page, whether a scheduled pass checks it or carries it forward."""
    every = timedelta(days=settings.other_every_days)
    out: list[PageDecision] = []
    for url in urls:
        key = url_key(url) or url
        last = captured.get(key)
        lastmod = sitemap.of(url) if sitemap else None
        tier = "core" if key in core else "other"
        if last is None:
            action, why = "check", "never captured"
        elif tier == "core":
            action, why = "check", f"core page ({core[key]})"
        elif lastmod is not None and lastmod > last:
            action, why = "check", f"sitemap shows a change on {_day(lastmod)}"
        elif now - last >= every:
            action, why = "check", f"last checked {(now - last).days} days ago"
        elif lastmod is not None:
            action, why = "skip", "sitemap: unchanged since the last capture"
        else:
            action, why = "skip", f"not a core page; next check by {_day(last + every)}"
        out.append(
            PageDecision(
                url=url,
                tier=tier,
                action=action,
                reason=why,
                sitemap_lastmod=lastmod,
                last_captured=last,
            )
        )
    return out


def _latest_finished(runs: list[RunState], site_id: str) -> RunState | None:
    done = [r for r in runs if r.site_id == site_id and r.status == "finished"]
    return max(done, key=lambda r: r.created_at) if done else None


def plan_project(
    projects: ProjectStore,
    project_id: str,
    settings: WatchSettings,
    *,
    theme_map: ThemeMap | None,
    http: Callable[[], httpx.Client] | None = None,
    now: datetime | None = None,
    trigger: str = "preview",
) -> RecrawlPlan:
    """A plan for every website of the project that has addresses."""
    moment = now or datetime.now(UTC)
    runs = projects.runs(project_id)
    sites: list[SitePlan] = []
    client = http() if http else _client()
    try:
        for site in projects.sites(project_id):
            urls = [u.url for u in projects.urls(project_id, site.site_id)]
            if urls:
                sites.append(
                    _site_plan(
                        projects, project_id, site, urls, runs, settings, theme_map, client, moment
                    )
                )
    finally:
        client.close()
    return RecrawlPlan(
        plan_id="recrawl-"
        + moment.strftime("%Y%m%d-%H%M%S-")
        + f"{moment.microsecond // 1000:03d}",
        project_id=project_id,
        created_at=moment,
        trigger=trigger,
        core_every_days=settings.rescrape_every_days,
        other_every_days=settings.other_every_days,
        sites=sites,
    )


def _client() -> httpx.Client:
    from src.modules.content_processor.workers.fetch_worker import visitor_user_agent

    return httpx.Client(
        timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": visitor_user_agent()}
    )


def _site_plan(
    projects: ProjectStore,
    project_id: str,
    site: SiteRecord,
    urls: list[str],
    runs: list[RunState],
    settings: WatchSettings,
    theme_map: ThemeMap | None,
    client: httpx.Client,
    now: datetime,
) -> SitePlan:
    last_run = _latest_finished(runs, site.site_id)
    folder = projects.run_dir(project_id, site.site_id, last_run.run_id) if last_run else None
    captured = _captures(folder, last_run.run_id) if folder and last_run else {}
    stamp = _site_stamp(folder, last_run.run_id) if folder and last_run else None
    website = site.domains[0].split("/")[0] if site.domains else ""
    core = core_pages(
        urls,
        website=_theme_key(theme_map, site) or website,
        theme_map=theme_map,
        site=stamp,
        pinned=settings.core_urls,
        limit=settings.core_limit,
    )
    sitemap = read_sitemaps(website, client) if settings.use_sitemaps and website else None
    pages = plan_site(
        urls, captured=captured, core=core, sitemap=sitemap, settings=settings, now=now
    )
    return SitePlan(
        site_id=site.site_id,
        name=site.name,
        role=site.role,
        website=website,
        baseline_run=last_run.run_id if last_run else None,
        sitemap_files=sitemap.files if sitemap else [],
        sitemap_addresses=sitemap.addresses if sitemap else 0,
        sitemap_dates=len(sitemap.lastmod) if sitemap else 0,
        sitemap_note=(sitemap.note if sitemap else "sitemaps not used"),
        core=sum(1 for p in pages if p.tier == "core"),
        check=sum(1 for p in pages if p.action == "check"),
        skip=sum(1 for p in pages if p.action == "skip"),
        pages=pages,
    )


def _theme_key(theme_map: ThemeMap | None, site: SiteRecord) -> str | None:
    """The theme map's key for this website (its shortest domain)."""
    if theme_map is None:
        return None
    for w in theme_map.websites:
        if any(
            d.split("/")[0] == w.website or d.split("/")[0].endswith("." + w.website)
            for d in site.domains
        ):
            return w.website
    return None
