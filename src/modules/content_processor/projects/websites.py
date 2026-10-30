"""Unique websites across all projects, and who competes with whom.

A website is the same website whichever project it was entered in: two project
websites are one when they share a domain, or one's domain is a subdomain of the
other's (`usa.canon.com` and `canon.com`). Competitor relations are collected from
every project: in each project the client's website competes with each of its
competitor websites, in both directions.

A question about a website is asked of its *competitive scope*: the website itself
and every website related to it as a competitor in any project. Each website is
searched once, in its most recently built knowledge space (its freshest copy), so
a competitor scraped for two projects is not counted twice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from src.modules.content_processor.knowledge.federated import Space
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.projects.urls import domain_of

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

    def freshest(self) -> SiteCopy | None:
        """The copy whose knowledge space was built last; None when none is built."""
        built = [c for c in self.copies if c.knowledge_at is not None]
        return max(built, key=lambda c: c.knowledge_at or datetime.min) if built else None


def _same_site(a: str, b: str) -> bool:
    return a == b or a.endswith("." + b) or b.endswith("." + a)


class WebsiteGraph:
    """Unique websites and competitor relations, built from every project."""

    def __init__(self) -> None:
        """An empty graph; use `build`."""
        self._sites: dict[str, Website] = {}

    @classmethod
    def build(cls, store: ProjectStore, namespace: Callable[[str, str], str]) -> WebsiteGraph:
        """Read every project's websites, their knowledge state and their relations."""
        graph = cls()
        for project in sorted(store.projects(), key=lambda p: p.created_at):
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
                )
                keys[site.site_id] = graph._add(copy, set(site.domains))
            client = next((s for s in project.sites if s.role == "client"), None)
            if client is None:
                continue
            for site in project.sites:
                if site.role == "competitor":
                    a, b = keys[client.site_id], keys[site.site_id]
                    graph._sites[a].competitors.add(b)
                    graph._sites[b].competitors.add(a)
        return graph

    def _add(self, copy: SiteCopy, domains: set[str]) -> str:
        """Attach a copy to the website it belongs to (merging websites it joins)."""
        if not domains:
            # Without a domain the website cannot be recognised elsewhere: its own node.
            key = f"{copy.project_id}/{copy.site_id}"
            self._sites[key] = Website(key=key, copies=[copy])
            return key
        matches = [
            w
            for w in self._sites.values()
            if any(_same_site(d, e) for d in domains for e in w.domains)
        ]
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
        keep.competitors |= gone.competitors
        del self._sites[gone.key]
        for site in self._sites.values():
            if gone.key in site.competitors:
                site.competitors.discard(gone.key)
                if site.key != keep.key:
                    site.competitors.add(keep.key)
        keep.competitors.discard(keep.key)

    def websites(self) -> list[Website]:
        """Every unique website, by name."""
        return sorted(self._sites.values(), key=lambda w: w.name.lower())

    def get(self, key: str) -> Website | None:
        """A website by its key."""
        return self._sites.get(key)

    def find(self, text: str) -> Website | None:
        """The website of an address or domain as a person types it, or None."""
        if text in self._sites:
            return self._sites[text]
        try:
            domain = domain_of(text)
        except ValueError:
            return None
        for site in self._sites.values():
            if any(_same_site(domain, d) for d in site.domains):
                return site
        return None

    def competitive_scope(self, website: Website) -> list[Website]:
        """The website itself, then every website it competes with in any project."""
        others = [self._sites[k] for k in website.competitors if k in self._sites]
        return [website, *sorted(others, key=lambda w: w.name.lower())]
