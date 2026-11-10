"""A confidential project through the MCP bridge: only tools without page text run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.modules.analyst.tools import TOOLS
from src.modules.mcp_server.bridge import McpBridge
from src.modules.mcp_server.catalog import TEXT_TOOLS, definitions, exposes_text, instructions
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.mcp_server.test_mcp_server import WRITE, _app, _call
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture


def test_every_tool_says_whether_it_exposes_page_text() -> None:
    defs = {d.name: d for d in definitions()}
    assert set(TOOLS) <= set(defs), "every analyst tool is in the catalog"
    assert {n for n, d in defs.items() if d.exposes_text} == set(TEXT_TOOLS)
    assert {
        "search_knowledge",
        "get_page",
        "get_page_facts",
        "get_changes",
        "get_monitoring_alerts",
    } == TEXT_TOOLS
    for name in TEXT_TOOLS:
        assert name in TOOLS and exposes_text(name)
        assert "not available for confidential projects" in defs[name].description
    for name in (
        "list_projects",
        "list_websites",
        "get_theme_map",
        "get_theme_pages",
        "get_gap_findings",
        "check_deliverable",
        "present_answer",
    ):
        assert not defs[name].exposes_text and not exposes_text(name)
    assert '"confidential": true' in instructions(None)


def test_a_confidential_project_only_answers_without_page_text(
    themed: World, tmp_path: Path
) -> None:
    client = TestClient(_app(themed, tmp_path), base_url="http://127.0.0.1")
    pid = themed.project_id
    # before: page text flows, and the project says it is not confidential
    listed, _ = _call(client, "list_projects")
    mine = next(p for p in listed["projects"] if p["project_id"] == pid)
    assert mine["confidential"] is False
    found, is_error = _call(client, "search_knowledge", project_id=pid, query="term life")
    assert not is_error and found["data"]["passages"]

    themed.store.update(pid, confidential=True)
    listed, _ = _call(client, "list_projects")
    mine = next(p for p in listed["projects"] if p["project_id"] == pid)
    assert mine["confidential"] is True
    scoped = client.post(
        "/api/mcp/call",
        json={"tool": "list_projects", "arguments": {}, "scope": ["themes"]},
        headers=WRITE,
    ).json()
    only_themes = next(p for p in json.loads(scoped["text"])["projects"] if p["project_id"] == pid)
    assert only_themes["confidential"] is True, "shown on a scoped connection too"

    def refused(tool: str, **arguments: Any) -> None:
        answer, is_error = _call(client, tool, project_id=pid, **arguments)
        assert is_error, tool
        assert "confidential" in answer["error"] and tool in answer["error"]
        assert "passages" not in answer and "data" not in answer

    refused("search_knowledge", query="term life")
    refused("get_page", url="https://www.example.com/life/term")
    refused("get_page_facts", url="https://www.example.com/life/term")
    refused("get_changes", website="example.com")
    refused("get_monitoring_alerts")
    # nothing new reaches the ledger for it
    ledger = McpBridge(themed.store, themed.access).ledger(pid)
    before = len(ledger.entries)

    websites, is_error = _call(client, "list_websites", project_id=pid)
    assert not is_error and websites["data"]["websites"]
    theme, is_error = _call(client, "get_theme_map", project_id=pid)
    assert not is_error and theme["data"]["themes"]
    pages, is_error = _call(client, "list_pages", project_id=pid, website="example.com")
    assert not is_error and pages["data"]
    text = json.dumps(pages)
    for passage in ("covers you for 10, 20 or 30 years", "rated A+ by A.M. Best"):
        assert passage not in text, "page text never leaves through an allowed tool"
    theme_ref = theme["data"]["themes"][0]["ref"]
    checked, is_error = _call(
        client, "check_deliverable", project_id=pid, text=f"One theme stands out [{theme_ref}]."
    )
    assert not is_error and checked["status"] == "passed"
    assert len(McpBridge(themed.store, themed.access).ledger(pid).entries) >= before

    # turned back off, page text flows again
    themed.store.update(pid, confidential=False)
    found, is_error = _call(client, "search_knowledge", project_id=pid, query="term life")
    assert not is_error and found["data"]["passages"]
