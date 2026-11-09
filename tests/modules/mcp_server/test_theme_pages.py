"""get_theme_pages: every page of one theme, in full, served by the stdio server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import Client

from src.modules.mcp_server.server import build_server
from src.modules.mcp_server.theme_pages import theme_pages_text
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.mcp_server.test_mcp_server import WRITE, _app
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture


def test_theme_pages_lists_every_page_of_the_theme(themed: World, tmp_path: Path) -> None:
    app = _app(themed, tmp_path)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], str, bool]:
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1", headers=WRITE
            ) as http,
            Client(build_server(http)) as client,
        ):
            themes = await client.call_tool("get_theme_map", {"project_id": themed.project_id})
            first_theme = json.loads(themes.content[0].text)["data"]["themes"][0]
            ok = await client.call_tool(
                "get_theme_pages",
                {"project_id": themed.project_id, "theme": first_theme["theme_id"]},
            )
            bad = await client.call_tool(
                "get_theme_pages", {"project_id": themed.project_id, "theme": "no such theme"}
            )
        return first_theme, json.loads(ok.content[0].text), bad.content[0].text, bool(bad.is_error)

    theme, answer, bad_text, bad_error = anyio.run(scenario)
    assert answer["theme_id"] == theme["theme_id"]
    assert answer["count"] == len(answer["pages"]) == theme["pages"]
    assert [p["url"] for p in answer["pages"]] == sorted(p["url"] for p in answer["pages"])
    assert bad_error and "No theme matches" in bad_text


def test_theme_pages_needs_a_project_and_a_theme() -> None:
    async def run() -> tuple[str, bool]:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:9") as http:
            return await theme_pages_text(http, {"project_id": "", "theme": "x"})

    text, is_error = anyio.run(run)
    assert is_error and "project_id" in text


def test_theme_pages_says_how_to_start_the_app_when_it_is_down() -> None:
    async def run() -> tuple[str, bool]:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:9") as http:
            return await theme_pages_text(http, {"project_id": "demo", "theme": "x"})

    text, is_error = anyio.run(run)
    assert is_error and "not running" in text
