"""`kb-check`, `index`, `search`, `ask` (RFC 0002 stages D and E).

    python -m src.modules.content_processor.cli kb-check
    python -m src.modules.content_processor.cli index --run jh_full
    python -m src.modules.content_processor.cli search "term life conversion" --top-k 5
    python -m src.modules.content_processor.cli ask "Can I convert my term policy?"

`kb-check` reports whether the local model server (Ollama) and Postgres with
pgvector are ready, and which store and embedder `index` would use.

`index` reads `exports/<run>/<run>.chunks.jsonl`, `.stamps.jsonl`, `.catalog.json`
(and `.structured.jsonl` when present), embeds new texts, labels every chunk and
stores it. It writes `<run>.labels.jsonl` and `<run>_chunk_labels.xlsx`.

Stores: Postgres with pgvector when `CP_DATABASE_URL` (or `--db-url`) points at
one, else the SQLite file `<CP_KNOWLEDGE_ROOT>/<tenant>/vectors.sqlite`.
Embedder: Ollama `bge-m3` when it is running, else the offline hashing fallback
(words only; the store remembers which, and search uses the same one).
Model endpoints must be local or private-network; nothing is sent elsewhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from src.core.config import REPO_ROOT, get_settings
from src.modules.content_processor.knowledge.assist import LocalChat, LocalOnlyError
from src.modules.content_processor.knowledge.cpu_embedder import cpu_available
from src.modules.content_processor.knowledge.embedders import (
    HASHING_DIMS,
    Embedder,
    EmbedderChoice,
    check_model_server,
    embedder_for_model,
    open_embedder,
)

if TYPE_CHECKING:
    from src.modules.content_processor.knowledge.vector_store import VectorStore

# The store, labels and search use numpy; they are imported when a command runs,
# so other commands (and processes that only build the CLI) never load it.

__all__ = ["cmd_ask", "cmd_index", "cmd_kb_check", "cmd_search", "register"]

DEFAULT_TENANT = "rankuno-dev"


def _db_url(args: argparse.Namespace) -> str | None:
    given = getattr(args, "db_url", None)
    if given is not None:
        return str(given)
    secret = get_settings().cp_database_url
    return secret.get_secret_value() if secret else None


def _sqlite_root(args: argparse.Namespace) -> Path:
    given = getattr(args, "store_path", None)
    return Path(given) if given else get_settings().cp_knowledge_root


def _endpoint(args: argparse.Namespace) -> str:
    return str(getattr(args, "endpoint", None) or get_settings().cp_model_endpoint)


def _fail(message: str) -> int:
    try:
        sys.stderr.write(message.rstrip() + "\n")
    except UnicodeEncodeError:
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.write(message.rstrip() + "\n")
    return 2


def _safe_write_stdout(text: str) -> None:
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))


def cmd_kb_check(args: argparse.Namespace) -> int:
    """Report what the knowledge base would use, and what is missing."""
    from src.modules.content_processor.knowledge.vector_store import probe_pgvector

    settings = get_settings()
    endpoint = _endpoint(args)
    server = check_model_server(endpoint)
    url = _db_url(args)
    pg_ok, pg_detail = probe_pgvector(url) if url else (False, "CP_DATABASE_URL is not set")
    report: dict[str, Any] = {
        "model_server": {
            "endpoint": endpoint,
            "reachable": server.reachable,
            "models": server.models,
            "embed_model": settings.cp_embed_model,
            "embed_model_ready": server.has(settings.cp_embed_model),
            "chat_model": settings.cp_chat_model,
            "chat_model_ready": server.has(settings.cp_chat_model),
            "detail": server.detail,
        },
        "postgres": {"configured": bool(url), "pgvector_ready": pg_ok, "detail": pg_detail},
        "index_would_use": {
            "store": "postgres" if pg_ok else f"sqlite: {_sqlite_root(args) / args.tenant}",
            "embedder": settings.cp_embed_model
            if server.has(settings.cp_embed_model)
            else (
                f"CPU fallback ({settings.cp_fallback_embed_model})"
                if cpu_available() and settings.cp_fallback_embed_model
                else "hashing fallback (words only)"
            ),
            "answers": "local model" if server.has(settings.cp_chat_model) else "extractive quotes",
        },
    }
    _safe_write_stdout(json.dumps(report, indent=2) + "\n")
    return 0


def _open(args: argparse.Namespace, dims: int) -> tuple[VectorStore, str]:
    from src.modules.content_processor.knowledge.vector_store import StoreChoice, open_store

    return open_store(
        cast(StoreChoice, args.store),
        tenant=args.tenant,
        sqlite_root=_sqlite_root(args),
        db_url=_db_url(args),
        dims=dims,
    )


def _progress(done: int, total: int) -> None:
    if done == total or done % 320 == 0:
        sys.stderr.write(f"embedded {done}/{total}\n")


def cmd_index(args: argparse.Namespace) -> int:
    """Embed, label and store a run's chunks and pages."""
    from src.modules.content_processor.knowledge.indexing import (
        ModelMismatchError,
        index_run,
        load_run,
        write_label_review,
    )
    from src.modules.content_processor.knowledge.labels import Labeller

    settings = get_settings()
    folder = Path(args.dir) if args.dir else REPO_ROOT / "exports" / args.run
    try:
        run = load_run(folder, args.run)
    except FileNotFoundError as exc:
        return _fail(str(exc))
    endpoint = _endpoint(args)
    chat: LocalChat | None = None
    try:
        embedder, embed_note = open_embedder(
            cast(EmbedderChoice, args.embedder),
            endpoint,
            args.embed_model or settings.cp_embed_model,
        )
        if args.llm_model:
            server = check_model_server(endpoint)
            if not server.has(args.llm_model):
                return _fail(f"{args.llm_model} is not available at {endpoint}")
            chat = LocalChat(endpoint, args.llm_model)
    except (LocalOnlyError, RuntimeError) as exc:
        return _fail(str(exc))
    probe = embedder.embed(["dimension probe"])
    if probe is None:
        return _fail(f"{embedder.model} did not return a vector")
    try:
        store, store_note = _open(args, len(probe[0]))
    except RuntimeError as exc:
        return _fail(str(exc))
    try:
        if args.rebuild:
            store.reset()
        labeller = Labeller(run.stamps, run.catalog, chat=chat)
        report = index_run(run, store, embedder, labeller, progress=_progress)
        counts = store.counts()
    except ModelMismatchError as exc:
        return _fail(str(exc))
    finally:
        store.close()
        embedder.close()
        if chat is not None:
            chat.close()
    files = write_label_review(
        report.labels, run.chunks, Path(args.out) if args.out else folder, args.run
    )
    summary = {
        "run": args.run,
        "store": store_note,
        "store_location": store.location,
        "embedder": embed_note,
        "label_model": chat.model if chat else None,
        "label_model_calls": labeller.model_calls,
        **report.summary(),
        "store_counts": counts,
        "files": files,
    }
    _safe_write_stdout(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return 0


def _query_embedder(args: argparse.Namespace, store: VectorStore) -> Embedder:
    model = store.meta().get("model")
    if not model:
        msg = "the store is empty: run `index --run <run>` first"
        raise RuntimeError(msg)
    return embedder_for_model(model, _endpoint(args))


def cmd_search(args: argparse.Namespace) -> int:
    """Hybrid search: meaning and keywords, fused by rank."""
    from src.modules.content_processor.knowledge.retrieval import hybrid_search, render_hits

    try:
        store, _note = _open(args, HASHING_DIMS)
    except RuntimeError as exc:
        return _fail(str(exc))
    try:
        embedder = _query_embedder(args, store)
        hits = hybrid_search(store, embedder, args.query, top_k=args.top_k, offering=args.offering)
    except (RuntimeError, ValueError, LocalOnlyError) as exc:
        return _fail(str(exc))
    finally:
        store.close()
    if args.json:
        payload = [
            {
                "rank": n,
                "score": h.score,
                "vector_rank": h.vector_rank,
                "vector_score": h.vector_score,
                "keyword_rank": h.keyword_rank,
                "chunk_id": h.chunk.chunk_id,
                "url": h.chunk.url,
                "headings": h.chunk.heading_path,
                "role": h.chunk.chunk_role,
                "offering": h.chunk.offering_id,
                "text": h.chunk.verbatim_text,
                "block_ids": h.chunk.block_ids,
                "evidence_ids": h.chunk.evidence_ids,
            }
            for n, h in enumerate(hits, start=1)
        ]
        _safe_write_stdout(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    else:
        _safe_write_stdout(render_hits(args.query, hits))
    return 0


def _chat_for(args: argparse.Namespace) -> LocalChat | None:
    if args.no_model:
        return None
    model = args.llm_model or get_settings().cp_chat_model
    endpoint = _endpoint(args)
    server = check_model_server(endpoint)
    return LocalChat(endpoint, model) if server.has(model) else None


def cmd_ask(args: argparse.Namespace) -> int:
    """Answer a question from the stored passages, every point checked and cited."""
    from src.modules.content_processor.knowledge.retrieval import answer_question, render_answer

    try:
        store, _note = _open(args, HASHING_DIMS)
    except RuntimeError as exc:
        return _fail(str(exc))
    chat: LocalChat | None = None
    try:
        embedder = _query_embedder(args, store)
        chat = _chat_for(args)
        answer = answer_question(
            store, embedder, args.question, chat=chat, top_k=args.top_k, offering=args.offering
        )
    except (RuntimeError, ValueError, LocalOnlyError) as exc:
        return _fail(str(exc))
    finally:
        store.close()
        if chat is not None:
            chat.close()
    if args.json:
        _safe_write_stdout(json.dumps(answer.as_dict(), indent=2, ensure_ascii=False) + "\n")
    else:
        _safe_write_stdout(render_answer(answer))
    return 0


def _store_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant", default=DEFAULT_TENANT, help="tenant (default rankuno-dev)")
    parser.add_argument(
        "--store",
        choices=["auto", "sqlite", "postgres"],
        default="auto",
        help="auto: Postgres with pgvector when configured, else the local SQLite file",
    )
    parser.add_argument("--db-url", default=None, help="Postgres URL (default: CP_DATABASE_URL)")
    parser.add_argument(
        "--store-path", default=None, help="SQLite store root (default: CP_KNOWLEDGE_ROOT)"
    )
    parser.add_argument(
        "--endpoint", default=None, help="local model endpoint (default: CP_MODEL_ENDPOINT)"
    )


def register(subparsers: Any) -> None:
    """Add `kb-check`, `index`, `search` and `ask` to the CLI's subparsers."""
    check = subparsers.add_parser(
        "kb-check", help="is the local model server and Postgres with pgvector ready?"
    )
    _store_args(check)
    check.set_defaults(func=cmd_kb_check)

    index = subparsers.add_parser("index", help="embed, label and store a run's chunks")
    index.add_argument("--run", required=True, help="run name, e.g. jh_full")
    index.add_argument(
        "--dir", default=None, help="folder of the run files (default exports/<run>)"
    )
    index.add_argument("--out", default=None, help="folder for the label review files")
    _store_args(index)
    index.add_argument(
        "--embedder",
        choices=["auto", "ollama", "cpu", "hashing"],
        default="auto",
        help="auto: Ollama when it has the model, else the CPU fallback model, else hashing",
    )
    index.add_argument("--embed-model", default=None, help="default: CP_EMBED_MODEL (bge-m3)")
    index.add_argument(
        "--llm-model", default=None, help="local instruct model as a third label signal (optional)"
    )
    index.add_argument(
        "--rebuild", action="store_true", help="empty the tenant's store first (new model)"
    )
    index.set_defaults(func=cmd_index)

    search = subparsers.add_parser("search", help="hybrid search over the stored passages")
    search.add_argument("query")
    _store_args(search)
    search.add_argument("--offering", default=None, help="only this offering and those under it")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--json", action="store_true", help="JSON instead of Markdown")
    search.set_defaults(func=cmd_search)

    ask = subparsers.add_parser("ask", help="answer a question with checked, cited quotes")
    ask.add_argument("question")
    _store_args(ask)
    ask.add_argument("--offering", default=None, help="only this offering and those under it")
    ask.add_argument("--top-k", type=int, default=5)
    ask.add_argument("--llm-model", default=None, help="default: CP_CHAT_MODEL")
    ask.add_argument(
        "--no-model", action="store_true", help="extractive answer only (no instruct model)"
    )
    ask.add_argument("--json", action="store_true", help="JSON instead of Markdown")
    ask.set_defaults(func=cmd_ask)
