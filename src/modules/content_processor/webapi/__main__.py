"""Command line: serve the analyst API and web app.

python -m src.modules.content_processor.webapi --exports <dir> --port 8780 \
--static webui/dist

The server listens on `CP_BIND_HOST:CP_BIND_PORT` (127.0.0.1:8780 unless set);
`--host` and `--port` override the settings. In a container bind 0.0.0.0 and
set `CP_ALLOWED_HOSTS` to the public host name (see `deploy/README.md`).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from src.core.config import get_settings
from src.modules.content_processor.projects.notify import Notifier, notifier_from_settings
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.app import create_app
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

__all__ = ["bind_address", "knowledge_access", "main", "parse_args"]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        prog="python -m src.modules.content_processor.webapi",
        description="Serve run results to the analyst web app (reads are open; writes local).",
    )
    parser.add_argument(
        "--exports", required=True, help="folder with <name>.records.jsonl files (and reports)"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="address to listen on (default: CP_BIND_HOST, 127.0.0.1 = this machine only)",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="port to listen on (default: CP_BIND_PORT, 8780)"
    )
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


def bind_address(args: argparse.Namespace) -> tuple[str, int]:
    """Where to listen: the command-line flags, else `CP_BIND_HOST` and `CP_BIND_PORT`."""
    settings = get_settings()
    host = args.host or settings.cp_bind_host
    port = args.port if args.port is not None else settings.cp_bind_port
    return host, port


def queue_notifier(app_data: Path) -> Notifier:
    """Queue emails through the configured SMTP server, or into `<app data>/outbox/`."""
    return notifier_from_settings(app_data)


def _warm_search_cache(store: ProjectStore, runner: ScrapeRunner, access: KnowledgeAccess) -> None:
    """With `CP_VECTOR_WARM`: warm the search cache now and after each new knowledge space."""
    settings = get_settings()
    limit = int(settings.cp_vector_cache_mb) * 1024 * 1024
    if not settings.cp_vector_warm or limit <= 0 or settings.cp_database_url:
        return
    from src.modules.content_processor.knowledge.federated import Space
    from src.modules.content_processor.knowledge.warm import freshest_spaces, warm_in_background

    warm_in_background(
        lambda: freshest_spaces(store, runner.namespace), access.open_store, limit_bytes=limit
    )

    def after_build(namespace: str) -> None:
        def spaces() -> list[Space]:
            return [s for s in freshest_spaces(store, runner.namespace) if s.namespace == namespace]

        warm_in_background(spaces, access.open_store, limit_bytes=limit, name="vector-warm-new")

    runner.on_knowledge_ready = after_build


def knowledge_access() -> KnowledgeAccess:
    """Open each website's knowledge space as indexing did: Postgres or the SQLite file."""
    from src.modules.content_processor.knowledge.embedders import (
        HASHING_DIMS,
        Embedder,
        embedder_for_model,
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
        return embedder_for_model(model, settings.cp_model_endpoint)

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
    if projects is not None:
        _warm_search_cache(projects[0], projects[1], access)
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
    host, port = bind_address(args)
    local = host in ("127.0.0.1", "localhost", "::1")
    accounts = getattr(app.state, "accounts", None)
    if not local and (accounts is None or not accounts.active()):
        sys.stderr.write(
            f"Refusing to listen on {host}: anyone could read and change everything. Create "
            "the first admin first: python -m src.modules.content_processor.cli accounts "
            "create-admin --username <name> --name '<Your name>'\n"
        )
        return 2
    # Fail closed: should the accounts file go missing while the server runs, the gate
    # then refuses every API request instead of opening them to anyone.
    app.state.accounts_required = not local
    sys.stdout.write(f"Serving {exports} at http://{host}:{port}/\n")
    if not local:
        sys.stdout.write("Listening beyond this machine: sign-in is required.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
