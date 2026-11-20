"""Start a lite scan of a website, and read back the latest one."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

from src.core.config import get_settings
from src.modules.content_processor.projects.models import RunState, SiteRecord
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.lite_scan.models import LiteInventory
from src.modules.lite_scan.sample import Ranking, choose_sample, is_page

__all__ = [
    "LiteStore",
    "latest_lite_run",
    "lite_store",
    "rankings_for",
    "start_lite_scan",
    "website_of",
]

_ID: Final = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class LiteStore:
    """Each website's latest lite scan (`<app data>/lite_scan/<project>/<site>.json`)."""

    def __init__(self, root: Path) -> None:
        """Bind to the folder."""
        self.root = root
        self._lock = threading.Lock()

    def _path(self, project_id: str, site_id: str) -> Path:
        if not _ID.match(project_id) or not _ID.match(site_id):
            raise KeyError(f"{project_id}/{site_id}")
        return self.root / project_id / f"{site_id}.json"

    def save(self, inventory: LiteInventory) -> Path:
        """Keep the scan (replaces the website's previous one)."""
        path = self._path(inventory.project_id, inventory.site_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(inventory.model_dump_json(), encoding="utf-8")
            tmp.replace(path)
        return path

    def latest(self, project_id: str, site_id: str) -> LiteInventory | None:
        """The website's latest lite scan, or None."""
        try:
            path = self._path(project_id, site_id)
        except KeyError:
            return None
        if not path.exists():
            return None
        with self._lock:
            return LiteInventory.model_validate_json(path.read_text(encoding="utf-8"))

    def project(self, project_id: str) -> dict[str, LiteInventory]:
        """Every website's latest lite scan, by site id."""
        if not _ID.match(project_id):
            return {}
        folder = self.root / project_id
        out: dict[str, LiteInventory] = {}
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            found = self.latest(project_id, path.stem)
            if found is not None:
                out[path.stem] = found
        return out


def lite_store(projects: ProjectStore) -> LiteStore:
    """The lite-scan store next to the projects."""
    return LiteStore(Path(str(projects.root)) / "lite_scan")


def website_of(site: SiteRecord) -> str:
    """The website's main domain (its first address, without a path)."""
    return site.domains[0].split("/")[0].lower() if site.domains else ""


def _matches(website: str, domains: Iterable[str]) -> bool:
    return any(
        (d := domain.split("/")[0].lower()) == website or d.endswith("." + website)
        for domain in domains
    )


def rankings_for(projects: ProjectStore, project_id: str, site: SiteRecord) -> dict[str, Ranking]:
    """The website's ranking pages in the latest search data (keyword count and traffic)."""
    from src.modules.research.service import research_store

    snapshot = research_store(projects).latest(project_id)
    if snapshot is None:
        return {}
    out: dict[str, Ranking] = {}
    for found in snapshot.websites:
        if not _matches(found.website, site.domains):
            continue
        for row in found.keywords:
            if not row.url:
                continue
            seen = out.get(row.url, Ranking(0, 0.0))
            out[row.url] = Ranking(seen.keywords + 1, seen.traffic + row.traffic_share)
    return out


def latest_lite_run(runs: Iterable[RunState]) -> RunState | None:
    """The newest finished run when it is a lite scan, else None."""
    done = [r for r in runs if r.status == "finished"]
    if not done:
        return None
    newest = max(done, key=lambda r: r.finished_at or r.created_at)
    return newest if newest.scan == "lite" else None


def _client() -> httpx.Client:
    from src.modules.content_processor.workers.fetch_worker import visitor_user_agent

    return httpx.Client(
        timeout=20, follow_redirects=True, headers={"User-Agent": visitor_user_agent()}
    )


def start_lite_scan(
    projects: ProjectStore,
    runner: ScrapeRunner,
    project_id: str,
    site_id: str,
    *,
    started_by: str,
    pinned: Iterable[str] = (),
    http: Callable[[], httpx.Client] | None = None,
    now: datetime | None = None,
) -> tuple[LiteInventory, RunState]:
    """Read the website's sitemaps, choose the sample and queue its scrape.

    Raises `ValueError` (plain words) for a client website, when a scrape of the
    website is already queued, or when no page address is found at all.
    """
    settings = get_settings()
    site = projects.site(project_id, site_id)
    if site.role != "competitor":
        raise ValueError(
            f"{site.name} is a client website: client websites get a full scrape, a lite "
            "scan is for competitors."
        )
    website = website_of(site)
    if not website:
        raise ValueError(f"Give {site.name} a web address first.")
    from src.modules.monitoring.recrawl import read_sitemaps

    with (http or _client)() as client:
        sitemap = read_sitemaps(website, client)
    inventory = [u for u in sitemap.urls if is_page(u)]
    # the website's own address list, and its home page, count even without a sitemap
    known = [u.url for u in projects.urls(project_id, site_id)] + [f"https://{site.domains[0]}"]
    picks, sections = choose_sample(
        website=website,
        inventory=inventory,
        known=known,
        ranking=rankings_for(projects, project_id, site),
        pinned=pinned,
        limit=settings.cp_lite_scan_max_pages,
        per_section=settings.cp_lite_scan_per_section,
    )
    if not picks:
        raise ValueError(
            f"No page address found for {site.name}: its sitemap could not be read "
            f"({sitemap.note or 'empty'}). Add a few addresses to the website and try again."
        )
    result = LiteInventory(
        project_id=project_id,
        site_id=site_id,
        website=website,
        created_at=now or datetime.now(UTC),
        sitemap_files=sitemap.files,
        sitemap_note=sitemap.note,
        inventory=inventory,
        sections=sections,
        sample=picks,
        max_pages=settings.cp_lite_scan_max_pages,
    )
    state = runner.submit(
        project_id,
        site_id,
        started_by=started_by,
        urls=[p.url for p in picks],
        lite={"inventory": result.pages, "sample": len(picks), "basis": result.basis()},
    )
    result = result.model_copy(update={"run_id": state.run_id})
    lite_store(projects).save(result)
    return result, state
