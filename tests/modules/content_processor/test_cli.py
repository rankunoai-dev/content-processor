from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.modules.content_processor import cli
from src.modules.content_processor.enums import AccessTier

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_SCOPE = ROOT / "fixtures" / "scopes" / "local_fixtures.json"
JH_SCOPE = ROOT / "fixtures" / "scopes" / "johnhancock.json"


def test_plan_fixture_scope_allows_loopback_in_development(capsys):
    assert cli.main(["plan", "--scope", str(FIXTURE_SCOPE)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scheduled"] == 23 and out["excluded"] == 0
    assert out["hosts"] == {"127.0.0.1": 23}


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
