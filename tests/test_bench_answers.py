"""scripts/bench_answers.py: times the model calls of real answers, or measures nothing."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.core.config import reset_settings_cache
from src.modules.content_processor.knowledge.embedders import ModelServer
from src.modules.content_processor.knowledge.federated import space_namespace
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture

_SPEC = importlib.util.spec_from_file_location(
    "bench_answers", Path(__file__).resolve().parents[1] / "scripts" / "bench_answers.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bench: Any = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench)

ENDPOINT = "http://127.0.0.1:11434/v1"


def _setup(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("CP_KNOWLEDGE_ROOT", str(world.knowledge_root))
    reset_settings_cache()
    questions = tmp_path / "q.txt"
    questions.write_text("What does term life cover?\nWho rates Example Life?\n", "utf-8")
    tenant = space_namespace("rankuno-dev", world.project_id, world.site_ids["client"])
    return ["--tenant", tenant, "--questions", str(questions), "--endpoint", ENDPOINT]


def test_nothing_is_measured_without_the_model(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = _setup(world, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bench, "check_model_server", lambda _e: ModelServer(ENDPOINT, False, detail="down")
    )
    assert bench.main(argv) == 2
    assert "Nothing measured" in capsys.readouterr().err
    reset_settings_cache()


def test_each_answer_times_its_model_calls(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = _setup(world, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bench, "check_model_server", lambda _e: ModelServer(ENDPOINT, True, ["qwen"], "")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"points": []})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40},
            },
        )

    real = bench._TimedClient
    monkeypatch.setattr(
        bench,
        "_TimedClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    out = tmp_path / "bench.json"
    assert bench.main([*argv, "--model", "qwen", "--out", str(out)]) == 0
    data = json.loads(out.read_text("utf-8"))
    assert data["summary"]["questions"] == 2
    for row in data["questions"]:
        assert row["model_calls"] >= 1 and row["completion_tokens"] == 40 * row["model_calls"]
        assert row["generation_s"] <= row["total_s"]
    assert '"generation_s"' in capsys.readouterr().out
    reset_settings_cache()
