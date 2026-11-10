"""The queue route reads the per-URL rate at most every 30 s, however many tabs poll it."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.app import create_app
from src.modules.content_processor.webapi.memo import TimedMemo


def test_timed_memo_recomputes_only_after_the_ttl() -> None:
    now = [100.0]
    calls = [0]

    def compute() -> int:
        calls[0] += 1
        return calls[0]

    memo = TimedMemo(compute, ttl_s=30.0, clock=lambda: now[0])
    assert memo.get() == 1 and memo.get() == 1, "within the ttl the value is reused"
    now[0] += 29.9
    assert memo.get() == 1
    now[0] += 0.1
    assert memo.get() == 2, "after the ttl it is computed again"
    memo.forget()
    assert memo.get() == 3


def test_queue_route_reads_the_rate_once_per_window(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "app_data")
    runner = ScrapeRunner(store, command=lambda *_a: 0, report=lambda *_a: 0)
    reads = [0]
    original = runner.seconds_per_url

    def counted() -> float:
        reads[0] += 1
        return original()

    runner.seconds_per_url = counted  # type: ignore[method-assign]
    signer = TokenSigner.from_file(tmp_path / "app_data" / "secret.key")
    (tmp_path / "exports").mkdir()
    client = TestClient(
        create_app(tmp_path / "exports", projects=(store, runner, signer)),
        base_url="http://127.0.0.1",
    )
    for _ in range(5):
        assert client.get("/api/queue").status_code == 200
    # `runner.queue()` reads the rate itself once per request (the runner is not ours to
    # change); the route's own read happens once for all five polls instead of five times.
    assert reads[0] == 5 + 1
