"""A theme map built before the latest finished scrape is rebuilt, wherever it is read."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from src.modules.analyst.playground import Playground
from src.modules.analyst.tools import run_tool
from src.modules.content_processor.projects.models import RunState
from src.modules.gaps.service import build_project_gaps
from src.modules.research.service import theme_map_for, theme_map_stale
from src.modules.theming.service import theme_store
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture


def _finished_scrape(themed: World, run_id: str, when: datetime) -> None:
    themed.store.save_run(
        RunState(
            run_id=run_id,
            project_id=themed.project_id,
            site_id=themed.site_ids["competitor"],
            status="finished",
            created_at=when,
            started_at=when,
            finished_at=when,
            started_by="test",
            url_count=1,
            batch_size=25,
            batches_total=1,
            batches_done=1,
        )
    )


def test_a_theme_map_older_than_the_latest_scrape_is_rebuilt(themed: World) -> None:
    store, access, pid = themed.store, themed.access, themed.project_id
    first, rebuilt = theme_map_for(store, access, pid)
    assert rebuilt and theme_store(store).latest(pid) is not None
    again, rebuilt = theme_map_for(store, access, pid)
    assert not rebuilt and again.map_id == first.map_id
    assert not theme_map_stale(store, pid, first)
    # a scrape of one website finishes after the map was built
    _finished_scrape(themed, "run-r9", datetime.now(UTC))
    assert theme_map_stale(store, pid, first)
    fresh, rebuilt = theme_map_for(store, access, pid)
    assert rebuilt and fresh.map_id != first.map_id
    assert not theme_map_stale(store, pid, fresh)
    assert theme_map_for(store, access, pid)[1] is False
    # a scrape that is still running, or failed, does not count
    _finished_scrape(themed, "run-r10", datetime.now(UTC) + timedelta(hours=1))
    running = themed.store.run(pid, themed.site_ids["competitor"], "run-r10")
    themed.store.save_run(running.model_copy(update={"status": "running", "finished_at": None}))
    assert not theme_map_stale(store, pid, fresh)


def test_the_card_tool_and_the_gap_report_read_a_fresh_map(themed: World) -> None:
    store, access, pid = themed.store, themed.access, themed.project_id
    play = Playground(store, access, [pid])
    text, is_error = run_tool(play, "get_theme_map", {})
    data = json.loads(text)["data"]
    assert not is_error and data["rebuilt_now"] is True
    data = json.loads(run_tool(play, "get_theme_map", {})[0])["data"]
    assert data["rebuilt_now"] is False
    stale_id = data["map_id"]
    _finished_scrape(themed, "run-r9", datetime.now(UTC))
    data = json.loads(run_tool(play, "get_theme_map", {})[0])["data"]
    assert data["rebuilt_now"] is True and data["map_id"] != stale_id
    newest = data["map_id"]
    # the gap report keeps using the fresh map; a later scrape makes it rebuild once more
    report, _ = build_project_gaps(store, access, pid)
    assert report.theme_map_id == newest
    _finished_scrape(themed, "run-r11", datetime.now(UTC))
    report, _ = build_project_gaps(store, access, pid)
    assert report.theme_map_id != newest
