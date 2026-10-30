"""File-backed project store: `<root>/projects/<project id>/`.

    project.json                    the project and its websites (password hash only)
    sites/<site id>/urls.json       one website's URL inventory, in the order added
    sites/<site id>/runs/<run id>/  one folder per scraping run of that website:
                                    state.json, batch work files,
                                    <run id>.records.jsonl and reports/

Each website is stored on its own (docs/app/ARCHITECTURE_PROJECTS_AND_SITES.md).
A project written before websites existed (urls.json and runs/ at the project
level) is converted the first time it is read: its list and runs move under one
client website. Nothing is deleted.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from src.modules.content_processor.projects.models import (
    ProjectRecord,
    ProjectUrl,
    RunState,
    SiteRecord,
    SiteRole,
)
from src.modules.content_processor.projects.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    verify_password,
)
from src.modules.content_processor.projects.urls import (
    ImportResult,
    domain_of,
    merge_urls,
    url_domain,
)

__all__ = [
    "ProjectNotFoundError",
    "ProjectStore",
    "RoutedImport",
    "SiteSpec",
    "long_path",
    "slug",
]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


class ProjectNotFoundError(LookupError):
    """No project (or website, or run) with that id."""


@dataclass(frozen=True)
class SiteSpec:
    """A website as a person enters it: a name and, usually, its address."""

    name: str
    website: str | None = None


@dataclass
class RoutedImport:
    """A project-level paste or import, sorted to the websites by domain."""

    per_site: dict[str, ImportResult] = field(default_factory=dict)
    unknown_domains: dict[str, int] = field(default_factory=dict)
    rejected: list[tuple[str, str]] = field(default_factory=list)


def slug(text: str, *, limit: int = 40) -> str:
    """Lowercase ASCII words joined by dashes ('Flynn's Print Q4' -> 'flynn-s-print-q4')."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(words) > limit:
        cut = words[:limit]
        # break at a word boundary when one is in reach ("print-security", not "print-securit")
        words = cut.rsplit("-", 1)[0] if "-" in cut[limit // 2 :] else cut
    return words.rstrip("-") or "project"


_EXTENDED = "\\\\?\\"  # the four characters \\?\


def long_path(path: Path) -> Path:
    r"""On Windows, the extended form (`\\?\C:\...`) that lifts the 260-character limit.

    A run's files sit several folders deep (project, website, run, batch outputs,
    reports), so under a long user folder the classic limit is reached; elsewhere
    the path is returned unchanged.
    """
    if os.name != "nt":
        return path
    text = str(path.resolve())
    return path if text.startswith(_EXTENDED) else Path(_EXTENDED + text)


def _now() -> datetime:
    return datetime.now(UTC)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _covers(domains: list[str], domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


class ProjectStore:
    """Projects, their websites, URL lists and runs under one root folder."""

    def __init__(self, root: Path) -> None:
        """Bind to `<root>/projects` (created on first write)."""
        self.root = long_path(Path(root))
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

    def site_dir(self, project_id: str, site_id: str) -> Path:
        """One website's folder inside its project."""
        if not _ID.match(site_id):
            raise ProjectNotFoundError(site_id)
        return self.project_dir(project_id) / "sites" / site_id

    def run_dir(self, project_id: str, site_id: str, run_id: str) -> Path:
        """A run's folder inside its website."""
        if not _ID.match(run_id.replace("_", "-")):
            raise ProjectNotFoundError(run_id)
        return self.site_dir(project_id, site_id) / "runs" / run_id

    # -- projects -------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        owner_name: str,
        client_name: str,
        password: str,
        client_site: SiteSpec | None = None,
        competitors: list[SiteSpec] | None = None,
    ) -> ProjectRecord:
        """A new project with its client website and any competitor websites.

        The password is hashed before anything is written. Website names must be
        different, and so must their domains.
        """
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.")
        now = _now()
        specs: list[tuple[SiteSpec, SiteRole]] = [
            (client_site or SiteSpec(name=client_name), "client"),
            *((c, "competitor") for c in competitors or []),
        ]
        sites: list[SiteRecord] = []
        for spec, role in specs:
            sites.append(self._new_site(spec, role, sites, now))
        with self._lock:
            # Short ids keep every file of a run well inside Windows' 260-character path limit.
            project_id = f"{slug(name, limit=24)}-{secrets.token_hex(3)}"
            record = ProjectRecord(
                project_id=project_id,
                name=name.strip(),
                owner_name=owner_name.strip(),
                client_name=client_name.strip(),
                password_hash=hash_password(password),
                created_at=now,
                updated_at=now,
                sites=sites,
            )
            self.project_dir(project_id).mkdir(parents=True, exist_ok=False)
            self._save(record)
            for site in sites:
                _write_json(self.site_dir(project_id, site.site_id) / "urls.json", [])
            return record

    @staticmethod
    def _new_site(
        spec: SiteSpec, role: SiteRole, existing: list[SiteRecord], now: datetime
    ) -> SiteRecord:
        name = spec.name.strip()
        if not name:
            raise ValueError("Every website needs a name.")
        site_id = slug(name, limit=20)
        if any(s.site_id == site_id for s in existing):
            raise ValueError(f"Two websites are called '{name}'. Give each a different name.")
        domains = [domain_of(spec.website)] if spec.website and spec.website.strip() else []
        for other in existing:
            clash = set(domains) & set(other.domains)
            if clash:
                raise ValueError(f"{clash.pop()} is already the website of '{other.name}'.")
        return SiteRecord(site_id=site_id, name=name, role=role, domains=domains, created_at=now)

    def _save(self, record: ProjectRecord) -> None:
        _write_json(
            self.project_dir(record.project_id) / "project.json", record.model_dump(mode="json")
        )

    def projects(self) -> list[ProjectRecord]:
        """Every readable project, newest first."""
        out: list[ProjectRecord] = []
        if not self._projects.is_dir():
            return out
        for folder in self._projects.iterdir():
            if not (folder / "project.json").is_file():
                continue
            try:
                out.append(self.get(folder.name))
            except (ValidationError, ProjectNotFoundError):
                continue
        return sorted(out, key=lambda p: p.created_at, reverse=True)

    def get(self, project_id: str) -> ProjectRecord:
        """One project, or `ProjectNotFoundError` (an older project is converted first)."""
        path = self.project_dir(project_id) / "project.json"
        if not path.is_file():
            raise ProjectNotFoundError(project_id)
        record = ProjectRecord.model_validate_json(self._read(path))
        if not record.sites:
            record = self._convert_single_list(record)
        return record

    def _convert_single_list(self, record: ProjectRecord) -> ProjectRecord:
        """Move an older project's URL list and runs under one client website."""
        with self._lock:
            folder = self.project_dir(record.project_id)
            legacy = folder / "urls.json"
            urls = (
                [ProjectUrl.model_validate(u) for u in json.loads(self._read(legacy))]
                if legacy.is_file()
                else []
            )
            domains = sorted({d for u in urls if (d := url_domain(u.url))})
            site = SiteRecord(
                site_id=slug(record.client_name, limit=20),
                name=record.client_name,
                role="client",
                domains=domains,
                created_at=record.created_at,
            )
            target = self.site_dir(record.project_id, site.site_id)
            target.mkdir(parents=True, exist_ok=True)
            _write_json(target / "urls.json", [u.model_dump(mode="json") for u in urls])
            if (folder / "runs").is_dir():
                shutil.move(str(folder / "runs"), str(target / "runs"))
                for state_path in (target / "runs").glob("*/state.json"):
                    data = json.loads(self._read(state_path))
                    data["site_id"] = site.site_id
                    _write_json(state_path, data)
            record = record.model_copy(update={"sites": [site]})
            self._save(record)
            if legacy.is_file():
                legacy.rename(folder / "urls.json.converted")
            return record

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
            self._save(record)
            return record

    def delete(self, project_id: str) -> None:
        """Remove the project and everything in it."""
        with self._lock:
            folder = self.project_dir(project_id)
            if not folder.is_dir():
                raise ProjectNotFoundError(project_id)
            shutil.rmtree(folder)

    # -- websites -------------------------------------------------------------

    def sites(self, project_id: str) -> list[SiteRecord]:
        """The project's websites: the client's first, then competitors in the order added."""
        return self.get(project_id).sites

    def site(self, project_id: str, site_id: str) -> SiteRecord:
        """One website, or `ProjectNotFoundError`."""
        for site in self.sites(project_id):
            if site.site_id == site_id:
                return site
        raise ProjectNotFoundError(site_id)

    def add_site(self, project_id: str, spec: SiteSpec) -> SiteRecord:
        """Add a competitor website to the project."""
        with self._lock:
            record = self.get(project_id)
            site = self._new_site(spec, "competitor", record.sites, _now())
            self._save(
                record.model_copy(update={"sites": [*record.sites, site], "updated_at": _now()})
            )
            _write_json(self.site_dir(project_id, site.site_id) / "urls.json", [])
            return site

    def remove_site(self, project_id: str, site_id: str) -> None:
        """Remove a competitor website, its URLs, runs and reports (never the client's)."""
        with self._lock:
            record = self.get(project_id)
            site = self.site(project_id, site_id)
            if site.role == "client":
                raise ValueError("The client's website cannot be removed from its project.")
            kept = [s for s in record.sites if s.site_id != site_id]
            self._save(record.model_copy(update={"sites": kept, "updated_at": _now()}))
            folder = self.site_dir(project_id, site_id)
            if folder.is_dir():
                shutil.rmtree(folder)

    def _set_domains(self, project_id: str, site_id: str, domains: list[str]) -> None:
        record = self.get(project_id)
        sites = [
            s.model_copy(update={"domains": domains}) if s.site_id == site_id else s
            for s in record.sites
        ]
        self._save(record.model_copy(update={"sites": sites}))

    def _owner_of(self, project_id: str, domain: str) -> SiteRecord | None:
        for site in self.sites(project_id):
            if _covers(site.domains, domain):
                return site
        return None

    # -- URLs -----------------------------------------------------------------

    def urls(self, project_id: str, site_id: str) -> list[ProjectUrl]:
        """One website's URL inventory, in the order added."""
        self.site(project_id, site_id)
        path = self.site_dir(project_id, site_id) / "urls.json"
        if not path.is_file():
            return []
        return [ProjectUrl.model_validate(u) for u in json.loads(self._read(path))]

    def url_count(self, project_id: str, site_id: str | None = None) -> int:
        """How many URLs one website (or every website of the project) has."""
        ids = [site_id] if site_id else [s.site_id for s in self.sites(project_id)]
        return sum(len(self.urls(project_id, i)) for i in ids)

    def _save_urls(self, project_id: str, site_id: str, urls: list[ProjectUrl]) -> None:
        _write_json(
            self.site_dir(project_id, site_id) / "urls.json",
            [u.model_dump(mode="json") for u in urls],
        )
        record = self.get(project_id)
        self._save(record.model_copy(update={"updated_at": _now()}))

    def add_urls(self, project_id: str, site_id: str, candidates: list[str]) -> ImportResult:
        """Merge candidates into one website's list (checked, de-duplicated).

        A URL of another domain is refused, naming the website it belongs to. A
        website without a known domain takes the domains of its first URLs.
        """
        with self._lock:
            site = self.site(project_id, site_id)
            current = self.urls(project_id, site_id)
            learning = not site.domains
            mine: list[str] = []
            refused: list[tuple[str, str]] = []
            for raw in candidates:
                domain = url_domain(raw.strip())
                if domain is None or learning or _covers(site.domains, domain):
                    mine.append(raw)  # merge_urls reports unusable lines with their reason
                    continue
                owner = self._owner_of(project_id, domain)
                reason = f"belongs to {owner.name}" if owner else f"not this website ({domain})"
                refused.append((raw.strip()[:300], reason))
            result = merge_urls((u.url for u in current), mine)
            result.rejected.extend(refused)
            if learning and result.added:
                learned = sorted({d for u in result.added if (d := url_domain(u))})
                self._set_domains(project_id, site_id, learned)
            if result.added:
                now = _now()
                current.extend(ProjectUrl(url=u, added_at=now) for u in result.added)
                self._save_urls(project_id, site_id, current)
            return result

    def add_urls_routed(self, project_id: str, candidates: list[str]) -> RoutedImport:
        """Sort a mixed list to the project's websites by domain and add each part."""
        routed = RoutedImport()
        buckets: dict[str, list[str]] = {}
        for raw in candidates:
            domain = url_domain(raw.strip())
            if domain is None:
                routed.rejected.append((raw.strip()[:300], "not a usable web address"))
                continue
            owner = self._owner_of(project_id, domain)
            if owner is None:
                routed.unknown_domains[domain] = routed.unknown_domains.get(domain, 0) + 1
                continue
            buckets.setdefault(owner.site_id, []).append(raw)
        for site_id, urls in buckets.items():
            routed.per_site[site_id] = self.add_urls(project_id, site_id, urls)
        return routed

    def remove_urls(self, project_id: str, site_id: str, urls: list[str] | None) -> int:
        """Remove the given URLs (all of them when `urls` is None); return how many."""
        with self._lock:
            current = self.urls(project_id, site_id)
            if urls is None:
                kept: list[ProjectUrl] = []
            else:
                drop = set(urls)
                kept = [u for u in current if u.url not in drop]
            removed = len(current) - len(kept)
            if removed:
                self._save_urls(project_id, site_id, kept)
            return removed

    # -- runs -----------------------------------------------------------------

    def save_run(self, state: RunState) -> None:
        """Write a run's state."""
        with self._lock:
            _write_json(
                self.run_dir(state.project_id, state.site_id, state.run_id) / "state.json",
                state.model_dump(mode="json"),
            )

    def run(self, project_id: str, site_id: str, run_id: str) -> RunState:
        """One run's state, or `ProjectNotFoundError`."""
        path = self.run_dir(project_id, site_id, run_id) / "state.json"
        if not path.is_file():
            raise ProjectNotFoundError(run_id)
        return RunState.model_validate_json(self._read(path))

    def runs(self, project_id: str, site_id: str | None = None) -> list[RunState]:
        """Runs of one website, or of every website of the project, newest first."""
        ids = [site_id] if site_id else [s.site_id for s in self.sites(project_id)]
        out: list[RunState] = []
        for sid in ids:
            folder = self.site_dir(project_id, sid) / "runs"
            if not folder.is_dir():
                continue
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

    def report_files(self, project_id: str, site_id: str, run_id: str) -> dict[str, Path]:
        """The run's report files by name (only files that exist)."""
        folder = self.run_dir(project_id, site_id, run_id) / "reports"
        if not folder.is_dir():
            return {}
        return {p.name: p for p in sorted(folder.iterdir()) if p.is_file()}
