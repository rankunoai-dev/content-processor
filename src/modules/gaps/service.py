"""Build and save a project's gap report from every module's latest data."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from src.modules.analyst.playground import Playground
from src.modules.content_processor.knowledge.catalog_models import Catalog
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.gaps.build import build_findings
from src.modules.gaps.models import GapReport
from src.modules.gaps.store import GapStore, write_gap_workbook
from src.modules.monitoring.service import monitor_store
from src.modules.research.competitors import client_domains_everywhere
from src.modules.research.service import competitor_book, research_store, theme_map_for

__all__ = ["build_project_gaps", "gap_store"]

_LONG_PREFIX = "\\\\?\\"  # Windows extended-length path prefix, not shown to people


def gap_store(projects: ProjectStore) -> GapStore:
    """The gap store next to the projects (`<app data>/gaps`)."""
    return GapStore(Path(str(projects.root)) / "gaps")


def _catalogs(play: Playground) -> dict[str, list[tuple[str, str | None]]]:
    out: dict[str, list[tuple[str, str | None]]] = {}
    for website in play.websites():
        data = play.site_data(website)
        path = data.file(".catalog.json") if data else None
        if path is None:
            continue
        catalog = Catalog.model_validate_json(path.read_text(encoding="utf-8"))
        out[website.key] = [(o.name, o.href) for o in catalog.offerings]
    return out


def build_project_gaps(
    projects: ProjectStore, access: KnowledgeAccess, project_id: str
) -> tuple[GapReport, dict[str, str]]:
    """Build the report (theme map built when missing or stale), apply statuses, save it."""
    play = Playground(projects, access, [project_id])
    roles = {w.key: play.role_in_run(w) for w in play.websites()}
    theme_map, _rebuilt = theme_map_for(projects, access, project_id)
    research = research_store(projects).latest(project_id)
    check = monitor_store(projects).latest(project_id)
    missing = []
    if research is None:
        missing.append("research: no keyword data yet, so keyword and demand findings are left out")
    if check is None:
        missing.append("monitoring: no check yet, so competitor momentum is left out")
    findings = build_findings(
        theme_map,
        roles,
        research=research,
        check=check,
        catalogs=_catalogs(play),
        suggested=competitor_book(projects, project_id).suggestions,
        settings=gap_store(projects).settings(project_id),
        client_domains=client_domains_everywhere(projects.projects()),
    )
    store = gap_store(projects)
    statuses = store.statuses(project_id)
    findings = [
        f.model_copy(update={"status": s.status, "note": s.note})
        if (s := statuses.get(f.finding_id))
        else f
        for f in findings
    ]
    previous = store.latest(project_id)
    now_ids = {f.finding_id for f in findings}
    resolved = [
        f.title
        for f in (previous.findings if previous else [])
        if f.finding_id not in now_ids and f.status in ("open", "accepted")
    ]
    created = datetime.now(UTC)
    report = GapReport(
        report_id="gaps-"
        + created.strftime("%Y%m%d-%H%M%S-")
        + f"{created.microsecond // 1000:03d}",
        project_id=project_id,
        created_at=created,
        theme_map_id=theme_map.map_id,
        research_snapshot_id=research.snapshot_id if research else None,
        monitor_check_id=check.check_id if check else None,
        missing_inputs=missing,
        settings=store.settings(project_id),
        findings=findings,
        resolved=resolved,
        stats={
            "findings": len(findings),
            "high": sum(1 for f in findings if f.severity == "high"),
            **{f"kind_{k}": v for k, v in Counter(f.kind for f in findings).items()},
        },
    )
    path = store.save(report)
    workbook = write_gap_workbook(report, store.workbook(report))
    files = {"report": path, "workbook": workbook}
    return report, {k: str(v).removeprefix(_LONG_PREFIX) for k, v in files.items()}
