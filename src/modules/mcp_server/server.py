"""The stdio MCP server Claude Desktop starts; it forwards every call to the running web app.

It never reads or writes the app data itself. When the app is not running, every tool
answers with how to start it instead of failing silently.
"""

from __future__ import annotations

from typing import Any, Final

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from src.core.logger import get_logger
from src.modules.mcp_server.catalog import INSTRUCTIONS, ToolDef, definitions

__all__ = ["DEFAULT_APP_URL", "SERVER_NAME", "build_server", "serve"]

_log = get_logger(__name__)

SERVER_NAME: Final = "content-processor"
DEFAULT_APP_URL: Final = "http://127.0.0.1:8780"
_HEADERS: Final = {"X-Requested-With": "content-processor", "Accept": "application/json"}
_TIMEOUT: Final = httpx.Timeout(180.0, connect=5.0)


def _start_hint(app_url: str) -> str:
    return (
        f"The Content Processor app is not running at {app_url}. Start it, then try again: "
        "python -m src.modules.content_processor.webapi --exports exports --port 8780"
    )


def _tool(d: ToolDef) -> types.Tool:
    return types.Tool(
        name=d.name,
        title=d.title,
        description=d.description,
        input_schema=d.input_schema,
        annotations=types.ToolAnnotations(
            title=d.title,
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )


def _result(text: str, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


def build_server(client: httpx.AsyncClient, app_url: str = DEFAULT_APP_URL) -> Server[Any]:
    """The MCP server, sending calls through `client` (based at the web app)."""
    tools = definitions()
    names = {d.name for d in tools}

    async def on_list_tools(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_tool(d) for d in tools])

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name not in names:
            return _result(f'{{"error": "unknown tool {params.name}"}}', True)
        try:
            response = await client.post(
                "/api/mcp/call", json={"tool": params.name, "arguments": params.arguments or {}}
            )
        except httpx.ConnectError:
            return _result(_start_hint(app_url), True)
        except httpx.TimeoutException:
            return _result("The app took too long to answer; try a narrower request.", True)
        if response.status_code != 200:
            _log.warning("mcp call %s: HTTP %s", params.name, response.status_code)
            return _result(
                f"The app answered with an error (HTTP {response.status_code}). Is it the same "
                "version as this server? Restart both.",
                True,
            )
        body = response.json()
        return _result(str(body["text"]), bool(body["is_error"]))

    return Server(
        SERVER_NAME,
        version="1",
        title="Content Processor (read-only)",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve(app_url: str = DEFAULT_APP_URL) -> None:
    """Serve over stdin and stdout until Claude Desktop closes the connection."""
    async with httpx.AsyncClient(base_url=app_url, headers=_HEADERS, timeout=_TIMEOUT) as client:
        server = build_server(client, app_url)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
