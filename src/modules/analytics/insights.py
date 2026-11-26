"""What GA4 tells about the project: pure code over a snapshot and what the app holds ($0).

- **Rollups** per theme and per funnel stage of the theme map: sessions, organic sessions,
  key events, revenue and conversion rate, next to the theme's search demand, so themes
  that bring traffic can be told from themes that bring leads.
- **Leaky pages**: real search traffic (organic sessions, or Search Console clicks when the
  channel is unknown) of at least `LEAKY_SESSIONS` that does not engage (rate under
  `LOW_ENGAGEMENT`) or converts nothing while the site converts elsewhere: fix the page
  (intent, UX, call to action) before writing new ones.
- **Decays**: against the period before, organic sessions (or sessions) down by
  `DECAY` or more from at least `DECAY_MIN_SESSIONS`, or key events down by `DECAY` from at
  least `DECAY_MIN_EVENTS`: traffic or conversions falling while rankings may look flat.
- **CTAs without conversions**: a page with a form or a call to action, at least
  `CTA_SESSIONS` sessions and no key event, on a site that records key events elsewhere
  (so tracking works): a broken or ignored form.
- **AI referrals**: sessions sent by AI assistants, per page.
- **After edits**: pages Monitoring saw change between two scrapes, with their sessions and
  key events in the period before and the latest one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final, Literal

from src.modules.analytics.models import (
    AiReferral,
    AnalyticsReport,
    CtaGap,
    Decay,
    Ga4Row,
    Ga4Snapshot,
    GroupRollup,
    LeakyPage,
    PostEdit,
)
from src.modules.content_processor.knowledge.models import PageStamp
from src.modules.monitoring.models import PageChange
from src.modules.research.links import page_key
from src.modules.research.models import ThemeDemand
from src.modules.theming.models import ThemeMap

__all__ = [
    "CTA_SESSIONS",
    "DECAY",
    "LEAKY_SESSIONS",
    "LOW_ENGAGEMENT",
    "build_report",
    "by_page",
]

LEAKY_SESSIONS: Final = 100
LOW_ENGAGEMENT: Final = 0.40
DECAY: Final = 0.30
DECAY_MIN_SESSIONS: Final = 50
DECAY_MIN_EVENTS: Final = 5
CTA_SESSIONS: Final = 100
_FUNNEL: Final = {
    "learn": "Learn",
    "compare": "Compare",
    "decide": "Decide",
    "onboard": "Onboard",
    "support": "Support",
    "unknown": "Not placed",
}


def by_page(snapshot: Ga4Snapshot | None) -> dict[str, Ga4Row]:
    """The snapshot's rows by page key."""
    return {page_key(r.url): r for r in snapshot.rows} if snapshot else {}


def _rollup(
    groups: Mapping[str, list[Ga4Row]], labels: Mapping[str, str], volume: Mapping[str, int]
) -> list[GroupRollup]:
    out = []
    for key, rows in groups.items():
        out.append(
            GroupRollup(
                key=key,
                label=labels.get(key, key),
                pages=len(rows),
                sessions=sum(r.sessions for r in rows),
                organic_sessions=sum(r.organic_sessions for r in rows),
                engaged_sessions=sum(r.engaged_sessions for r in rows),
                key_events=round(sum(r.key_events for r in rows), 2),
                revenue=round(sum(r.revenue for r in rows), 2),
                search_volume=volume.get(key),
            )
        )
    return sorted(out, key=lambda g: (-g.key_events, -g.sessions))


def build_report(
    current: Ga4Snapshot,
    *,
    previous: Ga4Snapshot | None = None,
    theme_map: ThemeMap | None = None,
    demand: Sequence[ThemeDemand] = (),
    stamps: Mapping[str, PageStamp] | None = None,
    gsc_clicks: Mapping[str, int] | None = None,
    changed: Sequence[PageChange] = (),
    now: datetime,
) -> AnalyticsReport:
    """The project's analytics report from the latest GA4 snapshot (every input optional)."""
    rows = by_page(current)
    report = AnalyticsReport(
        project_id=current.project_id,
        website=current.website,
        built_at=now,
        snapshot_id=current.snapshot_id,
        previous_snapshot_id=previous.snapshot_id if previous else None,
        period=(
            f"{current.start_date} to {current.end_date}"
            if current.start_date and current.end_date
            else ""
        ),
    )
    total = current.sessions
    events = sum(r.key_events for r in current.rows)
    report.totals = {
        "sessions": total,
        "organic_sessions": sum(r.organic_sessions for r in current.rows),
        "engagement_rate": round(sum(r.engaged_sessions for r in current.rows) / total, 4)
        if total
        else 0.0,
        "key_events": round(events, 2),
        "revenue": round(sum(r.revenue for r in current.rows), 2),
        "ai_sessions": sum(r.ai_sessions for r in current.rows),
    }
    for r in current.rows:
        for channel, n in r.channels.items():
            report.channels[channel] = report.channels.get(channel, 0) + n
    report.channels = dict(sorted(report.channels.items(), key=lambda kv: -kv[1]))

    if theme_map is not None:
        site = current.website.removeprefix("www.")
        themes: dict[str, list[Ga4Row]] = {}
        funnel: dict[str, list[Ga4Row]] = {}
        for p in theme_map.pages:
            if p.website.removeprefix("www.") != site:
                continue
            row = rows.get(page_key(p.url))
            if row is None:
                continue
            if p.theme_id:
                themes.setdefault(p.theme_id, []).append(row)
            funnel.setdefault(p.funnel_stage, []).append(row)
        labels = {t.theme_id: t.label for t in theme_map.themes}
        volume = {d.theme_id: d.volume for d in demand if d.theme_id}
        report.themes = _rollup(themes, labels, volume)
        report.funnel = _rollup(funnel, _FUNNEL, {})
    else:
        report.notes.append("No theme map yet: rollups per theme and funnel stage are left out.")

    clicks = gsc_clicks or {}
    for key, r in rows.items():
        search = r.organic_sessions if current.has_channels else clicks.get(key, 0)
        if search < LEAKY_SESSIONS:
            continue
        reasons = []
        if r.engagement_rate < LOW_ENGAGEMENT:
            reasons.append(f"only {r.engagement_rate:.0%} of sessions engage")
        if r.key_events == 0 and events > 0:
            reasons.append("no key event while the site converts elsewhere")
        if reasons:
            report.leaky.append(
                LeakyPage(
                    url=r.url,
                    organic_sessions=r.organic_sessions,
                    gsc_clicks=clicks.get(key),
                    engagement_rate=r.engagement_rate,
                    key_events=r.key_events,
                    reason="; ".join(reasons),
                )
            )
    report.leaky.sort(key=lambda p: -(p.organic_sessions or p.gsc_clicks or 0))

    before = by_page(previous)
    for key, r in rows.items():
        old = before.get(key)
        if old is None:
            continue
        organic = current.has_channels and previous is not None and previous.has_channels
        a, b = (old.organic_sessions, r.organic_sessions) if organic else (old.sessions, r.sessions)
        if a >= DECAY_MIN_SESSIONS and b <= a * (1 - DECAY):
            metric: Literal["organic_sessions", "sessions"] = (
                "organic_sessions" if organic else "sessions"
            )
            report.decays.append(
                Decay(url=r.url, metric=metric, before=a, after=b, change=round(b / a - 1, 3))
            )
        if old.key_events >= DECAY_MIN_EVENTS and r.key_events <= old.key_events * (1 - DECAY):
            report.decays.append(
                Decay(
                    url=r.url,
                    metric="key_events",
                    before=old.key_events,
                    after=r.key_events,
                    change=round(r.key_events / old.key_events - 1, 3),
                )
            )
    report.decays.sort(key=lambda d: d.change)
    if previous is None:
        report.notes.append("No earlier period: import or pull the period before to see decays.")
    elif not (current.has_channels and previous.has_channels):
        report.notes.append("Decays compare all sessions: a channel column is missing.")

    stamped = {page_key(u): s for u, s in (stamps or {}).items()}
    if events > 0:
        for key, r in rows.items():
            stamp = stamped.get(key)
            if stamp is None or r.sessions < CTA_SESSIONS or r.key_events > 0:
                continue
            ctas = [c.text for c in stamp.ctas if c.text and c.strength == "strong"][:5]
            forms = int(stamp.components.get("form", 0) + stamp.components.get("forms", 0))
            if ctas or forms:
                report.cta_gaps.append(
                    CtaGap(url=r.url, sessions=r.sessions, ctas=ctas, forms=forms)
                )
        report.cta_gaps.sort(key=lambda c: -c.sessions)
    else:
        report.notes.append(
            "No key events recorded: conversion checks are left out (are key events set up?)."
        )

    report.ai_referrals = sorted(
        (
            AiReferral(url=r.url, sessions=r.ai_sessions, sources=r.ai_sources)
            for r in current.rows
            if r.ai_sessions
        ),
        key=lambda a: -a.sessions,
    )
    if not current.has_sources:
        report.notes.append(
            "No session source column: AI referrals are unknown (add Session source to see "
            "chatgpt.com, perplexity.ai and others)."
        )

    for change in changed:
        key = page_key(change.url)
        now_row, old_row = rows.get(key), before.get(key)
        if now_row is None and old_row is None:
            continue
        what = ", ".join(
            [*(f"{k} changed" for k in change.head_changes)]
            + ([f"{change.drift:.0%} of sections changed"] if change.drift else [])
        )
        report.post_edit.append(
            PostEdit(
                url=change.url,
                change=what or "content changed",
                sessions_before=old_row.sessions if old_row else 0,
                sessions_after=now_row.sessions if now_row else 0,
                key_events_before=old_row.key_events if old_row else 0.0,
                key_events_after=now_row.key_events if now_row else 0.0,
            )
        )
    return report
