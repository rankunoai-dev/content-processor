"""Monitoring checks and watch settings on disk, and the check workbook.

`<app data>/monitoring/<project>/`: every check as `<check id>.json`, the newest as
`latest.json`, the watch settings as `watch.json`, and each check's workbook.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.monitoring.models import MonitorCheck, WatchSettings

__all__ = ["MonitorStore", "check_tables", "write_check_workbook"]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_CHECK_ID = re.compile(r"^check-\d{8}-\d{6}-\d{3}$")


class MonitorStore:
    """Checks and watch settings of every project under `<root>`."""

    def __init__(self, root: Path) -> None:
        """Bind to the folder (usually `<app data>/monitoring`)."""
        self.root = root
        self._lock = threading.Lock()

    def folder(self, project_id: str) -> Path:
        """The project's folder (the id is checked, so it cannot escape the root)."""
        if not _ID.match(project_id):
            raise KeyError(project_id)
        return self.root / project_id

    def save(self, check: MonitorCheck) -> Path:
        """Write the check and make it the latest."""
        folder = self.folder(check.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        text = check.model_dump_json(indent=1)
        path = folder / f"{check.check_id}.json"
        with self._lock:
            path.write_text(text, encoding="utf-8")
            tmp = folder / "latest.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(folder / "latest.json")
        return path

    def latest(self, project_id: str) -> MonitorCheck | None:
        """The newest check, or None."""
        path = self.folder(project_id) / "latest.json"
        if not path.exists():
            return None
        with self._lock:
            return MonitorCheck.model_validate_json(path.read_text(encoding="utf-8"))

    def get(self, project_id: str, check_id: str) -> MonitorCheck:
        """One check; raises `KeyError`."""
        if not _CHECK_ID.match(check_id):
            raise KeyError(check_id)
        path = self.folder(project_id) / f"{check_id}.json"
        if not path.exists():
            raise KeyError(check_id)
        return MonitorCheck.model_validate_json(path.read_text(encoding="utf-8"))

    def history(self, project_id: str) -> list[str]:
        """Every check's id, newest first."""
        folder = self.folder(project_id)
        if not folder.exists():
            return []
        return sorted((p.stem for p in folder.glob("check-*.json")), reverse=True)

    def watch(self, project_id: str) -> WatchSettings:
        """The project's watch settings (defaults when never set)."""
        path = self.folder(project_id) / "watch.json"
        if not path.exists():
            return WatchSettings()
        with self._lock:
            return WatchSettings.model_validate_json(path.read_text(encoding="utf-8"))

    def set_watch(self, project_id: str, settings: WatchSettings) -> WatchSettings:
        """Save the project's watch settings."""
        folder = self.folder(project_id)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = folder / "watch.json.tmp"
            tmp.write_text(settings.model_dump_json(indent=1), encoding="utf-8")
            tmp.replace(folder / "watch.json")
        return settings

    def workbook(self, check: MonitorCheck) -> Path:
        """Where a check's workbook is written."""
        return self.folder(check.project_id) / f"{check.check_id}_monitoring.xlsx"


_HOW_TO_READ: list[tuple[str, str]] = [
    (
        "What a check compares",
        "For each website, its latest scrape with the scrape that was latest at the previous "
        "check (the first time: the scrape before). Nothing is fetched: a check reads the "
        "scrapes already made.",
    ),
    (
        "Drift",
        "The share of a page's sections (or of a website's pages) that were added, removed "
        "or changed.",
    ),
    (
        "Price change",
        "An amount (currency or percentage) in the same wording in both scrapes with a different "
        "value, for example '$25 per month' becoming '$19 per month'.",
    ),
    (
        "Theme drift",
        "New pages placed in the project's theme map, removed pages in the previous map, and "
        "each theme's ownership before and after.",
    ),
    (
        "Alert severity",
        "high: a competitor push into a client theme, a theme moving away from the client, a "
        "competitor price change, client pages removed. medium: a competitor building a new "
        "theme, competitor title/H1 rewrites, pillar pages changing a lot, client price "
        "changes. low and info: competitor pages removed, client content changes, ownership "
        "moving towards the client.",
    ),
]


def check_tables(check: MonitorCheck) -> list[Table]:
    """The workbook's sheets."""
    alerts: list[dict[str, Cell]] = [
        {
            "Severity": a.severity,
            "Alert": a.title,
            "Website": a.website,
            "Detail": a.detail,
            "Pages": "\n".join(a.urls[:10]),
        }
        for a in check.alerts
    ]
    sites: list[dict[str, Cell]] = [
        {
            "Website": d.name,
            "Role": d.role,
            "Compared": f"{d.baseline_run or '-'} → {d.latest_run or '-'}",
            "New pages": len(d.new_pages),
            "Removed pages": len(d.removed_pages),
            "Changed pages": len(d.changed_pages),
            "Unchanged pages": d.unchanged_pages,
            "Template changes": len(d.template_changes),
            "Drift": d.drift,
            "Note": d.note,
        }
        for d in check.websites
    ]
    changed: list[dict[str, Cell]] = [
        {
            "Website": d.website,
            "URL": c.url,
            "Title": c.title or "",
            "Theme": c.theme or "",
            "Pillar": "yes" if c.is_pillar else "",
            "Drift": c.drift,
            "Sections added": c.sections_added,
            "Sections removed": c.sections_removed,
            "Head changes": "; ".join(
                f"{k}: {v.get('before')} → {v.get('after')}" for k, v in c.head_changes.items()
            ),
            "Prices": "; ".join(f"{p.before} → {p.after} ({p.context})" for p in c.prices),
            "Added text (start)": "\n".join(t[:300] for t in c.added_text[:3]),
        }
        for d in check.websites
        for c in d.changed_pages
    ]
    pages: list[dict[str, Cell]] = [
        {
            "Website": d.website,
            "Change": kind,
            "URL": p.url,
            "Title": p.title or "",
            "Theme": p.theme or "",
        }
        for d in check.websites
        for kind, refs in (("new", d.new_pages), ("removed", d.removed_pages))
        for p in refs
    ]
    drift: list[dict[str, Cell]] = [
        {
            "Theme": t.label,
            "Status": t.status,
            "Ownership before": t.ownership_before or "",
            "Ownership after": t.ownership_after or "",
            "New pages": ", ".join(f"{k} {v}" for k, v in sorted(t.new_pages.items())),
            "Removed pages": ", ".join(f"{k} {v}" for k, v in sorted(t.removed_pages.items())),
            "Pages before": ", ".join(f"{k} {v}" for k, v in sorted(t.pages_before.items())),
            "Pages after": ", ".join(f"{k} {v}" for k, v in sorted(t.pages_after.items())),
        }
        for t in check.theme_drift
    ]
    wrap = frozenset({"Detail", "Pages", "Added text (start)"})
    return [
        Table(
            "Alerts",
            ["Severity", "Alert", "Website", "Detail", "Pages"],
            alerts,
            wrap,
            {"Alert": 50, "Detail": 70, "Pages": 60},
        ),
        Table("Websites", list(sites[0]) if sites else ["Website"], sites),
        Table(
            "Changed pages",
            list(changed[0]) if changed else ["URL"],
            changed,
            wrap,
            {"URL": 60, "Added text (start)": 70},
        ),
        Table(
            "New and removed pages",
            ["Website", "Change", "URL", "Title", "Theme"],
            pages,
            widths={"URL": 70, "Title": 40},
        ),
        Table("Theme drift", list(drift[0]) if drift else ["Theme"], drift),
        Table(
            "How to read",
            ["Column", "Meaning"],
            [{"Column": k, "Meaning": v} for k, v in _HOW_TO_READ],
            widths={"Meaning": 110},
        ),
    ]


def write_check_workbook(check: MonitorCheck, path: Path) -> Path:
    """The check's workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, check_tables(check))
    return path
