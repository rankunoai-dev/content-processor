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
from collections.abc import Mapping, Sequence

from src.modules.gaps.models import EvidenceItem, Finding, FindingKind
from src.modules.monitoring.models import MonitorCheck
from src.modules.research.models import ResearchSnapshot
from src.modules.theming.models import Theme, ThemeMap

__all__ = ["build_findings", "finding_id", "priority"]

_BASE: dict[FindingKind, int] = {
    "theme_gap": 50,
    "keyword_gap": 45,
    "theme_behind": 40,
    "offering_gap": 40,
    "competitor_momentum": 35,
    "striking_distance": 30,
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


def _keyword_findings(
    snapshot: ResearchSnapshot, theme_map: ThemeMap, roles: Mapping[str, str]
) -> list[Finding]:
    labels = {t.theme_id: t for t in theme_map.themes}
    client_pos: dict[str, int] = {}
    for site in snapshot.websites:
        if roles.get(site.website, site.role) == "client":
            for r in site.keywords:
                k = r.keyword.casefold()
                client_pos[k] = min(client_pos.get(k, 101), r.position)
    gaps: dict[str | None, list[tuple[str, int, str, int, str | None]]] = defaultdict(list)
    for site in snapshot.websites:
        if roles.get(site.website, site.role) != "competitor":
            continue
        for r in site.keywords:
            k = r.keyword.casefold()
            if r.position <= _TOP10 and client_pos.get(k, 101) > _FAR:
                gaps[r.theme_id].append((r.keyword, r.volume, site.website, r.position, r.url))
    out: list[Finding] = []
    for theme_id, rows in gaps.items():
        best: dict[str, tuple[str, int, str, int, str | None]] = {}
        for row in rows:
            k = row[0].casefold()
            if k not in best or row[3] < best[k][3]:
                best[k] = row
        ranked = sorted(best.values(), key=lambda r: -r[1])
        volume = sum(r[1] for r in ranked)
        theme = labels.get(theme_id) if theme_id else None
        name = theme.label if theme else "keywords outside the themes"
        out.append(
            _finding(
                "keyword_gap",
                f"{theme_id}",
                theme=theme,
                title=f"{len(ranked)} competitor keyword(s) the client misses in {name}",
                detail=f"Competitors rank in the top 10 for these; the client does not rank in the "
                f"top 20. About {volume:,} searches a month together.",
                action="Map each keyword to an existing client page or a new one; start with the "
                "highest-volume keywords and the competitor pages ranking for them.",
                competitors=[r[2] for r in ranked],
                competitor_pages=len({r[4] for r in ranked}),
                demand=volume,
                evidence=[
                    EvidenceItem(
                        kind="keyword",
                        label=f"{r[0]} (#{r[3]} {r[2]}, {r[1]:,}/mo)",
                        website=r[2],
                        url=r[4],
                        value=r[1],
                    )
                    for r in ranked
                ],
            )
        )
    near: dict[str | None, list[tuple[str, int, int, str | None]]] = defaultdict(list)
    for site in snapshot.websites:
        if roles.get(site.website, site.role) == "client":
            for r in site.keywords:
                if _TOP10 < r.position <= _FAR and r.volume:
                    near[r.theme_id].append((r.keyword, r.volume, r.position, r.url))
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


def build_findings(
    theme_map: ThemeMap,
    roles: Mapping[str, str],
    *,
    research: ResearchSnapshot | None = None,
    check: MonitorCheck | None = None,
    catalogs: Mapping[str, list[tuple[str, str | None]]] | None = None,
) -> list[Finding]:
    """Every finding, highest priority first."""
    demand = _theme_demand(research)
    findings = _theme_findings(theme_map, roles, demand)
    if research is not None:
        findings += _keyword_findings(research, theme_map, roles)
    if check is not None:
        findings += _momentum_findings(check, theme_map, roles, demand)
    if catalogs:
        findings += _offering_findings(catalogs, roles)
    return sorted(findings, key=lambda f: (-f.priority, f.kind, f.title))
