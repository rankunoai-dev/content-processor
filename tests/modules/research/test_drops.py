"""Ranking drops: client keywords that left page one or fell 10+ places, as findings and alerts."""

from __future__ import annotations

from datetime import UTC, datetime

from src.modules.gaps.build import build_findings
from src.modules.monitoring.service import ranking_alerts
from src.modules.research.build import ranking_drops
from src.modules.research.models import KeywordRow, ResearchSnapshot, WebsiteResearch
from src.modules.research.service import research_store
from src.modules.theming.models import ThemeMap
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

NOW = datetime(2026, 11, 26, tzinfo=UTC)


def _snap(sid: str, rows: list[tuple[str, int, int]], *, limit: int | None = None,
          project: str = "p1") -> ResearchSnapshot:  # fmt: skip
    return ResearchSnapshot(
        snapshot_id=sid,
        project_id=project,
        created_at=NOW,
        database="us",
        websites=[
            WebsiteResearch(
                website="example.com",
                name="Example",
                role="client",
                source="semrush_api" if limit else "csv_import",
                pulled_at=NOW,
                keyword_limit=limit,
                keywords=[
                    KeywordRow(keyword=k, position=p, volume=v, url=f"https://example.com/{i}")
                    for i, (k, p, v) in enumerate(rows)
                ],
            )
        ],
    )


BEFORE = [
    ("term life quotes", 4, 8100),  # leaves page one
    ("whole life", 12, 2400),  # falls 10+
    ("annuity rates", 8, 5400),  # disappears
    ("steady", 5, 900),  # holds
    ("deep", 45, 100),  # was beyond 30: ignored
]
AFTER = [("term life quotes", 14, 8100), ("whole life", 25, 2400), ("steady", 6, 900),
         ("deep", 90, 100), ("annuity rates x", 50, 10)]  # fmt: skip


def test_drops_are_page_one_exits_big_falls_and_disappearances() -> None:
    old = _snap("research-20261101-000000-000", BEFORE)
    drops = ranking_drops(old, _snap("research-20261126-000000-000", AFTER), ["www.example.com"])
    assert [(d.keyword, d.before, d.after, d.certain) for d in drops] == [
        ("term life quotes", 4, 14, True),
        ("annuity rates", 8, None, True),
        ("whole life", 12, 25, True),
    ]
    cut = _snap("research-20261126-000000-000", AFTER, limit=5)  # 5 rows = the limit: cut short
    gone = next(d for d in ranking_drops(old, cut, ["example.com"]) if d.after is None)
    assert not gone.certain


def test_drops_become_findings_ranked_by_certainty_and_demand() -> None:
    theme_map = ThemeMap.model_validate(
        {"map_id": "m", "project_id": "p1", "built_at": NOW, "method": "t", "websites": [],
         "themes": [], "pages": []}
    )  # fmt: skip
    old = _snap("research-20261101-000000-000", BEFORE)
    new = _snap("research-20261126-000000-000", AFTER, limit=5)
    findings = [
        f
        for f in build_findings(
            theme_map, {"example.com": "client"}, research=new, previous_research=old
        )
        if f.kind == "ranking_drop"
    ]
    assert [f.title.split("”")[0].lstrip("“") for f in findings] == [
        "term life quotes",
        "whole life",
        "annuity rates",
    ]
    unsure = findings[-1]
    assert "cut short" in unsure.detail and unsure.priority < findings[0].priority
    assert "#4 to #14" in findings[0].title


def test_certain_drops_become_monitoring_alerts(themed: World) -> None:
    store = research_store(themed.store)
    pid = themed.project_id
    store.save(_snap("research-20261101-000000-000", BEFORE, project=pid))
    store.save(_snap("research-20261126-000000-000", AFTER, project=pid))
    alerts = ranking_alerts(themed.store, pid)
    assert [a.title for a in alerts] == [
        "“term life quotes” fell from #4 to #14",
        "“annuity rates” fell from #8 to out of the top 100",
        "“whole life” fell from #12 to #25",
    ]
    assert alerts[0].kind == "client_ranking_drop"
    assert ranking_alerts(themed.store, "nothing-here") == []
