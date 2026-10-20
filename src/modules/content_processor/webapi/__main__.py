"""Command line: serve the analyst API and web app on 127.0.0.1.

python -m src.modules.content_processor.webapi --exports <dir> --port 8780 \
--static webui/dist
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from src.modules.content_processor.webapi.app import create_app

__all__ = ["main", "parse_args"]

HOST = "127.0.0.1"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        prog="python -m src.modules.content_processor.webapi",
        description="Serve run results to the analyst web app (read-only, local only).",
    )
    parser.add_argument(
        "--exports", required=True, help="folder with <name>.records.jsonl files (and reports)"
    )
    parser.add_argument("--port", type=int, default=8780, help="port on 127.0.0.1 (default 8780)")
    parser.add_argument(
        "--static", default=None, help="built web app folder, usually webui/dist (optional)"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Start the server; returns a process exit code."""
    args = parse_args(argv)
    exports = Path(args.exports)
    if not exports.is_dir():
        sys.stderr.write(f"--exports {exports} is not a folder\n")
        return 2
    static = Path(args.static) if args.static else None
    if static is not None and not (static / "index.html").is_file():
        sys.stderr.write(f"--static {static} has no index.html; run `npm run build` in webui/\n")
        return 2
    app = create_app(exports, static_dir=static)
    sys.stdout.write(f"Serving {exports} at http://{HOST}:{args.port}/\n")
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
