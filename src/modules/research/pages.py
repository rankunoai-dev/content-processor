"""The page report: search performance per page, cannibalisation, links and health ($0).

Joins what the app already holds for the client's website: the latest research snapshot
(keywords and their ranking pages), the one before it, the scrape's inventory (links,
titles, descriptions, headings, issues) and page stamps (word counts), the theme map
and, when imported, Search Console. Nothing is fetched.

- **Per page**: keywords, monthly searches, estimated visits, average and best position,
  top keyword, searches of commercial and transactional keywords.
- **Cannibalisation**: a keyword two or more pages compete for: several rows of one
  keyword in the Semrush data (`other_urls`), the ranking page changing since the last
  snapshot, or several pages of the site getting a real share of one query's
  impressions in Search Console.
- **Zero traffic**: indexable scraped pages that rank for no keyword.
- **Stuck pages**: pages ranking 4-20 with on-page problems that may hold them back.
- **Links**: see `links.py`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

from src.modules.analytics.models import Ga4Snapshot
from src.modules.content_processor.knowledge.inventory_models import PageInventory
from src.modules.content_processor.knowledge.models import PageStamp
from src.modules.research.build import estimated_visits
from src.modules.research.cannibal import build_plan
from src.modules.research.gsc import GscSnapshot, ctr_opportunities
from src.modules.research.links import LinkGraph, build_graph, page_key
from src.modules.research.models import KeywordRow, ResearchSnapshot, WebsiteResearch
from src.modules.research.page_models import (
    Cannibalisation,
    ConsolidationPair,
    LinkSuggestion,
    PageReport,
    PageRow,
)
from src.modules.theming.models import ThemeMap

__all__ = ["HEALTH", "THIN_WORDS", "build_page_report"]

# Inventory issue codes that can hold a page back in search, in plain words.
HEALTH: Final[dict[str, str]] = {
    "title_missing": "no title",
    "title_too_long": "title too long",
    "title_too_short": "title too short",
    "description_missing": "no meta description",
    "description_too_long": "meta description too long",
    "h1_missing": "no H1",
    "h1_multiple": "several H1s",
    "heading_levels_skipped": "heading levels skipped",
    "noindex": "noindex",
    "canonical_other_page": "canonical points elsewhere",
}
THIN_WORDS: Final = 300
_CLOSE: Final = (4, 20)
_FEW_LINKS: Final = 2
_SOURCES_PER_TARGET: Final = 3
_MAX_SUGGESTIONS: Final = 60
_GSC_SHARE: Final = 0.1  # a page with 10 %+ of a query's impressions competes for it
_GSC_MIN: Final = 20
_COMMERCIAL: Final = ("commercial", "transactional")


def _client(snapshot: ResearchSnapshot, website: str) -> WebsiteResearch | None:
    return next((w for w in snapshot.websites if w.website == website), None)


def _cannibals(
    rows: Sequence[KeywordRow],
    previous: WebsiteResearch | None,
    gsc: GscSnapshot | None,
) -> list[Cannibalisation]:
    out: list[Cannibalisation] = []
    for r in rows:
        if r.url and r.other_urls:
            out.append(
                Cannibalisation(
                    keyword=r.keyword,
                    volume=r.volume,
                    intent=r.intent,
                    pages=[r.url, *r.other_urls],
                    positions=[r.position],
                    source="semrush_rows",
                    note=f"{len(r.other_urls) + 1} pages rank for it; the best is at {r.position}",
                )
            )
    if previous is not None:
        before = {p.keyword.casefold(): p for p in previous.keywords if p.url}
        listed = {c.keyword.casefold() for c in out}
        for r in rows:
            old = before.get(r.keyword.casefold())
            if (
                r.url
                and old is not None
                and old.url
                and page_key(old.url) != page_key(r.url)
                and r.keyword.casefold() not in listed
            ):
                out.append(
                    Cannibalisation(
                        keyword=r.keyword,
                        volume=r.volume,
                        intent=r.intent,
                        pages=[r.url, old.url],
                        positions=[r.position, old.position],
                        source="url_swap",
                        note=f"the ranking page changed since the last snapshot "
                        f"({old.position} -> {r.position})",
                    )
                )
    if gsc is not None:
        by_query: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
        for pair in gsc.pairs:
            if pair.query and pair.page:
                by_query[pair.query].append((pair.page, pair.impressions, pair.position))
        for query, pages in by_query.items():
            total = sum(i for _p, i, _pos in pages) or 1
            sharing = [
                (p, i, pos) for p, i, pos in pages if i >= _GSC_MIN and i / total >= _GSC_SHARE
            ]
            if len(sharing) >= 2:
                sharing.sort(key=lambda s: -s[1])
                out.append(
                    Cannibalisation(
                        keyword=query,
                        volume=total,
                        pages=[p for p, _i, _pos in sharing],
                        positions=[round(pos, 1) for _p, _i, pos in sharing],
                        source="search_console",
                        note=f"{len(sharing)} pages share its {total:,} impressions "
                        "(Search Console)",
                    )
                )
    return sorted(out, key=lambda c: -c.volume)


def _suggestions(
    rows: Mapping[str, PageRow], graph: LinkGraph, best: Mapping[str, KeywordRow]
) -> list[LinkSuggestion]:
    ranked = sorted(graph.rank.values())
    median = ranked[len(ranked) // 2] if ranked else 0.0
    out: list[LinkSuggestion] = []
    for key, keyword in sorted(best.items(), key=lambda kv: -kv[1].volume):
        row = rows.get(key)
        if row is None or (row.inbound_body_links > _FEW_LINKS and row.pagerank >= median):
            continue
        sources = [
            k
            for k, r in rows.items()
            if k != key
            and k in graph.urls
            and r.indexable
            and key not in graph.body_out.get(k, set())
            and (row.theme_id is None or r.theme_id == row.theme_id)
            and r.pagerank > row.pagerank
        ]
        sources.sort(key=lambda k: -rows[k].pagerank)
        for k in sources[:_SOURCES_PER_TARGET]:
            out.append(
                LinkSuggestion(
                    source_url=rows[k].url,
                    source_title=rows[k].title,
                    source_pagerank=rows[k].pagerank,
                    target_url=row.url,
                    target_title=row.title,
                    anchor=keyword.keyword,
                    keyword_position=keyword.position,
                    keyword_volume=keyword.volume,
                    theme=row.theme,
                    reason=(
                        f"ranks {keyword.position} for “{keyword.keyword}” "
                        f"({keyword.volume:,}/month) with {row.inbound_body_links} link(s) from "
                        "page text; a link from a stronger page of the same theme can lift it"
                    ),
                )
            )
        if len(out) >= _MAX_SUGGESTIONS:
            break
    return out


_GA4_MIN_NEW_ROW: Final = 10
_GA4_KEEP_SESSIONS: Final = 50
_GA4_BETTER: Final = 2.0
_GA4_MIN_EVENTS: Final = 5


def _owner_checks(pairs: Sequence[ConsolidationPair], rows: Mapping[str, PageRow]) -> None:
    """Flag pairs whose competing page converts clearly better than the chosen owner."""
    for pair in pairs:
        page, owner = rows.get(page_key(pair.page)), rows.get(page_key(pair.owner))
        if page is None or owner is None or not page.ga_sessions:
            continue
        rate = (page.ga_conversions or 0) / page.ga_sessions
        owner_rate = (owner.ga_conversions or 0) / owner.ga_sessions if owner.ga_sessions else 0.0
        if (page.ga_conversions or 0) >= _GA4_MIN_EVENTS and rate >= _GA4_BETTER * max(
            owner_rate, 1e-9
        ):
            pair.owner_check = (
                f"GA4: this page converts {rate:.1%} of its sessions against {owner_rate:.1%} "
                f"for the chosen owner (engagement {page.ga_engagement_rate or 0:.0%} against "
                f"{owner.ga_engagement_rate or 0:.0%}); confirm which page should own the topic "
                "before consolidating"
            )


def build_page_report(
    project_id: str,
    website: str,
    *,
    snapshot: ResearchSnapshot | None,
    previous: ResearchSnapshot | None = None,
    inventory: Sequence[PageInventory] = (),
    stamps: Mapping[str, PageStamp] | None = None,
    theme_map: ThemeMap | None = None,
    gsc: GscSnapshot | None = None,
    now: datetime,
    run_id: str | None = None,
    paragraphs: Mapping[str, Sequence[str]] | None = None,
    brands: Sequence[str] = (),
    ga4: Ga4Snapshot | None = None,
) -> PageReport:
    """The report for one website (the client's); every input may be missing."""
    report = PageReport(
        project_id=project_id,
        website=website,
        built_at=now,
        research_snapshot_id=snapshot.snapshot_id if snapshot else None,
        run_id=run_id,
        gsc_period=gsc.period if gsc else None,
        gsc_imported_at=gsc.imported_at if gsc else None,
        ga4_snapshot_id=ga4.snapshot_id if ga4 else None,
    )
    site = _client(snapshot, website) if snapshot else None
    keywords = site.keywords if site else []
    graph = build_graph(inventory)
    themes = {t.theme_id: t.label for t in theme_map.themes} if theme_map else {}
    theme_of = (
        {page_key(p.url): p.theme_id for p in theme_map.pages if p.theme_id} if theme_map else {}
    )
    by_stamp = {page_key(u): s for u, s in (stamps or {}).items()}
    issues = {
        page_key(p.url): {i.code for i in p.issues} for p in inventory if p.status == "inventoried"
    }

    rows: dict[str, PageRow] = {}

    def row_for(url: str) -> PageRow:
        key = page_key(url)
        if key not in rows:
            stamp = by_stamp.get(key)
            theme_id = theme_of.get(key)
            words = (stamp.main_word_count or stamp.word_count) if stamp else None
            flags = [HEALTH[c] for c in sorted(issues.get(key, set())) if c in HEALTH]
            if words is not None and words < THIN_WORDS:
                flags.append(f"thin: {words} words")
            if key not in graph.urls:
                flags.append("not in the scrape")
            rows[key] = PageRow(
                url=graph.urls.get(key, url),
                title=graph.titles.get(key) or (stamp.title if stamp else None),
                theme_id=theme_id,
                theme=themes.get(theme_id) if theme_id else None,
                inbound_body_links=len(graph.body_in.get(key, ())),
                inbound_chrome_links=len(graph.chrome_in.get(key, ())),
                pagerank=graph.rank.get(key, 0.0),
                word_count=words,
                flags=flags,
                indexable=graph.indexable.get(key, True),
            )
        return rows[key]

    for key in graph.urls:
        row_for(graph.urls[key])
    per_page: dict[str, list[KeywordRow]] = defaultdict(list)
    for r in keywords:
        if r.url:
            row_for(r.url)
            per_page[page_key(r.url)].append(r)
    best_close: dict[str, KeywordRow] = {}
    for key, ranked in per_page.items():
        row = rows[key]
        row.keywords = len(ranked)
        row.volume = sum(r.volume for r in ranked)
        row.visits = round(sum(estimated_visits(r.volume, r.position) for r in ranked), 1)
        row.average_position = round(sum(r.position for r in ranked) / len(ranked), 1)
        row.best_position = min(r.position for r in ranked)
        top = max(ranked, key=lambda r: (estimated_visits(r.volume, r.position), r.volume))
        row.top_keyword = top.keyword
        row.commercial_volume = sum(r.volume for r in ranked if r.intent in _COMMERCIAL)
        close = [r for r in ranked if _CLOSE[0] <= r.position <= _CLOSE[1]]
        if close:
            best_close[key] = max(close, key=lambda r: r.volume)
    if gsc is not None:
        for g in gsc.pages:
            if g.page:
                row = row_for(g.page)
                row.gsc_clicks, row.gsc_impressions = g.clicks, g.impressions
                row.gsc_ctr, row.gsc_position = round(g.ctr, 4), round(g.position, 1)
        report.ctr_opportunities = ctr_opportunities(gsc)

    if ga4 is not None:
        for ga in ga4.rows:
            key = page_key(ga.url)
            if key not in rows and ga.sessions < _GA4_MIN_NEW_ROW:
                continue
            row = row_for(ga.url)
            row.ga_sessions, row.ga_organic_sessions = ga.sessions, ga.organic_sessions
            row.ga_engagement_rate, row.ga_avg_engagement_s = (
                ga.engagement_rate,
                ga.avg_engagement_s,
            )
            row.ga_conversions, row.ga_revenue = ga.key_events, ga.revenue
            row.ga_ai_sessions = ga.ai_sessions
    report.pages = sorted(rows.values(), key=lambda r: (-r.visits, -r.volume, r.url))
    previous_site = _client(previous, website) if previous else None
    report.cannibalisation = _cannibals(keywords, previous_site, gsc)
    if keywords:
        unranked = [
            r
            for k, r in rows.items()
            if k in graph.urls and r.indexable and not r.keywords and not graph.is_home(k)
        ]
        # a page that ranks for nothing may still convert or draw visits from other
        # channels (direct, email, paid): never suggest pruning it
        kept = [
            r
            for r in unranked
            if (r.ga_conversions or 0) > 0 or (r.ga_sessions or 0) >= _GA4_KEEP_SESSIONS
        ]
        report.kept_by_ga4 = sorted(r.url for r in kept)
        report.zero_traffic = sorted(r.url for r in unranked if r not in kept)
    report.orphans = graph.orphans()
    report.near_orphans = graph.near_orphans()
    report.stuck = [
        r.url
        for r in sorted(rows.values(), key=lambda r: -r.volume)
        if r.best_position
        and _CLOSE[0] <= r.best_position <= _CLOSE[1]
        and any(f != "not in the scrape" for f in r.flags)
    ]
    report.link_suggestions = _suggestions(rows, graph, best_close)
    if site is not None or gsc is not None:
        report.plan = build_plan(
            website,
            site=site,
            gsc=gsc,
            theme_map=theme_map,
            inventory=inventory,
            paragraphs=paragraphs or {},
            brands=brands,
            top_keyword={k: r.top_keyword for k, r in rows.items() if r.top_keyword},
        )
        if ga4 is not None:
            _owner_checks(report.plan.pairs, rows)

    if inventory:
        report.notes.append(
            f"Links are counted between the {len(graph.urls)} scraped pages of {website}; "
            "links from pages that were not scraped are unknown, so a page listed as having "
            "no links from page text may have some on pages outside the scrape."
        )
    else:
        report.notes.append(
            f"{website} has no scrape inventory: links and on-page health are left out."
        )
    if site is None:
        report.notes.append(f"No search data for {website}: per-page rankings are left out.")
    elif not site.complete:
        report.notes.append(
            f"{website}'s keyword list is cut short ({site.coverage}): a page with no "
            "keywords may still rank for some outside the list."
        )
    if gsc is None:
        report.notes.append(
            "No Search Console data: import its Performance export for real clicks, "
            "impressions and CTR."
        )
    if ga4 is None:
        report.notes.append(
            "No GA4 data: import the GA4 landing page export for sessions, engagement and "
            "conversions per page."
        )
    return report
