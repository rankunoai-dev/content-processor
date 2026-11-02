"""Run a monitoring check of a project: diffs, theme drift and alerts.

For each website in the project's scope (its freshest copy):

- the scrape to compare against is the one that was latest at the previous check;
  without a previous check, the scrape before the latest one. When nothing new was
  scraped since the previous check the website is reported as unchanged.
- new, removed and changed pages come from `diff.py`.

The project's theme map is rebuilt (theme ids carried over), new pages are placed in
their themes, removed pages in the themes of the previous check's map, and theme
ownership is compared with that map. Alerts follow from both.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from src.modules.analyst.playground import Playground
from src.modules.monitoring.diff import diff_runs, load_run_files
from src.modules.monitoring.models import (
    Alert,
    AlertKind,
    MonitorCheck,
    PageRef,
    Severity,
    SiteDiff,
    ThemeDrift,
)
from src.modules.theming.models import ThemeMap
from src.modules.theming.service import build_project_themes, theme_store
from src.modules.theming.store import match_themes

__all__ = ["PILLAR_DRIFT", "run_check"]

PILLAR_DRIFT = 0.3
_REWRITE_ALERT = 1
_CLIENT_SIDE = {"client_owned", "contested", "competitor_led", "client_only"}
_RANK = {"client_only": 4, "client_owned": 3, "contested": 2, "competitor_led": 1, "client_gap": 0}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


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


def _site_diffs(
    play: Playground,
    previous: MonitorCheck | None,
    themes_now: dict[str, tuple[str, str]],
    themes_before: dict[str, tuple[str, str]],
    pillars: set[str],
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
        baseline = next((r for r in runs if r.run_id == base_id), None) if base_id else None
        if baseline is None and len(runs) >= 2:
            baseline = runs[-2]
        if baseline is None:
            diffs.append(
                SiteDiff(
                    website=website.key,
                    name=website.name,
                    role=role,
                    latest_run=latest.run_id,
                    latest_at=latest.finished_at,
                    note="first scrape: kept as the baseline for the next check",
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
        added, removed, changed = result.added, result.removed, result.changed
        unchanged = result.unchanged

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


def _alerts(
    diffs: list[SiteDiff], drift: list[ThemeDrift], current: ThemeMap, push_threshold: int
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
    return sorted(alerts, key=lambda a: (_SEVERITY_ORDER[a.severity], a.kind, a.website, a.title))


def run_check(
    play: Playground,
    project_id: str,
    previous: MonitorCheck | None,
    *,
    push_threshold: int = 3,
    trigger: Literal["manual", "auto"] = "manual",
) -> MonitorCheck:
    """One check of the project (it also saves a fresh theme map)."""
    store = theme_store(play.store)
    before = (
        store.get(project_id, previous.theme_map_id)
        if previous is not None and previous.theme_map_id
        else None
    )
    current, _files = build_project_themes(play.store, play.access, project_id)
    pillars = {c.pillar.url for t in current.themes for c in t.coverage if c.pillar is not None}
    diffs = _site_diffs(play, previous, _themes_of(current), _themes_of(before or current), pillars)
    drift = _drift(current, before, diffs)
    alerts = _alerts(diffs, drift, current, push_threshold)
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
            "alerts_high": sum(1 for a in alerts if a.severity == "high"),
            "alerts": len(alerts),
        },
    )
