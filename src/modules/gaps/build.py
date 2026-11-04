# ruff: noqa: E501 - findings are written as plain sentences
"""Find the client's gaps against its competitors, from every module's data.

Inputs (each optional except the theme map, which is built when missing):

- **theme map** (Theming): themes the client does not cover or where a competitor
  leads, themes without a client pillar page, funnel stages competitors cover in a
  theme and the client does not;
- **research snapshot** (Research): keywords competitors rank in the top 10 for and
  the client does not rank for (or ranks past 20), and the client's keywords on page
  two (positions 11-20), grouped by theme, weighted by search volume;
- **monitoring check** (Monitoring): themes where competitors added pages since the
  previous scrape and the client did not;
- **offering catalogs** (Content Extractor): competitor offerings with no client
  offering of a similar name.

Priority (0-100) grows with competitor coverage and search demand; each finding
lists its evidence (pages, pillars, keywords, counts) and a recommended next step.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from src.modules.gaps.models import EvidenceItem, Finding, FindingKind, GapSettings
from src.modules.monitoring.models import MonitorCheck
from src.modules.research.competitors import CompetitorSuggestion
from src.modules.research.models import ResearchSnapshot, WebsiteResearch
from src.modules.theming.models import Theme, ThemeMap

__all__ = ["build_findings", "finding_id", "priority"]

_BASE: dict[FindingKind, int] = {
    "theme_gap": 50,
    "keyword_gap": 45,
    "theme_behind": 40,
    "offering_gap": 40,
    "competitor_momentum": 35,
    "striking_distance": 30,
    "unwatched_competitor": 30,
    "keyword_behind": 35,
    "keyword_unverified": 30,
    "pillar_gap": 25,
    "funnel_gap": 15,
}
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"and", "for", "the", "of", "with", "our", "your", "services", "service", "solutions"}
_MAX_EVIDENCE = 15
# Menu labels that are navigation or site sections, not offerings
_NOT_OFFERING = re.compile(
    r"^(next|previous|prev|read|learn|see|view|back|go to|more)\b|"
    r"\b(industries we serve|about|contact|careers|blog|news|resources|support|log ?in|"
    r"sign ?in|privacy|terms|sitemap|locations?|events?|press)\b",
    re.IGNORECASE,
)
_MAX_OFFERING_WORDS = 6
_TOP10 = 10
_FAR = 20


def finding_id(kind: str, key: str) -> str:
    """Stable id of a finding: its kind and what it is about."""
    return "gap-" + hashlib.sha1(f"{kind}\x1f{key}".encode()).hexdigest()[:12]  # noqa: S324


def priority(kind: FindingKind, *, competitor_pages: int = 0, demand: int = 0) -> int:
    """Base by kind, plus competitor coverage and search demand (log scale), capped at 100."""
    score = _BASE[kind] + min(20, 4 * competitor_pages) + (min(30, int(6 * math.log10(demand + 1))))
    return min(100, score)


def _severity(score: int) -> str:
    return "high" if score >= 70 else "medium" if score >= 45 else "low"


def _finding(
    kind: FindingKind,
    key: str,
    *,
    title: str,
    detail: str,
    action: str,
    theme: Theme | None = None,
    competitors: Sequence[str] = (),
    competitor_pages: int = 0,
    demand: int = 0,
    evidence: Sequence[EvidenceItem] = (),
) -> Finding:
    score = priority(kind, competitor_pages=competitor_pages, demand=demand)
    return Finding(
        finding_id=finding_id(kind, key),
        kind=kind,
        severity=_severity(score),
        priority=score,
        title=title,
        detail=detail,
        action=action,
        theme_id=theme.theme_id if theme else None,
        theme=theme.label if theme else None,
        competitors=sorted(set(competitors)),
        demand=demand,
        evidence=list(evidence)[:_MAX_EVIDENCE],
    )


def _theme_demand(snapshot: ResearchSnapshot | None) -> dict[str | None, int]:
    if snapshot is None:
        return {}
    return {d.theme_id: d.volume for d in snapshot.demand}


def _theme_findings(
    theme_map: ThemeMap, roles: Mapping[str, str], demand: Mapping[str | None, int]
) -> list[Finding]:
    out: list[Finding] = []
    for theme in theme_map.themes:
        client = [c for c in theme.coverage if roles.get(c.website) == "client"]
        rivals = [c for c in theme.coverage if roles.get(c.website) == "competitor" and c.pages]
        client_pages = sum(c.pages for c in client)
        rival_pages = max((c.pages for c in rivals), default=0)
        volume = demand.get(theme.theme_id, 0)
        pillars = [
            EvidenceItem(
                kind="pillar",
                label=c.pillar.title or c.pillar.url,
                website=c.website,
                url=c.pillar.url,
            )
            for c in rivals
            if c.pillar is not None
        ]
        counts = [
            EvidenceItem(
                kind="count",
                label=f"{c.website}: {c.pages} pages",
                website=c.website,
                value=c.pages,
            )
            for c in theme.coverage
        ]
        if theme.ownership == "client_gap":
            out.append(
                _finding(
                    "theme_gap",
                    theme.theme_id,
                    theme=theme,
                    title=f"No client content on {theme.label}",
                    detail=f"Competitors cover it with up to {rival_pages} pages; the client has none."
                    + (f" About {volume:,} searches a month." if volume else ""),
                    action="Create a pillar page for this theme, then supporting pages for its topics; "
                    "use the competitors' pillar pages as the benchmark.",
                    competitors=[c.website for c in rivals],
                    competitor_pages=rival_pages,
                    demand=volume,
                    evidence=[*pillars, *counts],
                )
            )
        elif theme.ownership == "competitor_led":
            out.append(
                _finding(
                    "theme_behind",
                    theme.theme_id,
                    theme=theme,
                    title=f"Behind on {theme.label}",
                    detail=f"A competitor has {rival_pages} pages against the client's {client_pages}."
                    + (f" About {volume:,} searches a month." if volume else ""),
                    action="Expand the client's coverage: add the subtopics competitors cover and "
                    "strengthen the pillar page.",
                    competitors=[c.website for c in rivals if c.pages > client_pages],
                    competitor_pages=rival_pages - client_pages,
                    demand=volume,
                    evidence=[*pillars, *counts],
                )
            )
        client_pillar = any(c.pillar for c in client)
        if client_pages >= 1 and not client_pillar and pillars:
            out.append(
                _finding(
                    "pillar_gap",
                    theme.theme_id,
                    theme=theme,
                    title=f"No client pillar page for {theme.label}",
                    detail=f"The client has {client_pages} page(s) on it but none anchors the theme; "
                    f"{len(pillars)} competitor(s) have one.",
                    action="Turn the strongest client page into a pillar (overview of every topic, "
                    "linking to supporting pages) or create one.",
                    competitors=[e.website or "" for e in pillars],
                    competitor_pages=len(pillars),
                    demand=volume,
                    evidence=pillars,
                )
            )
        stages_client: Counter[str] = Counter(
            p.funnel_stage
            for p in theme_map.pages
            if p.theme_id == theme.theme_id and roles.get(p.website) == "client"
        )
        stages_rival: dict[str, set[str]] = defaultdict(set)
        for p in theme_map.pages:
            if p.theme_id == theme.theme_id and roles.get(p.website) == "competitor":
                stages_rival[p.funnel_stage].add(p.website)
        missing = sorted(s for s in stages_rival if s != "unknown" and not stages_client.get(s))
        if client_pages and missing:
            out.append(
                _finding(
                    "funnel_gap",
                    theme.theme_id,
                    theme=theme,
                    title=f"Missing {', '.join(missing)} content on {theme.label}",
                    detail="Competitors have pages at these buying stages for this theme; the client "
                    "does not.",
                    action="Add content for the missing stages (for example comparisons, pricing, "
                    "how-to or help pages) and link them from the theme's pillar.",
                    competitors=sorted({w for s in missing for w in stages_rival[s]}),
                    competitor_pages=len(missing),
                    demand=volume,
                    evidence=[
                        EvidenceItem(
                            kind="count", label=f"{s}: " + ", ".join(sorted(stages_rival[s]))
                        )
                        for s in missing
                    ],
                )
            )
    return out


def _client_positions(
    snapshot: ResearchSnapshot, roles: Mapping[str, str]
) -> tuple[dict[str, tuple[int, str | None]], list[WebsiteResearch]]:
    """The client's best position (and ranking page) per keyword, and its websites."""
    best: dict[str, tuple[int, str | None]] = {}
    sites = [s for s in snapshot.websites if roles.get(s.website, s.role) == "client"]
    for site in sites:
        for r in site.keywords:
            k = r.keyword.casefold()
            if k not in best or r.position < best[k][0]:
                best[k] = (r.position, r.url)
    return best, sites


@dataclass
class _Row:
    keyword: str
    volume: int
    website: str
    position: int
    url: str | None
    client_position: int | None = None
    client_url: str | None = None


def _best_rows(rows: list[_Row]) -> list[_Row]:
    """One row per keyword (the best-ranking competitor), highest volume first."""
    best: dict[str, _Row] = {}
    for row in rows:
        k = row.keyword.casefold()
        if k not in best or row.position < best[k].position:
            best[k] = row
    return sorted(best.values(), key=lambda r: (-r.volume, r.keyword))


def _by_competitor(rows: list[_Row], every: list[_Row]) -> str:
    """'coupa.com ranks for 120 of them with 25 pages; rival.com for 60 with 8 pages'."""
    wanted = {r.keyword.casefold() for r in rows}
    per: dict[str, tuple[set[str], set[str]]] = defaultdict(lambda: (set(), set()))
    for r in every:
        if r.keyword.casefold() in wanted:
            per[r.website][0].add(r.keyword.casefold())
            if r.url:
                per[r.website][1].add(r.url)
    ordered = sorted(per.items(), key=lambda kv: (-len(kv[1][0]), kv[0]))
    parts = []
    for i, (site, (words, pages)) in enumerate(ordered[:4]):
        lead = f"{site} ranks for {len(words)} of them" if i == 0 else f"{site} for {len(words)}"
        parts.append(lead + (f" with {len(pages)} page(s)" if pages else ""))
    return "; ".join(parts)


def _evidence(rows: list[_Row], client_text: Callable[[_Row], str]) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            kind="keyword",
            label=f"{r.keyword} (#{r.position} {r.website}, {r.volume:,}/mo; {client_text(r)})",
            website=r.website,
            url=r.url,
            value=r.volume,
        )
        for r in rows
    ]


def _keyword_findings(
    snapshot: ResearchSnapshot,
    theme_map: ThemeMap,
    roles: Mapping[str, str],
    settings: GapSettings | None = None,
) -> list[Finding]:
    """Keyword opportunities by the client's position, theme by theme.

    For every keyword a competitor ranks in its top `competitor_top` for (with at least
    `min_volume` searches a month), the client's own position decides the kind:

    - not in the client's list and that list is complete -> **complete deficit**: the
      client does not rank in the top 100;
    - not in the client's list but the list was cut (pull limit) -> **probable gap**:
      it may still rank beyond the keywords pulled;
    - the client ranks 21-100 -> **behind**: a page exists and needs optimizing;
    - the client ranks 11-20 -> **page two** (quick win; also listed from the client's
      own keywords).
    """
    s = settings or GapSettings()
    labels = {t.theme_id: t for t in theme_map.themes}
    client, client_sites = _client_positions(snapshot, roles)
    complete = bool(client_sites) and all(site.complete for site in client_sites)
    coverage = "; ".join(f"{site.website}: {site.coverage}" for site in client_sites)
    every: dict[str | None, list[_Row]] = defaultdict(list)
    for site in snapshot.websites:
        if roles.get(site.website, site.role) != "competitor":
            continue
        for r in site.keywords:
            if r.position > s.competitor_top or r.volume < s.min_volume:
                continue
            found = client.get(r.keyword.casefold())
            every[r.theme_id].append(
                _Row(
                    r.keyword,
                    r.volume,
                    site.website,
                    r.position,
                    r.url,
                    found[0] if found else None,
                    found[1] if found else None,
                )  # fmt: skip
            )
    near: dict[str | None, list[tuple[str, int, int, str | None]]] = defaultdict(list)
    for site in client_sites:
        for r in site.keywords:
            if _TOP10 < r.position <= _FAR and r.volume >= s.min_volume:
                near[r.theme_id].append((r.keyword, r.volume, r.position, r.url))
    out: list[Finding] = []
    for theme_id, rows in every.items():
        theme = labels.get(theme_id) if theme_id else None
        name = theme.label if theme else "keywords outside the themes"
        quick = len({k for k, *_ in near.get(theme_id, [])})
        quick_line = (
            f" {quick} other keyword(s) of this theme are on page two for the client: the "
            "quickest wins (see Page two)."
            if quick
            else ""
        )
        missing = _best_rows([r for r in rows if r.client_position is None])
        behind = _best_rows(
            [r for r in rows if r.client_position is not None and r.client_position > _FAR]
        )
        if missing and complete:
            volume = sum(r.volume for r in missing)
            out.append(
                _finding(
                    "keyword_gap",
                    f"{theme_id}",
                    theme=theme,
                    title=f"Complete deficit in {name}: {len(missing)} keyword(s), "
                    f"{volume:,} searches a month",
                    detail=f"{_by_competitor(missing, rows)}. The client does not rank in the "
                    f"top 100 for any of them (its search data holds {coverage})." + quick_line,
                    action="Plan new pages for these: start with the highest-volume keywords "
                    "and study the competitor pages that rank for them.",
                    competitors=[r.website for r in missing],
                    competitor_pages=len({r.url for r in missing if r.url}),
                    demand=volume,
                    evidence=_evidence(missing, lambda _r: "client not in the top 100"),
                )
            )
        elif missing:
            volume = sum(r.volume for r in missing)
            why = (
                f"the client's search data holds only {coverage}"
                if client_sites
                else "there is no search data for the client yet"
            )
            out.append(
                _finding(
                    "keyword_unverified",
                    f"{theme_id}",
                    theme=theme,
                    title=f"Probable gap in {name}: {len(missing)} keyword(s), "
                    f"{volume:,} searches a month",
                    detail=f"{_by_competitor(missing, rows)}. They are not in the client's "
                    f"rankings, but {why}, so the client may still rank lower for some."
                    + quick_line,
                    action="Confirm with the client's full Semrush export or a pull with a "
                    "higher keyword limit, then plan pages for the real gaps.",
                    competitors=[r.website for r in missing],
                    competitor_pages=len({r.url for r in missing if r.url}),
                    demand=volume,
                    evidence=_evidence(missing, lambda _r: "client not in the data pulled"),
                )
            )
        if behind:
            volume = sum(r.volume for r in behind)
            out.append(
                _finding(
                    "keyword_behind",
                    f"{theme_id}",
                    theme=theme,
                    title=f"{len(behind)} keyword(s) where the client is far behind in {name}",
                    detail=f"{_by_competitor(behind, rows)}. The client ranks between 21 and "
                    f"100 for them, so it has pages to improve rather than pages to write. "
                    f"About {volume:,} searches a month together." + quick_line,
                    action="Optimize the client's ranking page for each keyword: answer the "
                    "query directly, cover what the competitor page covers, add internal links "
                    "from the pillar.",
                    competitors=[r.website for r in behind],
                    competitor_pages=len({r.url for r in behind if r.url}),
                    demand=volume,
                    evidence=_evidence(
                        behind,
                        lambda r: f"client #{r.client_position} {r.client_url or ''}".strip(),
                    ),
                )
            )
    for theme_id, near_rows in near.items():
        near_rows.sort(key=lambda r: -r[1])
        volume = sum(r[1] for r in near_rows)
        theme = labels.get(theme_id) if theme_id else None
        name = theme.label if theme else "keywords outside the themes"
        out.append(
            _finding(
                "striking_distance",
                f"{theme_id}",
                theme=theme,
                title=f"{len(near_rows)} client keyword(s) on page two in {name}",
                detail=f"The client ranks 11-20 for these; moving them to page one is usually the "
                f"quickest win. About {volume:,} searches a month together.",
                action="Improve the ranking pages (answer the query directly, add missing subtopics, "
                "internal links from the pillar) and recheck rankings.",
                demand=volume,
                evidence=[
                    EvidenceItem(
                        kind="keyword", label=f"{r[0]} (#{r[2]}, {r[1]:,}/mo)", url=r[3], value=r[1]
                    )
                    for r in near_rows
                ],
            )
        )
    return out


def _momentum_findings(
    check: MonitorCheck,
    theme_map: ThemeMap,
    roles: Mapping[str, str],
    demand: Mapping[str | None, int],
) -> list[Finding]:
    themes = {t.theme_id: t for t in theme_map.themes}
    out: list[Finding] = []
    for drift in check.theme_drift:
        rival_new = {s: n for s, n in drift.new_pages.items() if roles.get(s) == "competitor" and n}
        client_new = sum(n for s, n in drift.new_pages.items() if roles.get(s) == "client")
        if not rival_new or client_new:
            continue
        total = sum(rival_new.values())
        theme = themes.get(drift.theme_id)
        out.append(
            _finding(
                "competitor_momentum",
                drift.theme_id,
                theme=theme,
                title=f"Competitors are adding pages on {drift.label}",
                detail=f"{total} new competitor page(s) since the previous scrape "
                f"({', '.join(f'{s} {n}' for s, n in sorted(rival_new.items()))}); none from the client.",
                action="Read the new competitor pages (Monitoring tab) and decide whether to answer "
                "with new or updated client content.",
                competitors=list(rival_new),
                competitor_pages=total,
                demand=demand.get(drift.theme_id, 0),
                evidence=[
                    EvidenceItem(
                        kind="change", label=p.title or p.url, website=d.website, url=p.url
                    )
                    for d in check.websites
                    if d.website in rival_new
                    for p in d.new_pages
                    if p.theme_id == drift.theme_id
                ],
            )
        )
    return out


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if w not in _STOP and len(w) > 2}


def _offering_findings(
    catalogs: Mapping[str, list[tuple[str, str | None]]], roles: Mapping[str, str]
) -> list[Finding]:
    client_names = [
        _words(name)
        for site, offers in catalogs.items()
        if roles.get(site) == "client"
        for name, _href in offers
    ]
    seen: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for site, offers in catalogs.items():
        if roles.get(site) != "competitor":
            continue
        for name, href in offers:
            words = _words(name)
            plain = " ".join(name.split())  # also turns non-breaking spaces into spaces
            if not words or _NOT_OFFERING.search(plain) or len(plain.split()) > _MAX_OFFERING_WORDS:
                continue
            covered = any(
                len(words & c) / len(words | c) >= 0.5 or words <= c for c in client_names if c
            )
            if not covered:
                seen[" ".join(sorted(words))].append((site, href or name))
    out: list[Finding] = []
    for key, where in seen.items():
        name = next(
            n
            for site, offers in catalogs.items()
            for n, _ in offers
            if " ".join(sorted(_words(n))) == key
        )
        out.append(
            _finding(
                "offering_gap",
                key,
                title=f"Competitors offer {name}; the client's catalog does not",
                detail=f"Found in the offering catalog of {', '.join(sorted({s for s, _ in where}))}; no "
                "client offering has a similar name.",
                action="Check whether the client offers this under another name (then align naming "
                "and add a page) or decide it is out of scope.",
                competitors=[s for s, _ in where],
                competitor_pages=len(where),
                evidence=[
                    EvidenceItem(
                        kind="offering",
                        label=name,
                        website=s,
                        url=h if h and h.startswith("http") else None,
                    )
                    for s, h in where
                ],
            )
        )
    return out


_CLOSE_RELEVANCE = 0.3
_CLOSE_COMMON = 50
_CLOSE_RESULTS = 2


def _unwatched_findings(suggested: Sequence[CompetitorSuggestion]) -> list[Finding]:
    close = [
        s
        for s in suggested
        if s.status == "suggested"
        and (
            (s.relevance or 0.0) >= _CLOSE_RELEVANCE
            or s.common_keywords >= _CLOSE_COMMON
            or s.search_results >= _CLOSE_RESULTS
        )
    ]
    if not close:
        return []
    shared = sum(s.common_keywords for s in close)
    names = ", ".join(s.domain for s in close[:5]) + (" and others" if len(close) > 5 else "")
    return [
        _finding(
            "unwatched_competitor",
            "unwatched",
            title=f"{len(close)} competitor(s) in the search results are not in the project",
            detail=f"The search data shows {names} competing with the client"
            + (f" ({shared:,} keywords in common together)" if shared else "")
            + "; their pages are not scraped, so no gap against them is measured.",
            action="Review the suggested competitors in the Research tab; add the closest ones "
            "to the project and scrape them, or dismiss the ones that are not real competitors.",
            competitors=[s.domain for s in close],
            demand=0,
            evidence=[
                EvidenceItem(
                    kind="competitor",
                    label=s.domain
                    + (f" (relevance {s.relevance:.0%})" if s.relevance is not None else "")
                    + (f", {s.common_keywords:,} common keywords" if s.common_keywords else "")
                    + (f", top 10 for {s.search_results} keyword(s)" if s.search_results else ""),
                    website=s.domain,
                    url=f"https://{s.domain}/",
                    value=float(s.common_keywords or s.search_results),
                )
                for s in close
            ],
        )
    ]


def build_findings(
    theme_map: ThemeMap,
    roles: Mapping[str, str],
    *,
    research: ResearchSnapshot | None = None,
    check: MonitorCheck | None = None,
    catalogs: Mapping[str, list[tuple[str, str | None]]] | None = None,
    suggested: Sequence[CompetitorSuggestion] = (),
    settings: GapSettings | None = None,
) -> list[Finding]:
    """Every finding, highest priority first."""
    demand = _theme_demand(research)
    findings = _theme_findings(theme_map, roles, demand)
    if research is not None:
        findings += _keyword_findings(research, theme_map, roles, settings)
    if check is not None:
        findings += _momentum_findings(check, theme_map, roles, demand)
    if catalogs:
        findings += _offering_findings(catalogs, roles)
    findings += _unwatched_findings(suggested)
    return sorted(findings, key=lambda f: (-f.priority, f.kind, f.title))
