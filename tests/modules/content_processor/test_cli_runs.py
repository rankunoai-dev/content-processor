"""CLI run commands (Lane L6): dryrun and the durable path against a fake Postgres ledger.

The durable commands only need the `DurableLedger` surface, so a MemoryLedger
wrapped with `TaskLedger`'s signatures exercises every line of the durable
path without a database. Fetching goes to the in-process fixture app.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fixtures.server import app
from src.integrations.http_client import FetchLimits, HttpFetcher
from src.modules.content_processor import cli
from src.modules.content_processor.ledger.memory import MemoryLedger

ROOT = Path(__file__).resolve().parents[3]
SCOPE_FILE = ROOT / "fixtures" / "scopes" / "local_fixtures.json"


def _small_scope(tmp_path: Path, paths: list[str]) -> Path:
    scope = json.loads(SCOPE_FILE.read_text(encoding="utf-8"))
    scope["input_urls"] = [f"http://127.0.0.1:8765{p}" for p in paths] + ["mailto:a@b.c"]
    scope["hosts_in_scope"] = ["127.0.0.1"]
    scope["authorization"]["hosts"] = ["127.0.0.1"]
    target = tmp_path / "scope.json"
    target.write_text(json.dumps(scope), encoding="utf-8")
    return target


class FakeDurable:
    """TaskLedger-shaped wrapper around MemoryLedger."""

    def __init__(self) -> None:
        self.mem = MemoryLedger(base_delay_s=0.0)
        self.runs: dict[str, Any] = {}
        self.schema_created = 0
        self.reaped = 0
        self.closed = 0

    def __getattr__(self, name: str) -> Any:  # noqa: D105 - delegation
        return getattr(self.mem, name)

    def create_schema(self) -> None:
        self.schema_created += 1

    def create_run(self, run_id: str, scope: Any, tool_versions: dict[str, str]) -> None:
        self.runs.setdefault(run_id, scope)

    def enqueue(self, tenant_id: str, tasks: Any) -> int:
        return self.mem.enqueue(tasks)

    def task_counts(self, run_id: str) -> dict[str, int]:
        return self.mem.task_counts()

    def reap(self) -> list[dict[str, Any]]:
        self.reaped += 1
        return []

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, settings: Any, tmp_path: Path) -> FakeDurable:
    """Route the CLI to the fixture app, a temp evidence dir and a fake durable ledger."""
    durable = FakeDurable()
    settings.cp_evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_open_durable_ledger", lambda: durable)

    def fetchers(scope: Any) -> tuple[Any, list[Any]]:
        return (
            HttpFetcher(
                host_checker=lambda _h: (True, "fixture"),
                user_agent="cli-test",
                transport=httpx.ASGITransport(app=app),
                limits=FetchLimits(max_bytes=1_000_000, total_timeout_s=5),
            ),
            [],
        )

    monkeypatch.setattr(cli, "build_fetchers", fetchers)
    return durable


def _last_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    return json.loads(out[out.index("{") :])


def test_dryrun_writes_url_records(wired: FakeDurable, tmp_path: Path, capsys):
    scope = _small_scope(tmp_path, ["/pages/static_ok", "/pages/gated_whitepaper"])
    out = tmp_path / "runs" / "dry.json"
    code = cli.main(
        ["dryrun", "--scope", str(scope), "--out", str(out), "--no-render", "--max-wait", "0.5"]
    )
    assert code == 0
    printed = _last_json(capsys)
    assert printed["mode"] == "dryrun_memory_ledger"
    assert printed["execution"]["stop_reason"] == "drained"
    assert printed["execution"]["stages"] == ["fetch", "docs"]
    summary = printed["url_records"]
    assert summary["urls"] == 3 and summary["by_badge"].get("EXCLUDED") == 1
    rows = out.with_suffix(".records.jsonl").read_text("utf-8").splitlines()
    assert len(rows) == 3
    report = json.loads(out.read_text("utf-8"))
    assert report["records"] == {"path": str(out.with_suffix(".records.jsonl")), "count": 3}
    assert sum(report["verdicts"].values()) == 3
    assert (out.parent / "dry_reports" / "manifest.json").exists(), "Stage 4 bundle written"
    assert wired.schema_created == 0, "dryrun never touches the durable ledger"


def test_run_drives_a_durable_run_and_reports(wired: FakeDurable, tmp_path: Path, capsys):
    scope = _small_scope(tmp_path, ["/pages/static_ok", "/downloads"])
    out = tmp_path / "runs" / "durable.json"
    code = cli.main(
        [
            "run", "--scope", str(scope), "--run", "run-x", "--out", str(out),
            "--no-render", "--no-docs", "--max-wait", "0.5",
        ]
    )  # fmt: skip
    assert code == 0
    printed = _last_json(capsys)
    assert printed["mode"] == "durable_postgres" and printed["run_id"] == "run-x"
    assert printed["enqueued"] == 2
    assert printed["execution"]["stop_reason"] == "drained"
    assert printed["execution"]["processed"]["fetch"] > 2, "follow-ups fetched in the same run"
    assert wired.schema_created == 1 and "run-x" in wired.runs
    assert wired.reaped >= 1 and wired.closed == 1
    assert out.with_suffix(".content.jsonl").exists()


def test_work_resumes_and_report_reads_without_workers(wired: FakeDurable, tmp_path: Path, capsys):
    scope = _small_scope(tmp_path, ["/pages/static_ok"])
    assert cli.main(["start", "--scope", str(scope), "--run", "run-y"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "work",
                "--scope",
                str(scope),
                "--run",
                "run-y",
                "--stages",
                "fetch",
                "--max-wait",
                "0.5",
            ]
        )
        == 0
    )
    worked = _last_json(capsys)
    assert worked["execution"]["stages"] == ["fetch"]
    assert worked["execution"]["processed"]["fetch"] == 1
    out = tmp_path / "report.json"
    assert cli.main(["records", "--scope", str(scope), "--run", "run-y", "--out", str(out)]) == 0
    reported = _last_json(capsys)
    records_file = out.with_suffix(".records.jsonl")
    capsys.readouterr()
    bundle = tmp_path / "bundle"
    assert cli.main(["report", "--records", str(records_file), "--out", str(bundle)]) == 0
    assert (bundle / "r1_url_content.csv").exists(), "L5 report reads the durable records"
    assert reported["mode"] == "report_only" and reported["url_records"]["urls"] == 2


def test_durable_run_refuses_when_another_run_has_open_tasks(
    wired: FakeDurable, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
):
    scope = _small_scope(tmp_path, ["/pages/static_ok"])
    monkeypatch.setattr(cli, "foreign_open_tasks", lambda _ledger, _run: 4)
    code = cli.main(["run", "--scope", str(scope), "--run", "run-z", "--no-render"])
    assert code == cli.EXIT_FOREIGN_RUN
    printed = _last_json(capsys)
    assert printed["error"] == "other_run_active" and printed["foreign_open_tasks"] == 4
    assert "run-z" not in wired.runs, "nothing is created when the guard trips"


def test_fetch_alias_runs_fetch_only(wired: FakeDurable, tmp_path: Path, capsys):
    scope = _small_scope(tmp_path, ["/pages/static_ok"])
    cli.main(["start", "--scope", str(scope), "--run", "run-f"])
    capsys.readouterr()
    assert cli.main(["fetch", "--scope", str(scope), "--run", "run-f"]) == 0
    assert _last_json(capsys)["execution"]["stages"] == ["fetch"]


def test_unknown_stage_is_rejected(wired: FakeDurable, tmp_path: Path):
    scope = _small_scope(tmp_path, ["/pages/static_ok"])
    with pytest.raises(SystemExit, match="unknown stage"):
        cli.main(["dryrun", "--scope", str(scope), "--stages", "fetch,ocr"])


def test_build_fetchers_adds_t2_only_when_ceiling_and_library_allow(monkeypatch):
    scope = cli.load_scope(str(SCOPE_FILE))
    monkeypatch.setattr(cli, "impersonation_available", lambda: False)
    _, escalation = cli.build_fetchers(scope)
    assert escalation == []
    low = scope.model_copy(update={"access_tier_ceiling": "T1_POLITE_HTTP"})
    monkeypatch.setattr(cli, "impersonation_available", lambda: True)
    _, escalation = cli.build_fetchers(low)
    assert escalation == []


@pytest.mark.skipif(not cli.impersonation_available(), reason="curl_cffi not installed")
async def test_build_fetchers_t2_when_available():
    scope = cli.load_scope(str(SCOPE_FILE))
    t1, escalation = cli.build_fetchers(scope)
    try:
        assert [type(f).__name__ for f in escalation] == ["ImpersonatedFetcher"]
    finally:
        for f in (t1, *escalation):
            await f.aclose()
