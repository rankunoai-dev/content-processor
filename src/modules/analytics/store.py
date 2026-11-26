"""GA4 snapshots on disk, the project's GA4 settings, and the analytics workbook.

`<app data>/analytics/<project>/`: every snapshot as `<snapshot id>.json`, the newest as
`latest.json`, `settings.json` (GA4 property and website), `analytics.xlsx`.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from src.modules.analytics.models import AnalyticsReport, AnalyticsSettings, Ga4Snapshot
from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx

__all__ = ["AnalyticsStore", "analytics_tables", "write_analytics_workbook"]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_SNAPSHOT_ID = re.compile(r"^ga4-\d{8}-\d{6}-\d{3}$")


class AnalyticsStore:
    """GA4 snapshots of every project under `<root>`."""

    def __init__(self, root: Path) -> None:
        """Bind to the folder (usually `<app data>/analytics`)."""
        self.root = root
        self._lock = threading.Lock()

    def folder(self, project_id: str) -> Path:
        """The project's folder (the id is checked, so it cannot escape the root)."""
        if not _ID.match(project_id):
            raise KeyError(project_id)
        return self.root / project_id

    def save(self, snapshot: Ga4Snapshot) -> Path:
        """Write the snapshot and make it the latest."""
        folder = self.folder(snapshot.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        text = snapshot.model_dump_json(indent=1)
        path = folder / f"{snapshot.snapshot_id}.json"
        with self._lock:
            path.write_text(text, encoding="utf-8")
            tmp = folder / "latest.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(folder / "latest.json")
        return path

    def latest(self, project_id: str) -> Ga4Snapshot | None:
        """The newest snapshot, or None."""
        path = self.folder(project_id) / "latest.json"
        if not path.exists():
            return None
        with self._lock:
            return Ga4Snapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def get(self, project_id: str, snapshot_id: str) -> Ga4Snapshot:
        """One snapshot; raises `KeyError`."""
        path = self.folder(project_id) / f"{snapshot_id}.json"
        if not _SNAPSHOT_ID.match(snapshot_id) or not path.exists():
            raise KeyError(snapshot_id)
        return Ga4Snapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def history(self, project_id: str) -> list[str]:
        """Every snapshot's id, newest first."""
        folder = self.folder(project_id)
        if not folder.exists():
            return []
        return sorted((p.stem for p in folder.glob("ga4-*.json")), reverse=True)

    def previous(self, project_id: str) -> Ga4Snapshot | None:
        """The period before the latest, or None.

        The newest earlier snapshot of the same website
        that ends before the latest starts (or, without dates, the one before it).
        """
        latest = self.latest(project_id)
        if latest is None:
            return None
        for snapshot_id in self.history(project_id):
            if snapshot_id == latest.snapshot_id:
                continue
            other = self.get(project_id, snapshot_id)
            if other.website != latest.website:
                continue
            if latest.start_date and other.end_date and other.end_date >= latest.start_date:
                continue
            return other
        return None

    def settings(self, project_id: str) -> AnalyticsSettings:
        """The project's GA4 property and website (empty until set)."""
        path = self.folder(project_id) / "settings.json"
        if not path.exists():
            return AnalyticsSettings()
        return AnalyticsSettings.model_validate_json(path.read_text(encoding="utf-8"))

    def save_settings(self, project_id: str, settings: AnalyticsSettings) -> AnalyticsSettings:
        """Keep the project's GA4 property and website."""
        folder = self.folder(project_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "settings.json").write_text(settings.model_dump_json(indent=1), encoding="utf-8")
        return settings

    def workbook(self, project_id: str) -> Path:
        """Where the analytics workbook is written."""
        return self.folder(project_id) / "analytics.xlsx"


_HOW_TO_READ: list[tuple[str, str]] = [
    ("Sessions", "Visits that started on the page (GA4 landing page), all channels."),
    ("Organic sessions", "Of those, from the Organic Search channel (needs the channel column)."),
    (
        "Engagement rate",
        "Engaged sessions / sessions: stayed 10+ seconds, saw 2+ pages or converted.",
    ),
    ("Key events", "GA4 conversions (key events) of sessions that started on the page."),
    (
        "Leaky pages",
        "Real search traffic that does not engage or convert: fix the page before "
        "writing new ones.",
    ),
    ("Decays", "Organic sessions or key events down 30%+ against the period before."),
    (
        "CTAs without conversions",
        "A form or call to action on the page, traffic, and no key events.",
    ),
    (
        "AI referrals",
        "Sessions sent by AI assistants (chatgpt.com, perplexity.ai, gemini.google.com...).",
    ),
]


def analytics_tables(snapshot: Ga4Snapshot, report: AnalyticsReport | None) -> list[Table]:
    """The workbook's sheets."""
    pages: list[dict[str, Cell]] = [
        {
            "Landing page": r.url,
            "Sessions": r.sessions,
            "Organic sessions": r.organic_sessions,
            "Engaged sessions": r.engaged_sessions,
            "Engagement rate": r.engagement_rate,
            "Avg engagement (s)": r.avg_engagement_s,
            "Key events": r.key_events,
            "Conversion rate": round(r.conversion_rate, 4),
            "Revenue": r.revenue,
            "AI sessions": r.ai_sessions,
        }
        for r in snapshot.rows
    ]
    tables = [
        Table(
            "Landing pages",
            list(pages[0]) if pages else ["Landing page"],
            pages,
            widths={"Landing page": 70},
        )
    ]
    if report is not None:

        def rollup(rows: list[object], name: str) -> Table:
            data: list[dict[str, Cell]] = [
                {
                    name: getattr(g, "label", ""),
                    "Pages": getattr(g, "pages", 0),
                    "Sessions": getattr(g, "sessions", 0),
                    "Organic sessions": getattr(g, "organic_sessions", 0),
                    "Key events": getattr(g, "key_events", 0.0),
                    "Conversion rate": round(getattr(g, "conversion_rate", 0.0), 4),
                    "Revenue": getattr(g, "revenue", 0.0),
                    "Searches a month": getattr(g, "search_volume", None),
                }
                for g in rows
            ]
            cols = [
                name,
                "Pages",
                "Sessions",
                "Organic sessions",
                "Key events",
                "Conversion rate",
                "Revenue",
                "Searches a month",
            ]
            return Table(f"By {name.lower()}", cols, data, widths={name: 50})

        tables += [
            rollup(list(report.themes), "Theme"),
            rollup(list(report.funnel), "Funnel stage"),
            Table(
                "Leaky pages",
                ["URL", "Organic sessions", "GSC clicks", "Engagement rate", "Key events", "Why"],
                [
                    {"URL": p.url, "Organic sessions": p.organic_sessions,
                     "GSC clicks": p.gsc_clicks, "Engagement rate": p.engagement_rate,
                     "Key events": p.key_events, "Why": p.reason}
                    for p in report.leaky
                ],
                widths={"URL": 60, "Why": 60},
            ),
            Table(
                "Decays",
                ["URL", "Metric", "Before", "After", "Change"],
                [
                    {"URL": d.url, "Metric": d.metric.replace("_", " "), "Before": d.before,
                     "After": d.after, "Change": d.change}
                    for d in report.decays
                ],
                widths={"URL": 60},
            ),
            Table(
                "CTAs without conversions",
                ["URL", "Sessions", "Forms", "Calls to action"],
                [
                    {"URL": c.url, "Sessions": c.sessions, "Forms": c.forms,
                     "Calls to action": "; ".join(c.ctas)}
                    for c in report.cta_gaps
                ],
                widths={"URL": 60, "Calls to action": 60},
            ),
            Table(
                "AI referrals",
                ["URL", "Sessions", "Sources"],
                [
                    {"URL": a.url, "Sessions": a.sessions,
                     "Sources": "; ".join(f"{k} {v}" for k, v in a.sources.items())}
                    for a in report.ai_referrals
                ],
                widths={"URL": 60},
            ),
            Table(
                "After edits",
                ["URL", "Change", "Sessions before", "Sessions after", "Key events before",
                 "Key events after"],
                [
                    {"URL": e.url, "Change": e.change, "Sessions before": e.sessions_before,
                     "Sessions after": e.sessions_after, "Key events before": e.key_events_before,
                     "Key events after": e.key_events_after}
                    for e in report.post_edit
                ],
                widths={"URL": 60},
            ),
        ]  # fmt: skip
    tables.append(
        Table(
            "How to read",
            ["Column", "Meaning"],
            [{"Column": k, "Meaning": v} for k, v in _HOW_TO_READ],
            widths={"Meaning": 110},
        )
    )
    return tables


def write_analytics_workbook(
    snapshot: Ga4Snapshot, report: AnalyticsReport | None, path: Path
) -> Path:
    """The analytics workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, analytics_tables(snapshot, report))
    return path
