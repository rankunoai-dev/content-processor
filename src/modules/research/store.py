"""Research snapshots on disk and the research workbook.

`<app data>/research/<project>/`: every snapshot as `<snapshot id>.json`, the newest
as `latest.json`, each snapshot's workbook; `<app data>/research/units.jsonl` logs
every Semrush call.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.research.build import estimated_visits
from src.modules.research.models import Mover, ResearchSnapshot

__all__ = ["ResearchStore", "research_tables", "write_research_workbook"]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_SNAPSHOT_ID = re.compile(r"^research-\d{8}-\d{6}-\d{3}$")


class ResearchStore:
    """Snapshots of every project under `<root>`."""

    def __init__(self, root: Path) -> None:
        """Bind to the folder (usually `<app data>/research`)."""
        self.root = root
        self._lock = threading.Lock()

    def folder(self, project_id: str) -> Path:
        """The project's folder (the id is checked, so it cannot escape the root)."""
        if not _ID.match(project_id):
            raise KeyError(project_id)
        return self.root / project_id

    def save(self, snapshot: ResearchSnapshot) -> Path:
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

    def latest(self, project_id: str) -> ResearchSnapshot | None:
        """The newest snapshot, or None."""
        path = self.folder(project_id) / "latest.json"
        if not path.exists():
            return None
        with self._lock:
            return ResearchSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def get(self, project_id: str, snapshot_id: str) -> ResearchSnapshot:
        """One snapshot; raises `KeyError`."""
        if not _SNAPSHOT_ID.match(snapshot_id):
            raise KeyError(snapshot_id)
        path = self.folder(project_id) / f"{snapshot_id}.json"
        if not path.exists():
            raise KeyError(snapshot_id)
        return ResearchSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def history(self, project_id: str) -> list[str]:
        """Every snapshot's id, newest first."""
        folder = self.folder(project_id)
        if not folder.exists():
            return []
        return sorted((p.stem for p in folder.glob("research-*.json")), reverse=True)

    def previous(self, project_id: str) -> ResearchSnapshot | None:
        """The snapshot before the latest one, or None."""
        history = self.history(project_id)
        return self.get(project_id, history[1]) if len(history) > 1 else None

    def workbook(self, snapshot: ResearchSnapshot) -> Path:
        """Where a snapshot's workbook is written."""
        return self.folder(snapshot.project_id) / f"{snapshot.snapshot_id}_research.xlsx"


_HOW_TO_READ: list[tuple[str, str]] = [
    (
        "Source",
        "semrush_api: pulled with the Semrush API (units logged); csv_import: a Semrush "
        "Organic Positions export imported by hand.",
    ),
    ("Volume", "Average monthly searches of the keyword in the chosen country database."),
    (
        "Estimated visits",
        "Volume times a typical click-through rate for the position (28% for #1 down to 2% "
        "for #10, 1% on page two). An estimate, for comparing websites, not a measurement.",
    ),
    (
        "Theme",
        "The theme of the project's theme map the keyword belongs to: by its ranking page "
        "when that page is in the map, otherwise by the theme sharing most of its words.",
    ),
    (
        "Movers",
        "Keywords that moved 3 places or more, entered or left the top 10, appeared or "
        "disappeared since the previous snapshot.",
    ),
]


def research_tables(snapshot: ResearchSnapshot, moves: list[Mover] | None = None) -> list[Table]:
    """The workbook's sheets."""
    sites: list[dict[str, Cell]] = [
        {
            "Website": w.name,
            "Domain": w.website,
            "Role": w.role,
            "Source": w.source,
            "Organic keywords": w.overview.organic_keywords if w.overview else None,
            "Organic traffic": w.overview.organic_traffic if w.overview else None,
            "Traffic value (USD)": w.overview.organic_cost if w.overview else None,
            "Keywords in this snapshot": len(w.keywords),
            "Top 10": sum(1 for r in w.keywords if r.position <= 10),
            "Note": w.note,
        }
        for w in snapshot.websites
    ]
    labels = {d.theme_id: d.label for d in snapshot.demand}
    keywords: list[dict[str, Cell]] = [
        {
            "Website": w.website,
            "Keyword": r.keyword,
            "Position": r.position,
            "Previous": r.previous_position,
            "Volume": r.volume,
            "CPC": r.cpc,
            "Estimated visits": estimated_visits(r.volume, r.position),
            "URL": r.url or "",
            "Theme": labels.get(r.theme_id, "") if r.theme_id else "",
        }
        for w in snapshot.websites
        for r in w.keywords
    ]
    site_keys = [w.website for w in snapshot.websites]
    demand: list[dict[str, Cell]] = [
        {
            "Theme": d.label,
            "Keywords": d.keywords,
            "Volume": d.volume,
            **{f"Top 10: {s}": d.top10.get(s, 0) for s in site_keys},
            **{f"Visits: {s}": d.traffic.get(s, 0.0) for s in site_keys},
        }
        for d in snapshot.demand
    ]
    movers_rows: list[dict[str, Cell]] = [
        {
            "Website": m.website,
            "Keyword": m.keyword,
            "Before": m.before,
            "After": m.after,
            "Volume": m.volume,
            "URL": m.url or "",
        }
        for m in moves or []
    ]
    serps: list[dict[str, Cell]] = [
        {
            "Keyword": k.keyword,
            "Volume": k.volume,
            "CPC": k.cpc,
            "Rank": s.rank,
            "Domain": s.domain,
            "URL": s.url,
        }
        for k in snapshot.keywords
        for s in k.serp
    ]
    return [
        Table("Websites", list(sites[0]) if sites else ["Website"], sites),
        Table(
            "Keywords",
            list(keywords[0]) if keywords else ["Keyword"],
            keywords,
            widths={"Keyword": 40, "URL": 60},
        ),
        Table(
            "Theme demand", list(demand[0]) if demand else ["Theme"], demand, widths={"Theme": 40}
        ),
        Table("Movers", ["Website", "Keyword", "Before", "After", "Volume", "URL"], movers_rows),
        Table("Search results", ["Keyword", "Volume", "CPC", "Rank", "Domain", "URL"], serps),
        Table(
            "How to read",
            ["Column", "Meaning"],
            [{"Column": k, "Meaning": v} for k, v in _HOW_TO_READ],
            widths={"Meaning": 110},
        ),
    ]


def write_research_workbook(
    snapshot: ResearchSnapshot, path: Path, moves: list[Mover] | None = None
) -> Path:
    """The snapshot's workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, research_tables(snapshot, moves))
    return path
