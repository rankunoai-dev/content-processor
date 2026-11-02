"""Where theme maps are kept, how two builds compare, and the theme workbook.

Maps live in `<app data>/theming/<project id>/`: every build as `<map id>.json` and
the newest as `latest.json`. Theme ids are stable for the same content, so two
builds can be compared theme by theme (`compare_maps`): which themes appeared or
disappeared, which grew or shrank on which website, and whether ownership moved.
Monitoring and Gap Analysis read that.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.theming.models import ThemeMap

__all__ = [
    "ThemeStore",
    "carry_ids",
    "compare_maps",
    "match_themes",
    "theme_tables",
    "write_theme_workbook",
]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_MAP_ID = re.compile(r"^themes-\d{8}-\d{6}(-\d{3})?$")


class ThemeStore:
    """Theme maps of every project under `<root>` (usually `<app data>/theming`)."""

    def __init__(self, root: Path) -> None:
        """Bind to the folder."""
        self.root = root
        self._lock = threading.Lock()

    def folder(self, project_id: str) -> Path:
        """The project's folder (the id is checked, so it cannot escape the root)."""
        if not _ID.match(project_id):
            raise KeyError(project_id)
        return self.root / project_id

    def save(self, theme_map: ThemeMap) -> Path:
        """Write the map and make it the latest; returns its file."""
        folder = self.folder(theme_map.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        text = theme_map.model_dump_json(indent=1)
        path = folder / f"{theme_map.map_id}.json"
        with self._lock:
            path.write_text(text, encoding="utf-8")
            tmp = folder / "latest.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(folder / "latest.json")
        return path

    def latest(self, project_id: str) -> ThemeMap | None:
        """The newest map, or None."""
        path = self.folder(project_id) / "latest.json"
        if not path.exists():
            return None
        with self._lock:
            return ThemeMap.model_validate_json(path.read_text(encoding="utf-8"))

    def get(self, project_id: str, map_id: str) -> ThemeMap:
        """One build; raises `KeyError`."""
        if not _MAP_ID.match(map_id):
            raise KeyError(map_id)
        path = self.folder(project_id) / f"{map_id}.json"
        if not path.exists():
            raise KeyError(map_id)
        return ThemeMap.model_validate_json(path.read_text(encoding="utf-8"))

    def history(self, project_id: str) -> list[str]:
        """Every build's id, newest first."""
        folder = self.folder(project_id)
        if not folder.exists():
            return []
        return sorted((p.stem for p in folder.glob("themes-*.json")), reverse=True)


MATCH_PAGES = 0.5
MATCH_TERMS = 0.3


def _members(theme_map: ThemeMap) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for page in theme_map.pages:
        if page.theme_id:
            out.setdefault(page.theme_id, set()).add(page.url)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def match_themes(old: ThemeMap, new: ThemeMap) -> dict[str, str]:
    """New theme id -> old theme id for the same theme whose id changed.

    Ids are equal when a theme's defining terms are unchanged. When content grows or
    shrinks the terms can shift; the theme is still the same when it keeps at least
    half of its pages, or a third of its defining terms. Pairs are taken best first.
    """
    old_ids = {t.theme_id for t in old.themes}
    new_ids = {t.theme_id for t in new.themes}
    old_left = {t.theme_id: t for t in old.themes if t.theme_id not in new_ids}
    new_left = {t.theme_id: t for t in new.themes if t.theme_id not in old_ids}
    old_pages, new_pages = _members(old), _members(new)
    pairs: list[tuple[float, str, str]] = []
    for n_id, n_theme in new_left.items():
        for o_id, o_theme in old_left.items():
            pages = _jaccard(new_pages.get(n_id, set()), old_pages.get(o_id, set()))
            terms = _jaccard(set(n_theme.terms), set(o_theme.terms))
            if pages >= MATCH_PAGES or terms >= MATCH_TERMS:
                pairs.append((max(pages, terms), n_id, o_id))
    matched: dict[str, str] = {}
    used: set[str] = set()
    for _score, n_id, o_id in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        if n_id not in matched and o_id not in used:
            matched[n_id] = o_id
            used.add(o_id)
    return matched


def carry_ids(previous: ThemeMap | None, new: ThemeMap) -> ThemeMap:
    """The new map with each theme keeping its previous id when it is the same theme."""
    if previous is None:
        return new
    matched = match_themes(previous, new)
    if not matched:
        return new
    themes = [
        t.model_copy(update={"theme_id": matched.get(t.theme_id, t.theme_id)}) for t in new.themes
    ]
    pages = [
        p.model_copy(update={"theme_id": matched.get(p.theme_id or "", p.theme_id)})
        for p in new.pages
    ]
    return new.model_copy(update={"themes": themes, "pages": pages})


def compare_maps(old: ThemeMap, new: ThemeMap) -> dict[str, Any]:
    """Theme by theme: appeared, disappeared, pages per website, ownership moves."""
    new = carry_ids(old, new)
    before = {t.theme_id: t for t in old.themes}
    after = {t.theme_id: t for t in new.themes}
    changed = []
    for theme_id in sorted(set(before) & set(after)):
        a, b = before[theme_id], after[theme_id]
        pages_a = {c.website: c.pages for c in a.coverage}
        pages_b = {c.website: c.pages for c in b.coverage}
        moves = {
            site: {"before": pages_a.get(site, 0), "after": pages_b.get(site, 0)}
            for site in sorted(set(pages_a) | set(pages_b))
            if pages_a.get(site, 0) != pages_b.get(site, 0)
        }
        if moves or a.ownership != b.ownership:
            changed.append(
                {
                    "theme_id": theme_id,
                    "label": b.label,
                    "pages": moves,
                    "ownership": {"before": a.ownership, "after": b.ownership},
                }
            )
    return {
        "from": old.map_id,
        "to": new.map_id,
        "appeared": [
            {"theme_id": t, "label": after[t].label} for t in sorted(set(after) - set(before))
        ],
        "disappeared": [
            {"theme_id": t, "label": before[t].label} for t in sorted(set(before) - set(after))
        ],
        "changed": changed,
    }


_HOW_TO_READ: list[tuple[str, str]] = [
    ("Theme", "A group of pages about the same subject, across all websites of the project."),
    ("Terms", "The words that set the theme apart; the theme's name is made from the first ones."),
    (
        "Ownership",
        "client_owned: the client has at least 1.5 times the pages of any competitor. "
        "competitor_led: a competitor has at least 1.5 times the client's. contested: in "
        "between. client_gap: competitors cover it, the client does not. client_only: only "
        "the client covers it.",
    ),
    (
        "Pillar",
        "On each website, the page that anchors the theme: central to it, shallow in the "
        "site, an offering page in the catalog, and rich in content. The other pages are spokes.",
    ),
    ("Topic", "A narrower group inside a theme."),
    (
        "Funnel stage",
        "learn (education, news), compare (comparisons, pricing), decide (product, quote, "
        "contact), onboard (getting started), support (help, forms, account); from the page's "
        "type, address, title and calls to action.",
    ),
    (
        "How it is built",
        "Offline and free: page titles, headings, breadcrumbs, descriptions and the start of "
        "the text are compared in one word space for all websites. Nothing is sent anywhere.",
    ),
]


def theme_tables(theme_map: ThemeMap) -> list[Table]:
    """The workbook's sheets."""
    sites = [w.website for w in theme_map.websites]
    names = {w.website: f"{w.name} ({w.role})" for w in theme_map.websites}
    themes: list[dict[str, Cell]] = []
    matrix: list[dict[str, Cell]] = []
    for t in theme_map.themes:
        cov = {c.website: c for c in t.coverage}
        themes.append(
            {
                "Theme": t.label,
                "Theme id": t.theme_id,
                "Pages": t.pages,
                "Ownership": t.ownership,
                "Why": t.ownership_reason,
                "Terms": ", ".join(t.terms),
                "Topics": len(t.topics),
                "Offerings": ", ".join(t.offerings),
                "Funnel": ", ".join(f"{k} {v}" for k, v in sorted(t.funnel.items())),
                **{
                    f"Pillar: {names[s]}": (p.url if (p := cov[s].pillar) else "")
                    for s in sites
                    if s in cov
                },
            }
        )
        matrix.append(
            {
                "Theme": t.label,
                "Ownership": t.ownership,
                **{names[s]: cov[s].pages if s in cov else 0 for s in sites},
            }
        )
    topics: list[dict[str, Cell]] = [
        {
            "Theme": t.label,
            "Topic": tp.label,
            "Topic id": tp.topic_id,
            "Pages": len(tp.pages),
            "Websites": ", ".join(f"{k} {v}" for k, v in sorted(tp.websites.items())),
            "Terms": ", ".join(tp.terms),
        }
        for t in theme_map.themes
        for tp in t.topics
    ]
    labels = {t.theme_id: t.label for t in theme_map.themes}
    topic_labels = {tp.topic_id: tp.label for t in theme_map.themes for tp in t.topics}
    pages: list[dict[str, Cell]] = [
        {
            "URL": p.url,
            "Website": p.website,
            "Title": p.title or "",
            "Theme": labels.get(p.theme_id or "", "(unclustered)"),
            "Topic": topic_labels.get(p.topic_id or "", ""),
            "Role": p.role,
            "Similarity": p.similarity,
            "Funnel stage": p.funnel_stage,
            "Why this stage": p.funnel_reason,
            "Page type": p.page_type or "",
            "Words": p.words,
        }
        for p in theme_map.pages
    ]
    theme_cols = [
        "Theme",
        "Theme id",
        "Pages",
        "Ownership",
        "Why",
        "Terms",
        "Topics",
        "Offerings",
        "Funnel",
        *[f"Pillar: {names[s]}" for s in sites],
    ]
    return [
        Table("Themes", theme_cols, themes, widths={"Theme": 40, "Terms": 50, "Why": 45}),
        Table("Ownership matrix", ["Theme", "Ownership", *[names[s] for s in sites]], matrix),
        Table("Topics", ["Theme", "Topic", "Topic id", "Pages", "Websites", "Terms"], topics),
        Table(
            "Pages",
            [
                "URL",
                "Website",
                "Title",
                "Theme",
                "Topic",
                "Role",
                "Similarity",
                "Funnel stage",
                "Why this stage",
                "Page type",
                "Words",
            ],
            pages,
            widths={"URL": 60, "Title": 40, "Theme": 35},
        ),
        Table(
            "How to read",
            ["Column", "Meaning"],
            [{"Column": k, "Meaning": v} for k, v in _HOW_TO_READ],
            widths={"Meaning": 110},
        ),
    ]


def write_theme_workbook(theme_map: ThemeMap, path: Path) -> Path:
    """`<project>_themes.xlsx` with themes, ownership, topics, pages and how to read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, theme_tables(theme_map))
    return path
