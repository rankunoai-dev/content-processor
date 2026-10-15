"""Tests for typed configuration loading."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from src.core.config import Environment, Settings, get_settings, reset_settings_cache
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
