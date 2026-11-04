"""HTTP routes of the downloads.

GET /api/projects/{project}/exports                  every report: available or what to do first
GET /api/projects/{project}/exports/{key}.{format}   one report as xlsx, csv or docx
GET /api/projects/{project}/exports/all.zip          every available report in every format

Reports are built on request from the modules' latest saved data, into a temporary
folder removed once the file is sent. Reading needs no unlock, like the tabs.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.reports.catalog import REPORTS, build_report
from src.modules.reports.writers import (
    FORMATS,
    Format,
    Report,
    file_slug,
    word_available,
    write_report,
)

__all__ = ["formats", "register_report_routes"]

_MEDIA = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "csv": "text/csv",
    "zip": "application/zip",
}
_LABELS = {"xlsx": "Excel", "csv": "CSV", "docx": "Word"}


def formats() -> list[Format]:
    """The formats this installation can write (Word needs python-docx)."""
    return [f for f in FORMATS if f != "docx" or word_available()]


def _name(project_name: str, key: str) -> str:
    return f"{file_slug(project_name)}_{key}_{datetime.now(UTC):%Y-%m-%d}"


def _readme(project_name: str, built: list[tuple[Report, list[str]]], missing: list[str]) -> str:
    lines = [
        f"{project_name}: every report, downloaded {datetime.now(UTC):%d %b %Y %H:%M} UTC",
        "",
        "Excel (.xlsx) has every row and the full text. CSV has one file per table (a zip when",
        "a report has several). Word (.docx) is for reading and sharing; long tables show",
        "their first rows there.",
        "",
    ]
    for report, files in built:
        lines += [f"{report.title}: {report.subtitle}", *[f"  {f}" for f in files], ""]
    if missing:
        lines += ["Not included yet:", *[f"  {m}" for m in missing]]
    return "\n".join(lines) + "\n"


def register_report_routes(
    app: FastAPI,
    *,
    projects: ProjectStore,
    analyst_runs: Callable[[str], list[object]] | None = None,
) -> None:
    """Add the download routes (before the app's catch-all routes)."""

    def project_name(project_id: str) -> str:
        try:
            return projects.get(project_id).name
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="There is no such project.") from None

    def build(key: str, project_id: str) -> Report | None:
        return build_report(key, projects, project_id, analyst_runs)

    @app.get("/api/projects/{project_id}/exports")
    def catalog(project_id: str) -> dict[str, Any]:
        project_name(project_id)
        offered = formats()
        reports = []
        for d in REPORTS:
            if d.key == "analyst" and analyst_runs is None:
                continue
            report = build(d.key, project_id)
            reports.append(
                {
                    "key": d.key,
                    "module": d.module,
                    "title": d.title,
                    "description": d.description,
                    "available": report is not None,
                    "missing": "" if report is not None else d.missing,
                    "subtitle": report.subtitle if report else "",
                    "rows": sum(len(t.rows) for t in report.tables) if report else 0,
                    "formats": offered,
                }
            )
        return {"formats": [{"key": f, "label": _LABELS[f]} for f in offered], "reports": reports}

    @app.get("/api/projects/{project_id}/exports/{file_name}")
    def download(project_id: str, file_name: str) -> FileResponse:
        name = project_name(project_id)
        key, _, fmt = file_name.rpartition(".")
        folder = Path(tempfile.mkdtemp(prefix="cp-export-"))
        cleanup = BackgroundTask(shutil.rmtree, folder, ignore_errors=True)
        try:
            if key == "all" and fmt == "zip":
                path = _everything(project_id, name, folder)
            else:
                if fmt not in formats() or key not in {d.key for d in REPORTS}:
                    raise HTTPException(status_code=404, detail="There is no such download.")
                report = build(key, project_id)
                if report is None:
                    missing = next(d.missing for d in REPORTS if d.key == key)
                    raise HTTPException(
                        status_code=404, detail=f"Nothing to download yet. {missing}"
                    )
                path = write_report(report, fmt, folder, _name(name, key))
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return FileResponse(
            path,
            filename=path.name,
            media_type=_MEDIA.get(path.suffix.lstrip("."), "application/octet-stream"),
            content_disposition_type="attachment",
            background=cleanup,
        )

    def _everything(project_id: str, name: str, folder: Path) -> Path:
        built: list[tuple[Report, list[str]]] = []
        missing: list[str] = []
        files = folder / "files"
        for d in REPORTS:
            if d.key == "analyst" and analyst_runs is None:
                continue
            report = build(d.key, project_id)
            if report is None:
                missing.append(f"{d.title}: {d.missing}")
                continue
            written = [write_report(report, f, files, _name(name, d.key)).name for f in formats()]
            built.append((report, written))
        archive = folder / f"{_name(name, 'all_reports')}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("README.txt", _readme(name, built, missing))
            for _report, written in built:
                for file in written:
                    zipped.write(files / file, file)
        return archive
