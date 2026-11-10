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
    MAX_SITE_TAGS,
    MAX_TAG_LENGTH,
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
    address_of,
    address_score,
    addresses_overlap,
    merge_urls,
    url_domain,
)

__all__ = [
    "PROJECT_DATA_MODULES",
    "ProjectNotFoundError",
    "ProjectStore",
    "RoutedImport",
    "SiteSpec",
    "clean_tags",
    "long_path",
    "owner_of",
    "slug",
]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")

PROJECT_DATA_MODULES = ("theming", "monitoring", "gaps", "research")
"""Modules that keep `<root>/<module>/<project id>/` (see each module's `store.py`)."""


class ProjectNotFoundError(LookupError):
    """No project (or website, or run) with that id."""


@dataclass(frozen=True)
class SiteSpec:
    """A website as a person enters it: a name, usually its address, a note and tags."""

    name: str
    website: str | None = None
    note: str = ""
    tags: tuple[str, ...] = ()


def clean_tags(tags: list[str] | tuple[str, ...]) -> list[str]:
    """Tags trimmed, without empty ones or repeats (ignoring case); at most 12, of 40 characters."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        text = " ".join(str(tag).split())
        if not text or text.lower() in seen:
            continue
        if len(text) > MAX_TAG_LENGTH:
            msg = f"The tag '{text[:20]}...' is longer than {MAX_TAG_LENGTH} characters."
            raise ValueError(msg)
        seen.add(text.lower())
        out.append(text)
    if len(out) > MAX_SITE_TAGS:
        raise ValueError(f"A website can have at most {MAX_SITE_TAGS} tags.")
    return out


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


_REPLACE_TRIES = 25


def _write_json(path: Path, data: object) -> None:
    """Write through a temporary file and swap it in.

    On Windows the swap is refused for a moment while another process has the target
    open (a scan by the antivirus, a reader in a worker process): it is retried for up
    to about two seconds instead of failing the whole run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.02 * (attempt + 1))


def owner_of(sites: list[SiteRecord], url: str) -> SiteRecord | None:
    """The website whose address covers the URL most specifically, or None.

    When one domain holds several websites (`client.com` and `brand.client.com`, or
    `client.com/brand-a`), the most specific address wins.
    """
    best: tuple[tuple[int, int], SiteRecord] | None = None
    for site in sites:
        for address in site.domains:
            score = address_score(url, address)
            if score is not None and (best is None or score > best[0]):
                best = (score, site)
    return best[1] if best else None


def _address(website: str, existing: list[SiteRecord]) -> str:
    """A new website's address: its domain, or a section of it when the domain is taken.

    A path ('client.com/brand-a') is kept only when another website of the project
    already has that domain: that is when one domain holds several websites.
    """
    with_path = address_of(website, keep_path=True)
    plain = address_of(website, keep_path=False)
    if with_path != plain and any(
        addresses_overlap(plain, a) for other in existing for a in other.domains
    ):
        return with_path
    return plain


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
        client_brands: list[SiteSpec] | None = None,
        confidential: bool = False,
    ) -> ProjectRecord:
        """A new project with the client's website(s) and any competitor websites.

        `client_site` is the client's main website; `client_brands` are its other
        websites or brands. The password is hashed before anything is written.
        Website names must be different, and so must their addresses. A
        `confidential` project's page text never goes to Claude or Claude Desktop.
        """
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.")
        now = _now()
        specs: list[tuple[SiteSpec, SiteRole]] = [
            (client_site or SiteSpec(name=client_name), "client"),
            *((b, "client") for b in client_brands or []),
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
                confidential=confidential,
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
        typed = spec.website.strip() if spec.website else ""
        domains = [_address(typed, existing)] if typed else []
        for other in existing:
            clash = set(domains) & set(other.domains)
            if clash:
                address = clash.pop()
                raise ValueError(
                    f"{address} is already the website of '{other.name}'. If that domain holds "
                    "several websites, give this one its own subdomain or section "
                    f"(for example brand.{address} or {address}/brand)."
                )
        return SiteRecord(
            site_id=site_id,
            name=name,
            role=role,
            domains=domains,
            created_at=now,
            note=spec.note.strip(),
            tags=clean_tags(spec.tags),
        )

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
        data = json.loads(self._read(path))
        record = ProjectRecord.model_validate(data)
        if not record.sites:
            record = self._convert_single_list(record)
        if "confidential" not in data:
            # A project written before the confidential option existed: not confidential,
            # written once so the file says so explicitly.
            with self._lock:
                self._save(record)
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
        self,
        project_id: str,
        *,
        name: str | None = None,
        client_name: str | None = None,
        confidential: bool | None = None,
    ) -> ProjectRecord:
        """Rename the project or its client, or mark it confidential (or not)."""
        with self._lock:
            record = self.get(project_id)
            changes: dict[str, object] = {"updated_at": _now()}
            if name is not None:
                changes["name"] = name.strip()
            if client_name is not None:
                changes["client_name"] = client_name.strip()
            if confidential is not None:
                changes["confidential"] = confidential
            record = ProjectRecord.model_validate(record.model_dump() | changes)
            self._save(record)
            return record

    def delete(self, project_id: str) -> None:
        """Remove the project and everything in it, including the other modules' data.

        Themes, monitoring checks, gap reports, research pulls and the Claude Desktop
        ledger each keep a folder or file per project next to `projects/` under the
        same root; they go too. Evidence is shared across projects and stays.
        """
        with self._lock:
            folder = self.project_dir(project_id)
            if not folder.is_dir():
                raise ProjectNotFoundError(project_id)
            shutil.rmtree(folder)
            for module in PROJECT_DATA_MODULES:
                sibling = self.root / module / project_id
                if sibling.is_dir():
                    shutil.rmtree(sibling, ignore_errors=True)
            (self.root / "mcp" / "ledgers" / f"{project_id}.json").unlink(missing_ok=True)

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

    def add_site(
        self, project_id: str, spec: SiteSpec, role: SiteRole = "competitor"
    ) -> SiteRecord:
        """Add a competitor website, or (role `client`) another website or brand of the client."""
        with self._lock:
            record = self.get(project_id)
            site = self._new_site(spec, role, record.sites, _now())
            self._save(
                record.model_copy(update={"sites": [*record.sites, site], "updated_at": _now()})
            )
            _write_json(self.site_dir(project_id, site.site_id) / "urls.json", [])
            return site

    def remove_site(self, project_id: str, site_id: str) -> None:
        """Remove a website, its URLs, runs and reports (never the client's main website)."""
        with self._lock:
            record = self.get(project_id)
            self.site(project_id, site_id)
            main = record.main_site()
            if main is not None and main.site_id == site_id:
                raise ValueError("The client's main website cannot be removed from its project.")
            kept = [s for s in record.sites if s.site_id != site_id]
            self._save(record.model_copy(update={"sites": kept, "updated_at": _now()}))
            folder = self.site_dir(project_id, site_id)
            if folder.is_dir():
                shutil.rmtree(folder)

    def update_site(
        self,
        project_id: str,
        site_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        tags: list[str] | None = None,
        role: SiteRole | None = None,
    ) -> SiteRecord:
        """Rename a website, change its note or tags, or make it a client or competitor website.

        The client's main website stays the client's. Its id (and so its folders and
        knowledge space) never changes.
        """
        with self._lock:
            record = self.get(project_id)
            current = self.site(project_id, site_id)
            changes: dict[str, object] = {}
            if name is not None:
                text = name.strip()
                if not text:
                    raise ValueError("Every website needs a name.")
                if any(
                    s.name.lower() == text.lower() for s in record.sites if s.site_id != site_id
                ):
                    raise ValueError(f"Another website is already called '{text}'.")
                changes["name"] = text
            if note is not None:
                changes["note"] = note.strip()
            if tags is not None:
                changes["tags"] = clean_tags(tags)
            if role is not None and role != current.role:
                main = record.main_site()
                if main is not None and main.site_id == site_id:
                    raise ValueError("The client's main website is always the client's.")
                changes["role"] = role
            updated = SiteRecord.model_validate(current.model_dump() | changes)
            sites = [updated if s.site_id == site_id else s for s in record.sites]
            self._save(record.model_copy(update={"sites": sites, "updated_at": _now()}))
            return updated

    def _set_domains(self, project_id: str, site_id: str, domains: list[str]) -> None:
        record = self.get(project_id)
        sites = [
            s.model_copy(update={"domains": domains}) if s.site_id == site_id else s
            for s in record.sites
        ]
        self._save(record.model_copy(update={"sites": sites}))

    def owner_of(self, project_id: str, url: str) -> SiteRecord | None:
        """The project's website a URL belongs to (most specific address), or None."""
        return owner_of(self.sites(project_id), url)

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

        A URL of another website is refused, naming the website it belongs to (when
        one domain holds several websites, the most specific address decides). A
        website without a known address takes the domains of its first URLs.
        """
        with self._lock:
            site = self.site(project_id, site_id)
            sites = self.sites(project_id)
            current = self.urls(project_id, site_id)
            learning = not site.domains
            mine: list[str] = []
            refused: list[tuple[str, str]] = []
            for raw in candidates:
                domain = url_domain(raw.strip())
                owner = owner_of(sites, raw.strip()) if domain is not None else None
                if (
                    domain is None
                    or (owner is not None and owner.site_id == site_id)
                    or (owner is None and learning)
                ):
                    mine.append(raw)  # merge_urls reports unusable lines with their reason
                    continue
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
        sites = self.sites(project_id)
        buckets: dict[str, list[str]] = {}
        for raw in candidates:
            domain = url_domain(raw.strip())
            if domain is None:
                routed.rejected.append((raw.strip()[:300], "not a usable web address"))
                continue
            owner = owner_of(sites, raw.strip())
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
