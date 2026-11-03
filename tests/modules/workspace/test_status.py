"""Each project's status in every module, for the home screen and Claude Desktop."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from src.modules.gaps.service import build_project_gaps
from src.modules.mcp_server.bridge import McpBridge
from src.modules.research.service import import_csv
from src.modules.workspace.status import project_statuses
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.research.test_research import CLIENT_EXPORT
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture


def test_status_follows_what_each_module_stored(themed: World, tmp_path: Path) -> None:
    before = next(s for s in project_statuses(themed.store) if s["project_id"] == themed.project_id)
    assert before["finished_scrapes"] >= 2 and before["themes_built"] is None
    assert before["gaps_built"] is None and before["research_added"] is None
    import_csv(
        themed.store, themed.access, themed.project_id, "example.com", CLIENT_EXPORT.encode()
    )
    build_project_gaps(themed.store, themed.access, themed.project_id)
    after = next(s for s in project_statuses(themed.store) if s["project_id"] == themed.project_id)
    assert after["themes_built"] and after["themes"] > 0  # the gap build built the theme map
    assert after["research_added"] and after["keywords"] > 0
    assert after["gaps_built"] and after["findings"] == after["open_findings"] > 0
    # the home screen and Claude Desktop say the same thing
    assert McpBridge(themed.store, themed.access).list_projects() == project_statuses(themed.store)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports, projects=(themed.store, ScrapeRunner(themed.store), TokenSigner(b"k" * 32))
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    listed = client.get("/api/modules/status").json()
    assert [s["project_id"] for s in listed] == [
        s["project_id"] for s in project_statuses(themed.store)
    ]
