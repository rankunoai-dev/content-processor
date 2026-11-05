"""Make research snapshots: pull from Semrush or import exports; place keywords in themes.

- **Pull**: for every website in the project's scope, its domain overview and the
  keywords it ranks for (up to the keyword limit, by traffic); optionally demand and
  the top results for chosen keywords. Every call is priced and capped first.
  **Fair split**: the pull budget (after what the chosen keywords and the competitor
  report need) is shared out website by website, client first: each gets an equal
  share of what is left, so the first website cannot use up what the others need,
  and what one does not use passes to the next. A website whose share cannot pay for
  its overview and `MIN_KEYWORDS` keywords is left out, with a note.
  **Fresh data is reused**: websites in `reuse` (pulled recently) are copied from the
  previous snapshot instead of being paid for again.
- **Import**: one website's Semrush "Organic Positions" export; the other websites keep
  the data of the latest snapshot.

Each keyword is placed in a theme of the project's theme map: by its ranking page
when that page is in the map, else by the theme whose terms share most of its words.
Demand per theme sums the volume of its distinct keywords; each website's visibility
is the number of its keywords in the top 10 and its estimated visits (volume times
the click-through rate of its position).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from src.modules.analyst.playground import Playground
from src.modules.research.importer import keyword_rows
from src.modules.research.models import (
    DomainOverview,
    KeywordMetrics,
    KeywordRow,
    Mover,
    ResearchSnapshot,
    SerpRow,
    Source,
    ThemeDemand,
    WebsiteResearch,
)
from src.modules.research.semrush import UNITS_PER_LINE, SemrushClient
from src.modules.theming.models import ThemeMap

__all__ = [
    "CTR",
    "MIN_KEYWORDS",
    "fair_lines",
    "estimated_visits",
    "finish_snapshot",
    "import_website",
    "movers",
    "place_keywords",
    "pull_snapshot",
]

# Organic click-through rate by position (a common industry curve; an estimate).
CTR: dict[int, float] = {
    1: 0.28,
    2: 0.15,
    3: 0.10,
    4: 0.07,
    5: 0.05,
    6: 0.04,
    7: 0.03,
    8: 0.025,
    9: 0.02,
    10: 0.018,
}
_PAGE_TWO = 0.01
MIN_KEYWORDS = 10
_WORD = re.compile(r"[a-z0-9]+")
_MATCH_SHARE = 0.5
_MOVE = 3


def estimated_visits(volume: int, position: int) -> float:
    """Monthly visits a position brings (volume times the position's click-through rate)."""
    rate = CTR.get(position, _PAGE_TWO if position <= 20 else 0.0)
    return round(volume * rate, 1)


def _now_id(prefix: str) -> tuple[datetime, str]:
    moment = datetime.now(UTC)
    return moment, prefix + moment.strftime("%Y%m%d-%H%M%S-") + f"{moment.microsecond // 1000:03d}"


def _int(text: str | None) -> int:
    try:
        return int(float((text or "0").replace(",", "")))
    except ValueError:
        return 0


def _float(text: str | None) -> float:
    try:
        return float((text or "0").replace(",", ""))
    except ValueError:
        return 0.0


def _overview(rows: list[dict[str, str]]) -> DomainOverview | None:
    if not rows:
        return None
    row = {k.casefold(): v for k, v in rows[0].items()}
    rank = _int(row.get("rank"))
    return DomainOverview(
        organic_keywords=_int(row.get("organic keywords")),
        organic_traffic=_int(row.get("organic traffic")),
        organic_cost=max(0.0, _float(row.get("organic cost"))),
        rank=rank or None,
    )


def _role(play: Playground, website: object) -> Literal["client", "competitor"]:
    return "client" if play.role_in_run(website) == "client" else "competitor"  # type: ignore[arg-type]


def fair_lines(left: int, websites_left: int, keyword_limit: int) -> tuple[int, int]:
    """(keyword lines, units share) for the next website: an equal share of what is left."""
    share = left // max(1, websites_left)
    lines = (share - UNITS_PER_LINE["domain_ranks"]) // UNITS_PER_LINE["domain_organic"]
    return max(0, min(keyword_limit, lines)), share


def pull_snapshot(
    play: Playground,
    client: SemrushClient,
    *,
    database: str,
    keyword_limit: int,
    serp_keywords: Sequence[str] = (),
    reserve: int = 0,
    reuse: Mapping[str, WebsiteResearch] | None = None,
) -> ResearchSnapshot:
    """Pull every website of the scope (and the chosen keywords) from Semrush.

    `reserve` units are kept for calls made after the websites (the chosen keywords
    are pulled here, after the websites; the competitor report by the caller).
    """
    created, snapshot_id = _now_id("research-")
    websites: list[WebsiteResearch] = []
    notes: list[str] = []
    kept = dict(reuse or {})
    order = sorted(play.websites(), key=lambda w: (_role(play, w) != "client", w.name.lower()))
    to_pull = [w for w in order if w.key not in kept]
    shares: list[str] = []
    for website in order:
        if website.key in kept:
            old = kept[website.key]
            websites.append(
                old.model_copy(
                    update={
                        "note": f"reused from the pull of {old.pulled_at:%d %b %Y} "
                        "(recent enough; not paid for again)"
                    }
                )
            )
            continue
        remaining = len(to_pull) - to_pull.index(website)
        lines, share = fair_lines(client.budget.left - reserve, remaining, keyword_limit)
        if lines < MIN_KEYWORDS:
            notes.append(
                f"{website.name} was left out: its share of the pull budget ({share} units) "
                f"cannot pay for its overview and {MIN_KEYWORDS} keywords"
            )
            continue
        overview = _overview(client.domain_overview(website.key, database))
        rows = keyword_rows(client.domain_organic(website.key, database, lines))
        shares.append(f"{website.name} {lines}")
        websites.append(
            WebsiteResearch(
                website=website.key,
                name=website.name,
                role=_role(play, website),
                source="semrush_api",
                pulled_at=datetime.now(UTC),
                overview=overview,
                keywords=rows,
                note="" if rows else "Semrush has no ranking keywords for this domain",
                keyword_limit=lines,
            )
        )
    if shares and any(not s.endswith(f" {keyword_limit}") for s in shares):
        notes.append(
            "The pull budget was shared fairly: keywords asked per website: " + ", ".join(shares)
        )
    keywords: list[KeywordMetrics] = []
    for phrase in dict.fromkeys(p.strip() for p in serp_keywords if p.strip()):
        metric = client.phrase_this(phrase, database)
        serp = client.phrase_organic(phrase, database)
        row = {k.casefold(): v for k, v in metric[0].items()} if metric else {}
        keywords.append(
            KeywordMetrics(
                keyword=phrase,
                volume=_int(row.get("search volume")),
                cpc=max(0.0, _float(row.get("cpc"))),
                competition=_float(row.get("competition")) if row.get("competition") else None,
                results=_int(row.get("number of results")) or None,
                serp=[
                    SerpRow(rank=i, domain=r.get("Domain", ""), url=r.get("Url", ""))
                    for i, r in enumerate(serp, start=1)
                ],
            )
        )
    return ResearchSnapshot(
        snapshot_id=snapshot_id,
        project_id=play.project_ids[0],
        created_at=created,
        database=database,
        websites=websites,
        keywords=keywords,
        units_spent=client.budget.spent,
        notes=notes,
    )


def import_website(
    play: Playground,
    website_text: str,
    rows: list[KeywordRow],
    previous: ResearchSnapshot | None,
    *,
    database: str,
    source: Source = "csv_import",
) -> ResearchSnapshot:
    """A new snapshot: this website's imported rows, the others from the latest snapshot."""
    website = play.website(website_text)
    created, snapshot_id = _now_id("research-")
    kept = [w for w in (previous.websites if previous else []) if w.website != website.key]
    imported = WebsiteResearch(
        website=website.key,
        name=website.name,
        role=_role(play, website),
        source=source,
        pulled_at=created,
        keywords=rows,
    )
    return ResearchSnapshot(
        snapshot_id=snapshot_id,
        project_id=play.project_ids[0],
        created_at=created,
        database=previous.database if previous else database,
        websites=sorted([*kept, imported], key=lambda w: (w.role != "client", w.website)),
        keywords=list(previous.keywords) if previous else [],
        notes=[f"{website.name} imported from a Semrush export ({len(rows)} keywords)"],
    )


# -- themes and demand ----------------------------------------------------------------------


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def _url_key(url: str) -> str:
    return url.split("#", 1)[0].split("?", 1)[0].rstrip("/").casefold()


def place_keywords(snapshot: ResearchSnapshot, theme_map: ThemeMap | None) -> None:
    """Set each keyword's theme: by its ranking page, else by shared words with a theme."""
    if theme_map is None:
        return
    by_url = {_url_key(p.url): p.theme_id for p in theme_map.pages if p.theme_id}
    theme_words = {t.theme_id: _words(" ".join(t.terms)) for t in theme_map.themes}

    def theme_for(keyword: str, url: str | None) -> str | None:
        if url and (found := by_url.get(_url_key(url))):
            return found
        words = _words(keyword)
        if not words:
            return None
        best: tuple[float, str] | None = None
        for theme_id, tw in theme_words.items():
            share = len(words & tw) / len(words)
            if share >= _MATCH_SHARE and (best is None or share > best[0]):
                best = (share, theme_id)
        return best[1] if best else None

    for site in snapshot.websites:
        for row in site.keywords:
            row.theme_id = theme_for(row.keyword, row.url)
    for metric in snapshot.keywords:
        metric.theme_id = theme_for(metric.keyword, None)
    snapshot.theme_map_id = theme_map.map_id


def demand_by_theme(snapshot: ResearchSnapshot, theme_map: ThemeMap | None) -> list[ThemeDemand]:
    """Demand and each website's visibility per theme (unplaced keywords grouped last)."""
    labels = {t.theme_id: t.label for t in theme_map.themes} if theme_map else {}
    groups: dict[str | None, dict[str, int]] = {}
    out: dict[str | None, ThemeDemand] = {}
    for site in snapshot.websites:
        for row in site.keywords:
            vols = groups.setdefault(row.theme_id, {})
            vols[row.keyword.casefold()] = max(vols.get(row.keyword.casefold(), 0), row.volume)
            entry = out.setdefault(
                row.theme_id,
                ThemeDemand(
                    theme_id=row.theme_id,
                    label=labels.get(row.theme_id or "", "Not in any theme"),
                    keywords=0,
                    volume=0,
                ),
            )
            entry.ranked[site.website] = entry.ranked.get(site.website, 0) + 1
            if row.position <= 10:
                entry.top10[site.website] = entry.top10.get(site.website, 0) + 1
            entry.traffic[site.website] = round(
                entry.traffic.get(site.website, 0.0) + estimated_visits(row.volume, row.position), 1
            )
    for theme_id, entry in out.items():
        entry.keywords = len(groups[theme_id])
        entry.volume = sum(groups[theme_id].values())
    return sorted(out.values(), key=lambda d: (d.theme_id is None, -d.volume, d.label))


def finish_snapshot(snapshot: ResearchSnapshot, theme_map: ThemeMap | None) -> ResearchSnapshot:
    """Place keywords in themes and compute demand per theme."""
    place_keywords(snapshot, theme_map)
    snapshot.demand = demand_by_theme(snapshot, theme_map)
    return snapshot


def movers(old: ResearchSnapshot, new: ResearchSnapshot, *, step: int = _MOVE) -> list[Mover]:
    """Keywords that moved `step` places or more, entered or left the top 10, or appeared."""
    before = {(w.website, r.keyword.casefold()): r for w in old.websites for r in w.keywords}
    out: list[Mover] = []
    seen: set[tuple[str, str]] = set()
    for site in new.websites:
        for row in site.keywords:
            key = (site.website, row.keyword.casefold())
            seen.add(key)
            prior = before.get(key)
            was = prior.position if prior else None
            crossed = (was or 101) <= 10 < row.position or row.position <= 10 < (was or 101)
            if was is None or abs(row.position - was) >= step or crossed:
                out.append(
                    Mover(
                        website=site.website,
                        keyword=row.keyword,
                        before=was,
                        after=row.position,
                        volume=row.volume,
                        url=row.url,
                    )
                )
    websites_now = {w.website for w in new.websites}
    for (site_key, _kw), row in before.items():
        if site_key in websites_now and (site_key, row.keyword.casefold()) not in seen:
            out.append(
                Mover(
                    website=site_key,
                    keyword=row.keyword,
                    before=row.position,
                    after=None,
                    volume=row.volume,
                    url=row.url,
                )
            )
    return sorted(out, key=lambda m: (-m.volume, m.website, m.keyword))


def keywords_of(snapshot: ResearchSnapshot, website: str) -> Iterable[KeywordRow]:
    """A website's keyword rows in a snapshot."""
    for site in snapshot.websites:
        if site.website == website:
            yield from site.keywords
