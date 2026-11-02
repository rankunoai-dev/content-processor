"""Pull, import and read research snapshots (shared by the CLI, the API and the cards)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from src.modules.analyst.playground import Playground
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.research.build import finish_snapshot, import_website, movers, pull_snapshot
from src.modules.research.importer import read_positions_csv
from src.modules.research.models import ResearchSnapshot
from src.modules.research.semrush import UNITS_PER_LINE, SemrushClient, UnitBudget, UnitLog
from src.modules.research.store import ResearchStore, write_research_workbook
from src.modules.theming.models import ThemeMap
from src.modules.theming.service import build_project_themes, theme_store

__all__ = [
    "ResearchSettings",
    "estimate_units",
    "import_csv",
    "pull_project",
    "research_store",
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
        )


def research_store(projects: ProjectStore) -> ResearchStore:
    """The research store next to the projects (`<app data>/research`)."""
    return ResearchStore(Path(str(projects.root)) / "research")


def unit_log(projects: ProjectStore) -> UnitLog:
    """The log of every Semrush call (`<app data>/research/units.jsonl`)."""
    return UnitLog(Path(str(projects.root)) / "research")


def estimate_units(websites: int, settings: ResearchSettings, keywords: int = 0) -> int:
    """The worst-case units of a pull (keyword list per website, results per keyword)."""
    per_site = (
        UNITS_PER_LINE["domain_ranks"] + settings.keyword_limit * UNITS_PER_LINE["domain_organic"]
    )
    per_keyword = UNITS_PER_LINE["phrase_this"] + 10 * UNITS_PER_LINE["phrase_organic"]
    return websites * per_site + keywords * per_keyword


def _theme_map(projects: ProjectStore, access: KnowledgeAccess, project_id: str) -> ThemeMap:
    found = theme_store(projects).latest(project_id)
    if found is None:  # building the theme map is local and free
        found, _files = build_project_themes(projects, access, project_id)
    return found


def _save(
    projects: ProjectStore, snapshot: ResearchSnapshot
) -> tuple[ResearchSnapshot, dict[str, str]]:
    store = research_store(projects)
    previous = store.latest(snapshot.project_id)
    path = store.save(snapshot)
    moves = movers(previous, snapshot) if previous else []
    workbook = write_research_workbook(snapshot, store.workbook(snapshot), moves)
    files = {"snapshot": path, "workbook": workbook}
    return snapshot, {k: str(v).removeprefix(_LONG_PREFIX) for k, v in files.items()}


def pull_project(
    projects: ProjectStore,
    access: KnowledgeAccess,
    project_id: str,
    settings: ResearchSettings,
    *,
    serp_keywords: Sequence[str] = (),
    http: Callable[[], httpx.Client] | None = None,
) -> tuple[ResearchSnapshot, dict[str, str]]:
    """Pull the project's websites from Semrush (refused unless switched on and capped)."""
    from src.modules.research.semrush import SemrushError

    log = unit_log(projects)
    budget = UnitBudget(
        log,
        enabled=settings.enabled and bool(settings.api_key),
        monthly_units=settings.monthly_units,
        pull_units=settings.pull_units,
    )
    refusal = budget.refusal()
    if refusal or not settings.api_key:
        raise SemrushError(refusal or "SEMRUSH_API_KEY is not set in .env")
    play = Playground(projects, access, [project_id])
    worst = estimate_units(len(play.websites()), settings, len(serp_keywords))
    if worst > budget.left:
        msg = (
            f"this pull could use up to {worst} units ({len(play.websites())} websites x "
            f"{settings.keyword_limit} keywords); only {budget.left} are allowed: lower "
            "CP_SEMRUSH_KEYWORD_LIMIT or raise the caps"
        )
        raise SemrushError(msg)
    client = SemrushClient(
        settings.api_key, budget, log, project_id, client=http() if http else None
    )
    try:
        snapshot = pull_snapshot(
            play,
            client,
            database=settings.database,
            keyword_limit=settings.keyword_limit,
            serp_keywords=serp_keywords,
        )
    finally:
        client.close()
    return _save(projects, finish_snapshot(snapshot, _theme_map(projects, access, project_id)))


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
