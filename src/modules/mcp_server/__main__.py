"""Start the read-only MCP server for Claude Desktop, or print its config.

python -m src.modules.mcp_server [--app-url http://127.0.0.1:8780]
python -m src.modules.mcp_server --print-config
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.modules.mcp_server.server import DEFAULT_APP_URL, SERVER_NAME

__all__ = ["claude_desktop_config", "main"]

REPO_ROOT = Path(__file__).resolve().parents[3]


def claude_desktop_config(app_url: str = DEFAULT_APP_URL) -> dict[str, Any]:
    """The `mcpServers` entry for claude_desktop_config.json, with full paths."""
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": sys.executable,
                "args": ["-m", "src.modules.mcp_server", "--app-url", app_url],
                "env": {"PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"},
            }
        }
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the server (default) or print the Claude Desktop config."""
    parser = argparse.ArgumentParser(prog="python -m src.modules.mcp_server")
    parser.add_argument("--app-url", default=DEFAULT_APP_URL, help="the running web app")
    parser.add_argument(
        "--print-config", action="store_true", help="print the Claude Desktop config entry"
    )
    args = parser.parse_args(argv)
    url = urlparse(args.app_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error(
            "--app-url must be this computer (http://127.0.0.1:... or http://localhost:...)"
        )
    if args.print_config:
        sys.stdout.write(json.dumps(claude_desktop_config(args.app_url), indent=2) + "\n")
        return 0
    import anyio

    from src.modules.mcp_server.server import serve

    anyio.run(serve, args.app_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
