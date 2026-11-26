"""Google Search Console data from its export ($0, no Google sign-in yet).

Semrush estimates a website's traffic from a click curve and misses much of the long
tail; Search Console has the client's real impressions, clicks, CTR and average
position. This module reads the **Performance export** a person downloads from Search
Console: the zip (it holds `Queries.csv`, `Pages.csv`, `Filters.csv` and more) or one
of its CSV files. A table that has both a query and a page column (an API or Looker
Studio export) is read as query-page pairs, which show two pages of the site sharing one
query (cannibalisation). Columns are found by name, so exports with extra columns work;
CTR is read as "4.2%" or 0.042.

**CTR opportunities**: a page or query on page one whose real CTR is well below what its
position usually gets (`build.CTR`, the same curve the visit estimates use) and that has
enough impressions to matter. Those are title and description rewrites: no new content
or links needed. The clicks it could gain are impressions x (expected - real CTR).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "MIN_IMPRESSIONS",
    "CtrOpportunity",
    "GscError",
    "GscRow",
    "GscSnapshot",
    "ctr_opportunities",
    "expected_ctr",
    "read_gsc_export",
]

MIN_IMPRESSIONS: Final = 200  # in the export's period: less is too little to judge a CTR
_BELOW: Final = 0.6  # a real CTR under 60 % of the expected one counts as below
_MAX_POSITION: Final = 10.5

_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "query": ("top queries", "query", "queries", "search query"),
    "page": ("top pages", "page", "pages", "landing page", "url"),
    "clicks": ("clicks", "url clicks"),
    "impressions": ("impressions",),
    "ctr": ("ctr", "url ctr", "site ctr"),
    "position": ("position", "average position", "avg. position", "avg position"),
}


class GscError(ValueError):
    """The file is not a Search Console performance export."""


class GscRow(StrictModel):
    """One query, one page, or one query on one page, over the export's period."""

    query: str | None = None
    page: str | None = None
    clicks: int = Field(default=0, ge=0)
    impressions: int = Field(default=0, ge=0)
    ctr: float = Field(default=0.0, ge=0.0, le=1.0)
    position: float = Field(default=0.0, ge=0.0)


class GscSnapshot(StrictModel):
    """One import of a website's Search Console export."""

    project_id: str
    website: str
    imported_at: datetime
    period: str = Field(default="", description="The export's date filter, when it says")
    queries: list[GscRow] = Field(default_factory=list)
    pages: list[GscRow] = Field(default_factory=list)
    pairs: list[GscRow] = Field(default_factory=list, description="Query-page pairs")


class CtrOpportunity(StrictModel):
    """A page or query that gets fewer clicks than its position usually brings."""

    kind: Literal["page", "query"]
    label: str
    impressions: int
    clicks: int
    ctr: float
    expected_ctr: float
    position: float
    extra_clicks: int = Field(description="Clicks a usual CTR would bring over the period")


def expected_ctr(position: float) -> float:
    """The usual CTR at an average position (the curve of `build.CTR`, between steps)."""
    from src.modules.research.build import CTR

    if position <= 0:
        return 0.0
    low = max(1, min(10, int(position)))
    high = min(10, low + 1)
    frac = min(1.0, max(0.0, position - low))
    return CTR[low] + (CTR[high] - CTR[low]) * frac if position <= 10 else 0.01


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().strip("﻿").casefold())


def _number(text: str) -> float:
    cleaned = text.replace(",", "").strip()
    pct = cleaned.endswith("%")
    try:
        value = float(cleaned.rstrip("%") or 0)
    except ValueError:
        return 0.0
    return value / 100 if pct else value


def _table(text: str) -> list[GscRow] | None:
    """Rows of one CSV, or None when it is not a performance table."""
    sample = text[:2048]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = {_key(h): h for h in reader.fieldnames or [] if h}
    col = {f: next((headers[a] for a in al if a in headers), None) for f, al in _ALIASES.items()}
    if col["impressions"] is None or (col["query"] is None and col["page"] is None):
        return None
    rows = []
    for raw in reader:

        def get(field: str, raw: dict[str, str] = raw) -> str:
            name = col[field]
            return (raw.get(name) or "").strip() if name else ""

        query, page = get("query") or None, get("page") or None
        if not (query or page):
            continue
        ctr = _number(get("ctr"))
        rows.append(
            GscRow(
                query=query,
                page=page,
                clicks=int(_number(get("clicks"))),
                impressions=int(_number(get("impressions"))),
                ctr=min(1.0, ctr if ctr <= 1 else ctr / 100),
                position=_number(get("position")),
            )
        )
    return rows


def _period(text: str) -> str:
    """The date filter written in `Filters.csv` ("Date: Last 3 months")."""
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].strip().casefold() == "date":
            return row[1].strip()
    return ""


def read_gsc_export(data: bytes, *, project_id: str, website: str, now: datetime) -> GscSnapshot:
    """A snapshot from the export zip or one of its CSVs; raises `GscError` otherwise."""
    texts: dict[str, str] = {}
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for name in archive.namelist():
                    if name.lower().endswith(".csv") and archive.getinfo(name).file_size < 50e6:
                        texts[name.rsplit("/", 1)[-1]] = archive.read(name).decode(
                            "utf-8-sig", errors="replace"
                        )
        except zipfile.BadZipFile as exc:
            raise GscError("the zip file cannot be read") from exc
    else:
        texts["export.csv"] = data.decode("utf-8-sig", errors="replace")
    snapshot = GscSnapshot(project_id=project_id, website=website, imported_at=now)
    for name, text in texts.items():
        if name.casefold() == "filters.csv":
            snapshot.period = _period(text)
            continue
        rows = _table(text)
        if not rows:
            continue
        if all(r.query and r.page for r in rows):
            snapshot.pairs += rows
        elif all(r.page for r in rows):
            snapshot.pages += rows
        elif all(r.query for r in rows):
            snapshot.queries += rows
    if not (snapshot.queries or snapshot.pages or snapshot.pairs):
        msg = (
            "not a Search Console performance export: it needs a Queries or Pages table with "
            "Clicks, Impressions, CTR and Position (Search Console, Performance, Export)"
        )
        raise GscError(msg)
    return snapshot


def ctr_opportunities(
    snapshot: GscSnapshot, *, min_impressions: int = MIN_IMPRESSIONS
) -> list[CtrOpportunity]:
    """Page-one pages and queries with a CTR well below usual, most clicks to gain first."""
    out = []
    for kind, rows in (("page", snapshot.pages), ("query", snapshot.queries)):
        for r in rows:
            if r.impressions < min_impressions or not 0 < r.position <= _MAX_POSITION:
                continue
            expected = expected_ctr(r.position)
            if r.ctr >= expected * _BELOW:
                continue
            out.append(
                CtrOpportunity(
                    kind=kind,
                    label=(r.page if kind == "page" else r.query) or "",
                    impressions=r.impressions,
                    clicks=r.clicks,
                    ctr=round(r.ctr, 4),
                    expected_ctr=round(expected, 4),
                    position=round(r.position, 1),
                    extra_clicks=round(r.impressions * (expected - r.ctr)),
                )
            )
    return sorted(out, key=lambda o: -o.extra_clicks)
