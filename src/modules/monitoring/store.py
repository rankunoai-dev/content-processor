"""Monitoring checks and watch settings on disk, and the check workbook.

`<app data>/monitoring/<project>/`: every check as `<check id>.json`, the newest as
`latest.json`, the watch settings as `watch.json`, the digest's addresses and sent
alerts as `digest.json` (kept apart: the watch settings show the addresses hidden),
and each check's workbook.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.content_processor.projects.notify import hide_email
from src.modules.monitoring.check import GROWTH_WORDS
from src.modules.monitoring.digest import DigestBook
from src.modules.monitoring.models import MonitorCheck, PageChange, RecrawlPlan, WatchSettings

__all__ = ["MonitorStore", "check_tables", "plan_tables", "write_check_workbook"]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_CHECK_ID = re.compile(r"^check-\d{8}-\d{6}-\d{3}$")


class MonitorStore:
    """Checks and watch settings of every project under `<root>`."""

    def __init__(self, root: Path) -> None:
        """Bind to the folder (usually `<app data>/monitoring`)."""
        self.root = root
        self._lock = threading.Lock()

    def folder(self, project_id: str) -> Path:
        """The project's folder (the id is checked, so it cannot escape the root)."""
        if not _ID.match(project_id):
            raise KeyError(project_id)
        return self.root / project_id

    def save(self, check: MonitorCheck) -> Path:
        """Write the check and make it the latest."""
        folder = self.folder(check.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        text = check.model_dump_json(indent=1)
        path = folder / f"{check.check_id}.json"
        with self._lock:
            path.write_text(text, encoding="utf-8")
            tmp = folder / "latest.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(folder / "latest.json")
        return path

    def latest(self, project_id: str) -> MonitorCheck | None:
        """The newest check, or None."""
        path = self.folder(project_id) / "latest.json"
        if not path.exists():
            return None
        with self._lock:
            return MonitorCheck.model_validate_json(path.read_text(encoding="utf-8"))

    def get(self, project_id: str, check_id: str) -> MonitorCheck:
        """One check; raises `KeyError`."""
        if not _CHECK_ID.match(check_id):
            raise KeyError(check_id)
        path = self.folder(project_id) / f"{check_id}.json"
        if not path.exists():
            raise KeyError(check_id)
        return MonitorCheck.model_validate_json(path.read_text(encoding="utf-8"))

    def history(self, project_id: str) -> list[str]:
        """Every check's id, newest first."""
        folder = self.folder(project_id)
        if not folder.exists():
            return []
        return sorted((p.stem for p in folder.glob("check-*.json")), reverse=True)

    def watch(self, project_id: str) -> WatchSettings:
        """The project's watch settings (defaults when never set)."""
        path = self.folder(project_id) / "watch.json"
        settings = WatchSettings()
        if path.exists():
            with self._lock:
                settings = WatchSettings.model_validate_json(path.read_text(encoding="utf-8"))
        hidden = [hide_email(a) for a in self.digest(project_id).to]
        return settings.model_copy(update={"digest_to": hidden})

    def digest(self, project_id: str) -> DigestBook:
        """Who gets the project's digest, and what was sent (empty when never set)."""
        path = self.folder(project_id) / "digest.json"
        if not path.exists():
            return DigestBook()
        with self._lock:
            return DigestBook.model_validate_json(path.read_text(encoding="utf-8"))

    def set_digest(self, project_id: str, book: DigestBook) -> None:
        """Save the digest's addresses and sent alerts."""
        folder = self.folder(project_id)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = folder / "digest.json.tmp"
            tmp.write_text(book.model_dump_json(indent=1), encoding="utf-8")
            tmp.replace(folder / "digest.json")

    def set_watch(self, project_id: str, settings: WatchSettings) -> WatchSettings:
        """Save the project's watch settings."""
        folder = self.folder(project_id)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = folder / "watch.json.tmp"
            # the addresses live in digest.json; the hidden forms are never written here
            tmp.write_text(settings.model_dump_json(indent=1, exclude={"digest_to"}), "utf-8")
            tmp.replace(folder / "watch.json")
        return self.watch(project_id)

    def save_plan(self, plan: RecrawlPlan) -> Path:
        """Keep a recrawl plan (`plans/<plan id>.json`, the newest as `plans/latest.json`)."""
        folder = self.folder(plan.project_id) / "plans"
        folder.mkdir(parents=True, exist_ok=True)
        text = plan.model_dump_json(indent=1)
        (folder / f"{plan.plan_id}.json").write_text(text, encoding="utf-8")
        tmp = folder / "latest.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(folder / "latest.json")
        return folder / f"{plan.plan_id}.json"

    def latest_plan(self, project_id: str) -> RecrawlPlan | None:
        """The newest recrawl plan, or None."""
        path = self.folder(project_id) / "plans" / "latest.json"
        if not path.exists():
            return None
        return RecrawlPlan.model_validate_json(path.read_text(encoding="utf-8"))

    def workbook(self, check: MonitorCheck) -> Path:
        """Where a check's workbook is written."""
        return self.folder(check.project_id) / f"{check.check_id}_monitoring.xlsx"


_HOW_TO_READ: list[tuple[str, str]] = [
    (
        "What a check compares",
        "For each website, its latest scrape with the scrape that was latest at the previous "
        "check (the first time: the scrape before). Nothing is fetched: a check reads the "
        "scrapes already made.",
    ),
    (
        "Drift",
        "The share of a page's sections (or of a website's pages) that were added, removed "
        "or changed.",
    ),
    (
        "Meaning (old vs new)",
        "How close a changed page's new version is in meaning to its old one, from 0 to 1, "
        "scored with the website's embedding model. Near 1: the same topic, reworded or "
        "extended. Below 0.85: the page now covers a different topic (on a competitor "
        "website this raises a topic drift alert). Empty when no meaning model is available "
        "or the page has too little text.",
    ),
    (
        "Price change",
        "An amount (currency or percentage) in the same wording in both scrapes with a different "
        "value, for example '$25 per month' becoming '$19 per month'.",
    ),
    (
        "SEO changes",
        "Pages whose search-engine facts changed: noindex added or removed, a canonical that "
        "pointed at the page itself now pointing elsewhere, structured-data types removed, "
        "broken or added, strong calls to action added or removed, and more images without "
        "alt text (only when both scrapes have an inventory). Compared only when both scrapes "
        "read the page's stored HTML.",
    ),
    (
        "Rankings",
        "Keywords that moved between the research snapshot the previous check read and the "
        "newest one. Client: left the top 10, or fell 5+ places in the top 20. Competitors: "
        "entered the top 3 for a keyword the client ranks for. Keywords under the project's "
        "minimum volume are left out unless they are buyer searches. 'out' with 'list cut' "
        "means the new list stopped at a line limit, so the keyword may still rank lower.",
    ),
    (
        "Page change seen in the same weeks",
        "What a scrape saw change on the page that ranked, in a comparison that overlaps the "
        "two snapshots' dates. A lead to check, not proof of the cause.",
    ),
    (
        "Links",
        "Each website's Domain Rating and referring domains between two backlink snapshots, "
        "and the referring domains lost (spam left out; for a cut list, only domains the new "
        "list would have held).",
    ),
    (
        "Competitor moves",
        "Competitor pages that grew by 500+ words, had their title or H1 rewritten, gained "
        "new heading terms, new tables, pricing blocks, FAQs or forms, or changed a price. "
        "Biggest growth first.",
    ),
    (
        "Template changes",
        "A line that appeared on (or disappeared from) many pages at once: navigation, "
        "footer, related links. Counted once here instead of on every page.",
    ),
    (
        "Theme drift",
        "New pages placed in the project's theme map, removed pages in the previous map, and "
        "each theme's ownership before and after.",
    ),
    (
        "Alert severity",
        "high: a competitor push into a client theme, a theme moving away from the client, a "
        "competitor price change, client pages removed. medium: a competitor building a new "
        "theme, competitor title/H1 rewrites, pillar pages changing a lot, client price "
        "changes. low and info: competitor pages removed, client content changes, ownership "
        "moving towards the client.",
    ),
]


def _competitor_move(page: PageChange) -> list[str]:
    """Why a competitor page change is worth a look (empty: it is not)."""
    why: list[str] = []
    if page.words_delta >= GROWTH_WORDS:
        why.append(f"added {page.words_delta:,} words")
    if "h1" in page.head_changes or "title" in page.head_changes:
        why.append("rewrote " + " and ".join(k for k in ("title", "h1") if k in page.head_changes))
    if page.new_terms:
        why.append("new heading terms")
    blocks = [f"+{n} {k}" for k, n in sorted(page.blocks_added.items()) if n > 0]
    if blocks:
        why.append("new " + ", ".join(blocks))
    if page.prices:
        why.append(f"{len(page.prices)} price change(s)")
    return why


def check_tables(check: MonitorCheck) -> list[Table]:
    """The workbook's sheets."""
    alerts: list[dict[str, Cell]] = [
        {
            "Severity": a.severity,
            "Alert": a.title,
            "Website": a.website,
            "Detail": a.detail,
            "Pages": "\n".join(a.urls[:10]),
        }
        for a in check.alerts
    ]
    sites: list[dict[str, Cell]] = [
        {
            "Website": d.name,
            "Role": d.role,
            "Compared": f"{d.baseline_run or '-'} → {d.latest_run or '-'}",
            "New pages": len(d.new_pages),
            "Removed pages": len(d.removed_pages),
            "Changed pages": len(d.changed_pages),
            "Unchanged pages": d.unchanged_pages,
            "Template changes": len(d.template_changes),
            "Drift": d.drift,
            "Meaning model": d.meaning_model or "",
            "Note": "; ".join(n for n in (d.note, d.meaning_note) if n),
        }
        for d in check.websites
    ]
    changed: list[dict[str, Cell]] = [
        {
            "Website": d.website,
            "URL": c.url,
            "Title": c.title or "",
            "Theme": c.theme or "",
            "Pillar": "yes" if c.is_pillar else "",
            "Drift": c.drift,
            "Meaning (old vs new)": c.meaning if c.meaning is not None else "",
            "Sections added": c.sections_added,
            "Sections removed": c.sections_removed,
            "Words before": c.words_before,
            "Words after": c.words_after,
            "Words added": c.words_delta,
            "Headings added": "\n".join(c.headings_added),
            "Headings removed": "\n".join(c.headings_removed),
            "New heading terms": ", ".join(c.new_terms),
            "Dropped heading terms": ", ".join(c.dropped_terms),
            "Blocks added": ", ".join(f"+{n} {k}" for k, n in sorted(c.blocks_added.items())),
            "Blocks removed": ", ".join(f"-{n} {k}" for k, n in sorted(c.blocks_removed.items())),
            "Head changes": "; ".join(
                f"{k}: {v.get('before')} → {v.get('after')}" for k, v in c.head_changes.items()
            ),
            "Prices": "; ".join(f"{p.before} → {p.after} ({p.context})" for p in c.prices),
            "SEO regressions": "; ".join(c.seo_regressions),
            "Schema added": ", ".join(c.schema_added),
            "Calls to action added": ", ".join(c.ctas_added),
            "Added text (start)": "\n".join(t[:300] for t in c.added_text[:3]),
        }
        for d in check.websites
        for c in d.changed_pages
    ]
    seo: list[dict[str, Cell]] = [
        {
            "Website": d.name,
            "Role": d.role,
            "URL": c.url,
            "Title": c.title or "",
            "Pillar": "yes" if c.is_pillar else "",
            "Noindex": c.noindex or "",
            "Canonical moved": "yes" if c.canonical_moved else "",
            "Schema removed": ", ".join(c.schema_removed),
            "Schema broken": "yes" if c.schema_broken else "",
            "Schema added": ", ".join(c.schema_added),
            "Images without alt (before → after)": f"{c.images_missing_alt[0]} → "
            f"{c.images_missing_alt[1]}"
            if c.images_missing_alt
            else "",
            "Calls to action added": ", ".join(c.ctas_added),
            "Calls to action removed": ", ".join(c.ctas_removed),
        }
        for d in check.websites
        for c in d.changed_pages
        if c.seo_regressions or c.schema_added or c.ctas_added or c.ctas_removed or c.noindex
    ]
    signals = check.signals
    rankings: list[dict[str, Cell]] = [
        {
            "Website": r.name,
            "Role": r.role,
            "Keyword": r.keyword,
            "Before": r.before if r.before is not None else "",
            "After": r.after if r.after is not None else "out",
            "Uncertain": "list cut: may rank lower" if r.uncertain else "",
            "Volume": r.volume,
            "Intent": r.intent or "",
            "Theme": r.theme or "",
            "Client position": r.client_position if r.client_position is not None else "",
            "URL before": r.url_before or "",
            "URL after": r.url_after or "",
            "Page change seen in the same weeks": r.page_change,
        }
        for r in (signals.rankings if signals else [])
    ]
    links: list[dict[str, Cell]] = [
        {
            "Website": x.name,
            "Role": x.role,
            "DR before": x.dr_before if x.dr_before is not None else "",
            "DR after": x.dr_after if x.dr_after is not None else "",
            "Referring domains before": x.refdomains_before
            if x.refdomains_before is not None
            else "",
            "Referring domains after": x.refdomains_after if x.refdomains_after is not None else "",
            "Lost": len(x.lost),
            "Strongest lost": ", ".join(x.lost_strong[:15]),
            "Lost domains": ", ".join(x.lost[:60]),
            "Note": x.note,
        }
        for x in (signals.links if signals else [])
    ]
    basis: list[dict[str, Cell]] = (
        [
            {
                "Source": "Research",
                "Compared": f"{signals.research_before or '-'} → {signals.research_after or '-'}",
                "Note": signals.research_note,
            },
            {
                "Source": "Backlinks",
                "Compared": f"{signals.backlinks_before or '-'} → {signals.backlinks_after or '-'}",
                "Note": signals.backlinks_note,
            },
        ]
        if signals
        else []
    )
    basis += [{"Source": "Check", "Compared": "", "Note": n} for n in check.notes]
    pages: list[dict[str, Cell]] = [
        {
            "Website": d.website,
            "Change": kind,
            "URL": p.url,
            "Title": p.title or "",
            "Theme": p.theme or "",
        }
        for d in check.websites
        for kind, refs in (("new", d.new_pages), ("removed", d.removed_pages))
        for p in refs
    ]
    drift: list[dict[str, Cell]] = [
        {
            "Theme": t.label,
            "Status": t.status,
            "Ownership before": t.ownership_before or "",
            "Ownership after": t.ownership_after or "",
            "New pages": ", ".join(f"{k} {v}" for k, v in sorted(t.new_pages.items())),
            "Removed pages": ", ".join(f"{k} {v}" for k, v in sorted(t.removed_pages.items())),
            "Pages before": ", ".join(f"{k} {v}" for k, v in sorted(t.pages_before.items())),
            "Pages after": ", ".join(f"{k} {v}" for k, v in sorted(t.pages_after.items())),
        }
        for t in check.theme_drift
    ]
    prices: list[dict[str, Cell]] = [
        {
            "Website": d.name,
            "Role": d.role,
            "URL": c.url,
            "Title": c.title or "",
            "Theme": c.theme or "",
            "Before": p.before,
            "After": p.after,
            "Context": p.context,
        }
        for d in check.websites
        for c in d.changed_pages
        for p in c.prices
    ]
    templates: list[dict[str, Cell]] = [
        {
            "Website": d.name,
            "Role": d.role,
            "Line": t.line,
            "Added on pages": t.added_on,
            "Removed on pages": t.removed_on,
        }
        for d in check.websites
        for t in d.template_changes
    ]
    moves: list[dict[str, Cell]] = []
    for d in check.websites:
        if d.role != "competitor":
            continue
        for c in d.changed_pages:
            why = _competitor_move(c)
            if why:
                moves.append(
                    {
                        "Competitor": d.name,
                        "URL": c.url,
                        "Title": c.title or "",
                        "Theme": c.theme or "",
                        "What they did": "; ".join(why),
                        "Words added": c.words_delta,
                        "New heading terms": ", ".join(c.new_terms),
                        "Headings added": "\n".join(c.headings_added),
                    }
                )
    moves.sort(key=lambda r: -int(r["Words added"] or 0))
    wrap = frozenset({"Detail", "Pages", "Added text (start)", "Context", "Line", "Headings added"})
    return [
        Table(
            "Alerts",
            ["Severity", "Alert", "Website", "Detail", "Pages"],
            alerts,
            wrap,
            {"Alert": 50, "Detail": 70, "Pages": 60},
        ),
        Table("Websites", list(sites[0]) if sites else ["Website"], sites),
        Table(
            "Changed pages",
            list(changed[0]) if changed else ["URL"],
            changed,
            wrap,
            {"URL": 60, "Added text (start)": 70},
        ),
        Table(
            "New and removed pages",
            ["Website", "Change", "URL", "Title", "Theme"],
            pages,
            widths={"URL": 70, "Title": 40},
        ),
        Table("Theme drift", list(drift[0]) if drift else ["Theme"], drift),
        Table(
            "Competitor moves",
            [
                "Competitor",
                "URL",
                "Title",
                "Theme",
                "What they did",
                "Words added",
                "New heading terms",
                "Headings added",
            ],
            moves,
            wrap,
            {"URL": 60, "What they did": 50, "Headings added": 50},
        ),
        Table(
            "Price changes",
            ["Website", "Role", "URL", "Title", "Theme", "Before", "After", "Context"],
            prices,
            wrap,
            {"URL": 60, "Context": 60},
        ),
        Table(
            "Template changes",
            ["Website", "Role", "Line", "Added on pages", "Removed on pages"],
            templates,
            wrap,
            {"Line": 80},
        ),
        Table(
            "SEO changes",
            list(seo[0]) if seo else ["Website", "URL"],
            seo,
            wrap,
            {"URL": 60},
        ),
        Table(
            "Rankings",
            list(rankings[0]) if rankings else ["Website", "Keyword"],
            rankings,
            frozenset({"Page change seen in the same weeks"}),
            {"Keyword": 35, "URL before": 50, "Page change seen in the same weeks": 60},
        ),
        Table(
            "Links",
            list(links[0]) if links else ["Website"],
            links,
            frozenset({"Strongest lost", "Lost domains"}),
            {"Strongest lost": 50, "Lost domains": 70},
        ),
        Table("Data compared", ["Source", "Compared", "Note"], basis, widths={"Note": 90}),
        Table(
            "How to read",
            ["Column", "Meaning"],
            [{"Column": k, "Meaning": v} for k, v in _HOW_TO_READ],
            widths={"Meaning": 110},
        ),
    ]


def plan_tables(plan: RecrawlPlan) -> list[Table]:
    """A recrawl plan as sheets: one row per website, one per page."""
    sites: list[dict[str, Cell]] = [
        {
            "Website": s.name,
            "Side": "Client" if s.role == "client" else "Competitor",
            "Address": s.website,
            "Pages": len(s.pages),
            "Core pages": s.core,
            "To check": s.check,
            "Carried forward": s.skip,
            "Sitemap files": len(s.sitemap_files),
            "Sitemap addresses": s.sitemap_addresses,
            "With a date": s.sitemap_dates,
            "Sitemap note": s.sitemap_note,
            "Scrape queued": s.run_id or "",
            "Note": s.note,
        }
        for s in plan.sites
    ]
    pages: list[dict[str, Cell]] = [
        {
            "Website": s.name,
            "URL": p.url,
            "Tier": p.tier,
            "Action": "check" if p.action == "check" else "carry forward",
            "Why": p.reason,
            "Sitemap last modified": p.sitemap_lastmod.date().isoformat()
            if p.sitemap_lastmod
            else "",
            "Last captured": p.last_captured.date().isoformat() if p.last_captured else "",
        }
        for s in plan.sites
        for p in s.pages
    ]
    return [
        Table("Websites", list(sites[0]) if sites else ["Website"], sites),
        Table("Pages", list(pages[0]) if pages else ["URL"], pages),
    ]


def write_check_workbook(check: MonitorCheck, path: Path) -> Path:
    """The check's workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, check_tables(check))
    return path
