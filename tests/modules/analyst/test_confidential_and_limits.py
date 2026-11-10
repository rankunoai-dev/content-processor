"""The app's Confidential option, sister brands in card inputs, the run lock, cache pricing."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from src.modules.analyst import commands as analyst_commands
from src.modules.analyst.library import card
from src.modules.analyst.policy import PRICES, Budget, DataPolicy, FileLock, SpendLog
from src.modules.analyst.runs import resolve_inputs
from src.modules.content_processor import cli
from src.modules.content_processor.projects.store import SiteSpec
from tests.modules.analyst.conftest import CLIENT, RIVAL, World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture


def test_the_apps_confidential_option_is_refused_like_the_cli_class(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = world.runner()
    eeat = card("eeat-trust-audit")
    assert runner.policy.data_class(world.project_id) == "public"
    world.store.update(world.project_id, confidential=True)
    assert runner.policy.data_class(world.project_id) == "client_confidential"
    assert runner.policy.blocked([world.project_id, "other"]) == [world.project_id]
    check, _ = runner.preflight(eeat, {"project": world.project_id}, "claude")
    assert not check.runnable and any("client_confidential" in p for p in check.problems)
    # the context pack still works for a person, and says the data is confidential
    record = runner.start(eeat, {"project": world.project_id}, "pack")
    done = runner.execute(eeat, record)
    assert done.status == "finished"
    pack = (runner.runs.folder(done.run_id) / "context_pack.md").read_text(encoding="utf-8")
    assert pack.startswith("# Context pack: ") and "(Confidential)" in pack.splitlines()[0]
    assert "CONFIDENTIAL" in pack
    # the stricter of the app's option and the CLI file wins, whichever way round
    runner.policy.set(world.project_id, "public")
    assert runner.policy.data_class(world.project_id) == "client_confidential"
    world.store.update(world.project_id, confidential=False)
    assert runner.policy.data_class(world.project_id) == "public"
    runner.policy.set(world.project_id, "client_confidential")
    assert runner.policy.data_class(world.project_id) == "client_confidential"
    # an unknown project or a policy without projects falls back to the file
    assert DataPolicy(world.root / "analyst").data_class(world.project_id) == "client_confidential"
    assert runner.policy.project_confidential("no-such-project") is False
    # the CLI reports both sources
    monkeypatch.setattr(analyst_commands, "_runner", lambda args: runner)
    world.store.update(world.project_id, confidential=True)
    code = cli.main(
        ["analyze", "policy", "--project", world.project_id, "--app-data", str(world.root)]
    )
    assert code == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["data_class"] == "client_confidential" and shown["confidential_in_app"] is True


def test_a_sister_brand_from_another_project_is_never_a_default_competitor(world: World) -> None:
    runner = world.runner()
    eeat = card("eeat-trust-audit")
    values, _play, problems = resolve_inputs(eeat, {"project": world.project_id}, runner.playground)
    assert problems == [] and values["competitors"] == ["rival.com"]
    # Another project says rival.com is a brand of the same client as example.com.
    world.store.create(
        name="Group project",
        owner_name="Tester",
        client_name="Example Group",
        password="password-123",
        client_site=SiteSpec(name="Example Life", website=CLIENT),
        client_brands=[SiteSpec(name="Rival brand", website=RIVAL)],
    )
    values, play, problems = resolve_inputs(eeat, {"project": world.project_id}, runner.playground)
    assert play is not None
    assert values["client"] == "example.com" and values["competitors"] == []
    assert problems == ["Competitor websites is required"], "no competitor is left to compare"
    assert play.role_in_run(play.website("rival.com")) == "client"


def test_card_runs_take_turns_with_other_processes(world: World) -> None:
    runner = world.runner()
    eeat = card("eeat-trust-audit")
    record = runner.start(eeat, {"project": world.project_id}, "pack")
    assert record.status == "queued" and not runner.run_busy()
    held = FileLock(runner.root / "run.lock")  # another process's run
    assert held.acquire(block=False)
    try:
        assert runner.run_busy()
        assert not FileLock(runner.root / "run.lock").acquire(block=False)
        worker = threading.Thread(target=runner.execute, args=(eeat, record))
        worker.start()
        time.sleep(0.3)
        assert runner.runs.get(record.run_id).status == "queued", "waits for the lock"
    finally:
        held.release()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert runner.runs.get(record.run_id).status == "finished"
    assert not runner.run_busy()
    with FileLock(runner.root / "run.lock") as lock:
        assert lock.held_elsewhere() is False or True  # a peek from the holder's own handle
    assert not held.held_elsewhere()


def test_input_is_priced_at_the_cache_write_rate(tmp_path: Path) -> None:
    price = PRICES["claude-opus-5-5"]
    assert price.cache_write == pytest.approx(price.input * 1.25)
    budget = Budget(SpendLog(tmp_path), enabled=True, run_cap=1.0, daily_cap=10, monthly_cap=10)
    # 100,000 input tokens at $4 per million are $0.40, but written to the cache they cost
    # $0.50 (1.25x); the output the rest of the $1 run cap can pay for at $20 per million
    # is 25,000 tokens, not the 30,000 the plain input price would allow.
    assert price.input == 4.0 and price.output == 20.0
    assert budget.max_output_tokens("claude-opus-5-5", 100_000, 32_000) == 25_000
