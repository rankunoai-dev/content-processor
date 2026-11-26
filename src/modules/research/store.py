"""Research snapshots on disk and the research workbook.

`<app data>/research/<project>/`: every snapshot as `<snapshot id>.json`, the newest
as `latest.json`, each snapshot's workbook; `<app data>/research/units.jsonl` logs
every Semrush call.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.research.build import estimated_visits
from src.modules.research.gsc import GscSnapshot
from src.modules.research.intent import INTENTS
from src.modules.research.models import Mover, ResearchSnapshot
from src.modules.research.page_models import PageReport

__all__ = ["ResearchStore", "research_tables", "write_research_workbook"]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_SNAPSHOT_ID = re.compile(r"^research-\d{8}-\d{6}-\d{3}$")


class ResearchStore:
    """Snapshots of every project under `<root>`."""

    def __init__(self, root: Path) -> None:
        """Bind to the folder (usually `<app data>/research`)."""
        self.root = root
        self._lock = threading.Lock()

    def folder(self, project_id: str) -> Path:
        """The project's folder (the id is checked, so it cannot escape the root)."""
        if not _ID.match(project_id):
            raise KeyError(project_id)
        return self.root / project_id

    def save(self, snapshot: ResearchSnapshot) -> Path:
        """Write the snapshot and make it the latest."""
        folder = self.folder(snapshot.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        text = snapshot.model_dump_json(indent=1)
        path = folder / f"{snapshot.snapshot_id}.json"
        with self._lock:
            path.write_text(text, encoding="utf-8")
            tmp = folder / "latest.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(folder / "latest.json")
        return path

    def latest(self, project_id: str) -> ResearchSnapshot | None:
        """The newest snapshot, or None."""
        path = self.folder(project_id) / "latest.json"
        if not path.exists():
            return None
        with self._lock:
            return ResearchSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def get(self, project_id: str, snapshot_id: str) -> ResearchSnapshot:
        """One snapshot; raises `KeyError`."""
        if not _SNAPSHOT_ID.match(snapshot_id):
            raise KeyError(snapshot_id)
        path = self.folder(project_id) / f"{snapshot_id}.json"
        if not path.exists():
            raise KeyError(snapshot_id)
        return ResearchSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def history(self, project_id: str) -> list[str]:
        """Every snapshot's id, newest first."""
        folder = self.folder(project_id)
        if not folder.exists():
            return []
        return sorted((p.stem for p in folder.glob("research-*.json")), reverse=True)

    def previous(self, project_id: str) -> ResearchSnapshot | None:
        """The snapshot before the latest one, or None."""
        history = self.history(project_id)
        return self.get(project_id, history[1]) if len(history) > 1 else None

    def workbook(self, snapshot: ResearchSnapshot) -> Path:
        """Where a snapshot's workbook is written."""
        return self.folder(snapshot.project_id) / f"{snapshot.snapshot_id}_research.xlsx"

    def _write_latest(self, project_id: str, name: str, text: str) -> Path:
        folder = self.folder(project_id)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = folder / f"{name}.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(folder / name)
        return folder / name

    def _read_latest(self, project_id: str, name: str) -> str | None:
        path = self.folder(project_id) / name
        if not path.exists():
            return None
        with self._lock:
            return path.read_text(encoding="utf-8")

    def save_gsc(self, snapshot: GscSnapshot) -> Path:
        """Keep a Search Console import (one per project: the newest replaces the last)."""
        return self._write_latest(
            snapshot.project_id, "gsc-latest.json", snapshot.model_dump_json(indent=1)
        )

    def latest_gsc(self, project_id: str) -> GscSnapshot | None:
        """The newest Search Console import, or None."""
        text = self._read_latest(project_id, "gsc-latest.json")
        return GscSnapshot.model_validate_json(text) if text else None

    def save_pages(self, report: PageReport) -> Path:
        """Keep the newest page report."""
        return self._write_latest(
            report.project_id, "pages-latest.json", report.model_dump_json(indent=1)
        )

    def latest_pages(self, project_id: str) -> PageReport | None:
        """The newest page report, or None."""
        text = self._read_latest(project_id, "pages-latest.json")
        return PageReport.model_validate_json(text) if text else None

    def pages_workbook(self, project_id: str) -> Path:
        """Where the page report's workbook is written."""
        return self.folder(project_id) / "pages_report.xlsx"


_HOW_TO_READ: list[tuple[str, str]] = [
    (
        "Source",
        "semrush_api: pulled with the Semrush API (units logged); csv_import: a Semrush "
        "Organic Positions export imported by hand.",
    ),
    ("Volume", "Average monthly searches of the keyword in the chosen country database."),
    (
        "Estimated visits",
        "Volume times a typical click-through rate for the position (28% for #1 down to 2% "
        "for #10, 1% on page two). An estimate, for comparing websites, not a measurement.",
    ),
    (
        "Theme",
        "The theme of the project's theme map the keyword belongs to: by its ranking page "
        "when that page is in the map, otherwise by the theme sharing most of its words.",
    ),
    (
        "Movers",
        "Keywords that moved 3 places or more, entered or left the top 10, appeared or "
        "disappeared since the previous snapshot.",
    ),
]


def research_tables(snapshot: ResearchSnapshot, moves: list[Mover] | None = None) -> list[Table]:
    """The workbook's sheets."""
    sites: list[dict[str, Cell]] = [
        {
            "Website": w.name,
            "Domain": w.website,
            "Role": w.role,
            "Source": w.source,
            "Organic keywords": w.overview.organic_keywords if w.overview else None,
            "Organic traffic": w.overview.organic_traffic if w.overview else None,
            "Traffic value (USD)": w.overview.organic_cost if w.overview else None,
            "Keywords in this snapshot": len(w.keywords),
            "Top 10": sum(1 for r in w.keywords if r.position <= 10),
            "Note": w.note,
        }
        for w in snapshot.websites
    ]
    labels = {d.theme_id: d.label for d in snapshot.demand}
    keywords: list[dict[str, Cell]] = [
        {
            "Website": w.website,
            "Keyword": r.keyword,
            "Position": r.position,
            "Previous": r.previous_position,
            "Volume": r.volume,
            "CPC": r.cpc,
            "Estimated visits": estimated_visits(r.volume, r.position),
            "URL": r.url or "",
            "Theme": labels.get(r.theme_id, "") if r.theme_id else "",
            "Intent": r.intent or "",
            "Other pages ranking": ", ".join(r.other_urls),
        }
        for w in snapshot.websites
        for r in w.keywords
    ]
    site_keys = [w.website for w in snapshot.websites]
    demand: list[dict[str, Cell]] = [
        {
            "Theme": d.label,
            "Keywords": d.keywords,
            "Volume": d.volume,
            "Weighted by intent": d.weighted_volume,
            **{f"{k.capitalize()} volume": d.by_intent.get(k, 0) for k in INTENTS},
            **{f"Top 10: {s}": d.top10.get(s, 0) for s in site_keys},
            **{f"Visits: {s}": d.traffic.get(s, 0.0) for s in site_keys},
        }
        for d in snapshot.demand
    ]
    movers_rows: list[dict[str, Cell]] = [
        {
            "Website": m.website,
            "Keyword": m.keyword,
            "Before": m.before,
            "After": m.after,
            "Volume": m.volume,
            "URL": m.url or "",
        }
        for m in moves or []
    ]
    serps: list[dict[str, Cell]] = [
        {
            "Keyword": k.keyword,
            "Volume": k.volume,
            "CPC": k.cpc,
            "Rank": s.rank,
            "Domain": s.domain,
            "URL": s.url,
        }
        for k in snapshot.keywords
        for s in k.serp
    ]
    return [
        Table("Websites", list(sites[0]) if sites else ["Website"], sites),
        Table(
            "Keywords",
            list(keywords[0]) if keywords else ["Keyword"],
            keywords,
            widths={"Keyword": 40, "URL": 60},
        ),
        Table(
            "Theme demand", list(demand[0]) if demand else ["Theme"], demand, widths={"Theme": 40}
        ),
        Table("Movers", ["Website", "Keyword", "Before", "After", "Volume", "URL"], movers_rows),
        Table("Search results", ["Keyword", "Volume", "CPC", "Rank", "Domain", "URL"], serps),
        Table(
            "How to read",
            ["Column", "Meaning"],
            [{"Column": k, "Meaning": v} for k, v in _HOW_TO_READ],
            widths={"Meaning": 110},
        ),
    ]


def write_research_workbook(
    snapshot: ResearchSnapshot, path: Path, moves: list[Mover] | None = None
) -> Path:
    """The snapshot's workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, research_tables(snapshot, moves))
    return path


_PAGES_HOW_TO_READ: list[tuple[str, str]] = [
    (
        "Pages",
        "Every scraped page and every page that ranks, with what it ranks for, its links and "
        "its health.",
    ),
    (
        "Estimated visits",
        "Volume times a typical click-through rate for the position: an estimate for "
        "comparing pages.",
    ),
    (
        "Commercial volume",
        "Searches of its keywords tagged commercial or transactional by rules "
        "(research/intent.py).",
    ),
    (
        "Links from page text",
        "Other scraped pages linking to it from their content, not from menus, header, footer "
        "or breadcrumbs. Links on pages outside the scrape are unknown.",
    ),
    (
        "PageRank",
        "Where internal link equity collects, from links in page text; 100 = the strongest page.",
    ),
    (
        "Flags",
        "On-page problems from the scrape: title, meta description, H1s, skipped headings, "
        "noindex, thin text (under 300 words).",
    ),
    (
        "Cannibalisation",
        "Two or more pages compete for one keyword: several rows in the Semrush data, the "
        "ranking page changing since the last snapshot, or several pages sharing a query's "
        "impressions in Search Console.",
    ),
    ("Stuck pages", "Ranking 4-20 with on-page problems: fixing them is the cheapest way up."),
    (
        "Link suggestions",
        "A stronger page of the same theme that does not link to a page ranking 4-20 yet, "
        "with the target's keyword as anchor text.",
    ),
    (
        "CTR opportunities",
        "Search Console: page-one pages or queries whose real CTR is under 60% of what the "
        "position usually gets; rewrite the title and description. Extra clicks = impressions "
        "x (usual - real CTR).",
    ),
]


def page_tables(report: PageReport) -> list[Table]:
    """The page report's sheets."""
    pages: list[dict[str, Cell]] = [
        {
            "URL": r.url,
            "Title": r.title or "",
            "Theme": r.theme or "",
            "Keywords": r.keywords,
            "Volume": r.volume,
            "Estimated visits": r.visits,
            "Average position": r.average_position,
            "Best position": r.best_position,
            "Top keyword": r.top_keyword or "",
            "Commercial volume": r.commercial_volume,
            "Links from page text": r.inbound_body_links,
            "Links from menus only": r.inbound_chrome_links,
            "PageRank": r.pagerank,
            "Words": r.word_count,
            "Flags": "; ".join(r.flags),
            "GSC clicks": r.gsc_clicks,
            "GSC impressions": r.gsc_impressions,
            "GSC CTR": r.gsc_ctr,
            "GSC position": r.gsc_position,
        }
        for r in report.pages
    ]
    cannibal: list[dict[str, Cell]] = [
        {
            "Keyword": c.keyword,
            "Volume": c.volume,
            "Intent": c.intent or "",
            "Pages": "\n".join(c.pages),
            "Positions": ", ".join(str(p) for p in c.positions),
            "Found by": c.source,
            "Note": c.note,
        }
        for c in report.cannibalisation
    ]
    links: list[dict[str, Cell]] = [
        {
            "Link from": s.source_url,
            "Its PageRank": s.source_pagerank,
            "Link to": s.target_url,
            "Anchor text": s.anchor,
            "Position": s.keyword_position,
            "Volume": s.keyword_volume,
            "Theme": s.theme or "",
            "Why": s.reason,
        }
        for s in report.link_suggestions
    ]
    by_url = {r.url: r for r in report.pages}
    stuck: list[dict[str, Cell]] = [
        {
            "URL": u,
            "Best position": by_url[u].best_position if u in by_url else None,
            "Volume": by_url[u].volume if u in by_url else None,
            "Top keyword": (by_url[u].top_keyword or "") if u in by_url else "",
            "Flags": "; ".join(by_url[u].flags) if u in by_url else "",
        }
        for u in report.stuck
    ]
    ctr: list[dict[str, Cell]] = [
        {
            "Page or query": o.label,
            "Kind": o.kind,
            "Impressions": o.impressions,
            "Clicks": o.clicks,
            "CTR": o.ctr,
            "Usual CTR": o.expected_ctr,
            "Position": o.position,
            "Extra clicks": o.extra_clicks,
        }
        for o in report.ctr_opportunities
    ]
    named: list[tuple[str, list[str]]] = [
        ("No links from page text", report.orphans),
        ("One link from page text", report.near_orphans),
        ("Ranks for nothing", report.zero_traffic),
    ]
    lists: list[dict[str, Cell]] = [{"List": name, "URL": u} for name, urls in named for u in urls]
    return [
        Table(
            "Pages", list(pages[0]) if pages else ["URL"], pages, widths={"URL": 60, "Title": 40}
        ),
        Table(
            "Cannibalisation",
            ["Keyword", "Volume", "Intent", "Pages", "Positions", "Found by", "Note"],
            cannibal,
            wrap_columns=frozenset({"Pages"}),
        ),
        Table(
            "Stuck pages",
            ["URL", "Best position", "Volume", "Top keyword", "Flags"],
            stuck,
            widths={"URL": 60},
        ),
        Table(
            "Link suggestions",
            [
                "Link from",
                "Its PageRank",
                "Link to",
                "Anchor text",
                "Position",
                "Volume",
                "Theme",
                "Why",
            ],
            links,
            widths={"Link from": 50, "Link to": 50},
        ),
        Table(
            "CTR opportunities",
            [
                "Page or query",
                "Kind",
                "Impressions",
                "Clicks",
                "CTR",
                "Usual CTR",
                "Position",
                "Extra clicks",
            ],
            ctr,
            widths={"Page or query": 60},
        ),
        Table("Link and traffic lists", ["List", "URL"], lists, widths={"URL": 80}),
        Table("Notes", ["Note"], [{"Note": n} for n in report.notes], widths={"Note": 120}),
        Table(
            "How to read",
            ["Column", "Meaning"],
            [{"Column": k, "Meaning": v} for k, v in _PAGES_HOW_TO_READ],
            widths={"Meaning": 110},
        ),
    ]


def write_page_workbook(report: PageReport, path: Path) -> Path:
    """The page report's workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, page_tables(report))
    return path
