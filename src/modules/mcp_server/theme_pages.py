"""`get_theme_pages`: every page of one theme, in full, for Claude Desktop.

It runs in the stdio server, not in the web app. It reads the project's theme map from the
app's existing read-only endpoint (`GET /api/projects/{id}/themes`), so the web app's code
and its running scrapes are not touched. The map tags every captured page with the theme it
belongs to, so the list is complete; nothing is truncated.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

__all__ = ["LOCAL_TOOLS", "theme_pages_text"]

LOCAL_TOOLS: frozenset[str] = frozenset({"get_theme_pages"})
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def _error(message: str) -> tuple[str, bool]:
    return json.dumps({"error": message}, ensure_ascii=False), True


def _match_theme(themes: list[dict[str, Any]], wanted: str) -> tuple[dict[str, Any] | None, str]:
    """The theme for an id or a name; (None, reason) when none or more than one matches."""
    for theme in themes:
        if theme["theme_id"] == wanted:
            return theme, ""
    folded = " ".join(wanted.split()).casefold()
    exact = [t for t in themes if " ".join(t["label"].split()).casefold() == folded]
    if len(exact) == 1:
        return exact[0], ""
    partial = [t for t in themes if folded in " ".join(t["label"].split()).casefold()]
    if len(partial) == 1:
        return partial[0], ""
    names = "; ".join(f"{t['theme_id']} = {t['label']}" for t in themes)
    if not partial and not exact:
        return None, f"No theme matches '{wanted}'. Themes: {names}"
    return None, f"'{wanted}' matches more than one theme; use the id. Themes: {names}"


async def theme_pages_text(
    client: httpx.AsyncClient, arguments: dict[str, Any]
) -> tuple[str, bool]:
    """The JSON text answer for `get_theme_pages`, and whether it is an error."""
    project = str(arguments.get("project_id") or "").strip()
    theme_arg = str(arguments.get("theme") or "").strip()
    website = str(arguments.get("website") or "").strip().lower()
    if not _PROJECT.match(project):
        return _error("Give the project_id from list_projects.")
    if not theme_arg:
        return _error("Give the theme id (th-...) or its name, as get_theme_map lists them.")
    try:
        response = await client.get(f"/api/projects/{project}/themes")
    except httpx.ConnectError:
        return _error("The Content Processor app is not running; start it, then try again.")
    except httpx.TimeoutException:
        return _error("The app took too long to answer; try again.")
    if response.status_code == 404:
        return _error("There is no project with that id.")
    if response.status_code != 200:
        return _error(f"The app answered with an error (HTTP {response.status_code}).")
    data = response.json()
    themes = data.get("themes") or []
    if not themes:
        return _error("This project has no theme map yet. Build it in the app's Themes tab.")
    theme, reason = _match_theme(themes, theme_arg)
    if theme is None:
        return _error(reason)
    pages = [p for p in data.get("pages") or [] if p.get("theme_id") == theme["theme_id"]]
    if website:
        pages = [p for p in pages if (p.get("website") or "").lower() == website]
    pages.sort(key=lambda p: p.get("url") or "")
    rows = [
        {
            "url": p.get("url"),
            "title": p.get("title"),
            "website": p.get("website"),
            "role": p.get("role"),
            "page_type": p.get("page_type"),
            "funnel_stage": p.get("funnel_stage"),
            "words": p.get("words"),
        }
        for p in pages
    ]
    answer: dict[str, Any] = {
        "project_id": project,
        "theme_id": theme["theme_id"],
        "theme": theme["label"],
        "map_built_at": data.get("built_at"),
        "website_filter": website or None,
        "count": len(rows),
        "pages": rows,
    }
    return json.dumps(answer, ensure_ascii=False, indent=2), False
