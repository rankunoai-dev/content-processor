"""Pull, import and read research snapshots (shared by the CLI, the API and the cards)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from src.modules.analyst.playground import Playground
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.research.build import (
    MIN_KEYWORDS,
    finish_snapshot,
    import_website,
    movers,
    pull_snapshot,
)
from src.modules.research.competitors import (
    COMPETITOR_LINES,
    CompetitorBook,
    CompetitorsStore,
    CompetitorSuggestion,
    client_domains_everywhere,
    from_competitor_rows,
    from_search_results,
    merge_suggestions,
    read_competitors_csv,
    with_project_status,
)
from src.modules.research.gsc import GscSnapshot
from src.modules.research.importer import read_positions_csv
from src.modules.research.intent import brand_terms
from src.modules.research.models import ResearchSnapshot, WebsiteResearch
from src.modules.research.page_models import PageReport
from src.modules.research.semrush import (
    UNITS_PER_LINE,
    Pacer,
    SemrushClient,
    UnitBudget,
    UnitLog,
)
from src.modules.research.store import ResearchStore, write_research_workbook
from src.modules.theming.models import ThemeMap
from src.modules.theming.service import build_project_themes, theme_store

__all__ = [
    "ResearchSettings",
    "competitor_book",
    "competitors_store",
    "estimate_units",
    "import_competitors",
    "import_csv",
    "pull_project",
    "refresh_research_themes",
    "research_store",
    "theme_map_for",
    "theme_map_stale",
    "unit_log",
]

_LONG_PREFIX = "\\\\?\\"  # Windows extended-length path prefix, not shown to people


@dataclass(frozen=True)
class ResearchSettings:
    """The `CP_SEMRUSH_*` settings (passed in so tests can set them)."""

    enabled: bool = False
    api_key: str | None = None
    monthly_units: int = 0
    pull_units: int = 5000
    database: str = "us"
    keyword_limit: int = 200
    min_interval_ms: int = 500
    fresh_days: int = 14
    base_url: str = "https://api.semrush.com/"

    @classmethod
    def from_env(cls) -> ResearchSettings:
        """Read the settings from `.env`."""
        from src.core.config import get_settings

        s = get_settings()
        return cls(
            enabled=s.cp_semrush_enabled,
            api_key=s.semrush_api_key.get_secret_value() if s.semrush_api_key else None,
            monthly_units=s.cp_semrush_monthly_units,
            pull_units=s.cp_semrush_pull_units,
            database=s.cp_semrush_database,
            keyword_limit=s.cp_semrush_keyword_limit,
            min_interval_ms=s.cp_semrush_min_interval_ms,
            fresh_days=s.cp_semrush_fresh_days,
            base_url=s.cp_semrush_base_url,
        )


def address_refusal(base_url: str) -> str | None:
    """Why the API may not be called at this address (None when it may).

    The key travels with every call, so it goes only to Semrush itself (https) or to a
    server on this computer: the stand-in that serves dummy data for trying the module.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    host = (parts.hostname or "").casefold()
    if parts.scheme == "https" and host == "api.semrush.com":
        return None
    if parts.scheme in ("http", "https") and host in ("127.0.0.1", "localhost", "::1"):
        return None
    return (
        f"CP_SEMRUSH_BASE_URL {base_url!r} is refused: the API key is sent only to "
        "https://api.semrush.com/ or to the stand-in on this computer (http://127.0.0.1:...)"
    )


def research_store(projects: ProjectStore) -> ResearchStore:
    """The research store next to the projects (`<app data>/research`)."""
    return ResearchStore(Path(str(projects.root)) / "research")


def competitors_store(projects: ProjectStore) -> CompetitorsStore:
    """The suggested competitors next to the snapshots (`<app data>/research`)."""
    return CompetitorsStore(Path(str(projects.root)) / "research")


def competitor_book(projects: ProjectStore, project_id: str) -> CompetitorBook:
    """The project's suggested competitors, against its websites as they are now.

    A website that is a client's website in any project (a sister brand) is never
    among them: the client's websites are the client's side everywhere.
    """
    project = projects.get(project_id)
    found = competitors_store(projects).get(project_id)
    if found is None:
        main = project.main_site()
        found = CompetitorBook(
            project_id=project_id,
            updated_at=project.updated_at,
            client_domain=main.domains[0] if main and main.domains else None,
        )
    return with_project_status(found, project, client_domains_everywhere(projects.projects()))


def remember_competitors(
    projects: ProjectStore, project_id: str, found: list[CompetitorSuggestion]
) -> CompetitorBook:
    """Merge newly found competitors into the project's book and save it."""
    store = competitors_store(projects)
    book = merge_suggestions(
        store.get(project_id),
        found,
        projects.get(project_id),
        other_clients=client_domains_everywhere(projects.projects()),
    )
    store.save(book)
    return book


def unit_log(projects: ProjectStore) -> UnitLog:
    """The log of every Semrush call (`<app data>/research/units.jsonl`)."""
    return UnitLog(Path(str(projects.root)) / "research")


def estimate_units(websites: int, settings: ResearchSettings, keywords: int = 0) -> int:
    """The worst-case units of a pull (keyword list per website, results per keyword)."""
    per_site = (
        UNITS_PER_LINE["domain_ranks"] + settings.keyword_limit * UNITS_PER_LINE["domain_organic"]
    )
    per_keyword = UNITS_PER_LINE["phrase_this"] + 10 * UNITS_PER_LINE["phrase_organic"]
    competitors = COMPETITOR_LINES * UNITS_PER_LINE["domain_organic_organic"] if websites else 0
    return websites * per_site + keywords * per_keyword + competitors


def theme_map_stale(projects: ProjectStore, project_id: str, theme_map: ThemeMap) -> bool:
    """True when a scrape of any website of the project finished after the map was built."""
    newest = max(
        (
            r.finished_at
            for r in projects.runs(project_id)
            if r.status == "finished" and r.finished_at is not None
        ),
        default=None,
    )
    return newest is not None and newest > theme_map.built_at


def theme_map_for(
    projects: ProjectStore, access: KnowledgeAccess, project_id: str, *, rebuild: bool = False
) -> tuple[ThemeMap, bool]:
    """The project's theme map, and whether it was (re)built now.

    The latest build is reused unless `rebuild` is asked, none exists, or a scrape of
    any website of the project finished after it was built (a stale map would describe
    pages that have since changed). Building is local and free.
    """
    found = None if rebuild else theme_store(projects).latest(project_id)
    if found is not None and theme_map_stale(projects, project_id, found):
        found = None
    if found is None:
        found, _files = build_project_themes(projects, access, project_id)
        return found, True
    return found, False


def _theme_map(projects: ProjectStore, access: KnowledgeAccess, project_id: str) -> ThemeMap:
    return theme_map_for(projects, access, project_id)[0]


def _save(
    projects: ProjectStore, snapshot: ResearchSnapshot
) -> tuple[ResearchSnapshot, dict[str, str]]:
    store = research_store(projects)
    previous = store.latest(snapshot.project_id)
    path = store.save(snapshot)
    found = from_search_results(snapshot, snapshot.created_at)
    if found:
        remember_competitors(projects, snapshot.project_id, found)
    moves = movers(previous, snapshot) if previous else []
    workbook = write_research_workbook(snapshot, store.workbook(snapshot), moves)
    files = {"snapshot": path, "workbook": workbook}
    return snapshot, {k: str(v).removeprefix(_LONG_PREFIX) for k, v in files.items()}


def refresh_research_themes(
    projects: ProjectStore, project_id: str, theme_map: ThemeMap
) -> ResearchSnapshot | None:
    """Re-sync the latest research snapshot with a freshly built theme map."""
    store = research_store(projects)
    snapshot = store.latest(project_id)
    if snapshot is None:
        return None
    updated = finish_snapshot(snapshot, theme_map)
    store.save(updated)
    previous = store.previous(project_id)
    moves = movers(previous, updated) if previous else []
    write_research_workbook(updated, store.workbook(updated), moves)
    return updated


def _pull_competitors(
    projects: ProjectStore,
    project_id: str,
    client: SemrushClient,
    settings: ResearchSettings,
    snapshot: ResearchSnapshot,
) -> ResearchSnapshot:
    """Ask Semrush who competes with the client's main website and keep the answer."""
    from src.modules.research.semrush import SemrushError

    main = projects.get(project_id).main_site()
    if main is None or not main.domains:
        return snapshot
    domain = main.domains[0].split("/")[0]
    try:
        rows = client.organic_competitors(domain, settings.database, COMPETITOR_LINES)
    except SemrushError as exc:  # the keyword pull is kept; only the suggestions are missing
        note = f"Competitor suggestions could not be pulled: {exc}"
        return snapshot.model_copy(update={"notes": [*snapshot.notes, note]})
    found = from_competitor_rows(rows, "semrush_competitors", snapshot.created_at)
    book = remember_competitors(projects, project_id, found)
    new = sum(1 for s in book.suggestions if s.status == "suggested")
    note = f"Semrush named {len(found)} organic competitors of {domain}; {new} not in the project"
    return snapshot.model_copy(
        update={"notes": [*snapshot.notes, note], "units_spent": client.budget.spent}
    )


def import_competitors(projects: ProjectStore, project_id: str, data: bytes) -> CompetitorBook:
    """Import a Semrush "Organic Competitors" export of the client ($0)."""
    from src.modules.research.importer import ImportError_

    found = read_competitors_csv(data, datetime.now(UTC))
    if found is None:
        msg = (
            "this is not a Semrush Organic Competitors export (it needs a Domain column and "
            "Common Keywords or Competitor Relevance)"
        )
        raise ImportError_(msg)
    if not found:
        msg = "the export has no competitor rows"
        raise ImportError_(msg)
    return remember_competitors(projects, project_id, found)


def pull_project(
    projects: ProjectStore,
    access: KnowledgeAccess,
    project_id: str,
    settings: ResearchSettings,
    *,
    serp_keywords: Sequence[str] = (),
    http: Callable[[], httpx.Client] | None = None,
    refresh: bool = False,
    pacer: Pacer | None = None,
) -> tuple[ResearchSnapshot, dict[str, str]]:
    """Pull the project's websites from Semrush (refused unless switched on and capped).

    Websites pulled less than `fresh_days` ago are reused (unless `refresh`); the rest
    share the budget fairly (see `build.pull_snapshot`). Calls are paced.
    """
    from src.modules.research.semrush import SHARED_PACER, SemrushError

    log = unit_log(projects)
    budget = UnitBudget(
        log,
        enabled=settings.enabled and bool(settings.api_key),
        monthly_units=settings.monthly_units,
        pull_units=settings.pull_units,
    )
    refusal = budget.refusal() or address_refusal(settings.base_url)
    if refusal or not settings.api_key:
        raise SemrushError(refusal or "SEMRUSH_API_KEY is not set in .env")
    play = Playground(projects, access, [project_id])
    reuse = {} if refresh else _fresh(projects, project_id, settings)
    competitors_fresh = not refresh and _competitors_fresh(projects, project_id, settings)
    reserve = len(serp_keywords) * (
        UNITS_PER_LINE["phrase_this"] + 10 * UNITS_PER_LINE["phrase_organic"]
    ) + (0 if competitors_fresh else COMPETITOR_LINES * UNITS_PER_LINE["domain_organic_organic"])
    to_pull = len([w for w in play.websites() if w.key not in reuse])
    need = UNITS_PER_LINE["domain_ranks"] + MIN_KEYWORDS * UNITS_PER_LINE["domain_organic"]
    if to_pull and (budget.left - reserve) // to_pull < need:
        msg = (
            f"the pull may use {budget.left} units; after {reserve} for the chosen keywords and "
            f"the competitor report, each of the {to_pull} websites would get "
            f"{max(0, budget.left - reserve) // to_pull}, and at least {need} are needed (its "
            f"overview and {MIN_KEYWORDS} keywords): raise CP_SEMRUSH_PULL_UNITS or the monthly "
            "cap, or pull fewer keywords"
        )
        raise SemrushError(msg)
    pace = pacer or SHARED_PACER
    pace.interval_s = settings.min_interval_ms / 1000
    client = SemrushClient(
        settings.api_key,
        budget,
        log,
        project_id,
        client=http() if http else None,
        base_url=settings.base_url,
        pacer=pace,
    )
    try:
        snapshot = pull_snapshot(
            play,
            client,
            database=settings.database,
            keyword_limit=settings.keyword_limit,
            serp_keywords=serp_keywords,
            reserve=reserve,
            reuse=reuse,
        )
        if competitors_fresh:
            note = "Competitor suggestions are recent: the competitor report was not pulled again"
            snapshot = snapshot.model_copy(update={"notes": [*snapshot.notes, note]})
        else:
            snapshot = _pull_competitors(projects, project_id, client, settings, snapshot)
        if (
            address_refusal(settings.base_url) is None
            and "api.semrush.com" not in settings.base_url
        ):
            from src.modules.research.standin import DUMMY_NOTE

            snapshot = snapshot.model_copy(update={"notes": [DUMMY_NOTE, *snapshot.notes]})
        if client.retries:
            note = (
                f"Semrush asked to slow down {client.retries} time(s); the pull waited and went on"
            )
            snapshot = snapshot.model_copy(update={"notes": [*snapshot.notes, note]})
    finally:
        client.close()
    return _save(projects, finish_snapshot(snapshot, _theme_map(projects, access, project_id)))


def _fresh(
    projects: ProjectStore, project_id: str, settings: ResearchSettings
) -> dict[str, WebsiteResearch]:
    """Websites of the latest snapshot pulled from Semrush less than `fresh_days` ago."""
    if settings.fresh_days <= 0:
        return {}
    latest = research_store(projects).latest(project_id)
    cutoff = datetime.now(UTC) - timedelta(days=settings.fresh_days)
    return {
        w.website: w
        for w in (latest.websites if latest else [])
        if w.source == "semrush_api" and w.pulled_at >= cutoff and w.keywords
    }


def _competitors_fresh(projects: ProjectStore, project_id: str, settings: ResearchSettings) -> bool:
    """True when Semrush's competitor report was pulled less than `fresh_days` ago."""
    if settings.fresh_days <= 0:
        return False
    book = competitors_store(projects).get(project_id)
    cutoff = datetime.now(UTC) - timedelta(days=settings.fresh_days)
    return book is not None and any(
        "semrush_competitors" in s.sources and s.last_seen >= cutoff for s in book.suggestions
    )


def import_csv(
    projects: ProjectStore,
    access: KnowledgeAccess,
    project_id: str,
    website: str,
    data: bytes,
    *,
    database: str = "us",
) -> tuple[ResearchSnapshot, dict[str, str]]:
    """Import one website's Semrush Organic Positions export ($0)."""
    rows = read_positions_csv(data)
    play = Playground(projects, access, [project_id])
    previous = research_store(projects).latest(project_id)
    snapshot = import_website(play, website, rows, previous, database=database)
    return _save(projects, finish_snapshot(snapshot, _theme_map(projects, access, project_id)))


def import_gsc(projects: ProjectStore, project_id: str, website: str, data: bytes) -> GscSnapshot:
    """Keep a Search Console Performance export (zip or CSV) of one website ($0)."""
    from src.modules.research.gsc import read_gsc_export

    snapshot = read_gsc_export(data, project_id=project_id, website=website, now=datetime.now(UTC))
    research_store(projects).save_gsc(snapshot)
    return snapshot


def _paragraphs(path: Path) -> dict[str, list[str]]:
    """Body paragraphs per page from a run's knowledge chunks (one line, one paragraph)."""
    import json

    out: dict[str, list[str]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            chunk = json.loads(line)
            url, text = chunk.get("url"), chunk.get("text") or ""
            if url:
                out.setdefault(url, []).extend(t.strip() for t in text.splitlines() if t.strip())
    return out


def build_project_pages(
    projects: ProjectStore, access: KnowledgeAccess, project_id: str
) -> tuple[PageReport, dict[str, str]]:
    """The client website's page report from what the app holds; saved with its workbook."""
    from src.modules.content_processor.knowledge.inventory_models import PageInventory
    from src.modules.research.pages import build_page_report
    from src.modules.research.store import write_page_workbook

    play = Playground(projects, access, [project_id])
    store = research_store(projects)
    snapshot = store.latest(project_id)
    client = next((w for w in play.websites() if play.role_in_run(w) == "client"), None)
    website = (
        client.key
        if client
        else next(
            (w.website for w in (snapshot.websites if snapshot else []) if w.role == "client"), ""
        )
    )
    data = play.site_data(client) if client else None
    inventory: list[PageInventory] = []
    paragraphs: dict[str, list[str]] = {}
    stamps = {}
    if data is not None:
        path = data.file(".inventory.jsonl")
        if path is not None:
            with path.open(encoding="utf-8") as handle:
                inventory = [PageInventory.model_validate_json(x) for x in handle if x.strip()]
        stamps = play.stamps(data)
        chunks = data.file(".chunks.jsonl")
        if chunks is not None:
            paragraphs = _paragraphs(chunks)
    gsc = store.latest_gsc(project_id)
    report = build_page_report(
        project_id,
        website,
        snapshot=snapshot,
        previous=store.previous(project_id),
        inventory=inventory,
        stamps=stamps,
        theme_map=theme_store(projects).latest(project_id),
        gsc=gsc if gsc is not None and gsc.website == website else None,
        now=datetime.now(UTC),
        run_id=data.run_name if data else None,
        paragraphs=paragraphs,
        brands=brand_terms([w.key for w in play.websites()] + [w.name for w in play.websites()]),
    )
    path = store.save_pages(report)
    workbook = write_page_workbook(report, store.pages_workbook(project_id))
    return report, {"report": str(path), "workbook": str(workbook)}


def _client_run_name(
    projects: ProjectStore, access: KnowledgeAccess, project_id: str
) -> str | None:
    """The run the page report reads for the client website, or None (not scraped yet)."""
    play = Playground(projects, access, [project_id])
    client = next((w for w in play.websites() if play.role_in_run(w) == "client"), None)
    data = play.site_data(client) if client else None
    return data.run_name if data else None


def page_report(
    projects: ProjectStore, access: KnowledgeAccess, project_id: str
) -> PageReport | None:
    """The saved page report, rebuilt first when research, Search Console or the scrape is newer.

    The scrape alone is enough: internal links, PageRank, unlinked pages and on-page
    problems need no search data (the ranking columns are then empty). None when the
    project has no scraped client website and no research or Search Console data.
    """
    store = research_store(projects)
    snapshot = store.latest(project_id)
    gsc = store.latest_gsc(project_id)
    run_name = _client_run_name(projects, access, project_id)
    if snapshot is None and gsc is None and run_name is None:
        return None
    saved = store.latest_pages(project_id)
    if (
        saved is not None
        and saved.research_snapshot_id == (snapshot.snapshot_id if snapshot else None)
        and saved.gsc_imported_at == (gsc.imported_at if gsc else None)
        and saved.run_id == run_name
    ):
        return saved
    report, _files = build_project_pages(projects, access, project_id)
    return report
