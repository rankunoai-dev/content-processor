"""The monthly Semrush cap holds across pulls running at the same time."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.modules.research.semrush import SemrushClient, SemrushError, UnitBudget, UnitEntry, UnitLog


def _budget(log: UnitLog, monthly: int = 100, pull: int = 100) -> UnitBudget:
    return UnitBudget(log, enabled=True, monthly_units=monthly, pull_units=pull)


def test_two_pulls_cannot_both_pass_the_monthly_cap(tmp_path: Path) -> None:
    log = UnitLog(tmp_path)
    first, second = _budget(log), _budget(log)
    assert first.left == 100 and second.left == 100
    assert first.reserve("domain_organic", 6) == 60  # 60 of the month's 100, not logged yet
    reserved = json.loads(log.reserved_path.read_text(encoding="utf-8"))
    assert reserved[first.key]["units"] == 60 and log.reserved(except_key=first.key) == 0
    assert log.reserved() == 60
    with pytest.raises(SemrushError, match="only 40 are left"):
        second.reserve("domain_organic", 6)  # would be 120 for the month together
    assert second.reserve("domain_organic", 4) == 40
    with pytest.raises(SemrushError, match="only 0 are left"):
        first.reserve("domain_ranks", 1)  # its own 60 and the other pull's 40 fill the month
    # the first call comes back smaller than its worst case: logged, then the reservation goes
    log.add(
        UnitEntry(
            at=datetime.now(UTC),
            project_id="p",
            report="domain_organic",
            target="d",
            lines=2,
            units=20,
        )
    )
    first.charge(20)
    assert first.spent == 20 and log.reserved(except_key=second.key) == 0
    assert first.reserve("domain_organic", 4) == 40  # 20 logged + 40 reserved by the other + 40
    second.release()
    assert log.reserved(except_key=first.key) == 0
    # a third pull starting now sees the log and the live reservations
    third = _budget(log)
    assert third.left == 100 - 20 - 40
    # a reservation left by a pull that died an hour ago no longer counts
    log.set_reservation("ghost", 500, now=datetime.now(UTC) - timedelta(hours=2))
    assert log.reserved() == 40
    assert _budget(log).left == 40


def test_reservations_are_serialised_between_threads(tmp_path: Path) -> None:
    log = UnitLog(tmp_path)
    budgets = [_budget(log, monthly=100, pull=100) for _ in range(8)]
    outcomes: list[bool] = []
    gate = threading.Barrier(len(budgets))

    def pull(budget: UnitBudget) -> None:
        gate.wait()
        try:
            budget.reserve("domain_organic", 3)  # 30 units each; the month has room for 3
            outcomes.append(True)
        except SemrushError:
            outcomes.append(False)

    workers = [threading.Thread(target=pull, args=(b,)) for b in budgets]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)
    assert outcomes.count(True) == 3 and len(outcomes) == 8
    assert log.reserved() == 90


def test_a_failed_call_and_the_end_of_a_pull_let_the_reservation_go(tmp_path: Path) -> None:
    log = UnitLog(tmp_path)
    budget = _budget(log, monthly=1000, pull=1000)
    answers = iter(
        [
            httpx.Response(200, text="ERROR 132 :: API UNITS BALANCE IS ZERO"),
            httpx.Response(200, text="Dn;Ur\nrival.com;https://rival.com/\n"),
        ]
    )
    transport = httpx.MockTransport(lambda _request: next(answers))
    client = SemrushClient("k", budget, log, "p", client=httpx.Client(transport=transport))
    with pytest.raises(SemrushError, match="API UNITS"):
        client.phrase_organic("term life", "us", 10)
    assert log.reserved() == 0 and budget.spent == 0
    rows = client.phrase_organic("term life", "us", 10)
    assert len(rows) == 1 and budget.spent == 10 and log.reserved() == 0
    assert log.used(datetime.now(UTC) - timedelta(minutes=1)) == 10
    budget.reserve("domain_ranks", 1)
    assert log.reserved() == 10
    client.close()
    assert log.reserved() == 0
