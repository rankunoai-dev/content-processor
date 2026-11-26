"""Read GA4's Landing page report: the CSV a person downloads, or Data API rows ($0).

**The export.** In GA4: Reports, Engagement, Landing page, then Share this report,
Download file, Download CSV. The file starts with `#` comment lines (account, property,
`# Start date: 20261001`, `# End date: 20261028`), then a table; it may hold a
"Grand total" row and further tables after a blank line (only the first table with a
landing page column is read). Columns are found by name, case and spacing aside:

| Field | Column names accepted |
|---|---|
| page | Landing page + query string, Landing page, Page path + query string, Page location |
| channel (optional) | Session default channel group, Session primary channel group, |
| | Default channel group |
| source (optional) | Session source, Session source / medium, Source |
| sessions | Sessions |
| engaged sessions | Engaged sessions |
| engagement rate | Engagement rate ("65.2%" or 0.652) |
| engagement time | Average engagement time per session, Average engagement time |
| | (seconds, 1m 05s or 00:01:05) |
| key events | Key events, Conversions |
| revenue | Total revenue, Purchase revenue (currency signs and commas are ignored) |

With a channel or source column a page has several rows; they are added up per page
(organic sessions from the Organic Search channel, AI referrals from AI assistants'
sources). Pages are joined to the rest of the app by `links.page_key`: host without
www, path without trailing slash, query and anchor dropped.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from src.modules.analytics.models import AI_SOURCES, ORGANIC, Ga4Row, Ga4Snapshot
from src.modules.research.links import page_key

__all__ = ["Ga4ImportError", "aggregate", "ai_source", "read_ga4_csv"]

_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "page": (
        "landing page + query string", "landing page", "page path + query string",
        "page path and screen class", "page location", "landing page path",
    ),
    "channel": (
        "session default channel group", "session primary channel group",
        "default channel group", "session default channel grouping",
    ),
    "source": ("session source", "session source / medium", "source", "session source/medium"),
    "sessions": ("sessions",),
    "engaged": ("engaged sessions",),
    "rate": ("engagement rate",),
    "time": (
        "average engagement time per session", "average engagement time",
        "avg. engagement time per session",
    ),
    "events": ("key events", "conversions"),
    "revenue": ("total revenue", "purchase revenue", "revenue"),
}  # fmt: skip
_SKIP_PAGES: Final = frozenset({"(not set)", "(other)", "grand total", "total", ""})
_DATE: Final = re.compile(r"#\s*(start|end) date:\s*(\d{8})", re.IGNORECASE)


class Ga4ImportError(ValueError):
    """The file is not a GA4 landing page report."""


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().strip("﻿").casefold())


def _number(text: str) -> float:
    cleaned = re.sub(r"[^\d.\-%]", "", text.replace(",", ""))
    pct = cleaned.endswith("%")
    try:
        value = float(cleaned.rstrip("%") or 0)
    except ValueError:
        return 0.0
    return value / 100 if pct else value


def _seconds(text: str) -> float:
    """'83.2', '1m 23s', '00:01:23' or '1:23' as seconds."""
    t = text.strip().casefold()
    if ":" in t:
        parts = [float(p or 0) for p in t.split(":")]
        total = 0.0
        for p in parts:
            total = total * 60 + p
        return total
    found = re.findall(r"([\d.]+)\s*([hms])", t)
    if found:
        return sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in found)
    return _number(t)


def ai_source(source: str) -> str | None:
    """The AI assistant a session source names ('chatgpt.com / referral' -> chatgpt.com)."""
    host = source.casefold().split("/")[0].strip().removeprefix("www.")
    return next((a for a in AI_SOURCES if host == a or host.endswith("." + a)), None)


def _url(page: str, website: str) -> str | None:
    page = page.strip()
    if page.casefold() in _SKIP_PAGES:
        return None
    if page.startswith(("http://", "https://")):
        return page.split("#", 1)[0]
    path = page.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    return f"https://{website.removeprefix('www.')}{path}"


@dataclass
class _Page:
    url: str
    sessions: int = 0
    engaged: int = 0
    time: float = 0.0
    events: float = 0.0
    revenue: float = 0.0
    organic: int = 0
    ai: int = 0
    channels: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)


def _int(value: object) -> int:
    try:
        return int(float(str(value or 0)))
    except ValueError:
        return 0


def _float(value: object) -> float:
    try:
        return float(str(value or 0))
    except ValueError:
        return 0.0


def aggregate(records: Iterable[Mapping[str, object]], website: str) -> list[Ga4Row]:
    """One row per page from records with page, channel, source and metric fields."""
    pages: dict[str, _Page] = {}
    for r in records:
        url = _url(str(r.get("page") or ""), website)
        if url is None:
            continue
        agg = pages.setdefault(page_key(url), _Page(url))
        sessions = _int(r.get("sessions"))
        engaged = r.get("engaged")
        agg.sessions += sessions
        agg.engaged += (
            _int(engaged) if engaged not in (None, "") else round(_float(r.get("rate")) * sessions)
        )
        agg.time += _float(r.get("time")) * sessions
        agg.events += _float(r.get("events"))
        agg.revenue += _float(r.get("revenue"))
        channel = str(r.get("channel") or "").strip()
        if channel:
            agg.channels[channel] = agg.channels.get(channel, 0) + sessions
            if channel.casefold() in ORGANIC:
                agg.organic += sessions
        source = str(r.get("source") or "").strip()
        if source and (ai := ai_source(source)):
            # API source-only records carry their sessions as `ai_sessions` (0 `sessions`),
            # so the page total is not counted twice
            count = _int(r.get("ai_sessions")) if "ai_sessions" in r else sessions
            agg.ai += count
            agg.sources[ai] = agg.sources.get(ai, 0) + count
    out = []
    for a in pages.values():
        engaged = min(a.engaged, a.sessions)
        out.append(
            Ga4Row(
                url=a.url,
                sessions=a.sessions,
                engaged_sessions=engaged,
                engagement_rate=round(engaged / a.sessions, 4) if a.sessions else 0.0,
                avg_engagement_s=round(a.time / a.sessions, 1) if a.sessions else 0.0,
                key_events=round(a.events, 2),
                revenue=round(a.revenue, 2),
                organic_sessions=a.organic,
                ai_sessions=a.ai,
                channels=a.channels,
                ai_sources=a.sources,
            )
        )
    return sorted(out, key=lambda r: -r.sessions)


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig", errors="replace")


def read_ga4_csv(
    data: bytes, *, project_id: str, website: str, now: datetime, snapshot_id: str
) -> Ga4Snapshot:
    """A snapshot from GA4's landing page CSV; raises `Ga4ImportError` when it is not one."""
    text = _decode(data)
    dates: dict[str, date] = {}
    table: list[str] = []
    started = False
    for line in text.splitlines():
        if line.startswith("#"):
            if found := _DATE.search(line):
                dates[found.group(1).casefold()] = datetime.strptime(
                    found.group(2), "%Y%m%d"
                ).date()
            if started:
                break
            continue
        if not line.strip():
            if started:
                break
            continue
        table.append(line)
        started = True
    if not table:
        raise Ga4ImportError("the file has no table")
    sample = table[0]
    delimiter = (
        "\t"
        if sample.count("\t") > sample.count(",")
        else (";" if sample.count(";") > sample.count(",") else ",")
    )
    reader = csv.DictReader(io.StringIO("\n".join(table)), delimiter=delimiter)
    headers = {_key(h): h for h in reader.fieldnames or [] if h}
    col = {f: next((headers[a] for a in al if a in headers), None) for f, al in _ALIASES.items()}
    if col["page"] is None or col["sessions"] is None:
        msg = (
            "not a GA4 landing page report: it needs a Landing page column and Sessions "
            "(GA4: Reports, Engagement, Landing page, Download CSV)"
        )
        raise Ga4ImportError(msg)
    records = []
    for raw in reader:

        def get(field: str, raw: dict[str, str] = raw) -> str:
            name = col[field]
            return (raw.get(name) or "").strip() if name else ""

        records.append(
            {
                "page": get("page"),
                "channel": get("channel"),
                "source": get("source"),
                "sessions": _number(get("sessions")),
                "engaged": _number(get("engaged")) if col["engaged"] else None,
                "rate": _number(get("rate")),
                "time": _seconds(get("time")) if col["time"] else 0.0,
                "events": _number(get("events")),
                "revenue": _number(get("revenue")),
            }
        )
    rows = aggregate(records, website)
    notes = []
    if col["channel"] is None:
        notes.append(
            "No channel column: organic sessions are unknown (add Session default channel "
            "group as a secondary dimension before downloading)."
        )
    return Ga4Snapshot(
        snapshot_id=snapshot_id,
        project_id=project_id,
        website=website,
        source="csv_import",
        imported_at=now,
        start_date=dates.get("start"),
        end_date=dates.get("end"),
        rows=rows,
        has_channels=col["channel"] is not None,
        has_sources=col["source"] is not None,
        notes=notes,
    )
