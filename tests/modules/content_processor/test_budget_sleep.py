"""The runtime budget counts time the run worked, not time the computer slept."""

from __future__ import annotations

from src.modules.content_processor.contracts import Budgets
from src.modules.content_processor.scheduler.budget import SLEEP_GAP_S, BudgetMeter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_night_asleep_does_not_use_up_the_runtime_budget():
    """Live case: a 7,200 s budget, 11.5 h of sleep, 32 pages ended BUDGET_EXHAUSTED."""
    clock = Clock()
    meter = BudgetMeter(Budgets(max_runtime_s=7200), clock=clock)
    clock.now += 100
    assert meter.elapsed_s == 100
    clock.now += 11.5 * 3600  # the PC slept between two looks at the meter
    assert meter.exceeded() is None
    assert meter.elapsed_s == 100
    assert meter.remaining_runtime_s() == 7100
    clock.now += 50
    usage = meter.usage()["runtime_s"]
    assert usage == {"used": 150.0, "limit": 7200, "excluded_s": 41400.0}


def test_time_is_credited_in_the_increments_observed():
    clock = Clock()
    meter = BudgetMeter(Budgets(max_runtime_s=600), clock=clock)
    for _ in range(3):
        clock.now += SLEEP_GAP_S  # exactly the threshold still counts
        meter.exceeded()
    assert meter.exceeded() == "runtime"
    assert meter.excluded_s == 0


def test_a_single_long_wait_within_the_threshold_still_counts():
    clock = Clock()
    meter = BudgetMeter(Budgets(max_runtime_s=400), clock=clock)
    clock.now += 360 + 150  # one wait for held tasks plus one full render
    assert meter.exceeded() == "runtime"
    assert meter.remaining_runtime_s() == 0


def test_a_clock_that_goes_backwards_is_ignored():
    clock = Clock()
    meter = BudgetMeter(Budgets(), clock=clock)
    clock.now -= 5
    assert meter.elapsed_s == 0
    clock.now += 7
    assert meter.elapsed_s == 7
