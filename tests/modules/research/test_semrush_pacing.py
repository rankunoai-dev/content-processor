"""Semrush pacing, polite retries, the fair budget split and reuse of recent pulls."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from src.modules.research.semrush import Pacer, SemrushClient, SemrushError, UnitBudget, UnitLog
from src.modules.research.service import pull_project, unit_log
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.research.test_research import ON, semrush
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def test_pacer_keeps_calls_apart_and_holds_after_a_refusal() -> None:
    t = FakeClock()
    pacer = Pacer(0.5, clock=t.clock, sleep=t.sleep)
    for _ in range(3):
        pacer.wait()
    assert t.slept == [0.5, 0.5] and pacer.calls == 3 and pacer.waited_s == 1.0
    t.now += 2.0  # a quiet moment: no wait
    pacer.wait()
    assert t.slept == [0.5, 0.5]
    pacer.hold(3.0)
    pacer.wait()
    assert t.slept[-1] == 3.0


def test_429_is_retried_after_retry_after_and_not_charged(tmp_path: Path) -> None:
    answers = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(429),
        httpx.Response(200, text="Keyword;Position\nterm;3\n"),
    ]
    t = FakeClock()
    log = UnitLog(tmp_path)
    budget = UnitBudget(log, enabled=True, monthly_units=1000, pull_units=1000)
    client = SemrushClient(
        "k",
        budget,
        log,
        "p",
        client=httpx.Client(transport=httpx.MockTransport(lambda _r: answers.pop(0))),
        pacer=Pacer(0.5, clock=t.clock, sleep=t.sleep),
    )
    rows = client.domain_organic("rival.com", "us", 5)
    assert rows == [{"Keyword": "term", "Position": "3"}]
    assert client.retries == 2 and budget.spent == 10  # one line, charged once
    assert t.slept == [1.0, 4.0]  # Retry-After, then a growing pause (2 s x attempt 2)
    always = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(429)))
    stubborn = SemrushClient("k", budget, log, "p", client=always, pacer=Pacer(0.0))
    with pytest.raises(SemrushError, match="HTTP 429"):
        stubborn.domain_organic("rival.com", "us", 5)
    assert stubborn.retries == 2 and budget.spent == 10


def test_the_budget_is_shared_fairly_between_websites(themed: World) -> None:
    calls: list[dict[str, str]] = []
    tight = replace(ON, pull_units=1100, keyword_limit=200)
    snapshot, _ = pull_project(
        themed.store, themed.access, themed.project_id, tight, http=lambda: semrush(calls=calls)
    )
    asked = [(c["domain"], int(c["display_limit"])) for c in calls if c["type"] == "domain_organic"]
    # 1,100 units, 800 kept for the competitor report: the client gets half of 300 (14
    # keywords after its overview); it used 30, so the rival gets all of the 270 left (26)
    assert asked == [("example.com", 14), ("rival.com", 26)]
    assert any("shared fairly" in n and "Example Life 14" in n for n in snapshot.notes)
    client = next(w for w in snapshot.websites if w.role == "client")
    assert client.keyword_limit == 14 and not client.complete  # the overview counts 3; 2 came
    assert snapshot.units_spent <= 1100


def test_recent_pulls_are_reused_unless_refreshed(themed: World) -> None:
    fresh = replace(ON, fresh_days=14)
    first, _ = pull_project(themed.store, themed.access, themed.project_id, fresh, http=semrush)
    spent = unit_log(themed.store).used(first.created_at.replace(year=2000))
    calls: list[dict[str, str]] = []
    again, _ = pull_project(
        themed.store, themed.access, themed.project_id, fresh, http=lambda: semrush(calls=calls)
    )
    assert calls == [] and again.units_spent == 0
    assert all("reused from the pull of" in w.note for w in again.websites)
    assert any("competitor report was not pulled again" in n for n in again.notes)
    assert {w.website: len(w.keywords) for w in again.websites} == {
        w.website: len(w.keywords) for w in first.websites
    }
    assert unit_log(themed.store).used(first.created_at.replace(year=2000)) == spent
    forced, _ = pull_project(
        themed.store,
        themed.access,
        themed.project_id,
        fresh,
        http=lambda: semrush(calls=calls),
        refresh=True,
    )
    assert {c["type"] for c in calls} >= {
        "domain_ranks",
        "domain_organic",
        "domain_organic_organic",
    }
    assert forced.units_spent > 0
