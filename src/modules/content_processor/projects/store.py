"""File-backed project store: `<root>/projects/<project id>/`.

    project.json        the project (password hash only)
    urls.json           the URL inventory, in the order added
    runs/<run id>/      one folder per scraping run: state.json, batch work files,
                        <run id>.records.jsonl and reports/

Writes are atomic (temporary file, then replace) and serialised by one lock, so
the API and the background runner never see a half-written file.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from src.modules.content_processor.projects.models import ProjectRecord, ProjectUrl, RunState
from src.modules.content_processor.projects.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    verify_password,
)
from src.modules.content_processor.projects.urls import ImportResult, merge_urls

__all__ = ["ProjectNotFoundError", "ProjectStore", "slug"]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


class ProjectNotFoundError(LookupError):
    """No project with that id."""


def slug(text: str, *, limit: int = 40) -> str:
    """Lowercase ASCII words joined by dashes ('Flynn's Print Q4' -> 'flynn-s-print-q4')."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return (words[:limit].rstrip("-")) or "project"


def _now() -> datetime:
    return datetime.now(UTC)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


class ProjectStore:
    """Projects, their URLs and their runs under one root folder."""

    def __init__(self, root: Path) -> None:
        """Bind to `<root>/projects` (created on first write)."""
        self.root = Path(root)
        self._projects = self.root / "projects"
        self._lock = threading.RLock()

    def _read(self, path: Path) -> str:
        """Read a file under the store's lock, the one its writes take.

        On Windows a file being replaced cannot be opened for a moment, so a read
        that meets that retries briefly instead of failing the request.
        """
        for attempt in range(5):
            try:
                with self._lock:
                    return path.read_text(encoding="utf-8")
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    # -- paths ----------------------------------------------------------------

    def project_dir(self, project_id: str) -> Path:
        """The project's folder (the id is checked, so it cannot escape the root)."""
        if not _ID.match(project_id):
            raise ProjectNotFoundError(project_id)
        return self._projects / project_id

    def run_dir(self, project_id: str, run_id: str) -> Path:
        """A run's folder inside its project."""
        if not _ID.match(run_id.replace("_", "-")):
            raise ProjectNotFoundError(run_id)
        return self.project_dir(project_id) / "runs" / run_id

    # -- projects -------------------------------------------------------------

    def create(
        self, *, name: str, owner_name: str, client_name: str, password: str
    ) -> ProjectRecord:
        """A new project; the password is hashed before anything is written."""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.")
        with self._lock:
            project_id = f"{slug(name)}-{secrets.token_hex(3)}"
            now = _now()
            record = ProjectRecord(
                project_id=project_id,
                name=name.strip(),
                owner_name=owner_name.strip(),
                client_name=client_name.strip(),
                password_hash=hash_password(password),
                created_at=now,
                updated_at=now,
            )
            folder = self.project_dir(project_id)
            folder.mkdir(parents=True, exist_ok=False)
            _write_json(folder / "project.json", record.model_dump(mode="json"))
            _write_json(folder / "urls.json", [])
            return record

    def projects(self) -> list[ProjectRecord]:
        """Every readable project, newest first."""
        out: list[ProjectRecord] = []
        if not self._projects.is_dir():
            return out
        for folder in self._projects.iterdir():
            path = folder / "project.json"
            if not path.is_file():
                continue
            try:
                out.append(ProjectRecord.model_validate_json(self._read(path)))
            except ValidationError:
                continue
        return sorted(out, key=lambda p: p.created_at, reverse=True)

    def get(self, project_id: str) -> ProjectRecord:
        """One project, or `ProjectNotFoundError`."""
        path = self.project_dir(project_id) / "project.json"
        if not path.is_file():
            raise ProjectNotFoundError(project_id)
        return ProjectRecord.model_validate_json(self._read(path))

    def check_password(self, project_id: str, password: str) -> bool:
        """True when the password is the project's."""
        return verify_password(password, self.get(project_id).password_hash)

    def update(
        self, project_id: str, *, name: str | None = None, client_name: str | None = None
    ) -> ProjectRecord:
        """Rename the project or its client."""
        with self._lock:
            record = self.get(project_id)
            changes: dict[str, object] = {"updated_at": _now()}
            if name is not None:
                changes["name"] = name.strip()
            if client_name is not None:
                changes["client_name"] = client_name.strip()
            record = ProjectRecord.model_validate(record.model_dump() | changes)
            _write_json(
                self.project_dir(project_id) / "project.json", record.model_dump(mode="json")
            )
            return record

    def delete(self, project_id: str) -> None:
        """Remove the project and everything in it."""
        with self._lock:
            folder = self.project_dir(project_id)
            if not folder.is_dir():
                raise ProjectNotFoundError(project_id)
            shutil.rmtree(folder)

    # -- URLs -----------------------------------------------------------------

    def urls(self, project_id: str) -> list[ProjectUrl]:
        """The URL inventory, in the order added."""
        path = self.project_dir(project_id) / "urls.json"
        if not path.is_file():
            raise ProjectNotFoundError(project_id)
        return [ProjectUrl.model_validate(u) for u in json.loads(self._read(path))]

    def _save_urls(self, project_id: str, urls: list[ProjectUrl]) -> None:
        _write_json(
            self.project_dir(project_id) / "urls.json", [u.model_dump(mode="json") for u in urls]
        )
        self._touch(project_id)

    def _touch(self, project_id: str) -> None:
        record = self.get(project_id)
        record = record.model_copy(update={"updated_at": _now()})
        _write_json(self.project_dir(project_id) / "project.json", record.model_dump(mode="json"))

    def add_urls(self, project_id: str, candidates: list[str]) -> ImportResult:
        """Merge candidates into the inventory (checked, de-duplicated)."""
        with self._lock:
            current = self.urls(project_id)
            result = merge_urls((u.url for u in current), candidates)
            if result.added:
                now = _now()
                current.extend(ProjectUrl(url=u, added_at=now) for u in result.added)
                self._save_urls(project_id, current)
            return result

    def remove_urls(self, project_id: str, urls: list[str] | None) -> int:
        """Remove the given URLs (all of them when `urls` is None); return how many."""
        with self._lock:
            current = self.urls(project_id)
            if urls is None:
                kept: list[ProjectUrl] = []
            else:
                drop = set(urls)
                kept = [u for u in current if u.url not in drop]
            removed = len(current) - len(kept)
            if removed:
                self._save_urls(project_id, kept)
            return removed

    # -- runs -----------------------------------------------------------------

    def save_run(self, state: RunState) -> None:
        """Write a run's state."""
        with self._lock:
            _write_json(
                self.run_dir(state.project_id, state.run_id) / "state.json",
                state.model_dump(mode="json"),
            )

    def run(self, project_id: str, run_id: str) -> RunState:
        """One run's state, or `ProjectNotFoundError`."""
        path = self.run_dir(project_id, run_id) / "state.json"
        if not path.is_file():
            raise ProjectNotFoundError(run_id)
        return RunState.model_validate_json(self._read(path))

    def runs(self, project_id: str) -> list[RunState]:
        """The project's runs, newest first."""
        folder = self.project_dir(project_id) / "runs"
        out: list[RunState] = []
        if folder.is_dir():
            for run in folder.iterdir():
                path = run / "state.json"
                if path.is_file():
                    try:
                        out.append(RunState.model_validate_json(self._read(path)))
                    except ValidationError:
                        continue
        return sorted(out, key=lambda r: r.created_at, reverse=True)

    def all_runs(self) -> list[RunState]:
        """Every run of every project (for resuming the queue after a restart)."""
        return [r for p in self.projects() for r in self.runs(p.project_id)]

    def report_files(self, project_id: str, run_id: str) -> dict[str, Path]:
        """The run's report files by name (only files that exist)."""
        folder = self.run_dir(project_id, run_id) / "reports"
        if not folder.is_dir():
            return {}
        return {p.name: p for p in sorted(folder.iterdir()) if p.is_file()}
