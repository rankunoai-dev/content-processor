"""A Semrush stand-in: dummy data in Semrush's own formats, for trying the Research module.

Nothing here is real search data. It lets the whole module run end to end (pulls,
imports, gaps, action cards, Claude Desktop tools, the workbook and the Research tab)
before a Semrush subscription is connected, and it shows what each report looks like.

- `make_dataset` invents, for every website of a project, the keywords it ranks for,
  its domain overview, the top 10 results of every keyword and its organic
  competitors. It is deterministic (the same project gives the same numbers). Keywords
  come from two places: **brand keywords** made from each theme of the project's theme
  map, ranking on that theme's own pages (so they are placed by page), and **market
  keywords** from a list for the industry (`MARKETS`), where competitors are made
  stronger than the client so keyword gaps appear. Volumes use Semrush's own volume
  steps; positions, difficulty, CPC and competition are drawn from plausible ranges.
- The dataset writes the two **website exports** a person would download from Semrush
  (Organic Positions and Organic Competitors, comma-separated with Semrush's column
  names) and answers the five **API reports** the module calls (`domain_ranks`,
  `domain_organic`, `phrase_this`, `phrase_organic`, `domain_organic_organic`) as the
  API does: `;`-separated text with a header line, columns chosen by `export_columns`,
  `display_limit` respected, `ERROR 50 :: NOTHING FOUND` for an unknown domain.
- `serve` answers those reports over HTTP on this computer only (127.0.0.1). Point
  `CP_SEMRUSH_BASE_URL` at it; `slow_down_every` answers every n-th call with HTTP 429
  so the pull's pacing and retries can be seen working.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

__all__ = [
    "DUMMY_NOTE",
    "MARKETS",
    "Dataset",
    "DummyKeyword",
    "DummyWebsite",
    "make_dataset",
    "serve",
]

DUMMY_NOTE: Final = (
    "DUMMY DATA from the Semrush stand-in: invented numbers for trying the module, "
    "not Semrush figures"
)

# Semrush reports search volume in steps; dummy volumes use the same steps.
_VOLUME_STEPS: Final = (
    10, 20, 30, 40, 50, 70, 90, 110, 140, 170, 210, 260, 320, 390, 480, 590, 720, 880,
    1000, 1300, 1600, 1900, 2400, 2900, 3600, 4400, 5400, 6600, 8100, 9900, 12100, 14800,
    18100, 22200, 27100, 33100, 40500, 49500, 60500, 74000, 90500,
)  # fmt: skip

MARKETS: Final[dict[str, tuple[str, ...]]] = {
    "insurance": (
        "life insurance",
        "term life insurance",
        "whole life insurance",
        "universal life insurance",
        "life insurance quotes",
        "best life insurance companies",
        "how much life insurance do i need",
        "life insurance calculator",
        "long term care insurance",
        "long term care insurance cost",
        "annuities",
        "fixed annuity rates",
        "variable annuity",
        "what is an annuity",
        "401k rollover",
        "retirement planning",
        "retirement calculator",
        "ira vs 401k",
        "closed end funds",
        "monthly dividend funds",
        "dividend income fund",
        "mutual funds",
        "life insurance for seniors",
        "no medical exam life insurance",
        "wellness program insurance discount",
        "insurance apple watch",
    ),
    "procurement": (
        "procurement software",
        "source to pay",
        "procure to pay software",
        "supplier risk management",
        "spend analysis",
        "contract management software",
        "e-sourcing",
        "supplier management",
        "procurement outsourcing",
        "supply chain planning",
        "category management",
        "strategic sourcing",
        "accounts payable automation",
        "supplier diversity",
        "purchase order software",
        "procurement analytics",
        "direct procurement",
        "indirect procurement",
        "procurement as a service",
        "supply chain risk",
    ),
    "generic": (
        "pricing",
        "reviews",
        "alternatives",
        "how it works",
        "comparison",
        "best software",
        "free trial",
        "case studies",
        "customer support",
        "demo",
    ),
}

# Websites that fill the top 10 besides the project's own (per market).
_OTHERS: Final[dict[str, tuple[str, ...]]] = {
    "insurance": (
        "nerdwallet.com", "forbes.com", "investopedia.com", "policygenius.com",
        "bankrate.com", "wikipedia.org", "fidelity.com", "nationwide.com",
        "newyorklife.com", "northwesternmutual.com", "massmutual.com", "statefarm.com",
        "aarp.org", "thebalancemoney.com", "lincolnfinancial.com", "transamerica.com",
    ),
    "procurement": (
        "gartner.com", "g2.com", "capterra.com", "forbes.com", "investopedia.com",
        "techtarget.com", "wikipedia.org", "spendmatters.com", "sap.com", "oracle.com",
        "mckinsey.com", "deloitte.com", "procurious.com", "cips.org", "ivalua.com",
        "jaggaer.com",
    ),
    "generic": (
        "g2.com", "capterra.com", "wikipedia.org", "forbes.com", "reddit.com",
        "youtube.com", "medium.com", "trustpilot.com", "quora.com", "linkedin.com",
    ),
}  # fmt: skip

# API column codes (export_columns) and the names Semrush answers with.
COLUMNS: Final[dict[str, str]] = {
    "Ph": "Keyword",
    "Po": "Position",
    "Pp": "Previous Position",
    "Nq": "Search Volume",
    "Cp": "CPC",
    "Ur": "Url",
    "Tr": "Traffic (%)",
    "Co": "Competition",
    "Kd": "Keyword Difficulty",
    "Nr": "Number of Results",
    "Db": "Database",
    "Dn": "Domain",
    "Rk": "Rank",
    "Or": "Organic Keywords",
    "Ot": "Organic Traffic",
    "Oc": "Organic Cost",
    "Cr": "Competitor Relevance",
    "Np": "Common Keywords",
}

# Share of clicks by position (a common click-through curve), for traffic estimates.
_CTR: Final = (0.28, 0.15, 0.10, 0.07, 0.05, 0.04, 0.03, 0.025, 0.02, 0.018)


def _ctr(position: int) -> float:
    return _CTR[position - 1] if position <= len(_CTR) else 0.005 if position <= 20 else 0.001


class _Draw:
    """Deterministic numbers from a text (the same input always gives the same draw)."""

    def __init__(self, *parts: str) -> None:
        self._seed = hashlib.sha256("\x1f".join(parts).encode()).digest()
        self._n = 0

    def unit(self) -> float:
        """A number in [0, 1)."""
        self._n += 1
        digest = hashlib.sha256(self._seed + self._n.to_bytes(4, "big")).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    def between(self, low: float, high: float) -> float:
        return low + (high - low) * self.unit()

    def integer(self, low: int, high: int) -> int:
        return min(high, int(self.between(low, high + 1)))


def _volume(draw: _Draw, low: int, high: int) -> int:
    """A Semrush volume step, spread evenly on a log scale between two steps."""
    lo = math.log(low)
    hi = math.log(high)
    target = math.exp(draw.between(lo, hi))
    return min(_VOLUME_STEPS, key=lambda v: abs(v - target))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:60]


@dataclass
class DummyKeyword:
    """One keyword's market figures and who ranks where for it."""

    keyword: str
    kind: str  # "brand" (from a theme) or "market"
    volume: int
    cpc: float
    competition: float
    difficulty: int
    results: int
    theme: str = ""  # the theme the brand keyword was made from
    positions: dict[str, int] = field(default_factory=dict)  # domain -> position
    previous: dict[str, int] = field(default_factory=dict)  # domain -> previous position
    urls: dict[str, str] = field(default_factory=dict)  # domain -> ranking page
    serp: list[tuple[str, str]] = field(default_factory=list)  # top 10 (domain, url)


@dataclass
class DummyWebsite:
    """A website of the project as the stand-in knows it."""

    domain: str
    role: str  # client or competitor
    strength: float  # 0-1: how well it ranks for market keywords


@dataclass
class Dataset:
    """Everything the stand-in answers with, saved as one JSON file."""

    project_id: str
    market: str
    database: str
    websites: list[DummyWebsite]
    keywords: list[DummyKeyword]
    note: str = DUMMY_NOTE

    # -- per-website views -------------------------------------------------------

    def website(self, domain: str) -> DummyWebsite | None:
        """The website with this domain, if it is in the dataset."""
        key = _domain(domain)
        return next((w for w in self.websites if w.domain == key), None)

    def ranked(self, domain: str) -> list[DummyKeyword]:
        """The keywords a website ranks for (top 100), by its estimated traffic."""
        key = _domain(domain)
        rows = [k for k in self.keywords if key in k.positions]
        return sorted(rows, key=lambda k: -k.volume * _ctr(k.positions[key]))

    def _traffic(self, domain: str) -> list[tuple[DummyKeyword, float]]:
        key = _domain(domain)
        return [(k, k.volume * _ctr(k.positions[key])) for k in self.ranked(domain)]

    def overview(self, domain: str) -> dict[str, str]:
        """Domain overview: its sample scaled up to a whole website's size."""
        traffic = self._traffic(domain)
        site = self.website(domain)
        scale = 40 if site and site.role == "client" else 55
        visits = sum(t for _k, t in traffic) * scale
        cost = sum(t * k.cpc for k, t in traffic) * scale
        draw = _Draw("rank", _domain(domain))
        return {
            "Db": self.database,
            "Dn": _domain(domain),
            "Rk": str(draw.integer(800, 60_000)),
            "Or": str(len(traffic) * scale),
            "Ot": str(round(visits)),
            "Oc": str(round(cost)),
        }

    def organic(self, domain: str) -> list[dict[str, str]]:
        """Its keyword rows (API column codes), by estimated traffic."""
        key = _domain(domain)
        traffic = self._traffic(domain)
        total = sum(t for _k, t in traffic) or 1.0
        return [
            {
                "Ph": k.keyword,
                "Po": str(k.positions[key]),
                "Pp": str(k.previous.get(key, 0)),
                "Nq": str(k.volume),
                "Cp": f"{k.cpc:.2f}",
                "Ur": k.urls[key],
                "Tr": f"{100 * t / total:.2f}",
                "Co": f"{k.competition:.2f}",
                "Kd": str(k.difficulty),
                "Nr": str(k.results),
            }
            for k, t in traffic
        ]

    def competitors(self, domain: str) -> list[dict[str, str]]:
        """Websites sharing its keywords, the project's and others, by overlap."""
        key = _domain(domain)
        mine = {k.keyword for k in self.ranked(key)}
        counts: dict[str, int] = {}
        for k in self.keywords:
            if k.keyword not in mine:
                continue
            # websites of the dataset: every shared keyword in the top 100; others: the
            # keywords they share in the top 10
            near = {d for d in k.positions if d != key} | {d for d, _u in k.serp if d != key}
            for d in near:
                counts[d] = counts.get(d, 0) + 1
        rows = []
        for d, common in sorted(counts.items(), key=lambda c: -c[1]):
            known = self.website(d)
            size = len(self.ranked(d)) if known else _Draw("size", d).integer(30, 200)
            rows.append(
                {
                    "Dn": d,
                    "Cr": f"{min(0.95, 0.7 * common / max(1, len(mine))):.2f}",
                    "Np": str(common * 40),
                    "Or": str(size * 55),
                    "Ot": str(_Draw("traffic", d).integer(20_000, 900_000)),
                }
            )
        return rows

    def phrase(self, phrase: str) -> DummyKeyword | None:
        """One keyword's figures, if the dataset has it."""
        key = phrase.strip().casefold()
        return next((k for k in self.keywords if k.keyword == key), None)

    # -- the API, as text --------------------------------------------------------

    def answer(self, query: Mapping[str, str]) -> str:
        """The API's text answer to one request (header line, `;` separated)."""
        report = query.get("type", "")
        if not query.get("key"):
            return "ERROR 120 :: WRONG KEY - ID PAIR"
        limit = int(query.get("display_limit") or 10_000)
        rows: list[dict[str, str]]
        if report == "domain_ranks":
            site = self.website(query.get("domain", ""))
            rows = [self.overview(site.domain)] if site else []
        elif report == "domain_organic":
            site = self.website(query.get("domain", ""))
            rows = self.organic(site.domain) if site else []
        elif report == "domain_organic_organic":
            site = self.website(query.get("domain", ""))
            rows = self.competitors(site.domain) if site else []
        elif report in ("phrase_this", "phrase_organic"):
            k = self.phrase(query.get("phrase", ""))
            if k is None:
                rows = []
            elif report == "phrase_this":
                rows = [
                    {
                        "Ph": k.keyword,
                        "Nq": str(k.volume),
                        "Cp": f"{k.cpc:.2f}",
                        "Co": f"{k.competition:.2f}",
                        "Nr": str(k.results),
                        "Kd": str(k.difficulty),
                    }
                ]
            else:
                rows = [{"Dn": d, "Ur": u} for d, u in k.serp]
        else:
            return "ERROR 40 :: QUERY TYPE NOT FOUND"
        if not rows:
            return "ERROR 50 :: NOTHING FOUND"
        codes = [c for c in (query.get("export_columns") or "").split(",") if c] or list(rows[0])
        out = io.StringIO()
        writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
        writer.writerow([COLUMNS.get(c, c) for c in codes])
        for row in rows[:limit]:
            writer.writerow([row.get(c, "") for c in codes])
        return out.getvalue()

    # -- the website's CSV exports -----------------------------------------------

    def positions_csv(self, domain: str) -> bytes:
        """Semrush's "Organic Positions" export, as downloaded from the website."""
        header = [
            "Keyword", "Position", "Previous position", "Search Volume",
            "Keyword Difficulty", "CPC", "URL", "Traffic", "Traffic (%)", "Traffic Cost",
            "Competition", "Number of Results", "Timestamp",
        ]  # fmt: skip
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(header)
        for row in self.organic(domain):
            traffic = int(row["Nq"]) * _ctr(int(row["Po"]))
            writer.writerow(
                [
                    row["Ph"], row["Po"], row["Pp"], row["Nq"], row["Kd"], row["Cp"],
                    row["Ur"], round(traffic), row["Tr"], round(traffic * float(row["Cp"])),
                    row["Co"], row["Nr"], "2026-11-01",
                ]
            )  # fmt: skip
        return out.getvalue().encode("utf-8-sig")

    def competitors_csv(self, domain: str) -> bytes:
        """Semrush's "Organic Competitors" export, as downloaded from the website."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(
            ["Domain", "Competitor Relevance", "Common Keywords", "Organic Keywords",
             "Organic Traffic", "Organic Cost", "Adwords Keywords"]
        )  # fmt: skip
        for row in self.competitors(domain):
            writer.writerow([row["Dn"], row["Cr"], row["Np"], row["Or"], row["Ot"], "0", "0"])
        return out.getvalue().encode("utf-8-sig")

    # -- saving --------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Write the dataset as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Dataset:
        """Read a dataset written by `save`."""
        data = json.loads(path.read_text(encoding="utf-8"))
        websites = [DummyWebsite(**w) for w in data.pop("websites")]
        keywords = []
        for k in data.pop("keywords"):
            k["serp"] = [tuple(pair) for pair in k["serp"]]
            keywords.append(DummyKeyword(**k))
        return cls(websites=websites, keywords=keywords, **data)


def _domain(text: str) -> str:
    host = urlsplit(text if "//" in text else f"//{text}").hostname or text
    return host.casefold().removeprefix("www.")


@dataclass
class ThemeSeed:
    """What the stand-in needs from one theme: its words and the client's pages in it."""

    label: str
    terms: Sequence[str]
    pages: Sequence[str]


def make_dataset(
    project_id: str,
    websites: Sequence[tuple[str, str]],
    themes: Sequence[ThemeSeed] = (),
    *,
    market: str = "insurance",
    database: str = "us",
    brand: str | None = None,
    period: int = 0,
) -> Dataset:
    """Invent the search data of a project's websites ((domain, role) pairs).

    `period` 1, 2, ... is a later month: about half the positions move by up to 9
    places, and the previous position is the one of the month before (for movers).
    """
    if market not in MARKETS:
        msg = f"unknown market {market!r}: one of {', '.join(MARKETS)}"
        raise ValueError(msg)
    sites = []
    for domain, role in websites:
        draw = _Draw("strength", _domain(domain))
        strength = draw.between(0.25, 0.45) if role == "client" else draw.between(0.55, 0.85)
        sites.append(DummyWebsite(_domain(domain), role, round(strength, 3)))
    client = next((s for s in sites if s.role == "client"), sites[0] if sites else None)
    name = brand or (client.domain.split(".")[0] if client else "brand")
    keywords: dict[str, DummyKeyword] = {}

    def figures(phrase: str, kind: str, theme: str = "") -> DummyKeyword:
        draw = _Draw("keyword", phrase)
        brand_kw = kind == "brand"
        return DummyKeyword(
            keyword=phrase,
            kind=kind,
            volume=_volume(draw, 20, 1_600) if brand_kw else _volume(draw, 170, 60_500),
            cpc=round(draw.between(0.4, 6.0) if brand_kw else draw.between(2.0, 48.0), 2),
            competition=round(draw.between(0.05, 0.5) if brand_kw else draw.between(0.4, 1.0), 2),
            difficulty=draw.integer(10, 45) if brand_kw else draw.integer(40, 92),
            results=draw.integer(2, 900) * 1_000_000,
            theme=theme,
        )

    # brand keywords: the client's own themes, ranking on that theme's pages
    for seed in themes:
        if client is None or not seed.pages:
            continue
        for term in list(dict.fromkeys(t.strip().casefold() for t in seed.terms if t.strip()))[:3]:
            phrase = f"{name} {term}"
            if phrase in keywords:
                continue
            k = figures(phrase, "brand", seed.label)
            draw = _Draw("brand-pos", phrase)
            page = seed.pages[draw.integer(0, len(seed.pages) - 1)]
            k.positions[client.domain] = draw.integer(1, 4)
            k.urls[client.domain] = page
            for site in sites:
                if site is not client and draw.unit() < 0.3:
                    k.positions[site.domain] = draw.integer(8, 40)
                    k.urls[site.domain] = f"https://www.{site.domain}/{_slug(term)}"
            keywords[phrase] = k

    # market keywords: everyone competes; competitors are made stronger
    real_pages = sorted({page for seed in themes for page in seed.pages})
    for phrase in MARKETS[market]:
        k = figures(phrase, "market")
        for site in sites:
            draw = _Draw("market-pos", phrase, site.domain)
            if draw.unit() > 0.35 + 0.6 * site.strength:
                continue  # does not rank in the top 100
            best = 1 + (1 - site.strength) * 25
            k.positions[site.domain] = max(1, min(100, round(draw.between(best, best * 3.2))))
            if site is client and real_pages:  # the client ranks with a page it really has
                k.urls[site.domain] = real_pages[draw.integer(0, len(real_pages) - 1)]
            else:
                k.urls[site.domain] = f"https://www.{site.domain}/{_slug(phrase)}"
        keywords[phrase] = k

    # previous positions and the top 10 of every keyword
    others = _OTHERS[market]
    for k in keywords.values():
        for domain, position in k.positions.items():
            draw = _Draw("previous", k.keyword, domain)
            moved = draw.integer(-6, 6) if draw.unit() < 0.6 else 0
            k.previous[domain] = 0 if draw.unit() < 0.08 else max(1, position + moved)
    dataset = Dataset(
        project_id=project_id,
        market=market,
        database=database,
        websites=sites,
        keywords=list(keywords.values()),
    )
    for n in range(1, period + 1):  # later months: some positions move
        for k in dataset.keywords:
            for domain, old in list(k.positions.items()):
                draw = _Draw("period", str(n), k.keyword, domain)
                k.previous[domain] = old
                if draw.unit() < 0.5:
                    k.positions[domain] = max(1, min(100, old + draw.integer(-9, 9)))
    for k in dataset.keywords:
        k.serp = _serp(k, others)
    return dataset


def _serp(k: DummyKeyword, others: Sequence[str]) -> list[tuple[str, str]]:
    """The top 10: the websites that rank there, the rest filled by other websites."""
    slots: dict[int, tuple[str, str]] = {}
    for domain, position in sorted(k.positions.items(), key=lambda p: p[1]):
        if position <= 10:
            spot = position
            while spot in slots and spot < 10:
                spot += 1
            slots.setdefault(spot, (domain, k.urls[domain]))
    draw = _Draw("serp", k.keyword)
    pool = [d for d in others if d not in k.positions]
    for rank in range(1, 11):
        if rank not in slots and pool:  # a different few fillers for each keyword
            d = pool.pop(draw.integer(0, len(pool) - 1))
            slots[rank] = (d, f"https://www.{d}/{_slug(k.keyword)}")
    return [slots[r] for r in sorted(slots)]


class _Handler(BaseHTTPRequestHandler):
    dataset: Dataset
    slow_down_every: int = 0
    calls: list[str]
    lock: threading.Lock

    def do_GET(self) -> None:  # noqa: N802 - the http.server name
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        with self.lock:
            self.calls.append(query.get("type", ""))
            count = len(self.calls)
        if self.slow_down_every and count % self.slow_down_every == 0:
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.end_headers()
            return
        body = self.dataset.answer(query).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature
        """Quiet: the module logs every call it makes itself."""


def serve(dataset: Dataset, port: int = 8790, *, slow_down_every: int = 0) -> ThreadingHTTPServer:
    """A stand-in API on 127.0.0.1 (only this computer); call `serve_forever()` on it."""
    handler = type(
        "StandinHandler",
        (_Handler,),
        {
            "dataset": dataset,
            "slow_down_every": slow_down_every,
            "calls": [],
            "lock": threading.Lock(),
        },
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
