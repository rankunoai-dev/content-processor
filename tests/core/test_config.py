"""Tests for typed configuration loading."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from src.core.config import (
    RENDER_PAGE_TIME_MARGIN_S,
    Environment,
    Settings,
    get_settings,
    reset_settings_cache,
)
from src.core.errors import ConfigurationError


def test_defaults_are_safe(tmp_path):
    settings = Settings(_env_file=None, audit_log_path=tmp_path / "a.jsonl")
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.guardrails_enabled is True
    assert settings.require_approval_for_writes is True
    assert settings.require_approval_for_spend is True


def test_log_level_is_normalised(tmp_path):
    settings = Settings(_env_file=None, audit_log_path=tmp_path / "a.jsonl", log_level="debug")
    assert settings.log_level == "DEBUG"


def test_invalid_log_level_rejected(tmp_path):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, audit_log_path=tmp_path / "a.jsonl", log_level="chatty")


def test_production_refuses_disabled_guardrails(tmp_path):
    """The one configuration we never want to discover in an incident review."""
    with pytest.raises(ConfigurationError):
        Settings(
            _env_file=None,
            audit_log_path=tmp_path / "a.jsonl",
            environment=Environment.PRODUCTION,
            guardrails_enabled=False,
        )


def test_require_returns_secret_value(tmp_path):
    settings = Settings(
        _env_file=None,
        audit_log_path=tmp_path / "a.jsonl",
        serp_api_key=SecretStr("abc123"),
    )
    assert settings.require("serp_api_key") == "abc123"


def test_require_raises_actionable_error_when_unset(tmp_path):
    settings = Settings(_env_file=None, audit_log_path=tmp_path / "a.jsonl")
    with pytest.raises(ConfigurationError, match="SERP_API_KEY"):
        settings.require("serp_api_key")


def test_require_rejects_unknown_field(tmp_path):
    settings = Settings(_env_file=None, audit_log_path=tmp_path / "a.jsonl")
    with pytest.raises(ConfigurationError):
        settings.require("not_a_real_setting")


def test_secrets_are_not_exposed_by_repr(tmp_path):
    settings = Settings(
        _env_file=None,
        audit_log_path=tmp_path / "a.jsonl",
        gemini_api_key=SecretStr("super-secret"),
    )
    assert "super-secret" not in repr(settings)


def test_get_settings_is_cached():
    reset_settings_cache()
    assert get_settings() is get_settings()


def test_render_timeouts_default_to_the_former_fixed_caps():
    settings = Settings(_env_file=None)
    assert settings.cp_render_navigation_timeout_s == 30.0
    assert settings.cp_render_max_page_time_s == 150.0
    assert settings.cp_render_settle_window_s == 12.0


def test_render_timeouts_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CP_RENDER_NAVIGATION_TIMEOUT_S", "120")
    monkeypatch.setenv("CP_RENDER_MAX_PAGE_TIME_S", "400.5")
    monkeypatch.setenv("CP_RENDER_SETTLE_WINDOW_S", "30")
    settings = Settings(_env_file=None)
    assert settings.cp_render_navigation_timeout_s == 120.0
    assert settings.cp_render_max_page_time_s == 400.5
    assert settings.cp_render_settle_window_s == 30.0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CP_RENDER_NAVIGATION_TIMEOUT_S", "4"),
        ("CP_RENDER_NAVIGATION_TIMEOUT_S", "601"),
        ("CP_RENDER_MAX_PAGE_TIME_S", "29"),
        ("CP_RENDER_MAX_PAGE_TIME_S", "1201"),
        ("CP_RENDER_SETTLE_WINDOW_S", "0.5"),
        ("CP_RENDER_SETTLE_WINDOW_S", "121"),
        ("CP_RENDER_SETTLE_WINDOW_S", "slow"),
    ],
)
def test_render_timeouts_outside_their_range_are_refused(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_page_cap_under_navigation_plus_margin_is_raised_and_logged(monkeypatch, caplog):
    monkeypatch.setenv("CP_RENDER_NAVIGATION_TIMEOUT_S", "300")
    monkeypatch.setenv("CP_RENDER_MAX_PAGE_TIME_S", "150")
    with caplog.at_level("WARNING", logger="src.core.config"):
        settings = Settings(_env_file=None)
    assert settings.cp_render_max_page_time_s == 300.0 + RENDER_PAGE_TIME_MARGIN_S
    assert "CP_RENDER_MAX_PAGE_TIME_S=150" in caplog.text
    assert "raised to 330" in caplog.text


def test_page_cap_at_the_margin_is_kept(monkeypatch, caplog):
    monkeypatch.setenv("CP_RENDER_NAVIGATION_TIMEOUT_S", "120")
    monkeypatch.setenv("CP_RENDER_MAX_PAGE_TIME_S", "150")
    with caplog.at_level("WARNING", logger="src.core.config"):
        settings = Settings(_env_file=None)
    assert settings.cp_render_max_page_time_s == 150.0
    assert "CP_RENDER_MAX_PAGE_TIME_S" not in caplog.text


def test_the_largest_navigation_timeout_still_fits_the_page_cap_range(monkeypatch):
    monkeypatch.setenv("CP_RENDER_NAVIGATION_TIMEOUT_S", "600")
    monkeypatch.setenv("CP_RENDER_MAX_PAGE_TIME_S", "30")
    settings = Settings(_env_file=None)
    assert settings.cp_render_max_page_time_s == 630.0
