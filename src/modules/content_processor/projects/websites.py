"""Unique websites across all projects, who competes with whom, and who belongs together.

A website is the same website whichever project it was entered in: two project
websites are one when they share an address, or one's address covers the other's
(`usa.canon.com` and `canon.com`) -- unless some project lists those two addresses
as separate websites (the client's brands at `brand.client.com` and `client.com`,
or sections such as `client.com/brand-a`): then they stay separate everywhere.

Relations are collected from every project, in both directions:

* **competitors**: each of the project's client websites competes with each of its
  competitor websites;
* **sister websites**: the client's websites in one project (its main website and
  its other websites or brands) belong to the same client. Sister websites are
  never competitors: a sister relation in any project cancels a competitor
  relation between the same two websites in any other project.

A question about a website is asked of its *competitive scope*: the website, its
sister websites, and every website any of them competes with in any project. Each
website is searched once, in its most recently built knowledge space (its freshest
copy), so a competitor scraped for two projects is not counted twice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from src.modules.content_processor.knowledge.federated import Space
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.projects.urls import address_score, addresses_overlap

__all__ = ["SiteCopy", "Website", "WebsiteGraph"]


@dataclass(frozen=True)
class SiteCopy:
    """One project's copy of a website: its knowledge space and when it was last built."""

    project_id: str
    project_name: str
    site_id: str
    site_name: str
    role: str
    namespace: str
    knowledge_at: datetime | None
    note: str = ""
    tags: tuple[str, ...] = ()

    def space(self) -> Space:
        """The knowledge space of this copy, as the bot searches it."""
        return Space(
            namespace=self.namespace,
            project_id=self.project_id,
            project_name=self.project_name,
            site_id=self.site_id,
            site_name=self.site_name,
            role=self.role,
        )


@dataclass
class Website:
    """A unique website: every project's copy of it and the websites it competes with."""

    key: str
    domains: set[str] = field(default_factory=set)
    copies: list[SiteCopy] = field(default_factory=list)
    competitors: set[str] = field(default_factory=set)
    sisters: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        """Its name where it is the client; else the name of its freshest (or last) copy."""
        for copy in reversed(self.copies):
            if copy.role == "client":
                return copy.site_name
        fresh = self.freshest()
        if fresh is not None:
            return fresh.site_name
        return self.copies[-1].site_name if self.copies else self.key

    @property
    def note(self) -> str:
        """Its note where it is the client's; else the latest note any project gave it."""
        for copy in reversed(self.copies):
            if copy.role == "client" and copy.note:
                return copy.note
        return next((c.note for c in reversed(self.copies) if c.note), "")

    @property
    def tags(self) -> list[str]:
        """Every tag any project gave it (first spelling of each)."""
        out: list[str] = []
        seen: set[str] = set()
        for copy in self.copies:
            for tag in copy.tags:
                if tag.lower() not in seen:
                    seen.add(tag.lower())
                    out.append(tag)
        return out

    def freshest(self) -> SiteCopy | None:
        """The copy whose knowledge space was built last; None when none is built."""
        built = [c for c in self.copies if c.knowledge_at is not None]
        return max(built, key=lambda c: c.knowledge_at or datetime.min) if built else None


def _pair(a: str, b: str) -> frozenset[str]:
    return frozenset((a, b))


class WebsiteGraph:
    """Unique websites and competitor relations, built from every project."""

    def __init__(self) -> None:
        """An empty graph; use `build`."""
        self._sites: dict[str, Website] = {}
        self._separate: set[frozenset[str]] = set()
        self._moved: dict[str, str] = {}

    @classmethod
    def build(cls, store: ProjectStore, namespace: Callable[[str, str], str]) -> WebsiteGraph:
        """Read every project's websites, their knowledge state and their relations."""
        graph = cls()
        projects = sorted(store.projects(), key=lambda p: p.created_at)
        # Addresses that some project lists as two websites are never merged.
        for project in projects:
            for i, one in enumerate(project.sites):
                for other in project.sites[i + 1 :]:
                    for a in one.domains:
                        for b in other.domains:
                            if a != b and addresses_overlap(a, b):
                                graph._separate.add(_pair(a, b))
        links: list[tuple[str, str, str]] = []
        for project in projects:
            keys: dict[str, str] = {}
            for site in project.sites:
                ready = [
                    r.finished_at
                    for r in store.runs(project.project_id, site.site_id)
                    if r.knowledge_status == "ready" and r.finished_at is not None
                ]
                copy = SiteCopy(
                    project_id=project.project_id,
                    project_name=project.name,
                    site_id=site.site_id,
                    site_name=site.name,
                    role=site.role,
                    namespace=namespace(project.project_id, site.site_id),
                    knowledge_at=max(ready) if ready else None,
                    note=site.note,
                    tags=tuple(site.tags),
                )
                keys[site.site_id] = graph._add(copy, set(site.domains))
            clients = [s.site_id for s in project.sites if s.role == "client"]
            rivals = [s.site_id for s in project.sites if s.role == "competitor"]
            for i, a in enumerate(clients):
                links.extend(("sister", keys[a], keys[b]) for b in clients[i + 1 :])
                links.extend(("competitor", keys[a], keys[r]) for r in rivals)
        # Keys may have changed as websites merged; resolve them at the end.
        for kind, a, b in links:
            ka, kb = graph._resolve(a), graph._resolve(b)
            if ka == kb:
                continue
            attr = "sisters" if kind == "sister" else "competitors"
            getattr(graph._sites[ka], attr).add(kb)
            getattr(graph._sites[kb], attr).add(ka)
        for node in graph._sites.values():
            node.competitors -= graph.family_keys(node)
        return graph

    def _resolve(self, key: str) -> str:
        while key not in self._sites:
            key = self._moved[key]
        return key

    def _overlaps(self, domains: set[str], website: Website) -> bool:
        return any(
            d == e or (addresses_overlap(d, e) and _pair(d, e) not in self._separate)
            for d in domains
            for e in website.domains
        )

    def _add(self, copy: SiteCopy, domains: set[str]) -> str:
        """Attach a copy to the website it belongs to (merging websites it joins)."""
        if not domains:
            # Without a domain the website cannot be recognised elsewhere: its own node.
            key = f"{copy.project_id}/{copy.site_id}"
            self._sites[key] = Website(key=key, copies=[copy])
            return key
        matches = [w for w in self._sites.values() if self._overlaps(domains, w)]
        if not matches:
            key = min(domains, key=len)
            self._sites[key] = Website(key=key, domains=set(domains), copies=[copy])
            return key
        target = matches[0]
        for other in matches[1:]:
            self._merge(target, other)
        target.domains |= domains
        target.copies.append(copy)
        return target.key

    def _merge(self, keep: Website, gone: Website) -> None:
        keep.domains |= gone.domains
        keep.copies.extend(gone.copies)
        del self._sites[gone.key]
        self._moved[gone.key] = keep.key

    def websites(self) -> list[Website]:
        """Every unique website, by name."""
        return sorted(self._sites.values(), key=lambda w: w.name.lower())

    def get(self, key: str) -> Website | None:
        """A website by its key."""
        return self._sites.get(key)

    def find(self, text: str) -> Website | None:
        """The website of an address or domain as a person types it, or None.

        When one domain holds several websites, the most specific address wins.
        """
        if text in self._sites:
            return self._sites[text]
        best: tuple[tuple[int, int], Website] | None = None
        for site in self._sites.values():
            for address in site.domains:
                score = address_score(text, address)
                if score is not None and (best is None or score > best[0]):
                    best = (score, site)
        return best[1] if best else None

    def family_keys(self, website: Website) -> set[str]:
        """The website and its sister websites (the same client's), followed through."""
        seen = {website.key}
        todo = [website.key]
        while todo:
            for k in self._sites[todo.pop()].sisters:
                if k in self._sites and k not in seen:
                    seen.add(k)
                    todo.append(k)
        return seen

    def family(self, website: Website) -> list[Website]:
        """The website, then its sister websites by name."""
        others = [self._sites[k] for k in self.family_keys(website) - {website.key}]
        return [website, *sorted(others, key=lambda w: w.name.lower())]

    def competitive_scope(self, website: Website) -> list[Website]:
        """The website and its sister websites, then every website any of them competes with."""
        family = self.family(website)
        keys = {w.key for w in family}
        rivals = {k for w in family for k in w.competitors if k in self._sites} - keys
        others = sorted((self._sites[k] for k in rivals), key=lambda w: w.name.lower())
        return [*family, *others]

    def sisters_of(self, website: Website) -> list[Website]:
        """The website's sister websites (not itself), by name."""
        return self.family(website)[1:]
