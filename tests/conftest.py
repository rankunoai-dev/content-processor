"""Shared pytest fixtures.

Invariants:

1. Tests never read the developer's real `.env`; every test gets hermetic settings.
2. Tests never touch the network. DNS is injected; HTTP is never called.
3. Ledger tests run only when `CP_DATABASE_URL` points at a disposable Postgres.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from src.core.config import Environment, Settings, reset_settings_cache
from src.core.guardrails import AutoApproveProvider, GuardrailEngine
from src.core.rate_limiter import CostLedger
from src.core.registry import registry
from src.core.schemas import RiskClass, ToolMetadata
from src.modules.content_processor.contracts import Authorization, Budgets, RunScope
from src.modules.content_processor.enums import AccessTier, AuthorizationKind


@pytest.fixture(autouse=True)
def _isolate_settings_cache() -> Iterator[None]:
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(autouse=True)
def _isolate_url_history(tmp_path, monkeypatch) -> None:
    """Runs never share the developer's URL history (each test starts with none)."""
    monkeypatch.setenv("CP_URL_HISTORY_ROOT", str(tmp_path / "url_history"))
    reset_settings_cache()


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.DEVELOPMENT,
        audit_log_path=tmp_path / "audit.jsonl",
        max_session_spend_usd=1.0,
        default_requests_per_minute=600,
        default_timeout_s=2.0,
    )


@pytest.fixture
def permissive_guardrails(settings: Settings) -> GuardrailEngine:
    return GuardrailEngine(approval_provider=AutoApproveProvider(), settings=settings)


@pytest.fixture
def strict_guardrails(settings: Settings) -> GuardrailEngine:
    return GuardrailEngine(settings=settings)


@pytest.fixture
def ledger() -> CostLedger:
    return CostLedger(ceiling_usd=1.0)


@pytest.fixture
def read_metadata() -> ToolMetadata:
    return ToolMetadata(
        name="test.reader", summary="Read-only test tool.", risk_class=RiskClass.READ
    )


@pytest.fixture
def write_metadata() -> ToolMetadata:
    return ToolMetadata(name="test.writer", summary="Write test tool.", risk_class=RiskClass.WRITE)


@pytest.fixture
def public_auth() -> Authorization:
    return Authorization(
        kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
        reference="public",
        approved_by="ai-lead",
        approved_at=datetime(2026, 10, 15, tzinfo=UTC),
        hosts=["example.com"],
    )


@pytest.fixture
def owner_auth() -> Authorization:
    return Authorization(
        kind=AuthorizationKind.OWNER_AUTHORIZED,
        reference="ENG-42",
        approved_by="client-signatory",
        approved_at=datetime(2026, 10, 15, tzinfo=UTC),
        hosts=["client.example"],
    )


@pytest.fixture
def scope(public_auth: Authorization) -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=["https://example.com/a", "https://example.com/b"],
        hosts_in_scope=["example.com"],
        authorization=public_auth,
        access_tier_ceiling=AccessTier.T3_FIDELITY_BROWSER,
        budgets=Budgets(cost_ceiling_usd=0.0),
    )


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("CP_DATABASE_URL")
    if not url:
        pytest.skip("CP_DATABASE_URL not set; ledger integration tests skipped")
    return url
