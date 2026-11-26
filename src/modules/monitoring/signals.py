"""Search and link signals of a check: ranking drops and gains, lost links, authority shifts.

A check compares the newest **research snapshot** (Research module: positions per
keyword) and the newest **backlink snapshot** (Backlinks module: Domain Rating and
referring domains) with the ones the previous check read, the same way it compares
scrapes: without a previous check, with the snapshot before the newest; when nothing
new was pulled or imported since, nothing is reported again.

Rules (all offline, $0; nothing is pulled here):

- **Ranking drop** (client): a keyword that was in the top 10 and is now below it, or
  that moved down `RANK_STEP`+ places while in the top 20. Keywords with fewer
  monthly searches than the project's `rank_min_volume` are left out unless they are
  buyer searches (commercial or transactional). A keyword missing from the new list
  counts as "out of the top 100" only when that list is complete; when the list was
  cut at a line limit it is marked uncertain (it may still rank, lower).
- **Competitor ranking gain**: a competitor entering the top 3 for a keyword the
  client also ranks for (or ranked for), with the same volume rule.
- Each client drop names what a scrape saw change on the page that ranked, when the
  scrape's window overlaps the two snapshots' window: removed, title or H1 rewritten,
  words cut, headings removed, topic changed, structured data removed, noindex. That
  is a lead to check, not proof of the cause; the detail says so.
- **Backlinks lost** (client): referring domains of the earlier snapshot gone now
  (the Backlinks module's rule for cut lists; spam domains left out).
- **Authority shift**: Domain Rating moving `DR_STEP`+ points, or a competitor
  overtaking the client.
- Snapshots from different Semrush databases are not compared; snapshots from the
  stand-ins mark every alert as dummy data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from src.modules.backlinks.insights import lost_domains
from src.modules.backlinks.models import BacklinkSnapshot, WebsiteBacklinks
from src.modules.content_processor.knowledge.catalog import url_key
from src.modules.monitoring.meaning import TOPIC_DRIFT
from src.modules.monitoring.models import (
    LinkSignal,
    PageChange,
    RankSignal,
    SearchSignals,
    SiteDiff,
)
from src.modules.research.models import KeywordRow, ResearchSnapshot, WebsiteResearch

__all__ = [
    "DR_STEP",
    "LOST_ALERT",
    "RANK_STEP",
    "describe_change",
    "link_signals",
    "rank_signals",
]

RANK_STEP = 5
TOP = 10
WATCH_TOP = 20
GAIN_TOP = 3
DR_STEP = 3.0
LOST_ALERT = 5
STRONG_DR = 50.0
_BUYER = frozenset({"commercial", "transactional"})
_DUMMY = "DUMMY DATA"


def _key(url: str | None) -> str | None:
    if not url:
        return None
    return url_key(url if "://" in url else "https://" + url)


def describe_change(change: PageChange) -> str:
    """What a scrape saw change on a page, in a few plain words."""
    parts: list[str] = []
    for name, label in (("title", "title"), ("h1", "H1")):
        if name in change.head_changes:
            parts.append(f"{label} rewritten")
    if change.words_before and change.words_after:
        delta = change.words_after - change.words_before
        if abs(delta) >= 100 or abs(delta) >= 0.2 * change.words_before:
            parts.append(f"words {change.words_before:,} → {change.words_after:,}")
    if change.headings_removed:
        parts.append(f"{len(change.headings_removed)} heading(s) removed")
    if change.meaning is not None and change.meaning < TOPIC_DRIFT:
        parts.append(f"topic changed (meaning {change.meaning:.2f})")
    parts += change.seo_regressions
    if not parts and change.drift:
        parts.append(f"{round(change.drift * 100)}% of its lines changed")
    return ", ".join(parts)


def _overlaps(
    diff: SiteDiff, start: datetime, end: datetime
) -> bool:  # a scrape window against a ranking window
    if diff.baseline_at is None or diff.latest_at is None:
        return False
    return diff.latest_at > start and diff.baseline_at < end


def _page_changes(
    diffs: Iterable[SiteDiff], website: str, start: datetime, end: datetime
) -> dict[str, str]:
    """Page key -> what changed on it, from every comparison overlapping the window."""
    out: dict[str, str] = {}
    for diff in diffs:
        if diff.website != website or not _overlaps(diff, start, end):
            continue
        for page in diff.removed_pages:
            if (k := _key(page.url)) is not None:
                out.setdefault(k, "the page was removed from the website")
        for change in diff.changed_pages:
            if (k := _key(change.url)) is not None and (text := describe_change(change)):
                out.setdefault(k, text)
    return out


def _counts(row: KeywordRow, min_volume: int) -> bool:
    return row.volume >= min_volume or (row.intent or "") in _BUYER


def _rows(site: WebsiteResearch) -> dict[str, KeywordRow]:
    return {r.keyword.casefold(): r for r in site.keywords}


def _themes(snapshot: ResearchSnapshot) -> dict[str, str]:
    return {d.theme_id: d.label for d in snapshot.demand if d.theme_id}


def _drops(
    old: WebsiteResearch,
    new: WebsiteResearch,
    min_volume: int,
    themes: dict[str, str],
    changes: dict[str, str],
) -> list[RankSignal]:
    now = _rows(new)
    out: list[RankSignal] = []
    for key, was in _rows(old).items():
        row = now.get(key)
        if was.position > WATCH_TOP or not _counts(row or was, min_volume):
            continue
        after = row.position if row else None
        uncertain = row is None and not new.complete
        left_top = was.position <= TOP and (after is None or after > TOP)
        fell = after is not None and after - was.position >= RANK_STEP
        if not (left_top or fell):
            continue
        url = was.url or (row.url if row else None)
        cause = changes.get(_key(url) or "", "")
        if row is not None and row.url and was.url and _key(row.url) != _key(was.url):
            cause = ", ".join(x for x in [cause, f"Google now ranks {row.url} instead"] if x)
        out.append(
            RankSignal(
                website=new.website,
                name=new.name,
                role="client",
                keyword=(row or was).keyword,
                before=was.position,
                after=after,
                volume=(row or was).volume,
                intent=(row or was).intent,
                theme=themes.get((row or was).theme_id or ""),
                url_before=was.url,
                url_after=row.url if row else None,
                uncertain=uncertain,
                page_change=cause,
            )
        )
    return out


def _gains(
    old: WebsiteResearch,
    new: WebsiteResearch,
    client_old: WebsiteResearch | None,
    client_new: WebsiteResearch | None,
    min_volume: int,
    themes: dict[str, str],
) -> list[RankSignal]:
    before = _rows(old)
    client_now = _rows(client_new) if client_new else {}
    client_then = _rows(client_old) if client_old else {}
    out: list[RankSignal] = []
    for key, row in _rows(new).items():
        was = before.get(key)
        if row.position > GAIN_TOP or (was is not None and was.position <= GAIN_TOP):
            continue
        if key not in client_now and key not in client_then:
            continue
        if not _counts(row, min_volume):
            continue
        mine = client_now.get(key)
        out.append(
            RankSignal(
                website=new.website,
                name=new.name,
                role="competitor",
                keyword=row.keyword,
                before=was.position if was else None,
                after=row.position,
                volume=row.volume,
                intent=row.intent,
                theme=themes.get(row.theme_id or ""),
                url_before=was.url if was else None,
                url_after=row.url,
                uncertain=was is None and not old.complete,
                client_position=mine.position if mine else None,
            )
        )
    return out


def rank_signals(
    signals: SearchSignals,
    old: ResearchSnapshot | None,
    new: ResearchSnapshot | None,
    diffs: Sequence[SiteDiff],
    *,
    min_volume: int,
) -> None:
    """Fill the ranking part of `signals` (notes when there is nothing to compare)."""
    if new is None:
        signals.research_note = "no search data yet (Research module)"
        return
    signals.research_after = new.snapshot_id
    if old is None:
        signals.research_note = "first search data: kept as the baseline for the next check"
        return
    signals.research_before = old.snapshot_id
    if old.snapshot_id == new.snapshot_id:
        signals.research_note = "no new search data since the last check"
        return
    if old.database != new.database:
        signals.research_note = (
            f"search data from different Semrush databases ({old.database} and "
            f"{new.database}) is not compared"
        )
        return
    signals.dummy = signals.dummy or any(_DUMMY in n for n in [*old.notes, *new.notes])
    themes = _themes(new)
    before = {w.website: w for w in old.websites}
    client_new = next((w for w in new.websites if w.role == "client"), None)
    client_old = before.get(client_new.website) if client_new else None
    rows: list[RankSignal] = []
    for site in new.websites:
        prior = before.get(site.website)
        if prior is None:
            continue
        if site.role == "client":
            changes = _page_changes(diffs, site.website, old.created_at, new.created_at)
            rows += _drops(prior, site, min_volume, themes, changes)
        else:
            rows += _gains(prior, site, client_old, client_new, min_volume, themes)
    signals.rankings = sorted(rows, key=lambda r: (r.role != "client", -r.volume, r.keyword))
    days = (new.created_at - old.created_at).days
    signals.research_note = (
        f"search data of {old.created_at:%Y-%m-%d} against {new.created_at:%Y-%m-%d} ({days} days)"
    )


def _refdomains(site: WebsiteBacklinks) -> int | None:
    return site.stats.live_refdomains if site.stats is not None else None


def _link_row(prev: WebsiteBacklinks, cur: WebsiteBacklinks) -> LinkSignal:
    spam = {r.domain for r in prev.refdomains if r.is_spam}
    rating = {r.domain: r.domain_rating or 0.0 for r in prev.refdomains}
    lost = [d for d in lost_domains(prev, cur) if d not in spam]
    strong = sorted((d for d in lost if rating.get(d, 0.0) >= STRONG_DR), key=lambda d: -rating[d])
    note = ""
    if prev.source != cur.source:
        note = "an export compared with an API pull: their coverage may differ"
    return LinkSignal(
        website=cur.website,
        name=cur.name,
        role=cur.role,
        dr_before=prev.domain_rating,
        dr_after=cur.domain_rating,
        refdomains_before=_refdomains(prev),
        refdomains_after=_refdomains(cur),
        lost=lost,
        lost_strong=[f"{d} (DR {rating[d]:.0f})" for d in strong],
        note=note,
    )


def link_signals(
    signals: SearchSignals, old: BacklinkSnapshot | None, new: BacklinkSnapshot | None
) -> None:
    """Fill the link part of `signals`."""
    if new is None:
        signals.backlinks_note = "no backlink data yet (Backlinks module)"
        return
    signals.backlinks_after = new.snapshot_id
    if old is None:
        signals.backlinks_note = "first backlink data: kept as the baseline for the next check"
        return
    signals.backlinks_before = old.snapshot_id
    if old.snapshot_id == new.snapshot_id:
        signals.backlinks_note = "no new backlink data since the last check"
        return
    signals.dummy = signals.dummy or any(_DUMMY in n for n in [*old.notes, *new.notes])
    before = {w.website: w for w in old.websites}
    signals.links = [_link_row(before[w.website], w) for w in new.websites if w.website in before]
    signals.backlinks_note = (
        f"backlink data of {old.created_at:%Y-%m-%d} against {new.created_at:%Y-%m-%d}"
    )
