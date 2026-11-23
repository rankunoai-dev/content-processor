"""A confidential project through Claude Desktop: nothing at all runs for it.

Everything a tool returns reaches Anthropic, so a confidential project (its own option or
the data policy file) gets no tool, is left out of list_projects, and a project that reads
a confidential project's scrape of a shared website is closed too.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
from fastapi.testclient import TestClient
from mcp import Client

from src.modules.analyst.policy import DataPolicy
from src.modules.analyst.tools import TOOLS
from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.store import SiteSpec
from src.modules.mcp_server.bridge import McpBridge
from src.modules.mcp_server.catalog import TEXT_TOOLS, definitions, exposes_text, instructions
from src.modules.mcp_server.server import build_server
from tests.modules.analyst.conftest import CLIENT, WHEN, World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.mcp_server.test_mcp_server import WRITE, _app, _call
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

# every project tool, with arguments that would otherwise succeed or fail on their own
PROJECT_TOOLS: dict[str, dict[str, Any]] = {
    "search_knowledge": {"query": "term life"},
    "get_page": {"url": "https://www.example.com/life/term"},
    "get_page_facts": {"url": "https://www.example.com/life/term"},
    "get_changes": {"website": "example.com"},
    "get_monitoring_alerts": {},
    "list_websites": {},
    "list_pages": {"website": "example.com"},
    "get_theme_map": {},
    "get_gap_findings": {},
    "get_offering_catalog": {"website": "example.com"},
    "get_seo_issues": {"website": "example.com"},
    "check_deliverable": {"text": "One theme stands out."},
    "present_answer": {"type": "answer", "title": "T", "summary": "S"},
    "save_deliverable": {"title": "T", "markdown": "Text."},
}


def test_every_project_tool_says_it_is_closed_to_confidential_projects() -> None:
    defs = {d.name: d for d in definitions()}
    assert set(TOOLS) <= set(defs), "every analyst tool is in the catalog"
    assert {n for n, d in defs.items() if d.exposes_text} == set(TEXT_TOOLS)
    for name in TOOLS:
        if name in defs:
            assert "not available for confidential projects" in defs[name].description
    assert exposes_text("search_knowledge") and not exposes_text("list_projects")
    rules = instructions(None)
    assert "Confidential projects are not shown to you at all" in rules
    assert "works for confidential projects too" not in rules


def _refused(client: TestClient, pid: str) -> None:
    for tool, arguments in PROJECT_TOOLS.items():
        answer, is_error = _call(client, tool, project_id=pid, **arguments)
        assert is_error, tool
        assert "Claude Desktop gets nothing from it" in answer["error"], tool
        assert set(answer) == {"error"}, tool


def test_a_confidential_project_gets_no_tool_and_is_not_listed(
    themed: World, tmp_path: Path
) -> None:
    client = TestClient(_app(themed, tmp_path), base_url="http://127.0.0.1")
    pid = themed.project_id
    listed, _ = _call(client, "list_projects")
    assert [p["project_id"] for p in listed["projects"]] == [pid]
    assert "hidden_projects" not in listed
    found, is_error = _call(client, "search_knowledge", project_id=pid, query="term life")
    assert not is_error and found["data"]["passages"]
    ledger_before = len(McpBridge(themed.store, themed.access).ledger(pid).entries)

    themed.store.update(pid, confidential=True)
    listed, _ = _call(client, "list_projects")
    assert listed["projects"] == [] and listed["hidden_projects"] == 1
    assert "app itself" in listed["hidden_note"]
    assert themed.store.get(pid).name not in json.dumps(listed), "not even its name"
    scoped = client.post(
        "/api/mcp/call",
        json={"tool": "list_projects", "arguments": {}, "scope": ["themes"]},
        headers=WRITE,
    ).json()
    assert json.loads(scoped["text"])["projects"] == []
    _refused(client, pid)
    assert len(McpBridge(themed.store, themed.access).ledger(pid).entries) == ledger_before
    assert client.get("/api/mcp/blocked").json() == {"projects": [pid]}

    themed.store.update(pid, confidential=False)
    found, is_error = _call(client, "search_knowledge", project_id=pid, query="term life")
    assert not is_error and found["data"]["passages"]
    assert client.get("/api/mcp/blocked").json() == {"projects": []}


def test_the_data_policy_file_closes_a_project_too(themed: World, tmp_path: Path) -> None:
    client = TestClient(_app(themed, tmp_path), base_url="http://127.0.0.1")
    pid = themed.project_id
    DataPolicy(themed.root / "analyst", themed.store).set(pid, "client_confidential")
    assert not themed.store.get(pid).confidential, "the app's option is still off"
    _refused(client, pid)
    listed, _ = _call(client, "list_projects")
    assert listed["projects"] == [] and listed["hidden_projects"] == 1


def test_a_project_reading_a_confidential_projects_scrape_is_closed(
    themed: World, tmp_path: Path
) -> None:
    client = TestClient(_app(themed, tmp_path), base_url="http://127.0.0.1")
    pid = themed.project_id
    other = themed.store.create(
        name="Secret pitch",
        owner_name="Tester",
        client_name="Example Life",
        password="password-123",
        client_site=SiteSpec(name="Example Life", website=CLIENT),
    )
    site = other.sites[0].site_id
    later = WHEN + timedelta(days=30)  # its copy of example.com is the freshest
    themed.store.save_run(
        RunState(
            run_id="run-other",
            project_id=other.project_id,
            site_id=site,
            status="finished",
            created_at=later,
            started_at=later,
            finished_at=later,
            started_by="test",
            url_count=1,
            batch_size=25,
            batches_total=1,
            batches_done=1,
            reports_ready=True,
            knowledge_status="ready",
        )
    )
    found, is_error = _call(client, "search_knowledge", project_id=pid, query="term life")
    assert not is_error, "the other project is not confidential yet"

    themed.store.update(other.project_id, confidential=True)
    answer, is_error = _call(client, "search_knowledge", project_id=pid, query="term life")
    assert is_error and "from the scrape of a confidential project" in answer["error"]
    assert "Secret pitch" not in answer["error"], "the confidential project is never named"
    _refused(client, pid)
    listed, _ = _call(client, "list_projects")
    assert listed["projects"] == [] and listed["hidden_projects"] == 2
    assert sorted(client.get("/api/mcp/blocked").json()["projects"]) == sorted(
        [pid, other.project_id]
    )


def test_an_unreadable_project_counts_as_confidential(world: World) -> None:
    bridge = McpBridge(world.store, world.access)
    assert bridge.confidential("no-such-project") is True
    assert bridge.confidential(world.project_id) is False


def test_the_stdio_servers_own_tools_refuse_and_hide_confidential_projects(
    themed: World, tmp_path: Path
) -> None:
    app = _app(themed, tmp_path)
    pid = themed.project_id
    themed.store.save_run(
        RunState(
            run_id="run-queued",
            project_id=pid,
            site_id=next(iter(themed.site_ids.values())),
            status="queued",
            created_at=WHEN,
            started_by="test",
            url_count=3,
            batch_size=25,
            batches_total=1,
        )
    )
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_theme_pages", {"project_id": pid, "theme": "x"}),
        ("export_theme_pages_csv", {"project_id": pid, "theme": "x"}),
        ("get_scrape_queue", {}),
    ]

    async def scenario() -> list[tuple[str, bool]]:
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1", headers=WRITE
            ) as http,
            Client(build_server(http)) as client,
        ):
            out: list[tuple[str, bool]] = []
            first = await client.call_tool("get_scrape_queue", {})
            out.append((getattr(first.content[0], "text", ""), first.is_error))
            themed.store.update(pid, confidential=True)
            for name, args in calls:
                reply = await client.call_tool(name, args)
                out.append((getattr(reply.content[0], "text", ""), reply.is_error))
            return out

    (open_queue, _), (pages, pages_error), (csv, csv_error), (queue, queue_error) = anyio.run(
        scenario
    )
    assert "hidden_scrapes" not in json.loads(open_queue)
    assert pages_error and "confidential" in pages
    assert csv_error and "confidential" in csv
    hidden = json.loads(queue)
    assert not queue_error and hidden["hidden_scrapes"] >= 1
    assert themed.store.get(pid).name not in queue
    assert all(
        e["project"] != themed.store.get(pid).name
        for part in ("running", "waiting", "ready")
        for e in hidden[part]
    )


def test_when_the_app_cannot_say_nothing_is_answered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/mcp/blocked":
            return httpx.Response(500)
        return httpx.Response(200, json={"running": [], "waiting": [], "ready": []})

    from src.modules.mcp_server.theme_pages import scrape_queue_text, theme_pages_text

    async def run() -> tuple[tuple[str, bool], tuple[str, bool]]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
        ) as http:
            return (
                await scrape_queue_text(http),
                await theme_pages_text(http, {"project_id": "demo", "theme": "x"}),
            )

    (queue, queue_error), (pages, pages_error) = anyio.run(run)
    assert queue_error and "could not say which projects are confidential" in queue
    assert pages_error and "could not say which projects are confidential" in pages
