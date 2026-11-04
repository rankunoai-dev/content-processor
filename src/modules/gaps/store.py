"""Gap reports and finding statuses on disk, and the gap workbook.

`<app data>/gaps/<project>/`: every report as `<report id>.json`, the newest as
`latest.json`, each report's workbook, and `statuses.json`: what a person decided
about each finding (by finding id), applied to every later build.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.gaps.models import FindingStatus, GapReport, GapSettings, StatusEntry

__all__ = ["GapStore", "gap_tables", "write_gap_workbook"]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_FINDING = re.compile(r"^gap-[0-9a-f]{12}$")


class GapStore:
    """Gap reports and statuses of every project under `<root>`."""

    def __init__(self, root: Path) -> None:
        """Bind to the folder (usually `<app data>/gaps`)."""
        self.root = root
        self._lock = threading.Lock()

    def folder(self, project_id: str) -> Path:
        """The project's folder (the id is checked, so it cannot escape the root)."""
        if not _ID.match(project_id):
            raise KeyError(project_id)
        return self.root / project_id

    def save(self, report: GapReport) -> Path:
        """Write the report and make it the latest."""
        folder = self.folder(report.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        text = report.model_dump_json(indent=1)
        path = folder / f"{report.report_id}.json"
        with self._lock:
            path.write_text(text, encoding="utf-8")
            tmp = folder / "latest.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(folder / "latest.json")
        return path

    def latest(self, project_id: str) -> GapReport | None:
        """The newest report, or None."""
        path = self.folder(project_id) / "latest.json"
        if not path.exists():
            return None
        with self._lock:
            return GapReport.model_validate_json(path.read_text(encoding="utf-8"))

    def statuses(self, project_id: str) -> dict[str, StatusEntry]:
        """Every decision about a finding, by finding id."""
        path = self.folder(project_id) / "statuses.json"
        if not path.exists():
            return {}
        with self._lock:
            data = json.loads(path.read_text(encoding="utf-8"))
        return {k: StatusEntry.model_validate(v) for k, v in data.items()}

    def set_status(
        self, project_id: str, finding_id: str, status: FindingStatus, note: str = ""
    ) -> StatusEntry:
        """Record a decision about a finding (and update the latest report to match)."""
        if not _FINDING.match(finding_id):
            raise KeyError(finding_id)
        latest = self.latest(project_id)
        if latest is None or all(f.finding_id != finding_id for f in latest.findings):
            raise KeyError(finding_id)
        entry = StatusEntry(status=status, note=note, updated_at=datetime.now(UTC))
        statuses = {k: v.model_dump(mode="json") for k, v in self.statuses(project_id).items()}
        statuses[finding_id] = entry.model_dump(mode="json")
        folder = self.folder(project_id)
        with self._lock:
            tmp = folder / "statuses.json.tmp"
            tmp.write_text(json.dumps(statuses, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(folder / "statuses.json")
        updated = latest.model_copy(
            update={
                "findings": [
                    f.model_copy(update={"status": status, "note": note})
                    if f.finding_id == finding_id
                    else f
                    for f in latest.findings
                ]
            }
        )
        self.save(updated)
        return entry

    def settings(self, project_id: str) -> GapSettings:
        """How the project's keyword gaps are counted (defaults when never set)."""
        path = self.folder(project_id) / "settings.json"
        if not path.exists():
            return GapSettings()
        return GapSettings.model_validate_json(path.read_text(encoding="utf-8"))

    def set_settings(self, project_id: str, settings: GapSettings) -> GapSettings:
        """Save the settings (used from the next build)."""
        folder = self.folder(project_id)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = folder / "settings.json.tmp"
            tmp.write_text(settings.model_dump_json(indent=1), encoding="utf-8")
            tmp.replace(folder / "settings.json")
        return settings

    def workbook(self, report: GapReport) -> Path:
        """Where a report's workbook is written."""
        return self.folder(report.project_id) / f"{report.report_id}_gaps.xlsx"


_HOW_TO_READ: list[tuple[str, str]] = [
    (
        "Priority",
        "0-100: a base by kind (theme gaps and keyword gaps highest), plus how much the "
        "competitors cover and how many searches are behind it.",
    ),
    (
        "Kinds",
        "theme_gap: competitors cover a theme, the client does not. theme_behind: a "
        "competitor has at least 1.5x the client's pages. pillar_gap: client pages but no "
        "pillar page. funnel_gap: buying stages competitors cover in a theme and the client "
        "does not. keyword_gap: competitors in the top 10, the client not in the top 20. "
        "striking_distance: client keywords at positions 11-20. competitor_momentum: "
        "competitors added pages in a theme since the previous scrape, the client none. "
        "offering_gap: a competitor offering with no similarly named client offering.",
    ),
    ("Status", "open, accepted (will act), done, dismissed: kept from one build to the next."),
    (
        "Inputs",
        "The theme map (always), the latest research snapshot and monitoring check when "
        "they exist; a report says which inputs were missing.",
    ),
]


def gap_tables(report: GapReport) -> list[Table]:
    """The workbook's sheets."""
    findings: list[dict[str, Cell]] = [
        {
            "Priority": f.priority,
            "Severity": f.severity,
            "Kind": f.kind,
            "Finding": f.title,
            "Theme": f.theme or "",
            "Competitors": ", ".join(f.competitors),
            "Searches/month": f.demand or None,
            "Detail": f.detail,
            "Recommended action": f.action,
            "Status": f.status,
            "Note": f.note,
        }
        for f in report.findings
    ]
    evidence: list[dict[str, Cell]] = [
        {"Finding": f.title, "Evidence": e.label, "Website": e.website or "", "URL": e.url or ""}
        for f in report.findings
        for e in f.evidence
    ]
    wrap = frozenset({"Detail", "Recommended action"})
    return [
        Table(
            "Findings",
            list(findings[0]) if findings else ["Finding"],
            findings,
            wrap,
            {"Finding": 50, "Detail": 60, "Recommended action": 60},
        ),
        Table(
            "Evidence",
            ["Finding", "Evidence", "Website", "URL"],
            evidence,
            widths={"Finding": 50, "Evidence": 60, "URL": 60},
        ),
        Table(
            "Resolved",
            ["Finding no longer detected"],
            [{"Finding no longer detected": t} for t in report.resolved],
        ),
        Table(
            "How to read",
            ["Column", "Meaning"],
            [{"Column": k, "Meaning": v} for k, v in _HOW_TO_READ],
            widths={"Meaning": 110},
        ),
    ]


def write_gap_workbook(report: GapReport, path: Path) -> Path:
    """The report's workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, gap_tables(report))
    return path
