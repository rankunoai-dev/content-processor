"""Which reports a project has, and how each is built from its module's latest data.

Every report is built on request from what the module last saved (no new scrape, pull
or analysis, no cost). A report whose module has nothing yet is listed as not
available, with what to do first.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from src.modules.content_processor.export.tabular import Cell, Table
from src.modules.content_processor.projects.models import ProjectRecord, RunState
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.reports.writers import Report

__all__ = ["REPORTS", "ReportDef", "build_report", "report_def"]

_DATE: Final = "%d %b %Y %H:%M UTC"


def _when(moment: datetime) -> str:
    return moment.strftime(_DATE).lstrip("0")


@dataclass(frozen=True)
class Context:
    """What builders read."""

    projects: ProjectStore
    project: ProjectRecord
    analyst_runs: Callable[[str], list[object]] | None = None


@dataclass(frozen=True)
class ReportDef:
    """One kind of report."""

    key: str
    module: str
    title: str
    description: str
    missing: str
    build: Callable[[Context], Report | None]


def _overview(ctx: Context) -> Report:
    p = ctx.project
    runs = ctx.projects.runs(p.project_id)
    names = {s.site_id: s.name for s in p.sites}
    last: dict[str, RunState] = {}
    for r in sorted(runs, key=lambda r: r.created_at):
        last[r.site_id] = r
    sites: list[dict[str, Cell]] = []
    for s in p.sites:
        run = last.get(s.site_id)
        sites.append(
            {
                "Website": s.name,
                "Side": "Client" if s.role == "client" else "Competitor",
                "Addresses": ", ".join(s.domains),
                "Note": s.note,
                "Tags": ", ".join(s.tags),
                "Addresses in its list": ctx.projects.url_count(p.project_id, s.site_id),
                "Last scrape": _when(run.created_at) if run else "",
                "Last scrape status": run.status if run else "",
            }
        )
    scrapes: list[dict[str, Cell]] = [
        {
            "Website": names.get(r.site_id, r.site_id),
            "Scrape": r.run_id,
            "Status": r.status,
            "Created": _when(r.created_at),
            "Finished": _when(r.finished_at) if r.finished_at else "",
            "Addresses": r.url_count,
            "Pages done": r.pages_done,
            "Results": ", ".join(f"{k} {v}" for k, v in sorted(r.results.items())),
            "Reports ready": r.reports_ready,
            "Message": r.message,
        }
        for r in sorted(runs, key=lambda r: r.created_at, reverse=True)
    ]
    clients = sum(1 for s in p.sites if s.role == "client")
    return Report(
        key="overview",
        title=f"{p.name}: project overview",
        subtitle=f"Client {p.client_name} · owner {p.owner_name} · created {_when(p.created_at)}",
        summary=[
            f"{clients} client website(s) and {len(p.sites) - clients} competitor website(s).",
            f"{len(runs)} scrape(s); {sum(1 for r in runs if r.status == 'finished')} finished.",
        ],
        tables=[
            Table("Websites", list(sites[0]) if sites else ["Website"], sites),
            Table(
                "Scrapes",
                list(scrapes[0]) if scrapes else ["Website", "Scrape", "Status"],
                scrapes,
            ),
        ],
    )


def _themes(ctx: Context) -> Report | None:
    from src.modules.theming.service import theme_store
    from src.modules.theming.store import theme_tables

    found = theme_store(ctx.projects).latest(ctx.project.project_id)
    if found is None:
        return None
    return Report(
        key="themes",
        title=f"{ctx.project.name}: themes",
        subtitle=f"Theme map built {_when(found.built_at)}",
        summary=[
            f"{len(found.themes)} themes across {len(found.websites)} website(s), "
            f"{len(found.pages)} pages placed.",
        ],
        tables=theme_tables(found),
    )


def _monitoring(ctx: Context) -> Report | None:
    from src.modules.monitoring.service import monitor_store
    from src.modules.monitoring.store import check_tables

    found = monitor_store(ctx.projects).latest(ctx.project.project_id)
    if found is None:
        return None
    high = sum(1 for a in found.alerts if a.severity == "high")
    return Report(
        key="monitoring",
        title=f"{ctx.project.name}: monitoring",
        subtitle=f"Check of {_when(found.created_at)}",
        summary=[f"{len(found.alerts)} alert(s), {high} high; {len(found.websites)} website(s)."],
        tables=check_tables(found),
    )


def _recrawl(ctx: Context) -> Report | None:
    from src.modules.monitoring.service import monitor_store
    from src.modules.monitoring.store import plan_tables

    found = monitor_store(ctx.projects).latest_plan(ctx.project.project_id)
    if found is None:
        return None
    check = sum(s.check for s in found.sites)
    skip = sum(s.skip for s in found.sites)
    return Report(
        key="recrawl",
        title=f"{ctx.project.name}: recrawl plan",
        subtitle=f"Plan of {_when(found.created_at)} ({found.trigger})",
        summary=[
            f"{check:,} page(s) to check, {skip:,} carried forward without a request.",
            f"Core pages are checked on every pass (every {found.core_every_days} days when "
            f"scheduled); other pages every {found.other_every_days} days, or sooner when the "
            "sitemap shows a change.",
        ],
        tables=plan_tables(found),
    )


def _research(ctx: Context) -> Report | None:
    from src.modules.research.build import movers
    from src.modules.research.service import research_store
    from src.modules.research.store import research_tables

    store = research_store(ctx.projects)
    found = store.latest(ctx.project.project_id)
    if found is None:
        return None
    previous = store.previous(ctx.project.project_id)
    keywords = sum(len(w.keywords) for w in found.websites)
    return Report(
        key="research",
        title=f"{ctx.project.name}: search data",
        subtitle=f"Snapshot of {_when(found.created_at)} ({found.database} database)",
        summary=[
            f"{keywords:,} ranking keyword(s) across {len(found.websites)} website(s); "
            f"{len(found.demand)} theme(s) with demand.",
            *found.notes,
        ],
        tables=research_tables(found, movers(previous, found) if previous else None),
    )


def _competitors(ctx: Context) -> Report | None:
    from src.modules.research.competitors import SOURCE_LABELS
    from src.modules.research.service import competitor_book

    book = competitor_book(ctx.projects, ctx.project.project_id)
    if not book.suggestions:
        return None
    status = {"suggested": "Suggested", "added": "In the project", "dismissed": "Dismissed"}
    rows: list[dict[str, Cell]] = [
        {
            "Website": s.domain,
            "Status": status[s.status],
            "Found by": ", ".join(SOURCE_LABELS[x] for x in s.sources),
            "Relevance %": round(s.relevance * 100) if s.relevance is not None else None,
            "Keywords in common": s.common_keywords,
            "Their keywords": s.organic_keywords,
            "Their visits / month": s.organic_traffic,
            "Top 10 for pulled keywords": s.search_results,
            "Example keywords": ", ".join(s.example_keywords),
            "First seen": _when(s.first_seen),
            "Last seen": _when(s.last_seen),
            "Times seen": s.times_seen,
        }
        for s in book.suggestions
    ]
    open_ = sum(1 for s in book.suggestions if s.status == "suggested")
    return Report(
        key="competitors",
        title=f"{ctx.project.name}: suggested competitors",
        subtitle=f"Client {book.client_domain or ctx.project.client_name} · updated "
        f"{_when(book.updated_at)}",
        summary=[
            f"{len(rows)} website(s) seen competing with the client; {open_} not decided yet.",
            "Suggestions come from Semrush and the search results; nothing about them is "
            "scraped until one is added to the project.",
        ],
        tables=[Table("Suggested competitors", list(rows[0]), rows)],
    )


def _gaps(ctx: Context) -> Report | None:
    from src.modules.gaps.service import gap_store
    from src.modules.gaps.store import gap_tables

    found = gap_store(ctx.projects).latest(ctx.project.project_id)
    if found is None:
        return None
    open_ = [f for f in found.findings if f.status == "open"]
    return Report(
        key="gaps",
        title=f"{ctx.project.name}: gaps",
        subtitle=f"Gap analysis of {_when(found.created_at)}",
        summary=[
            f"{len(found.findings)} finding(s), {len(open_)} open, "
            f"{sum(1 for f in open_ if f.severity == 'high')} open and high.",
            *found.missing_inputs,
        ],
        tables=gap_tables(found),
    )


def _analyst(ctx: Context) -> Report | None:
    from src.modules.analyst.library import CARDS
    from src.modules.analyst.models import RunRecord

    if ctx.analyst_runs is None:
        return None
    runs = [r for r in ctx.analyst_runs(ctx.project.project_id) if isinstance(r, RunRecord)]
    if not runs:
        return None
    titles = {c.id: c.title for c in CARDS}
    rows: list[dict[str, Cell]] = []
    for r in sorted(runs, key=lambda r: r.created_at, reverse=True):
        check = r.verification
        rows.append(
            {
                "Card": titles.get(r.card_id, r.card_id),
                "Run": r.run_id,
                "How": r.backend,
                "Status": r.status,
                "Started": _when(r.created_at),
                "Finished": _when(r.finished_at) if r.finished_at else "",
                "Cost USD": round(r.cost_usd, 4),
                "Check": ("passed" if check.passed else "needs review") if check else "",
                "Citations found": check.resolved if check else None,
                "Citations": check.references if check else None,
                "Inputs": ", ".join(f"{k}: {v}" for k, v in sorted(r.inputs.items())),
                "Message": r.message,
            }
        )
    return Report(
        key="analyst",
        title=f"{ctx.project.name}: analyst card runs",
        subtitle=f"{len(rows)} run(s)",
        summary=[
            f"{sum(1 for r in runs if r.status == 'finished')} finished; total cost "
            f"${sum(r.cost_usd for r in runs):.2f}.",
            "Each run's own result downloads from the Analyst cards tab (Markdown or Word).",
        ],
        tables=[Table("Analyst runs", list(rows[0]), rows, wrap_columns=frozenset({"Message"}))],
    )


REPORTS: Final[tuple[ReportDef, ...]] = (
    ReportDef(
        "overview",
        "content",
        "Project overview",
        "The project's websites, their address lists and every scrape with its results.",
        "",
        _overview,
    ),
    ReportDef(
        "themes",
        "themes",
        "Themes",
        "Every theme with its pages per website, pillar pages, topics, funnel stages and owner.",
        "Build the theme map in the Themes tab first.",
        _themes,
    ),
    ReportDef(
        "monitoring",
        "monitoring",
        "Monitoring",
        "Alerts ranked by severity, what changed on each website and theme drift.",
        "Run a monitoring check in the Monitoring tab first.",
        _monitoring,
    ),
    ReportDef(
        "recrawl",
        "monitoring",
        "Recrawl plan",
        "Which pages the latest recrawl checked or carried forward, and why (core page, "
        "sitemap date, last check).",
        "Preview or run a recrawl in the Monitoring tab first.",
        _recrawl,
    ),
    ReportDef(
        "research",
        "research",
        "Search data",
        "Keywords per website (position, volume, ranking page), demand per theme and movers.",
        "Import a Semrush export or pull from Semrush in the Research tab first.",
        _research,
    ),
    ReportDef(
        "competitors",
        "research",
        "Suggested competitors",
        "Competitors found in the search data, with relevance, overlap, traffic and status.",
        "Add search data in the Research tab first; competitors are suggested from it.",
        _competitors,
    ),
    ReportDef(
        "gaps",
        "gaps",
        "Gaps",
        "What the client is missing, highest priority first, with evidence and next steps.",
        "Build the gap analysis in the Gaps tab first.",
        _gaps,
    ),
    ReportDef(
        "analyst",
        "analyst",
        "Analyst card runs",
        "Every analyst card run of the project: card, status, cost and citation check.",
        "Run an analyst card in the Analyst cards tab first.",
        _analyst,
    ),
)


def report_def(key: str) -> ReportDef:
    """The report kind; raises `KeyError`."""
    found = next((r for r in REPORTS if r.key == key), None)
    if found is None:
        raise KeyError(key)
    return found


def build_report(
    key: str,
    projects: ProjectStore,
    project_id: str,
    analyst_runs: Callable[[str], list[object]] | None = None,
) -> Report | None:
    """Build one report, or None when its module has nothing yet; raises `KeyError`."""
    ctx = Context(projects, projects.get(project_id), analyst_runs)
    return report_def(key).build(ctx)
