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

from src.core.config import get_settings
from src.modules.content_processor.projects.notify import Notifier, smtp_mailer
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.app import create_app
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

__all__ = ["knowledge_access", "main", "parse_args"]

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
    parser.add_argument(
        "--app-data",
        default=None,
        help="projects folder for the Content Scraper module (default: CP_APP_DATA_ROOT)",
    )
    parser.add_argument(
        "--no-projects", action="store_true", help="serve run results only, without projects"
    )
    return parser.parse_args(argv)


def queue_notifier(app_data: Path) -> Notifier:
    """Queue emails through the configured SMTP server, or into `<app data>/outbox/`."""
    settings = get_settings()
    mailer = None
    if settings.cp_smtp_host and settings.cp_smtp_from:
        mailer = smtp_mailer(
            settings.cp_smtp_host,
            settings.cp_smtp_port,
            user=settings.cp_smtp_user,
            password=settings.cp_smtp_password.get_secret_value()
            if settings.cp_smtp_password
            else None,
            starttls=settings.cp_smtp_starttls,
        )
    return Notifier(
        outbox=app_data / "outbox",
        sender=settings.cp_smtp_from or "content-scraper@localhost",
        base_url=settings.cp_app_base_url,
        mailer=mailer,
    )


def knowledge_access() -> KnowledgeAccess:
    """Open each website's knowledge space as indexing did: Postgres or the SQLite file."""
    from src.modules.content_processor.knowledge.embedders import (
        HASHING_DIMS,
        Embedder,
        HashingEmbedder,
        open_embedder,
    )
    from src.modules.content_processor.knowledge.federated import Space
    from src.modules.content_processor.knowledge.vector_store import VectorStore, open_store

    settings = get_settings()
    db_url = settings.cp_database_url.get_secret_value() if settings.cp_database_url else None

    def open_space(space: Space) -> VectorStore:
        store, _note = open_store(
            "auto",
            tenant=space.namespace,
            sqlite_root=settings.cp_knowledge_root,
            db_url=db_url,
            dims=HASHING_DIMS,
        )
        return store

    def embedder_for(model: str) -> Embedder:
        if model.startswith("hashing-"):
            return HashingEmbedder()
        embedder, _note = open_embedder("ollama", settings.cp_model_endpoint, model)
        return embedder

    return KnowledgeAccess(open_store=open_space, embedder_for=embedder_for)


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
    projects = None
    if not args.no_projects:
        data = Path(args.app_data) if args.app_data else get_settings().cp_app_data_root
        store = ProjectStore(data)
        runner = ScrapeRunner(
            store, notifier=queue_notifier(data), ready_hours=get_settings().cp_queue_ready_hours
        )
        runner.start()
        projects = (store, runner, TokenSigner.from_file(data / "secret.key"))
    access = knowledge_access()
    analyst = None
    if projects is not None:
        from src.modules.analyst.runs import CardRunner

        analyst = CardRunner(projects[0], access, data / "analyst")
        from src.modules.monitoring.service import MonitorWatcher

        # checks by itself after new scrapes; re-scrapes only where a project opted in
        MonitorWatcher(projects[0], access, projects[1]).start()
    app = create_app(
        exports, static_dir=static, projects=projects, knowledge=access, analyst=analyst
    )
    sys.stdout.write(f"Serving {exports} at http://{HOST}:{args.port}/\n")
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
