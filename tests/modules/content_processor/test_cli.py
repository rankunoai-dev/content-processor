from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fixtures.server import app
from src.integrations.http_client import HttpFetcher
from src.modules.content_processor import cli
from src.modules.content_processor.enums import AccessTier, UrlBadge
from tests.modules.content_processor.l5_factory import record, report_path

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_SCOPE = ROOT / "fixtures" / "scopes" / "local_fixtures.json"
JH_SCOPE = ROOT / "fixtures" / "scopes" / "johnhancock.json"


def test_plan_fixture_scope_allows_loopback_in_development(capsys):
    assert cli.main(["plan", "--scope", str(FIXTURE_SCOPE)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scheduled"] == 28 and out["excluded"] == 0
    # access-failure fixtures live on a second host so their circuit cannot starve the rest (K13)
    assert out["hosts"] == {"127.0.0.1": 23, "localhost": 5}


def test_plan_johnhancock_scope_is_single_host_t3_ceiling(capsys):
    scope = cli.load_scope(str(JH_SCOPE))
    assert scope.access_tier_ceiling is AccessTier.T3_FIDELITY_BROWSER
    assert scope.budgets.cost_ceiling_usd == 0.0
    assert cli.main(["plan", "--scope", str(JH_SCOPE)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scheduled"] == 34 and out["hosts"] == {"www.johnhancock.com": 34}


def test_host_checker_denies_private_unless_scope_allows(monkeypatch):
    scope = cli.load_scope(str(JH_SCOPE))
    check = cli.host_checker_for(scope)
    allowed, reason = check("127.0.0.1")
    assert not allowed and reason.startswith("denied_literal")
    dev = cli.load_scope(str(FIXTURE_SCOPE))
    allowed, reason = cli.host_checker_for(dev)("127.0.0.1")
    assert allowed and reason == "allowed_private_fixture"
    allowed, reason = cli.host_checker_for(dev)("169.254.169.254")
    assert not allowed, "metadata endpoint is never allowed, even in development"


def test_database_commands_refuse_without_url(monkeypatch, settings):
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    with pytest.raises(SystemExit) as info:
        cli.main(["status", "--run", "x"])
    assert info.value.code == 2


def test_unknown_command_is_an_error():
    with pytest.raises(SystemExit):
        cli.main(["bogus"])


def test_report_subcommand_registered_and_shows_help(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["report", "--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "--records" in out
    assert "--out" in out
    assert "--threshold" in out


def test_report_subcommand_executes_successfully(tmp_path, capsys):
    records_file = tmp_path / "test.records.jsonl"
    r1 = record("http://example.com/one", ["Welcome to the test page."])
    r2 = record(
        "http://example.com/two",
        ["Another page with content."],
        badge=UrlBadge.PARTIAL,
    )
    records_file.write_text(
        r1.model_dump_json() + "\n" + r2.model_dump_json() + "\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "bundle_reports"
    rc = cli.main(["report", "--records", str(records_file), "--out", str(out_dir)])
    assert rc == 0

    stdout = capsys.readouterr().out
    manifest = json.loads(stdout)
    assert manifest["requested_urls"] == 2
    assert report_path(out_dir, "manifest.json").exists()
    assert report_path(out_dir, "r1_url_content.csv").exists()
    assert report_path(out_dir, "r1_url_content.xlsx").exists()
    assert report_path(out_dir, "review_queue.csv").exists()
    assert report_path(out_dir, "explorer.html").exists()


def _make_mini_scope(tmp_path: Path) -> Path:
    scope_data = {
        "tenant_id": "test-dev",
        "profile": "development",
        "mode": "SUPPLIED_URLS_PLUS_EMBEDDED",
        "input_urls": [
            "http://127.0.0.1:8765/pages/static_ok",
        ],
        "hosts_in_scope": ["127.0.0.1"],
        "authorization": {
            "kind": "PUBLIC_THIRD_PARTY",
            "reference": "fixtures",
            "approved_by": "tester",
            "approved_at": "2026-10-15T00:00:00Z",
            "hosts": ["127.0.0.1"],
        },
        "access_tier_ceiling": "T3_FIDELITY_BROWSER",
        "egress_allow_private": True,
        "budgets": {"cost_ceiling_usd": 0.0, "max_runtime_s": 60},
    }
    path = tmp_path / "mini_scope.json"
    path.write_text(json.dumps(scope_data), encoding="utf-8")
    return path


def _patch_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_fetcher(*args: Any, **kwargs: Any) -> HttpFetcher:
        kwargs["transport"] = httpx.ASGITransport(app=app)
        kwargs["host_checker"] = lambda _h: (True, "fixture")
        return HttpFetcher(*args, **kwargs)

    monkeypatch.setattr("src.modules.content_processor.cli.HttpFetcher", _fake_fetcher)


def test_dryrun_assembles_records_and_writes_reports_bundle(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    _patch_fetcher(monkeypatch)

    scope_path = _make_mini_scope(tmp_path)
    out_file = tmp_path / "dry_run.json"

    rc = cli.main(
        [
            "dryrun",
            "--scope",
            str(scope_path),
            "--no-render",
            "--out",
            str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.exists()

    report_data = json.loads(out_file.read_text(encoding="utf-8"))
    assert "verdicts" in report_data
    assert "records" in report_data
    assert report_data["records"]["count"] >= 1
    assert "reports" in report_data

    records_file = tmp_path / "dry_run.records.jsonl"
    assert records_file.exists()
    lines = [line for line in records_file.read_text("utf-8").splitlines() if line.strip()]
    assert len(lines) == report_data["records"]["count"]

    reports_dir = tmp_path / "dry_run_reports"
    assert reports_dir.is_dir()
    assert report_path(reports_dir, "manifest.json").exists()
    assert report_path(reports_dir, "r1_url_content.csv").exists()
    assert report_path(reports_dir, "explorer.html").exists()


def test_dryrun_with_explicit_reports_dir(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    _patch_fetcher(monkeypatch)

    scope_path = _make_mini_scope(tmp_path)
    custom_reports = tmp_path / "custom_reports_dir"

    rc = cli.main(
        [
            "dryrun",
            "--scope",
            str(scope_path),
            "--no-render",
            "--reports-dir",
            str(custom_reports),
        ]
    )
    assert rc == 0
    assert custom_reports.is_dir()
    assert report_path(custom_reports, "manifest.json").exists()
    assert report_path(custom_reports, "explorer.html").exists()


def test_dryrun_stdout_only(tmp_path, monkeypatch, settings, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    _patch_fetcher(monkeypatch)

    scope_path = _make_mini_scope(tmp_path)

    rc = cli.main(["dryrun", "--scope", str(scope_path), "--no-render"])
    assert rc == 0
    stdout = capsys.readouterr().out
    data = json.loads(stdout)
    assert "verdicts" in data
    assert data["scheduled"] == 1
