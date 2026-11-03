"""Start the read-only MCP server for Claude Desktop, or print its config.

python -m src.modules.mcp_server [--app-url http://127.0.0.1:8780] [--modules themes,gaps]
python -m src.modules.mcp_server --print-config [--modules themes]
python -m src.modules.mcp_server --print-config --every-module

`--modules` limits a connection to some modules: Claude then sees only their tools. With
`--every-module`, the config holds the full connection plus one connection per module, so
each chat in Claude Desktop can switch on only the module it needs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.modules.mcp_server.catalog import SCOPES, Scope, parse_scope
from src.modules.mcp_server.server import DEFAULT_APP_URL, server_name

__all__ = ["claude_desktop_config", "main"]

REPO_ROOT = Path(__file__).resolve().parents[3]


def _entry(app_url: str, scope: Scope) -> dict[str, Any]:
    args = ["-m", "src.modules.mcp_server", "--app-url", app_url]
    if scope is not None:
        args += ["--modules", ",".join(sorted(scope))]
    return {
        "command": sys.executable,
        "args": args,
        "env": {"PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"},
    }


def claude_desktop_config(
    app_url: str = DEFAULT_APP_URL, scope: Scope = None, *, every_module: bool = False
) -> dict[str, Any]:
    """The `mcpServers` entries for claude_desktop_config.json, with full paths."""
    scopes: list[Scope] = [scope]
    if every_module:
        scopes = [None, *(frozenset({key}) for key in SCOPES)]
    return {"mcpServers": {server_name(s): _entry(app_url, s) for s in scopes}}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the server (default) or print the Claude Desktop config."""
    parser = argparse.ArgumentParser(prog="python -m src.modules.mcp_server")
    parser.add_argument("--app-url", default=DEFAULT_APP_URL, help="the running web app")
    parser.add_argument(
        "--modules",
        default=None,
        help=f"limit the connection to these modules (comma separated: {', '.join(SCOPES)})",
    )
    parser.add_argument(
        "--print-config", action="store_true", help="print the Claude Desktop config entry"
    )
    parser.add_argument(
        "--every-module",
        action="store_true",
        help="with --print-config: the full connection plus one per module",
    )
    args = parser.parse_args(argv)
    url = urlparse(args.app_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error(
            "--app-url must be this computer (http://127.0.0.1:... or http://localhost:...)"
        )
    try:
        scope = parse_scope(args.modules)
    except ValueError as exc:
        parser.error(str(exc))
    if args.every_module and scope is not None:
        parser.error("--every-module and --modules cannot be combined")
    if args.print_config:
        config = claude_desktop_config(args.app_url, scope, every_module=args.every_module)
        sys.stdout.write(json.dumps(config, indent=2) + "\n")
        return 0
    import anyio

    from src.modules.mcp_server.server import serve

    anyio.run(serve, args.app_url, scope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
