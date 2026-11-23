"""How long the local model takes to write an answer, next to the search behind it.

Runs each question through the knowledge base's own answer path (`answer_question`: search,
rerank, context, then the local model at temperature 0) and times every model call
separately. Nothing leaves the model server named by `CP_MODEL_ENDPOINT` (the client
refuses non-local addresses); nothing is written except the optional --out file.

    python scripts/bench_answers.py --tenant rankuno-dev.<project>.<site> \
        --questions questions.txt [--limit 20] [--model qwen2.5:7b-instruct] [--out bench.json]

`questions.txt`: one question per line, or a JSON list of strings or of {"question": ...}.
Without a model server, or without the model on it, the script says so and measures
nothing (exit code 2): there is no simulated timing.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import get_settings  # noqa: E402
from src.modules.content_processor.analysis.nli import model_headers  # noqa: E402
from src.modules.content_processor.knowledge.assist import LocalChat  # noqa: E402
from src.modules.content_processor.knowledge.embedders import (  # noqa: E402
    HASHING_DIMS,
    check_model_server,
    embedder_for_model,
)
from src.modules.content_processor.knowledge.reranker import get_reranker  # noqa: E402
from src.modules.content_processor.knowledge.retrieval import answer_question  # noqa: E402
from src.modules.content_processor.knowledge.vector_store import open_store  # noqa: E402


class _TimedClient(httpx.Client):
    """Records the time and token counts of every model call."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls: list[dict[str, Any]] = []

    def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        started = time.perf_counter()
        response = super().post(*args, **kwargs)
        seconds = time.perf_counter() - started
        usage: dict[str, Any] = {}
        with contextlib.suppress(ValueError):
            usage = dict(response.json().get("usage") or {})
        self.calls.append(
            {
                "seconds": round(seconds, 3),
                "status": response.status_code,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            }
        )
        return response


def _questions(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    return [str(q["question"] if isinstance(q, dict) else q) for q in data]


def _summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "p90": None, "max": None}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {
        "median": round(statistics.median(ordered), 3),
        "p90": round(p90, 3),
        "max": round(ordered[-1], 3),
    }


def main(argv: list[str] | None = None) -> int:
    """Run the questions and print the timings (exit 2 when nothing could be measured)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant", required=True, help="knowledge namespace of one website")
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--model", default=None, help="default: CP_CHAT_MODEL")
    parser.add_argument("--endpoint", default=None, help="default: CP_MODEL_ENDPOINT")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--out", type=Path, default=None, help="write the details as JSON")
    args = parser.parse_args(argv)

    settings = get_settings()
    endpoint = args.endpoint or settings.cp_model_endpoint
    model = args.model or settings.cp_chat_model
    server = check_model_server(endpoint)
    if not server.has(model):
        sys.stderr.write(
            f"Nothing measured: the model server at {endpoint} "
            + ("does not have " + model if server.reachable else "is not reachable")
            + f" ({server.detail or 'no detail'}).\n"
        )
        return 2

    db_url = settings.cp_database_url.get_secret_value() if settings.cp_database_url else None
    store, _note = open_store(
        "auto",
        tenant=args.tenant,
        sqlite_root=settings.cp_knowledge_root,
        db_url=db_url,
        dims=HASHING_DIMS,
    )
    stored_model = store.meta().get("model")
    if not stored_model:
        sys.stderr.write(f"Nothing measured: the knowledge space {args.tenant} is empty.\n")
        return 2
    embedder = embedder_for_model(stored_model, endpoint)
    reranker = get_reranker()
    timed = _TimedClient(timeout=180.0, trust_env=False, headers=model_headers())
    chat = LocalChat(endpoint, model, client=timed)

    rows: list[dict[str, Any]] = []
    try:
        for question in _questions(args.questions)[: args.limit]:
            before = len(timed.calls)
            started = time.perf_counter()
            answer = answer_question(
                store, embedder, question, chat=chat, top_k=args.top_k, reranker=reranker
            )
            total = time.perf_counter() - started
            calls = timed.calls[before:]
            generation = sum(c["seconds"] for c in calls)
            tokens = sum(c["completion_tokens"] or 0 for c in calls)
            rows.append(
                {
                    "question": question,
                    "mode": answer.mode,
                    "total_s": round(total, 3),
                    "generation_s": round(generation, 3),
                    "search_and_checks_s": round(total - generation, 3),
                    "model_calls": len(calls),
                    "completion_tokens": tokens,
                    "tokens_per_s": round(tokens / generation, 1)
                    if generation and tokens
                    else None,
                }
            )
            sys.stdout.write(
                f"{total:6.2f}s total  {generation:6.2f}s model  "
                f"{answer.mode:10s} {question[:60]}\n"
            )
    finally:
        chat.close()
        store.close()

    generated = [r for r in rows if r["model_calls"]]
    summary = {
        "model": model,
        "endpoint": endpoint,
        "tenant": args.tenant,
        "questions": len(rows),
        "answered_by_model": sum(1 for r in rows if r["mode"] == "model"),
        "total_s": _summary([r["total_s"] for r in rows]),
        "generation_s": _summary([r["generation_s"] for r in generated]),
        "search_and_checks_s": _summary([r["search_and_checks_s"] for r in rows]),
        "tokens_per_s": _summary([r["tokens_per_s"] for r in generated if r["tokens_per_s"]]),
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    if args.out:
        args.out.write_text(
            json.dumps({"summary": summary, "questions": rows}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
