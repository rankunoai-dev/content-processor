"""Run a monitoring check of a project: diffs, theme drift and alerts.

For each website in the project's scope (its freshest copy):

- the scrape to compare against is the one that was latest at the previous check;
  without a previous check, the scrape before the latest one. When nothing new was
  scraped since the previous check the website is reported as unchanged.
- new, removed and changed pages come from `diff.py`.

The project's theme map is rebuilt (theme ids carried over), new pages are placed in
their themes, removed pages in the themes of the previous check's map, and theme
ownership is compared with that map. Alerts follow from both, and from the search and
link signals (`signals.py`) when the project has research or backlink snapshots.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from src.modules.analyst.playground import Playground, SiteData
from src.modules.backlinks.models import BacklinkSnapshot
from src.modules.content_processor.analysis.semantic import VectorCache
from src.modules.content_processor.knowledge.embedders import is_semantic
from src.modules.monitoring.diff import diff_runs, load_run_files
from src.modules.monitoring.meaning import TOPIC_DRIFT, page_meaning
from src.modules.monitoring.models import (
    Alert,
    AlertKind,
    MonitorCheck,
    PageChange,
    PageRef,
    RankSignal,
    SearchSignals,
    Severity,
    SiteDiff,
    ThemeDrift,
)
from src.modules.monitoring.signals import (
    DR_STEP,
    LOST_ALERT,
    TOP,
    link_signals,
    rank_signals,
)
from src.modules.research.models import ResearchSnapshot
from src.modules.theming.models import ThemeMap
from src.modules.theming.service import build_project_themes, theme_store
from src.modules.theming.store import match_themes

__all__ = ["PILLAR_DRIFT", "TOPIC_DRIFT", "run_check"]

PILLAR_DRIFT = 0.3
_REWRITE_ALERT = 1
GROWTH_WORDS = 500
SHIFT_HEADINGS = 2
_CLIENT_SIDE = {"client_owned", "contested", "competitor_led", "client_only"}
_RANK = {"client_only": 4, "client_owned": 3, "contested": 2, "competitor_led": 1, "client_gap": 0}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
# structured-data types that can earn rich results or say "this page sells"
RICH_TYPES = frozenset(
    {
        "Product",
        "Offer",
        "AggregateOffer",
        "FAQPage",
        "QAPage",
        "Review",
        "AggregateRating",
        "HowTo",
        "Service",
        "SoftwareApplication",
        "Course",
        "Event",
        "VideoObject",
        "LocalBusiness",
        "JobPosting",
    }
)
_DUMMY_PREFIX = "[dummy data] "


def _alert(
    kind: AlertKind,
    severity: Severity,
    website: str,
    title: str,
    detail: str,
    *,
    theme_id: str | None = None,
    urls: list[str] | None = None,
) -> Alert:
    key = "\x1f".join([kind, website, theme_id or "", *(urls or [])[:5]])
    return Alert(
        alert_id="al-" + hashlib.sha1(key.encode()).hexdigest()[:12],  # noqa: S324
        severity=severity,
        kind=kind,
        website=website,
        title=title,
        detail=detail,
        theme_id=theme_id,
        urls=(urls or [])[:20],
    )


def _themes_of(theme_map: ThemeMap | None) -> dict[str, tuple[str, str]]:
    """Page URL -> (theme id, theme label)."""
    if theme_map is None:
        return {}
    labels = {t.theme_id: t.label for t in theme_map.themes}
    return {
        p.url: (p.theme_id, labels.get(p.theme_id, p.theme_id))
        for p in theme_map.pages
        if p.theme_id
    }


def _score_meaning(
    play: Playground,
    data: SiteData,
    texts: dict[str, tuple[list[str], list[str]]],
    changed: list[PageChange],
    cache: VectorCache,
) -> tuple[str | None, str]:
    """Fill each changed page's meaning score; returns (model, note when not scored)."""
    if not changed:
        return None, ""
    store = play.open_store(data)
    try:
        model = store.meta().get("model", "")
    finally:
        store.close()
    if not model or not is_semantic(model):
        return None, "meaning not scored: the website was indexed with the words-only stand-in"
    try:
        embedder = play.access.embedder_for(model)
        scores = page_meaning(
            {c.url: texts[c.url] for c in changed if c.url in texts}, embedder, cache
        )
    except Exception as exc:  # noqa: BLE001 - model down or failing: the check still finishes
        return None, f"meaning not scored: {str(exc)[:200]}"
    for change in changed:
        change.meaning = scores.get(change.url)
    return embedder.model, ""


def _site_diffs(
    play: Playground,
    previous: MonitorCheck | None,
    themes_now: dict[str, tuple[str, str]],
    themes_before: dict[str, tuple[str, str]],
    pillars: set[str],
    cache: VectorCache | None = None,
    notes: list[str] | None = None,
) -> list[SiteDiff]:
    earlier = {s.website: s.latest_run for s in previous.websites} if previous else {}
    diffs: list[SiteDiff] = []
    for website in play.websites():
        role: Literal["client", "competitor"] = (
            "client" if play.role_in_run(website) == "client" else "competitor"
        )
        data = play.site_data(website)
        if data is None:
            diffs.append(
                SiteDiff(
                    website=website.key,
                    name=website.name,
                    role=role,
                    note="not scraped or not indexed yet",
                )
            )
            continue
        runs = play.ready_runs(data)
        latest = runs[-1]
        base_id = earlier.get(website.key)
        if base_id == latest.run_id:
            diffs.append(
                SiteDiff(
                    website=website.key,
                    name=website.name,
                    role=role,
                    baseline_run=base_id,
                    latest_run=latest.run_id,
                    latest_at=latest.finished_at,
                    note="nothing new scraped since the last check",
                )
            )
            continue
        # A lite scan is a sample: compared page by page only with an earlier lite scan.
        same = [r for r in runs[:-1] if r.scan == latest.scan]
        baseline = next((r for r in same if r.run_id == base_id), None) if base_id else None
        if baseline is None and same:
            baseline = same[-1]
        if baseline is None:
            first = (
                "first lite scan: kept as the baseline for the next lite scan (a sample is "
                "never compared page by page with a full scrape)"
                if latest.scan == "lite"
                else "first scrape: kept as the baseline for the next check"
            )
            diffs.append(
                SiteDiff(
                    website=website.key,
                    name=website.name,
                    role=role,
                    latest_run=latest.run_id,
                    latest_at=latest.finished_at,
                    note=first,
                )
            )
            continue
        folder = play.store.run_dir
        old = load_run_files(
            folder(data.copy.project_id, data.copy.site_id, baseline.run_id), baseline.run_id
        )
        new = load_run_files(
            folder(data.copy.project_id, data.copy.site_id, latest.run_id), latest.run_id
        )
        result = diff_runs(old, new)
        if notes is not None:
            notes.extend(f"{website.name}: {n}" for n in result.notes)
        added, removed, changed = result.added, result.removed, result.changed
        unchanged = result.unchanged
        sample_note = ""
        if latest.scan == "lite":
            # two samples hold different pages: a page in only one is not new or removed
            added, removed = [], []
            sample_note = "lite scans: only pages in both samples are compared"

        def ref(url: str, title: str | None, themes: dict[str, tuple[str, str]]) -> PageRef:
            theme = themes.get(url)
            return PageRef(
                url=url,
                title=title,
                theme_id=theme[0] if theme else None,
                theme=theme[1] if theme else None,
            )

        for change in changed:
            theme = themes_now.get(change.url)
            if theme:
                change.theme_id, change.theme = theme
            change.is_pillar = change.url in pillars
        meaning_model, meaning_note = (
            _score_meaning(play, data, result.texts, changed, cache)
            if cache is not None
            else (None, "")
        )
        total = len(added) + len(removed) + len(changed) + unchanged
        diffs.append(
            SiteDiff(
                website=website.key,
                name=website.name,
                role=role,
                baseline_run=baseline.run_id,
                latest_run=latest.run_id,
                baseline_at=baseline.finished_at,
                latest_at=latest.finished_at,
                new_pages=[ref(u, new.stamps[u].title, themes_now) for u in added],
                removed_pages=[ref(u, old.stamps[u].title, themes_before) for u in removed],
                changed_pages=changed,
                unchanged_pages=unchanged,
                template_changes=result.template,
                drift=round((len(added) + len(removed) + len(changed)) / max(total, 1), 3),
                meaning_model=meaning_model,
                meaning_note=meaning_note,
                note=sample_note,
            )
        )
    return diffs


def _drift(current: ThemeMap, before: ThemeMap | None, diffs: list[SiteDiff]) -> list[ThemeDrift]:
    carried = match_themes(before, current) if before else {}
    old_by_id = {t.theme_id: t for t in before.themes} if before else {}
    out: list[ThemeDrift] = []
    seen_old: set[str] = set()
    for theme in current.themes:
        old_id = carried.get(theme.theme_id, theme.theme_id)
        old = old_by_id.get(old_id)
        if old is not None:
            seen_old.add(old.theme_id)
        new_pages = Counter(
            d.website for d in diffs for p in d.new_pages if p.theme_id == theme.theme_id
        )
        removed = Counter(
            d.website
            for d in diffs
            for p in d.removed_pages
            if p.theme_id in (theme.theme_id, old_id)
        )
        out.append(
            ThemeDrift(
                theme_id=theme.theme_id,
                label=theme.label,
                status="existing" if old is not None or before is None else "new",
                ownership_before=old.ownership if old else None,
                ownership_after=theme.ownership,
                new_pages=dict(new_pages),
                removed_pages=dict(removed),
                pages_before={c.website: c.pages for c in old.coverage} if old else {},
                pages_after={c.website: c.pages for c in theme.coverage},
            )
        )
    for old in before.themes if before else []:
        if old.theme_id not in seen_old:
            out.append(
                ThemeDrift(
                    theme_id=old.theme_id,
                    label=old.label,
                    status="gone",
                    ownership_before=old.ownership,
                    pages_before={c.website: c.pages for c in old.coverage},
                )
            )
    return out


def _page_name(change: PageChange) -> str:
    return change.title or change.url


def _competitor_moves(d: SiteDiff, topic_drift: float = TOPIC_DRIFT) -> list[Alert]:
    """Pages a competitor grew, re-headed, moved to another topic, or gave new tables."""
    out: list[Alert] = []
    moved = sorted(
        (c for c in d.changed_pages if c.meaning is not None and c.meaning < topic_drift),
        key=lambda c: c.meaning or 0.0,
    )
    if moved:
        out.append(
            _alert(
                "competitor_topic_drift",
                "high" if any(c.is_pillar for c in moved) else "medium",
                d.website,
                f"{d.name} moved {len(moved)} page(s) onto a different topic",
                "; ".join(
                    f"{_page_name(c)}: meaning {c.meaning:.2f} old vs new"
                    + (f" (now about {', '.join(c.new_terms[:5])})" if c.new_terms else "")
                    for c in moved
                )[:700],
                urls=[c.url for c in moved],
            )
        )
    grown = [c for c in d.changed_pages if c.words_delta >= GROWTH_WORDS]
    if grown:
        out.append(
            _alert(
                "competitor_content_growth",
                "high" if any(c.is_pillar for c in grown) else "medium",
                d.website,
                f"{d.name} expanded {len(grown)} page(s) by {GROWTH_WORDS}+ words",
                "; ".join(
                    f"{_page_name(c)}: {c.words_before:,} → {c.words_after:,} words "
                    f"(+{c.words_delta:,})"
                    for c in sorted(grown, key=lambda c: -c.words_delta)
                )[:700],
                urls=[c.url for c in grown],
            )
        )
    shifted = [
        c
        for c in d.changed_pages
        if c.new_terms
        and (
            len(c.headings_added) + len(c.headings_removed) >= SHIFT_HEADINGS
            or "h1" in c.head_changes
        )
    ]
    if shifted:
        out.append(
            _alert(
                "competitor_messaging_shift",
                "high" if any(c.is_pillar for c in shifted) else "medium",
                d.website,
                f"{d.name} changed the headings of {len(shifted)} page(s) toward new terms",
                "; ".join(
                    f"{_page_name(c)}: added {', '.join(c.new_terms[:8])}"
                    + (f"; dropped {', '.join(c.dropped_terms[:6])}" if c.dropped_terms else "")
                    for c in shifted
                )[:700],
                urls=[c.url for c in shifted],
            )
        )
    tabled = [
        c
        for c in d.changed_pages
        if c.blocks_added.get("tables") or c.blocks_added.get("pricing blocks")
    ]
    if tabled:
        priced = any(c.blocks_added.get("pricing blocks") for c in tabled)
        out.append(
            _alert(
                "competitor_new_tables",
                "high" if priced else "medium",
                d.website,
                f"{d.name} added "
                + ("pricing or comparison blocks" if priced else "comparison tables")
                + f" on {len(tabled)} page(s)",
                "; ".join(
                    f"{_page_name(c)}: "
                    + ", ".join(f"+{n} {k}" for k, n in sorted(c.blocks_added.items()))
                    for c in tabled
                )[:700],
                urls=[c.url for c in tabled],
            )
        )
    return out


def _examples(changes: list[PageChange], limit: int = 6) -> str:
    return "; ".join(f"{_page_name(c)}: {', '.join(c.seo_regressions)}" for c in changes[:limit])


def _seo_alerts(d: SiteDiff) -> list[Alert]:
    """Search-engine regressions on the client's pages; competitors adding rich data or CTAs."""
    out: list[Alert] = []
    if d.role == "client":
        worse = [c for c in d.changed_pages if c.seo_regressions]
        if worse:
            severe = any(
                c.noindex == "added"
                or c.canonical_moved
                or (c.is_pillar and (c.schema_removed or c.schema_broken))
                for c in worse
            )
            kinds: Counter[str] = Counter()
            for c in worse:
                kinds.update(
                    label
                    for label, hit in (
                        ("now noindex", c.noindex == "added"),
                        ("canonical moved away", c.canonical_moved),
                        ("structured data broken", c.schema_broken),
                        ("structured data removed", bool(c.schema_removed)),
                        (
                            "more images without alt text",
                            c.images_missing_alt is not None
                            and c.images_missing_alt[1] - c.images_missing_alt[0] >= 3,
                        ),
                    )
                    if hit
                )
            out.append(
                _alert(
                    "client_seo_regression",
                    "high" if severe else "medium",
                    d.website,
                    f"{len(worse)} client page(s) got worse for search engines",
                    (
                        ", ".join(f"{k}: {n} page(s)" for k, n in kinds.most_common())
                        + ". "
                        + _examples(worse)
                    )[:700],
                    urls=[c.url for c in worse],
                )
            )
        return out
    rich = [c for c in d.changed_pages if RICH_TYPES & set(c.schema_added)]
    if rich:
        types = Counter(t for c in rich for t in c.schema_added if t in RICH_TYPES)
        out.append(
            _alert(
                "competitor_schema_added",
                "high" if any(c.is_pillar for c in rich) else "medium",
                d.website,
                f"{d.name} added rich-result structured data on {len(rich)} page(s)",
                (
                    ", ".join(f"{t} on {n}" for t, n in types.most_common())
                    + ". Search results may now show their prices, ratings or FAQs: "
                    + "; ".join(_page_name(c) for c in rich[:6])
                )[:700],
                urls=[c.url for c in rich],
            )
        )
    selling = [c for c in d.changed_pages if c.ctas_added and c.strong_ctas_before == 0]
    if selling:
        out.append(
            _alert(
                "competitor_intent_shift",
                "high" if any(c.is_pillar for c in selling) else "medium",
                d.website,
                f"{d.name} turned {len(selling)} informing page(s) into selling pages",
                "Strong calls to action where there were none: "
                + "; ".join(
                    f"{_page_name(c)}: {', '.join(repr(t) for t in c.ctas_added[:3])}"
                    for c in selling[:6]
                )[:650],
                urls=[c.url for c in selling],
            )
        )
    sitewide = [
        t for t in d.template_changes if t.line.startswith("Call to action: ") and t.added_on
    ]
    if sitewide:
        out.append(
            _alert(
                "competitor_intent_shift",
                "medium",
                d.website,
                f"{d.name} added a call to action across the website",
                "; ".join(
                    f"“{t.line.removeprefix('Call to action: ')}” on {t.added_on} page(s)"
                    for t in sitewide[:6]
                ),
            )
        )
    return out


def _place(n: int | None) -> str:
    return f"#{n}" if n is not None else "out"


def _signal_alerts(signals: SearchSignals | None) -> list[Alert]:
    """Ranking and link alerts from the search signals."""
    if signals is None:
        return []
    out: list[Alert] = []
    prefix = _DUMMY_PREFIX if signals.dummy else ""
    by_site: dict[str, list[RankSignal]] = {}
    for rank in signals.rankings:
        by_site.setdefault(rank.website, []).append(rank)
    for website, rows in by_site.items():
        name = rows[0].name
        if rows[0].role == "client":

            def certain_out(r: RankSignal) -> bool:
                return (r.after is None and not r.uncertain) or (r.after or 0) > TOP

            severe = any(
                certain_out(r)
                and (
                    (r.before or 99) <= 3
                    or r.volume >= 1000
                    or (r.intent or "") in ("commercial", "transactional")
                )
                for r in rows
            )
            caused = [r for r in rows if r.page_change]
            detail = "; ".join(
                f"“{r.keyword}” {_place(r.before)} → {_place(r.after)}"
                + (" (list cut, may rank lower)" if r.uncertain else "")
                + f", {r.volume:,}/mo"
                + (f": {r.page_change}" if r.page_change else "")
                for r in rows[:8]
            )
            if caused:
                detail += (
                    ". Page changes are what a scrape saw in the same weeks: a lead to check, "
                    "not proof of the cause."
                )
            out.append(
                _alert(
                    "ranking_drop",
                    "high" if severe else "medium",
                    website,
                    f"{prefix}{name} dropped on {len(rows)} keyword(s)"
                    + (f", {len(caused)} after page changes" if caused else ""),
                    detail[:800],
                    urls=list(dict.fromkeys(r.url_before for r in rows if r.url_before)),
                )
            )
        else:
            ahead = [
                r
                for r in rows
                if r.client_position is not None
                and r.client_position <= TOP
                and r.after is not None
                and r.after < r.client_position
            ]
            out.append(
                _alert(
                    "competitor_ranking_gain",
                    "high" if ahead else "medium",
                    website,
                    f"{prefix}{name} entered the top 3 for {len(rows)} keyword(s) the client "
                    "ranks for",
                    "; ".join(
                        f"“{r.keyword}” {_place(r.before)} → {_place(r.after)}, {r.volume:,}/mo"
                        + (
                            f" (client {_place(r.client_position)})"
                            if r.client_position
                            else " (client no longer ranks)"
                        )
                        for r in rows[:8]
                    )[:800],
                    urls=list(dict.fromkeys(r.url_after for r in rows if r.url_after)),
                )
            )
    client = next((r for r in signals.links if r.role == "client"), None)
    for link in signals.links:
        if link.role == "client" and (len(link.lost) >= LOST_ALERT or link.lost_strong):
            out.append(
                _alert(
                    "backlinks_lost",
                    "high" if len(link.lost_strong) >= 3 or len(link.lost) >= 20 else "medium",
                    link.website,
                    f"{prefix}{link.name} lost {len(link.lost)} referring domain(s)",
                    (
                        (
                            "Strongest: " + ", ".join(link.lost_strong[:8]) + ". "
                            if link.lost_strong
                            else ""
                        )
                        + "Others: "
                        + ", ".join(d for d in link.lost[:12])
                        + (f". {link.note}" if link.note else "")
                    )[:700],
                )
            )
        if link.dr_before is None or link.dr_after is None:
            continue
        delta = link.dr_after - link.dr_before
        overtook = (
            link.role == "competitor"
            and client is not None
            and client.dr_before is not None
            and client.dr_after is not None
            and link.dr_before <= client.dr_before
            and link.dr_after > client.dr_after
        )
        if abs(delta) < DR_STEP and not overtook:
            continue
        if link.role == "client":
            severity: Severity = (
                "info" if delta > 0 else ("high" if delta <= -2 * DR_STEP else "medium")
            )
        else:
            severity = "high" if overtook else ("medium" if delta > 0 else "info")
        out.append(
            _alert(
                "authority_shift",
                severity,
                link.website,
                f"{prefix}{link.name}: Domain Rating {link.dr_before:.0f} → {link.dr_after:.0f}"
                + (" (now above the client)" if overtook else ""),
                (
                    f"Referring domains {link.refdomains_before:,} → {link.refdomains_after:,}."
                    if link.refdomains_before is not None and link.refdomains_after is not None
                    else "Referring domain totals not in both snapshots."
                )
                + (f" {link.note}" if link.note else ""),
            )
        )
    return out


def _alerts(
    diffs: list[SiteDiff],
    drift: list[ThemeDrift],
    current: ThemeMap,
    push_threshold: int,
    topic_drift: float = TOPIC_DRIFT,
    signals: SearchSignals | None = None,
) -> list[Alert]:
    roles = {d.website: d.role for d in diffs}
    owners = {t.theme_id: t.ownership for t in current.themes}
    alerts: list[Alert] = []
    for theme in drift:
        for site, count in theme.new_pages.items():
            if roles.get(site) != "competitor" or count < 1:
                continue
            urls = [
                p.url
                for d in diffs
                if d.website == site
                for p in d.new_pages
                if p.theme_id == theme.theme_id
            ]
            client_theme = owners.get(theme.theme_id) in _CLIENT_SIDE
            if client_theme and count >= push_threshold:
                alerts.append(
                    _alert(
                        "competitor_push_in_client_theme",
                        "high",
                        site,
                        f"{site} added {count} pages in {theme.label}",
                        f"A theme the client covers ({owners.get(theme.theme_id)}); the competitor "
                        f"now has {theme.pages_after.get(site, 0)} pages in it.",
                        theme_id=theme.theme_id,
                        urls=urls,
                    )
                )
            elif theme.status == "new" or not client_theme:
                alerts.append(
                    _alert(
                        "competitor_new_theme",
                        "medium",
                        site,
                        f"{site} is building {theme.label}",
                        f"{count} new page(s) in a theme the client does not cover.",
                        theme_id=theme.theme_id,
                        urls=urls,
                    )
                )
        before, after = theme.ownership_before, theme.ownership_after
        if before and after and before != after:
            worse = _RANK.get(after, 0) < _RANK.get(before, 0)
            alerts.append(
                _alert(
                    "ownership_shift",
                    "high" if worse else "info",
                    "",
                    f"{theme.label}: {before.replace('_', ' ')} → {after.replace('_', ' ')}",
                    "Pages per website before "
                    + ", ".join(f"{k} {v}" for k, v in sorted(theme.pages_before.items()))
                    + "; after "
                    + ", ".join(f"{k} {v}" for k, v in sorted(theme.pages_after.items())),
                    theme_id=theme.theme_id,
                )
            )
    for d in diffs:
        priced = [c for c in d.changed_pages if c.prices]
        if priced:
            detail = "; ".join(
                f"{p.before} → {p.after} ({p.context})" for c in priced for p in c.prices
            )
            alerts.append(
                _alert(
                    "price_change",
                    "high" if d.role == "competitor" else "medium",
                    d.website,
                    f"Prices changed on {len(priced)} page(s) of {d.name}",
                    detail[:600],
                    urls=[c.url for c in priced],
                )
            )
        if d.removed_pages:
            client = d.role == "client"
            alerts.append(
                _alert(
                    "client_pages_removed" if client else "competitor_pages_removed",
                    "high" if client else "low",
                    d.website,
                    f"{len(d.removed_pages)} page(s) gone from {d.name}",
                    "Check that these were removed on purpose and redirected."
                    if client
                    else "The competitor dropped these pages.",
                    urls=[p.url for p in d.removed_pages],
                )
            )
        rewrites = [c for c in d.changed_pages if c.head_changes]
        if d.role == "competitor" and len(rewrites) >= _REWRITE_ALERT:
            alerts.append(
                _alert(
                    "competitor_rewrites",
                    "medium",
                    d.website,
                    f"{d.name} rewrote titles, H1s or descriptions on {len(rewrites)} page(s)",
                    "Often an SEO push: compare the new wording with the client's pages.",
                    urls=[c.url for c in rewrites],
                )
            )
        if d.role == "competitor":
            alerts += _competitor_moves(d, topic_drift)
        alerts += _seo_alerts(d)
        drifted = [c for c in d.changed_pages if c.is_pillar and c.drift >= PILLAR_DRIFT]
        if drifted:
            alerts.append(
                _alert(
                    "pillar_drift",
                    "medium",
                    d.website,
                    f"{len(drifted)} pillar page(s) of {d.name} changed substantially",
                    "; ".join(
                        f"{c.title or c.url}: {round(c.drift * 100)}% of sections" for c in drifted
                    )[:600],
                    urls=[c.url for c in drifted],
                )
            )
        if d.role == "client" and d.changed_pages:
            alerts.append(
                _alert(
                    "client_content_change",
                    "info",
                    d.website,
                    f"{len(d.changed_pages)} client page(s) changed",
                    "Changes on the client's own website since the last scrape.",
                    urls=[c.url for c in d.changed_pages],
                )
            )
    alerts += _signal_alerts(signals)
    return sorted(alerts, key=lambda a: (_SEVERITY_ORDER[a.severity], a.kind, a.website, a.title))


def run_check(
    play: Playground,
    project_id: str,
    previous: MonitorCheck | None,
    *,
    push_threshold: int = 3,
    trigger: Literal["manual", "auto"] = "manual",
    meaning_cache: VectorCache | None = None,
    topic_drift: float = TOPIC_DRIFT,
    research: tuple[ResearchSnapshot | None, ResearchSnapshot | None] = (None, None),
    backlinks: tuple[BacklinkSnapshot | None, BacklinkSnapshot | None] = (None, None),
    earlier_diffs: list[SiteDiff] | None = None,
    rank_min_volume: int = 50,
) -> MonitorCheck:
    """One check of the project (it also saves a fresh theme map).

    With `meaning_cache`, changed pages are scored by meaning (old vs new) with each
    website's index model, and competitor pages below `topic_drift` raise an alert.
    `research` and `backlinks` are (baseline, newest) snapshots; `earlier_diffs` are
    the comparisons of earlier checks, so a ranking drop can be matched with a page
    change seen by an earlier check in the same weeks.
    """
    store = theme_store(play.store)
    before = (
        store.get(project_id, previous.theme_map_id)
        if previous is not None and previous.theme_map_id
        else None
    )
    current, _files = build_project_themes(play.store, play.access, project_id)
    pillars = {c.pillar.url for t in current.themes for c in t.coverage if c.pillar is not None}
    notes: list[str] = []
    diffs = _site_diffs(
        play,
        previous,
        _themes_of(current),
        _themes_of(before or current),
        pillars,
        meaning_cache,
        notes,
    )
    drift = _drift(current, before, diffs)
    signals = SearchSignals()
    rank_signals(
        signals,
        research[0],
        research[1],
        [*diffs, *(earlier_diffs or [])],
        min_volume=rank_min_volume,
    )
    link_signals(signals, backlinks[0], backlinks[1])
    alerts = _alerts(diffs, drift, current, push_threshold, topic_drift, signals)
    created = datetime.now(UTC)
    return MonitorCheck(
        check_id="check-"
        + created.strftime("%Y%m%d-%H%M%S-")
        + f"{created.microsecond // 1000:03d}",
        project_id=project_id,
        created_at=created,
        trigger=trigger,
        previous_check_id=previous.check_id if previous else None,
        theme_map_id=current.map_id,
        previous_theme_map_id=before.map_id if before else None,
        websites=diffs,
        theme_drift=drift,
        alerts=alerts,
        stats={
            "new_pages": sum(len(d.new_pages) for d in diffs),
            "removed_pages": sum(len(d.removed_pages) for d in diffs),
            "changed_pages": sum(len(d.changed_pages) for d in diffs),
            "price_changes": sum(len(c.prices) for d in diffs for c in d.changed_pages),
            "topic_drift_pages": sum(
                1
                for d in diffs
                for c in d.changed_pages
                if c.meaning is not None and c.meaning < topic_drift
            ),
            "seo_regressions": sum(
                1 for d in diffs if d.role == "client" for c in d.changed_pages if c.seo_regressions
            ),
            "ranking_drops": sum(1 for r in signals.rankings if r.role == "client"),
            "competitor_gains": sum(1 for r in signals.rankings if r.role == "competitor"),
            "alerts_high": sum(1 for a in alerts if a.severity == "high"),
            "alerts": len(alerts),
        },
        signals=signals,
        notes=notes,
    )
