"""Suggested competitors: websites the search data shows competing with the client.

The engine notices competitors by itself and keeps them, so they are context for the
gap analysis and Claude even before anyone adds them to the project:

- **Semrush organic competitors** of the client's main website (a pull): competitor
  relevance, keywords in common, the competitor's own keywords and traffic.
- **A Semrush "Organic Competitors" export** imported in the Research tab ($0).
- **Search results** of the keywords pulled with their results: every domain in a
  top 10, with the keywords it appears for.

Suggestions are saved per project (`<app data>/research/<project>/competitors.json`)
and merged on every new pull or import: figures are refreshed, the sources and the
keywords add up, and a decision (added or dismissed) is kept. The client's own
websites are never suggested. A suggestion is only data: nothing is scraped or paid
for until a person adds it to the project as a competitor.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.projects.models import ProjectRecord
from src.modules.content_processor.projects.urls import address_of, addresses_overlap
from src.modules.research.models import ResearchSnapshot

__all__ = [
    "COMPETITOR_LINES",
    "SOURCE_LABELS",
    "CompetitorBook",
    "CompetitorSuggestion",
    "CompetitorsStore",
    "SuggestionSource",
    "SuggestionStatus",
    "domain_of",
    "from_competitor_rows",
    "from_search_results",
    "merge_suggestions",
    "read_competitors_csv",
    "with_project_status",
]

COMPETITOR_LINES: Final = 20
_TOP10: Final = 10
_EXAMPLES: Final = 8
_DOMAIN: Final = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

SuggestionSource = Literal["semrush_competitors", "competitors_export", "search_results"]
SuggestionStatus = Literal["suggested", "added", "dismissed"]

SOURCE_LABELS: Final[dict[str, str]] = {
    "semrush_competitors": "Semrush organic competitors",
    "competitors_export": "Semrush competitors export",
    "search_results": "Top 10 search results",
}

_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "domain": ("domain", "dn", "competitor"),
    "relevance": ("competitor relevance", "competition level", "cr", "relevance"),
    "common": ("common keywords", "np", "com. keywords", "common"),
    "keywords": ("organic keywords", "or", "se keywords", "keywords"),
    "traffic": ("organic traffic", "ot", "se traffic", "traffic"),
}


class CompetitorSuggestion(StrictModel):
    """A website the search data shows competing with the client."""

    domain: str
    status: SuggestionStatus = "suggested"
    sources: list[SuggestionSource] = Field(default_factory=list)
    relevance: float | None = Field(
        default=None, description="Semrush competitor relevance, 0 to 1 (higher is closer)"
    )
    common_keywords: int = Field(default=0, ge=0, description="Keywords it shares with the client")
    organic_keywords: int = Field(default=0, ge=0)
    organic_traffic: int = Field(default=0, ge=0, description="Estimated visits a month")
    search_results: int = Field(
        default=0, ge=0, description="Pulled keywords whose top 10 includes it"
    )
    example_keywords: list[str] = Field(default_factory=list)
    first_seen: datetime
    last_seen: datetime
    times_seen: int = Field(default=1, ge=1)
    decided_at: datetime | None = None
    site_id: str | None = Field(default=None, description="The project website, once added")


class CompetitorBook(StrictModel):
    """Every suggested competitor of one project."""

    project_id: str
    updated_at: datetime
    client_domain: str | None = None
    suggestions: list[CompetitorSuggestion] = Field(default_factory=list)


def domain_of(text: str) -> str | None:
    """The bare domain of a domain or address ('https://www.Coupa.com/x' -> 'coupa.com')."""
    try:
        domain = address_of(text.strip(), keep_path=False)
    except ValueError:
        return None
    return domain if _DOMAIN.match(domain) else None


def _int(text: str | None) -> int:
    try:
        return max(0, int(float((text or "0").replace(",", "").strip() or 0)))
    except ValueError:
        return 0


def _relevance(text: str | None) -> float | None:
    raw = (text or "").replace("%", "").replace(",", ".").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value > 1:
        value /= 100
    return round(min(max(value, 0.0), 1.0), 3)


def _field(row: Mapping[str, str], name: str) -> str | None:
    lowered = {" ".join(k.casefold().split()): v for k, v in row.items() if k}
    for alias in _COLUMNS[name]:
        if alias in lowered:
            return lowered[alias]
    return None


def from_competitor_rows(
    rows: Iterable[Mapping[str, str]], source: SuggestionSource, now: datetime
) -> list[CompetitorSuggestion]:
    """Suggestions from Semrush organic-competitor rows (API answer or export)."""
    out: dict[str, CompetitorSuggestion] = {}
    for row in rows:
        domain = domain_of(_field(row, "domain") or "")
        if domain is None or domain in out:
            continue
        out[domain] = CompetitorSuggestion(
            domain=domain,
            sources=[source],
            relevance=_relevance(_field(row, "relevance")),
            common_keywords=_int(_field(row, "common")),
            organic_keywords=_int(_field(row, "keywords")),
            organic_traffic=_int(_field(row, "traffic")),
            first_seen=now,
            last_seen=now,
        )
    return list(out.values())


def from_search_results(snapshot: ResearchSnapshot, now: datetime) -> list[CompetitorSuggestion]:
    """Every domain in the top 10 of the snapshot's pulled keywords, with those keywords."""
    seen: dict[str, list[str]] = {}
    for metric in snapshot.keywords:
        for row in metric.serp:
            if row.rank > _TOP10:
                continue
            domain = domain_of(row.domain or row.url)
            if domain is not None and metric.keyword not in seen.setdefault(domain, []):
                seen[domain].append(metric.keyword)
    return [
        CompetitorSuggestion(
            domain=domain,
            sources=["search_results"],
            search_results=len(words),
            example_keywords=words[:_EXAMPLES],
            first_seen=now,
            last_seen=now,
        )
        for domain, words in seen.items()
    ]


def read_competitors_csv(data: bytes, now: datetime) -> list[CompetitorSuggestion] | None:
    """Suggestions from a Semrush "Organic Competitors" export, or None when it is not one."""
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:2048]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    names = {" ".join((n or "").casefold().split()) for n in reader.fieldnames or []}
    has_domain = any(a in names for a in _COLUMNS["domain"])
    has_overlap = any(a in names for a in (*_COLUMNS["common"], *_COLUMNS["relevance"]))
    if not (has_domain and has_overlap):
        return None
    rows = [{k: v or "" for k, v in row.items() if k} for row in reader]
    return from_competitor_rows(rows, "competitors_export", now)


def _project_domains(project: ProjectRecord) -> tuple[list[str], dict[str, str]]:
    client: list[str] = []
    competitors: dict[str, str] = {}
    for site in project.sites:
        for address in site.domains:
            if site.role == "client":
                client.append(address)
            else:
                competitors.setdefault(address, site.site_id)
    return client, competitors


def _is_client(domain: str, client: list[str]) -> bool:
    return any(addresses_overlap(domain, a) or addresses_overlap(a, domain) for a in client)


def _site_of(domain: str, competitors: Mapping[str, str]) -> str | None:
    for address, site_id in competitors.items():
        if addresses_overlap(domain, address) or addresses_overlap(address, domain):
            return site_id
    return None


def _merge_one(old: CompetitorSuggestion, new: CompetitorSuggestion) -> CompetitorSuggestion:
    fresh_overlap = new.sources != ["search_results"]
    return old.model_copy(
        update={
            "sources": sorted({*old.sources, *new.sources}),
            "relevance": new.relevance if new.relevance is not None else old.relevance,
            "common_keywords": new.common_keywords if fresh_overlap else old.common_keywords,
            "organic_keywords": new.organic_keywords or old.organic_keywords,
            "organic_traffic": new.organic_traffic or old.organic_traffic,
            "search_results": new.search_results if not fresh_overlap else old.search_results,
            "example_keywords": list(dict.fromkeys([*new.example_keywords, *old.example_keywords]))[
                :_EXAMPLES
            ],
            "last_seen": new.last_seen,
            "times_seen": old.times_seen + 1,
        }
    )


def _rank(s: CompetitorSuggestion) -> tuple[int, float, int, int, str]:
    order = {"suggested": 0, "added": 1, "dismissed": 2}[s.status]
    return (order, -(s.relevance or 0.0), -s.common_keywords, -s.search_results, s.domain)


def with_project_status(book: CompetitorBook, project: ProjectRecord) -> CompetitorBook:
    """The book against the project as it is now: client websites dropped, added ones marked."""
    client, competitors = _project_domains(project)
    kept: list[CompetitorSuggestion] = []
    for s in book.suggestions:
        if _is_client(s.domain, client):
            continue
        site_id = _site_of(s.domain, competitors)
        if site_id is not None:
            s = s.model_copy(update={"status": "added", "site_id": site_id})
        elif s.status == "added":  # the website was removed from the project again
            s = s.model_copy(update={"status": "suggested", "site_id": None})
        kept.append(s)
    main = project.main_site()
    return book.model_copy(
        update={
            "suggestions": sorted(kept, key=_rank),
            "client_domain": main.domains[0] if main and main.domains else book.client_domain,
        }
    )


def merge_suggestions(
    book: CompetitorBook | None,
    found: Iterable[CompetitorSuggestion],
    project: ProjectRecord,
    now: datetime | None = None,
) -> CompetitorBook:
    """The book with newly found competitors added and known ones refreshed."""
    moment = now or datetime.now(UTC)
    by_domain = {s.domain: s for s in (book.suggestions if book else [])}
    for new in found:
        old = by_domain.get(new.domain)
        by_domain[new.domain] = new if old is None else _merge_one(old, new)
    merged = CompetitorBook(
        project_id=project.project_id,
        updated_at=moment,
        client_domain=book.client_domain if book else None,
        suggestions=list(by_domain.values()),
    )
    return with_project_status(merged, project)


class CompetitorsStore:
    """`<root>/<project>/competitors.json` of every project."""

    def __init__(self, root: Path) -> None:
        """Bind to the research folder (usually `<app data>/research`)."""
        self.root = root
        self._lock = threading.Lock()

    def path(self, project_id: str) -> Path:
        """Where the project's suggestions are kept (the id is checked)."""
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,80}$", project_id):
            raise KeyError(project_id)
        return self.root / project_id / "competitors.json"

    def get(self, project_id: str) -> CompetitorBook | None:
        """The project's suggestions as saved, or None."""
        path = self.path(project_id)
        if not path.exists():
            return None
        with self._lock:
            return CompetitorBook.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, book: CompetitorBook) -> Path:
        """Write the book (atomically)."""
        path = self.path(book.project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(book.model_dump(mode="json"), indent=1), encoding="utf-8")
            tmp.replace(path)
        return path

    def decide(
        self,
        project: ProjectRecord,
        domain: str,
        status: SuggestionStatus,
        site_id: str | None = None,
    ) -> CompetitorBook:
        """Mark one suggestion added, dismissed or suggested again; raises `KeyError`."""
        book = self.get(project.project_id)
        found = next((s for s in (book.suggestions if book else []) if s.domain == domain), None)
        if book is None or found is None:
            raise KeyError(domain)
        moment = datetime.now(UTC)
        changed = found.model_copy(
            update={"status": status, "decided_at": moment, "site_id": site_id}
        )
        book = book.model_copy(
            update={
                "updated_at": moment,
                "suggestions": [changed if s.domain == domain else s for s in book.suggestions],
            }
        )
        book = with_project_status(book, project)
        self.save(book)
        return book
