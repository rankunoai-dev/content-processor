"""The data a card run may read, and the ledger of everything it was shown.

A run is bound to one or more projects. Its **scope** is every website of those
projects plus every website they compete with in any project (the competitive
scope of `projects/websites.py`). Tools resolve websites and addresses only
inside that scope, so a card can never read a website the person did not bring
into the run.

Each website is read in its freshest copy: the project copy whose knowledge
space was built last, with that copy's latest finished run (stamps, chunks,
catalog, records). Everything a tool returns is entered in the run's
**evidence ledger** under a short reference (`chunk:<id>` for a passage,
`fact:<id>` for any other fact). The deliverable cites those references; code
then resolves them to URLs, block ids and evidence hashes, so Claude never has
to copy a hash and cannot invent one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from src.modules.content_processor.knowledge.federated import space_namespace
from src.modules.content_processor.knowledge.models import PageStamp
from src.modules.content_processor.knowledge.vector_store import VectorStore
from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.runner import DEFAULT_TENANT
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore
from src.modules.content_processor.projects.urls import domain_of
from src.modules.content_processor.projects.websites import SiteCopy, Website, WebsiteGraph
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

__all__ = ["EvidenceLedger", "LedgerEntry", "Playground", "ScopeError", "SiteData"]


class ScopeError(ValueError):
    """A website, address or project outside the run's scope (or unknown)."""


@dataclass
class LedgerEntry:
    """One thing a tool showed Claude, with where it comes from."""

    ref: str
    kind: str
    website: str
    url: str | None = None
    project: str | None = None
    title: str = ""
    text: str = ""
    block_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


class EvidenceLedger:
    """Every reference handed out in one run; the deliverable may cite only these."""

    def __init__(self) -> None:
        """An empty ledger."""
        self.entries: dict[str, LedgerEntry] = {}

    def chunk(
        self,
        chunk_id: str,
        *,
        website: str,
        url: str,
        project: str | None,
        headings: list[str],
        text: str,
        block_ids: list[str],
        evidence_ids: list[str],
    ) -> str:
        """Enter a passage; returns its reference `chunk:<chunk id>`."""
        ref = f"chunk:{chunk_id}"
        self.entries[ref] = LedgerEntry(
            ref=ref,
            kind="chunk",
            website=website,
            url=url,
            project=project,
            title=" > ".join(headings),
            text=text,
            block_ids=list(block_ids),
            evidence_ids=list(evidence_ids),
        )
        return ref

    def fact(
        self,
        key: str,
        *,
        kind: str,
        website: str,
        title: str,
        url: str | None = None,
        project: str | None = None,
        text: str = "",
        evidence_ids: list[str] | None = None,
    ) -> str:
        """Enter any other fact (page facts, an issue, an offering, a change, a pair)."""
        digest = hashlib.sha256(f"{kind}\x1f{key}".encode()).hexdigest()[:12]
        ref = f"fact:{kind}-{digest}"
        self.entries[ref] = LedgerEntry(
            ref=ref,
            kind=kind,
            website=website,
            url=url,
            project=project,
            title=title,
            text=text,
            evidence_ids=list(evidence_ids or []),
        )
        return ref

    def to_json(self) -> str:
        """The whole ledger, for the run folder."""
        return json.dumps(
            {ref: vars(entry) for ref, entry in sorted(self.entries.items())},
            indent=1,
            ensure_ascii=False,
        )


@dataclass
class SiteData:
    """A website as a run reads it: its freshest copy and that copy's latest run."""

    website: Website
    copy: SiteCopy
    run: RunState
    folder: Path

    @property
    def run_name(self) -> str:
        """Prefix of the run's files (`<run>.stamps.jsonl`, ...)."""
        return self.run.run_id

    def file(self, suffix: str) -> Path | None:
        """`<run><suffix>` in the run folder, when it exists."""
        path = self.folder / f"{self.run_name}{suffix}"
        return path if path.exists() else None


class Playground:
    """Projects, websites and knowledge spaces, limited to one run's scope."""

    def __init__(
        self,
        store: ProjectStore,
        access: KnowledgeAccess,
        project_ids: list[str],
        *,
        tenant: str = DEFAULT_TENANT,
        evidence_root: Path | None = None,
    ) -> None:
        """Bind to the projects of a run; raises `ScopeError` for an unknown project."""
        self.store = store
        self.access = access
        self.tenant = tenant
        self.evidence_root = evidence_root
        self.ledger = EvidenceLedger()
        for project_id in project_ids:
            try:
                store.get(project_id)
            except ProjectNotFoundError as exc:
                msg = f"unknown project {project_id!r}"
                raise ScopeError(msg) from exc
        self.project_ids = list(project_ids)
        self.graph = WebsiteGraph.build(
            store, lambda project, site: space_namespace(tenant, project, site)
        )
        self._scope: dict[str, Website] = {}
        for website in self.graph.websites():
            if any(c.project_id in self.project_ids for c in website.copies):
                for member in self.graph.competitive_scope(website):
                    self._scope[member.key] = member
        self._site_cache: dict[str, SiteData | None] = {}
        self._stamps: dict[str, dict[str, PageStamp]] = {}

    # -- scope -----------------------------------------------------------------

    def websites(self) -> list[Website]:
        """Every website the run may read, by name."""
        return sorted(self._scope.values(), key=lambda w: w.name.lower())

    def website(self, text: str) -> Website:
        """A website of the scope from a domain, address or key; raises `ScopeError`."""
        found = self.graph.find(text.strip())
        if found is None:
            msg = f"no website matches {text!r}; known: {self.known()}"
            raise ScopeError(msg)
        if found.key not in self._scope:
            msg = f"{found.name} ({found.key}) is not in this run's projects or their competitors"
            raise ScopeError(msg)
        return found

    def website_of_url(self, url: str) -> Website:
        """The website an address belongs to; raises `ScopeError` outside the scope."""
        try:
            domain_of(url)
        except ValueError as exc:
            msg = f"not a web address: {url!r}"
            raise ScopeError(msg) from exc
        return self.website(url)

    def known(self) -> str:
        """The scope's websites as `name (key)`, for error messages."""
        return ", ".join(f"{w.name} ({w.key})" for w in self.websites()) or "none"

    def role_in_run(self, website: Website) -> str:
        """`client` when it is a client in one of the run's projects, else `competitor`."""
        for copy in website.copies:
            if copy.project_id in self.project_ids and copy.role == "client":
                return "client"
        return "competitor"

    # -- data of one website ---------------------------------------------------------

    def site_data(self, website: Website) -> SiteData | None:
        """The freshest copy and its latest run with knowledge ready, or None."""
        if website.key in self._site_cache:
            return self._site_cache[website.key]
        data: SiteData | None = None
        copy = website.freshest()
        if copy is not None:
            ready = [
                r
                for r in self.store.runs(copy.project_id, copy.site_id)
                if r.knowledge_status == "ready" and r.finished_at is not None
            ]
            if ready:
                run = max(ready, key=lambda r: r.finished_at or r.created_at)
                folder = self.store.run_dir(copy.project_id, copy.site_id, run.run_id)
                data = SiteData(website=website, copy=copy, run=run, folder=folder)
        self._site_cache[website.key] = data
        return data

    def require(self, website: Website) -> SiteData:
        """`site_data`, or a `ScopeError` saying the website was never made searchable."""
        data = self.site_data(website)
        if data is None:
            msg = f"{website.name} has no finished scrape with its knowledge space built yet"
            raise ScopeError(msg)
        return data

    def ready_runs(self, data: SiteData) -> list[RunState]:
        """The copy's finished runs with knowledge ready, oldest first."""
        runs = [
            r
            for r in self.store.runs(data.copy.project_id, data.copy.site_id)
            if r.knowledge_status == "ready" and r.finished_at is not None
        ]
        return sorted(runs, key=lambda r: r.finished_at or r.created_at)

    def open_store(self, data: SiteData) -> VectorStore:
        """The website's vector store (close it after use)."""
        return self.access.open_store(data.copy.space())

    def stamps(self, data: SiteData) -> dict[str, PageStamp]:
        """The website's page stamps by URL (both requested and final address)."""
        if data.website.key not in self._stamps:
            stamps: dict[str, PageStamp] = {}
            path = data.file(".stamps.jsonl")
            if path is not None:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            stamp = PageStamp.model_validate_json(line)
                            stamps[stamp.url] = stamp
                            if stamp.final_url:
                                stamps.setdefault(stamp.final_url, stamp)
            self._stamps[data.website.key] = stamps
        return self._stamps[data.website.key]
