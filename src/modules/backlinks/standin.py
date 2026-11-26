"""An Ahrefs stand-in: dummy backlink data in Ahrefs' own formats, for trying the module.

Nothing here is real link data. It lets the whole Backlinks module run end to end
(pulls, imports, insights, gap findings, the Claude Desktop tool, the workbook and the
Research tab card) before an Ahrefs plan with API access is connected.

- `make_dataset` invents, for every website of a project, a Domain Rating and Ahrefs
  rank, the referring domains linking to it (drawn from one shared pool of invented
  linking websites, so the same sites link to several competitors and a link intersect
  appears; competitors are made stronger than the client) and its pages with the most
  links. It is deterministic: the same project gives the same numbers. A later `period`
  loses a few referring domains and gains a few, so lost domains can be seen.
- The dataset writes the two **website exports** a person would download from Ahrefs:
  Referring domains (UTF-16 with tabs, as Ahrefs exports it) and Best by links (UTF-8
  with commas).
- `serve` answers the four **API endpoints** the module calls (`domain-rating`,
  `backlinks-stats`, `referring-domains`, `pages-by-backlinks`) as JSON, honouring
  `select`, `limit` and `order_by`, on this computer only (127.0.0.1). It wants an
  `Authorization: Bearer` header like Ahrefs. `slow_down_every` answers every n-th call
  with HTTP 429 so pacing and retries can be seen working.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.backlinks.models import RefDomain, TopPage

__all__ = ["DUMMY_NOTE", "Dataset", "DummySite", "make_dataset", "serve"]

DUMMY_NOTE: Final = (
    "DUMMY DATA from the Ahrefs stand-in: invented numbers for trying the module, "
    "not Ahrefs figures"
)

_FIRST: Final = (
    "daily", "smart", "money", "family", "golden", "open", "true", "bright", "city",
    "global", "local", "prime", "clear", "safe", "next", "modern", "wise", "green",
    "north", "metro",
)  # fmt: skip
_SECOND: Final = (
    "finance", "planner", "review", "news", "guide", "advisor", "journal", "times",
    "insider", "digest", "hub", "post", "wire", "blog", "report",
)  # fmt: skip
_TLDS: Final = ("com", "org", "net", "co.uk", "io")
_PAGES: Final = (
    "", "about", "blog/guide", "tools/calculator", "products", "pricing", "news/press",
    "resources/glossary", "blog/how-to-choose", "contact", "careers", "blog/faq",
    "research/report-2026", "partners", "reviews", "help/getting-started",
    "blog/comparison", "investors", "sustainability", "blog/tips",
)  # fmt: skip
_POOL: Final = 260
_LAST_SEEN: Final = "2026-11-20"


class _Draw:
    """Deterministic numbers from a text (the same input always gives the same draw)."""

    def __init__(self, *parts: str) -> None:
        self._seed = hashlib.sha256("\x1f".join(parts).encode()).digest()
        self._n = 0

    def unit(self) -> float:
        self._n += 1
        digest = hashlib.sha256(self._seed + self._n.to_bytes(4, "big")).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    def between(self, low: float, high: float) -> float:
        return low + (high - low) * self.unit()

    def integer(self, low: int, high: int) -> int:
        return min(high, int(self.between(low, high + 1)))


class DummySite(StrictModel):
    """A website of the project as the stand-in knows it."""

    domain: str
    role: Literal["client", "competitor"]
    domain_rating: float
    ahrefs_rank: int
    refdomains: list[RefDomain] = Field(default_factory=list)
    pages: list[TopPage] = Field(default_factory=list)


class Dataset(StrictModel):
    """Everything the stand-in answers with, saved as one JSON file."""

    project_id: str
    period: int = 0
    websites: list[DummySite]
    note: str = DUMMY_NOTE

    def website(self, domain: str) -> DummySite | None:
        """The website with this domain, if it is in the dataset."""
        key = _domain(domain)
        return next((w for w in self.websites if w.domain == key), None)

    # -- the API --------------------------------------------------------------------

    def answer(self, endpoint: str, query: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        """(HTTP status, JSON body) of an API call, as Ahrefs answers it."""
        site = self.website(query.get("target", ""))
        limit = _limit(query.get("limit"))
        if endpoint == "site-explorer/domain-rating":
            return 200, {
                "domain_rating": {
                    "domain_rating": site.domain_rating if site else 0.0,
                    "ahrefs_rank": site.ahrefs_rank if site else None,
                }
            }
        if endpoint == "site-explorer/backlinks-stats":
            refs = site.refdomains if site else []
            live = sum(r.links_to_target for r in refs)
            return 200, {
                "metrics": {
                    "live": live,
                    "all_time": int(live * 1.6),
                    "live_refdomains": len(refs),
                    "all_time_refdomains": int(len(refs) * 1.4),
                }
            }
        if endpoint == "site-explorer/referring-domains":
            rows = [r.model_dump() for r in (site.refdomains if site else [])]
            rows = _ordered(rows, query.get("order_by", "domain_rating:desc"))
            return 200, {"refdomains": _selected(rows[:limit], query.get("select"))}
        if endpoint == "site-explorer/pages-by-backlinks":
            pages = [
                {
                    "url_to": p.url,
                    "title_target": p.title,
                    "refdomains_target": p.referring_domains,
                    "links_to_target": p.links,
                    "url_rating_target": p.url_rating,
                    "http_code_target": p.http_code,
                }
                for p in (site.pages if site else [])
            ]
            pages = _ordered(pages, query.get("order_by", "refdomains_target:desc"))
            return 200, {"pages": _selected(pages[:limit], query.get("select"))}
        return 404, {"error": f"unknown endpoint {endpoint!r}"}

    # -- the exports ---------------------------------------------------------------

    def refdomains_csv(self, domain: str) -> bytes:
        """The "Referring domains" export: UTF-16 with a byte order mark, tab separated."""
        site = self.website(domain)
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter="\t", lineterminator="\r\n")
        writer.writerow(
            [
                "#",
                "Domain",
                "Domain rating",
                "Domain traffic",
                "Referring domains",
                "Linked domains",
                "Links to target",
                "Dofollow links",
                "First seen",
                "Last seen",
            ]
        )
        for i, r in enumerate(site.refdomains if site else [], 1):
            draw = _Draw("export", r.domain)
            writer.writerow(
                [
                    i,
                    r.domain,
                    f"{r.domain_rating:g}" if r.domain_rating is not None else "",
                    draw.integer(0, 90_000),
                    draw.integer(10, 9_000),
                    draw.integer(5, 4_000),
                    r.links_to_target,
                    r.dofollow_links,
                    f"{r.first_seen} 08:14:00" if r.first_seen else "",
                    f"{r.last_seen} 03:02:00" if r.last_seen else "",
                ]
            )
        return buffer.getvalue().encode("utf-16")

    def pages_csv(self, domain: str) -> bytes:
        """The "Best by links" export: UTF-8 with a byte order mark, comma separated."""
        site = self.website(domain)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(
            [
                "#",
                "Page URL",
                "Title",
                "Referring domains",
                "Links to target",
                "URL Rating",
                "HTTP code",
            ]
        )
        for i, p in enumerate(site.pages if site else [], 1):
            writer.writerow(
                [i, p.url, p.title or "", p.referring_domains, p.links, p.url_rating, p.http_code]
            )
        return ("﻿" + buffer.getvalue()).encode("utf-8")

    # -- saving --------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Write the dataset as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Dataset:
        """Read a dataset written by `save`."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def _domain(text: str) -> str:
    host = urlsplit(text if "//" in text else f"//{text}").hostname or text
    return host.casefold().removeprefix("www.")


def _limit(text: str | None) -> int:
    try:
        return max(1, min(100_000, int(text or "1000")))
    except ValueError:
        return 1000


def _ordered(rows: list[dict[str, Any]], order_by: str) -> list[dict[str, Any]]:
    field, _, direction = order_by.partition(":")
    if not rows or field not in rows[0]:
        return rows

    def key(row: dict[str, Any]) -> float:
        value = row.get(field)
        return float(value) if isinstance(value, int | float) else float("-inf")

    return sorted(rows, key=key, reverse=direction != "asc")


def _selected(rows: list[dict[str, Any]], select: str | None) -> list[dict[str, Any]]:
    if not select:
        return rows
    wanted = [f.strip() for f in select.split(",") if f.strip()]
    return [{f: r.get(f) for f in wanted} for r in rows]


def _pool() -> list[tuple[str, float, bool]]:
    """The invented linking websites: (domain, Domain Rating, spam), the same every time."""
    out: list[tuple[str, float, bool]] = []
    seen: set[str] = set()
    n = 0
    while len(out) < _POOL:
        draw = _Draw("pool", str(n))
        n += 1
        name = (
            _FIRST[draw.integer(0, len(_FIRST) - 1)]
            + _SECOND[draw.integer(0, len(_SECOND) - 1)]
            + "."
            + _TLDS[draw.integer(0, len(_TLDS) - 1)]
        )
        if name in seen:
            continue
        seen.add(name)
        rating = round(min(95.0, 5 + 90 * draw.unit() ** 1.6), 0)
        out.append((name, rating, draw.unit() < 0.05))
    return out


def _date(draw: _Draw) -> str:
    year = draw.integer(2016, 2026)
    month = draw.integer(1, 10 if year == 2026 else 12)
    return f"{year}-{month:02d}-{draw.integer(1, 28):02d}"


def make_dataset(
    project_id: str, websites: Sequence[tuple[str, str]], *, period: int = 0
) -> Dataset:
    """Invent backlink data for (domain, role) websites; deterministic for the same input."""
    pool = _pool()
    sites: list[DummySite] = []
    for index, (raw, role) in enumerate(websites):
        domain = _domain(raw)
        draw = _Draw(project_id, domain)
        strength = (
            draw.between(0.25, 0.4) if role == "client" else draw.between(0.55, 0.8) - 0.05 * index
        )
        strength = max(0.2, strength)
        rating = round(20 + 60 * strength + draw.between(-3, 3), 0)
        refs: list[RefDomain] = []
        for name, dr, spam in pool:
            link = _Draw(domain, name)
            # stronger linking websites link to strong websites more often
            chance = strength * (0.35 + 0.4 * dr / 100)
            linked = link.unit() < chance
            if period:
                change = _Draw(domain, name, str(period)).unit()
                if linked and change < 0.06:  # lost this period
                    linked = False
                elif not linked and change < 0.03:  # new this period
                    linked = True
            if not linked:
                continue
            links = link.integer(1, 40)
            refs.append(
                RefDomain(
                    domain=name,
                    domain_rating=dr,
                    dofollow_links=max(0, links - link.integer(0, links)),
                    links_to_target=links,
                    first_seen=_date(link),
                    last_seen=_LAST_SEEN,
                    is_spam=spam,
                )
            )
        refs.sort(key=lambda r: (-(r.domain_rating or 0.0), r.domain))
        pages: list[TopPage] = []
        top = max(3, len(refs) // 3)
        for rank, path in enumerate(_PAGES):
            page = _Draw(domain, "page", path)
            referring = max(1, int(top / (rank + 1) ** 0.9))
            pages.append(
                TopPage(
                    url=f"https://www.{domain}/{path}",
                    title=f"{domain.split('.')[0].capitalize()} | "
                    + (path.split("/")[-1].replace("-", " ").capitalize() or "Home"),
                    referring_domains=referring,
                    links=referring * page.integer(1, 6),
                    url_rating=round(min(90.0, 8 + 4 * referring**0.5 + page.between(0, 6)), 0),
                    http_code=404 if path == "careers" else 200,
                )
            )
        sites.append(
            DummySite(
                domain=domain,
                role="client" if role == "client" else "competitor",
                domain_rating=rating,
                ahrefs_rank=int(40_000_000 / (1 + rating) ** 2.2),
                refdomains=refs,
                pages=pages,
            )
        )
    return Dataset(project_id=project_id, period=period, websites=sites)


class _Handler(BaseHTTPRequestHandler):
    dataset: Dataset
    slow_down_every: int = 0
    calls: list[str]
    lock: threading.Lock

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = _json(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - the http.server name
        parts = urlsplit(self.path)
        endpoint = parts.path.strip("/").removeprefix("v3/")
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        with self.lock:
            self.calls.append(endpoint)
            count = len(self.calls)
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth.removeprefix("Bearer ").strip():
            self._send(401, {"error": "Authorization header with a Bearer key is required"})
            return
        if self.slow_down_every and count % self.slow_down_every == 0:
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.end_headers()
            return
        status, body = self.dataset.answer(endpoint, query)
        self._send(status, body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature
        """Quiet: the module logs every call it makes itself."""


def _json(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode("utf-8")


def serve(dataset: Dataset, port: int = 8791, *, slow_down_every: int = 0) -> ThreadingHTTPServer:
    """A stand-in API on 127.0.0.1 (only this computer); call `serve_forever()` on it."""
    handler = type(
        "AhrefsStandinHandler",
        (_Handler,),
        {
            "dataset": dataset,
            "slow_down_every": slow_down_every,
            "calls": [],
            "lock": threading.Lock(),
        },
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
