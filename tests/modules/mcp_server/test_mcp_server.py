"""Read-only MCP server: catalog, bridge in the web app, the MCP protocol end to end, config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client

from src.modules.analyst.tools import TOOLS
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from src.modules.mcp_server import __main__ as mcp_main
from src.modules.mcp_server.bridge import McpBridge
from src.modules.mcp_server.catalog import INSTRUCTIONS, definitions
from src.modules.mcp_server.server import build_server
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

WRITE = {"X-Requested-With": "content-processor"}


def _app(world: World, tmp_path: Path) -> FastAPI:
    exports = tmp_path / "exports"
    exports.mkdir(exist_ok=True)
    signer = TokenSigner(b"k" * 32)
    return create_app(
        exports,
        projects=(world.store, ScrapeRunner(world.store), signer),
        knowledge=world.access,
    )


def _call(client: TestClient, tool: str, **arguments: Any) -> tuple[dict[str, Any], bool]:
    response = client.post(
        "/api/mcp/call", json={"tool": tool, "arguments": arguments}, headers=WRITE
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return json.loads(body["text"]), bool(body["is_error"])


def test_catalog_lists_every_built_tool_read_only_with_a_project() -> None:
    defs = definitions()
    names = [d.name for d in defs]
    built = [n for n, s in TOOLS.items() if s.built]
    assert names == ["list_projects", *built, "check_deliverable"] and len(built) == 16
    for d in defs[1:]:
        assert d.input_schema["required"][0] == "project_id"
        assert d.input_schema["additionalProperties"] is False
    search = next(d for d in defs if d.name == "search_knowledge")
    assert {"query", "websites", "top_k"} <= set(search.input_schema["properties"])
    assert "read-only" in search.description
    assert "check_deliverable" in INSTRUCTIONS and "Evidence only" in INSTRUCTIONS


def test_bridge_runs_tools_and_checks_deliverables(themed: World, tmp_path: Path) -> None:
    client = TestClient(_app(themed, tmp_path), base_url="http://127.0.0.1")
    listed = client.get("/api/mcp/tools").json()
    assert [t["name"] for t in listed] == [d.name for d in definitions()]
    projects, is_error = _call(client, "list_projects")
    assert not is_error
    mine = next(p for p in projects["projects"] if p["project_id"] == themed.project_id)
    assert {w["role"] for w in mine["websites"]} == {"client", "competitor"}
    assert mine["finished_scrapes"] >= 2 and mine["gaps_built"] is None
    # mistakes are answered, never raised
    assert "project_id is required" in _call(client, "search_knowledge", query="x")[0]["error"]
    missing = _call(client, "search_knowledge", project_id="nope", query="x")[0]["error"]
    assert "no project" in missing
    assert "unknown tool" in _call(client, "start_scrape", project_id=themed.project_id)[0]["error"]
    bad, is_error = _call(client, "search_knowledge", project_id=themed.project_id, top_k=99)
    assert is_error and "invalid input" in bad["error"]
    # the app's write guard still applies, and no web page may call the bridge
    body = {"tool": "list_projects", "arguments": {}}
    assert client.post("/api/mcp/call", json=body).status_code == 403
    from_page = {**WRITE, "Origin": "http://127.0.0.1"}
    refused = client.post("/api/mcp/call", json=body, headers=from_page)
    assert refused.status_code == 403 and "MCP server" in refused.json()["detail"]
    assert client.get("/api/mcp/call").status_code in (404, 405)
    # a real search hands out references and remembers them
    found, is_error = _call(
        client, "search_knowledge", project_id=themed.project_id, query="term life insurance"
    )
    assert not is_error and found["untrusted_website_content"] is True
    passage = found["data"]["passages"][0]
    ref, words = passage["ref"], " ".join(passage["text"].split()[:6])
    ledger_file = Path(str(themed.store.root)) / "mcp" / "ledgers" / f"{themed.project_id}.json"
    assert ref in json.loads(ledger_file.read_text(encoding="utf-8"))
    # a deliverable citing it passes; a made-up reference does not
    good = f'## Findings\n\nThe page says "{words}" [{ref}].\n'
    checked, _ = _call(client, "check_deliverable", project_id=themed.project_id, text=good)
    assert checked["status"] == "passed" and ref in checked["sources_markdown"]
    made_up = good + "\nIt has 12 more pages [chunk:invented-1].\n"
    checked, _ = _call(client, "check_deliverable", project_id=themed.project_id, text=made_up)
    assert checked["status"] == "needs review" and checked["unknown_refs"] == ["chunk:invented-1"]
    wrong_quote = f'The page says "this sentence is nowhere on the page" [{ref}].\n'
    checked, _ = _call(client, "check_deliverable", project_id=themed.project_id, text=wrong_quote)
    assert checked["quotes_failed"]
    assert _call(client, "check_deliverable", project_id=themed.project_id, text=" ")[1]
    assert _call(client, "check_deliverable", project_id=themed.project_id, text="x", extra=1)[1]
    # the references survive a restart of the app (a new bridge reads the same ledger)
    restarted = McpBridge(themed.store, themed.access)
    text, is_error = restarted.call(
        "check_deliverable", {"project_id": themed.project_id, "text": good}
    )
    assert not is_error and json.loads(text)["status"] == "passed"


def test_mcp_protocol_end_to_end(themed: World, tmp_path: Path) -> None:
    app = _app(themed, tmp_path)

    async def scenario() -> tuple[list[str], dict[str, Any], str, bool]:
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1", headers=WRITE
            ) as http,
            Client(build_server(http)) as client,
        ):
            tools = await client.list_tools()
            theme = await client.call_tool("get_theme_map", {"project_id": themed.project_id})
            wrong = await client.call_tool("not_a_tool", {})
        first = theme.content[0]
        text = first.text if hasattr(first, "text") else ""
        wrong_text = wrong.content[0].text if hasattr(wrong.content[0], "text") else ""
        return [t.name for t in tools.tools], json.loads(text), wrong_text, bool(wrong.is_error)

    names, theme, wrong_text, wrong_error = anyio.run(scenario)
    assert names[0] == "list_projects" and "check_deliverable" in names and len(names) == 18
    assert theme["tool"] == "get_theme_map" and theme["data"]
    assert wrong_error and "unknown tool" in wrong_text


def test_app_not_running_says_how_to_start_it() -> None:
    async def scenario() -> tuple[str, bool, bool]:
        async with (
            httpx.AsyncClient(base_url="http://127.0.0.1:9") as http,
            Client(build_server(http, "http://127.0.0.1:9")) as client,
        ):
            tools = await client.list_tools()
            result = await client.call_tool("list_projects", {})
        first = result.content[0]
        hint = first.text if hasattr(first, "text") else ""
        read_only = all(
            t.annotations is not None and t.annotations.read_only_hint for t in tools.tools
        )
        return hint, bool(result.is_error), read_only

    hint, is_error, read_only = anyio.run(scenario)
    assert is_error and "not running at http://127.0.0.1:9" in hint and read_only


def test_print_config_and_local_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert mcp_main.main(["--print-config"]) == 0
    entry = json.loads(capsys.readouterr().out)["mcpServers"]["content-processor"]
    assert entry["args"][:2] == ["-m", "src.modules.mcp_server"]
    assert Path(entry["command"]).exists()
    assert (Path(entry["env"]["PYTHONPATH"]) / "src" / "modules" / "mcp_server").is_dir()
    for url in ("http://example.com", "https://127.0.0.1:8780", "http://localhost.evil.com"):
        with pytest.raises(SystemExit):
            mcp_main.main(["--print-config", "--app-url", url])


def test_search_falls_back_to_exact_words_when_the_model_is_down(
    themed: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.modules.content_processor.knowledge import embedders

    def down(self: object, texts: list[str]) -> None:
        raise RuntimeError("no model server at http://localhost:11434/v1 (ConnectError)")

    monkeypatch.setattr(embedders.HashingEmbedder, "embed", down)
    client = TestClient(_app(themed, tmp_path), base_url="http://127.0.0.1")
    found, is_error = _call(
        client, "search_knowledge", project_id=themed.project_id, query="term life insurance"
    )
    assert not is_error and found["data"]["passages"] and not found["data"]["skipped"]
    assert all(p["found_by"] == ["keywords"] for p in found["data"]["passages"])
    assert any("exact words only" in n for n in found["notes"])
    page = client.get("/api/knowledge/search", params={"q": "term life insurance"}).json()
    assert page["hits"] and any("exact words only" in n for n in page["notes"])


# -- module scope: a connection limited to some modules --------------------------------------


def test_a_scoped_catalog_lists_only_that_module() -> None:
    from src.modules.mcp_server.catalog import instructions, parse_scope

    themes = parse_scope("themes")
    names = [d.name for d in definitions(themes)]
    assert names == ["list_projects", "get_offering_catalog", "get_theme_map", "check_deliverable"]
    assert all(d.description.startswith("[Themes]") for d in definitions(themes)[1:-1])
    assert "limited to: Themes" in instructions(themes) and "Module focus" not in instructions(
        themes
    )
    assert "Module focus" in instructions(None) and "[Themes]" in instructions(None)
    two = parse_scope(" Gaps , research ")
    assert two == frozenset({"gaps", "research"})
    assert {d.module for d in definitions(two)} == {None, "gaps", "research"}
    assert parse_scope("") is None and parse_scope(None) is None
    with pytest.raises(ValueError, match="unknown module"):
        parse_scope("themes,keywords")


def test_the_app_refuses_other_modules_on_a_scoped_connection(
    themed: World, tmp_path: Path
) -> None:
    client = TestClient(_app(themed, tmp_path), base_url="http://127.0.0.1")

    def call(tool: str, scope: list[str] | None, **arguments: Any) -> tuple[dict[str, Any], bool]:
        body = {"tool": tool, "arguments": arguments, "scope": scope}
        out = client.post("/api/mcp/call", json=body, headers=WRITE).json()
        return json.loads(out["text"]), bool(out["is_error"])

    pid = themed.project_id
    refused, is_error = call("search_knowledge", ["themes"], project_id=pid, query="term life")
    assert is_error and "limited to Themes" in refused["error"]
    assert "belongs to Content" in refused["error"]
    theme, is_error = call("get_theme_map", ["themes"], project_id=pid)
    assert not is_error and theme["tool"] == "get_theme_map"
    listed, _ = call("list_projects", ["themes"])
    mine = next(p for p in listed["projects"] if p["project_id"] == pid)
    assert set(mine) == {"project_id", "name", "client", "websites", "themes_built", "themes"}
    full, _ = call("list_projects", None)
    assert "finished_scrapes" in next(p for p in full["projects"] if p["project_id"] == pid)
    assert "unknown module" in call("list_projects", ["keywords"])[0]["error"]
    tools = client.get("/api/mcp/tools", params={"modules": "gaps"}).json()
    assert [t["name"] for t in tools] == ["list_projects", "get_gap_findings", "check_deliverable"]
    assert client.get("/api/mcp/tools", params={"modules": "nope"}).status_code == 422


def test_a_scoped_server_over_the_protocol(themed: World, tmp_path: Path) -> None:
    from src.modules.mcp_server.catalog import parse_scope
    from src.modules.mcp_server.server import server_name

    app = _app(themed, tmp_path)
    scope = parse_scope("monitoring")

    async def scenario() -> tuple[list[str], str, bool, str]:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=WRITE
            ) as http,
            Client(build_server(http, scope=scope)) as client,
        ):
            tools = await client.list_tools()
            other = await client.call_tool("get_theme_map", {"project_id": themed.project_id})
            own = await client.call_tool("get_changes", {"project_id": themed.project_id})
        other_text = other.content[0].text if hasattr(other.content[0], "text") else ""
        own_text = own.content[0].text if hasattr(own.content[0], "text") else ""
        return [t.name for t in tools.tools], other_text, bool(other.is_error), own_text

    names, other_text, other_error, own_text = anyio.run(scenario)
    assert names == ["list_projects", "get_changes", "get_monitoring_alerts", "check_deliverable"]
    assert other_error and "in this connection (Monitoring)" in other_text
    assert "limited to" not in own_text
    assert server_name(scope) == "content-processor-monitoring"


def test_print_config_for_one_module_and_for_every_module(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert mcp_main.main(["--print-config", "--modules", "themes"]) == 0
    one = json.loads(capsys.readouterr().out)["mcpServers"]
    assert list(one) == ["content-processor-themes"]
    assert one["content-processor-themes"]["args"][-2:] == ["--modules", "themes"]
    assert mcp_main.main(["--print-config", "--every-module"]) == 0
    every = json.loads(capsys.readouterr().out)["mcpServers"]
    assert list(every) == [
        "content-processor",
        "content-processor-content",
        "content-processor-themes",
        "content-processor-monitoring",
        "content-processor-research",
        "content-processor-gaps",
    ]
    assert "--modules" not in every["content-processor"]["args"]
    for bad in (["--modules", "nope"], ["--every-module", "--modules", "themes", "--print-config"]):
        with pytest.raises(SystemExit):
            mcp_main.main(bad)
