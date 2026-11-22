"""With team accounts, Claude Desktop's token reads with any role but saves only as an editor."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.modules.accounts.store import AccountStore
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture

WRITE = {"X-Requested-With": "content-processor"}


def test_saving_through_claude_desktop_needs_an_editor(world: World, tmp_path: Path) -> None:
    accounts = AccountStore(world.store.root / "accounts")
    accounts.create("gaurav", "Gaurav", "admin", "admin-password-1", must_change_password=False)
    viewer = accounts.create_token("gaurav", "Claude Desktop", "viewer")
    editor = accounts.create_token("gaurav", "Claude Desktop (saves)", "editor")
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(world.store, ScrapeRunner(world.store), TokenSigner(b"k" * 32)),
        knowledge=world.access,
    )
    c = TestClient(app, base_url="http://127.0.0.1")
    body = {"tool": "save_deliverable", "arguments": {"project_id": world.project_id, "text": "x"}}
    as_viewer = {**WRITE, "Authorization": f"Bearer {viewer}"}
    refused = c.post("/api/mcp/call", json=body, headers=as_viewer).json()
    assert refused["is_error"] is True and "editor role" in refused["text"]
    allowed = c.post(
        "/api/mcp/call", json=body, headers={**WRITE, "Authorization": f"Bearer {editor}"}
    ).json()
    assert "editor role" not in allowed["text"]  # past the role check
    read = c.post(
        "/api/mcp/call", json={"tool": "list_projects", "arguments": {}}, headers=as_viewer
    )
    assert read.json()["is_error"] is False
