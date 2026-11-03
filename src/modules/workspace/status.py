"""Each project's status in every module, read from what the modules stored (no building).

Used by the home screen (`GET /api/modules/status`) and by Claude Desktop's
`list_projects`, so both always say the same thing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI

from src.modules.content_processor.projects.store import ProjectStore

__all__ = ["project_statuses", "register_workspace_routes"]


def _when(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def project_statuses(projects: ProjectStore) -> list[dict[str, Any]]:
    """Every project with its websites and, per module, what exists and when (newest first)."""
    from src.modules.gaps.service import gap_store
    from src.modules.monitoring.service import monitor_store
    from src.modules.research.service import research_store
    from src.modules.theming.service import theme_store

    themes, checks = theme_store(projects), monitor_store(projects)
    research, gaps = research_store(projects), gap_store(projects)
    out: list[dict[str, Any]] = []
    for record in projects.projects():
        pid = record.project_id
        finished = [r for r in projects.runs(pid) if r.status == "finished"]
        theme_map, check = themes.latest(pid), checks.latest(pid)
        snapshot, report = research.latest(pid), gaps.latest(pid)
        out.append(
            {
                "project_id": pid,
                "name": record.name,
                "client": record.client_name,
                "websites": [
                    {"name": s.name, "role": s.role, "addresses": s.domains} for s in record.sites
                ],
                "finished_scrapes": len(finished),
                "last_scrape": max((_when(r.finished_at) or "" for r in finished), default="")
                or None,
                "searchable_scrapes": sum(1 for r in finished if r.knowledge_status == "ready"),
                "themes_built": _when(theme_map.built_at) if theme_map else None,
                "themes": len(theme_map.themes) if theme_map else 0,
                "last_monitoring_check": _when(check.created_at) if check else None,
                "alerts": len(check.alerts) if check else 0,
                "high_alerts": sum(1 for a in check.alerts if a.severity == "high") if check else 0,
                "research_added": _when(snapshot.created_at) if snapshot else None,
                "keywords": sum(len(w.keywords) for w in snapshot.websites) if snapshot else 0,
                "gaps_built": _when(report.created_at) if report else None,
                "findings": len(report.findings) if report else 0,
                "open_findings": sum(1 for f in report.findings if f.status == "open")
                if report
                else 0,
                "high_findings": sum(1 for f in report.findings if f.severity == "high")
                if report
                else 0,
            }
        )
    return out


def register_workspace_routes(app: FastAPI, *, projects: ProjectStore) -> None:
    """Add `GET /api/modules/status` (before the app's catch-all routes)."""

    @app.get("/api/modules/status")
    def status() -> list[dict[str, Any]]:
        return project_statuses(projects)
