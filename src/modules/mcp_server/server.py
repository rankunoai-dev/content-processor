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
from src.modules.mcp_server.catalog import (
    PromptDef,
    Scope,
    ToolDef,
    definitions,
    instructions,
    prompts_for,
    scope_label,
)
from src.modules.mcp_server.theme_pages import LOCAL_TOOLS, theme_pages_text

__all__ = ["DEFAULT_APP_URL", "SERVER_NAME", "build_server", "serve", "server_name"]

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


def _prompt(p: PromptDef) -> types.Prompt:
    return types.Prompt(
        name=p.name,
        title=p.title,
        description=p.description,
        arguments=[
            types.PromptArgument(name=n, description=d, required=r) for n, d, r in p.arguments
        ],
    )


def _result(text: str, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


def server_name(scope: Scope = None) -> str:
    """content-processor, or content-processor-themes for a connection limited to Themes."""
    return SERVER_NAME if scope is None else f"{SERVER_NAME}-{'-'.join(sorted(scope))}"


def build_server(
    client: httpx.AsyncClient, app_url: str = DEFAULT_APP_URL, scope: Scope = None
) -> Server[Any]:
    """The MCP server for `scope`, sending calls through `client` (based at the web app)."""
    tools = definitions(scope)
    names = {d.name for d in tools}
    sent_scope = sorted(scope) if scope is not None else None
    prompts = {p.name: p for p in prompts_for(scope)}

    async def on_list_prompts(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[_prompt(p) for p in prompts.values()])

    async def on_get_prompt(
        _ctx: Any, params: types.GetPromptRequestParams
    ) -> types.GetPromptResult:
        found = prompts.get(params.name)
        if found is None:
            msg = f"unknown prompt {params.name}"
            raise ValueError(msg)
        given = params.arguments or {}
        values = {n: (given.get(n) or "").strip() for n, _d, _r in found.arguments}
        if not values.get("project"):
            values["project"] = "(ask me which one if there is more than one; list_projects)"
        text = found.template.format(**values)
        return types.GetPromptResult(
            description=found.description,
            messages=[
                types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))
            ],
        )

    async def on_list_tools(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_tool(d) for d in tools])

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name not in names:
            where = "" if scope is None else f" in this connection ({scope_label(scope)})"
            return _result(f'{{"error": "unknown tool {params.name}{where}"}}', True)
        if params.name in LOCAL_TOOLS:
            # Answered here from the app's read-only endpoints: no app code is involved.
            text, is_error = await theme_pages_text(client, params.arguments or {})
            return _result(text, is_error)
        try:
            response = await client.post(
                "/api/mcp/call",
                json={
                    "tool": params.name,
                    "arguments": params.arguments or {},
                    "scope": sent_scope,
                },
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

    title = "Content Processor (read-only)"
    if scope is not None:
        title = f"Content Processor: {scope_label(scope)} only (read-only)"
    return Server(
        server_name(scope),
        version="1",
        title=title,
        instructions=instructions(scope),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )


async def serve(app_url: str = DEFAULT_APP_URL, scope: Scope = None) -> None:
    """Serve over stdin and stdout until Claude Desktop closes the connection."""
    async with httpx.AsyncClient(base_url=app_url, headers=_HEADERS, timeout=_TIMEOUT) as client:
        server = build_server(client, app_url, scope)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
