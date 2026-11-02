"""Build, save and read a project's theme map (shared by the CLI, the API and the cards)."""

from __future__ import annotations

from pathlib import Path

from src.modules.analyst.playground import Playground
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.theming.build import build_theme_map
from src.modules.theming.models import ThemeMap
from src.modules.theming.store import ThemeStore, carry_ids, write_theme_workbook

__all__ = ["build_project_themes", "theme_store", "workbook_path"]


def theme_store(projects: ProjectStore) -> ThemeStore:
    """The theme store next to the projects (`<app data>/theming`)."""
    return ThemeStore(Path(str(projects.root)) / "theming")


def workbook_path(store: ThemeStore, theme_map: ThemeMap) -> Path:
    """Where a map's workbook is written."""
    return store.folder(theme_map.project_id) / f"{theme_map.map_id}_themes.xlsx"


def build_project_themes(
    projects: ProjectStore,
    access: KnowledgeAccess,
    project_id: str,
    *,
    settings: dict[str, float] | None = None,
) -> tuple[ThemeMap, dict[str, str]]:
    """Build the map of a project's websites, save it and its workbook; returns both."""
    play = Playground(projects, access, [project_id])
    store = theme_store(projects)
    # a theme keeps its id from the previous build when it is the same theme
    theme_map = carry_ids(store.latest(project_id), build_theme_map(play, project_id, settings))
    path = store.save(theme_map)
    workbook = write_theme_workbook(theme_map, workbook_path(store, theme_map))
    files = {"map": path, "workbook": workbook}
    long_prefix = "\\\\?\\"  # Windows extended-length path prefix, not shown to people
    return theme_map, {k: str(v).removeprefix(long_prefix) for k, v in files.items()}
