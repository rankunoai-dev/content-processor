"""Tests for the HITL governance matrix.

These are the highest-value tests in the repository: they are what stops a
regression from silently letting an agent spend money or mutate a live site.
"""

from __future__ import annotations

import pytest

from src.core.config import Environment, Settings
from src.core.errors import ApprovalRequiredError
from src.core.guardrails import (
    AutoApproveProvider,
    CallbackApprovalProvider,
    GuardrailEngine,
)
from src.core.schemas import ApprovalMode, RiskClass, ToolMetadata


def _meta(risk: RiskClass) -> ToolMetadata:
    return ToolMetadata(name=f"test.{risk.value}", summary="x", risk_class=risk)


def test_read_actions_are_automatic(strict_guardrails):
    decision = strict_guardrails.authorize(_meta(RiskClass.READ))
    assert decision.allowed is True
    assert decision.mode is ApprovalMode.AUTOMATIC
    assert decision.requires_human_review is False


def test_draft_actions_are_allowed_but_flagged_for_review(strict_guardrails):
    decision = strict_guardrails.authorize(_meta(RiskClass.DRAFT))
    assert decision.allowed is True
    assert decision.mode is ApprovalMode.OPERATOR_REVIEW
    assert decision.requires_human_review is True


@pytest.mark.parametrize("risk", [RiskClass.WRITE, RiskClass.FINANCIAL])
def test_write_and_financial_are_denied_without_an_approver(strict_guardrails, risk):
    """Deny-by-default: an unattended run cannot write or spend."""
    decision = strict_guardrails.authorize(_meta(risk))
    assert decision.allowed is False
    assert decision.mode is ApprovalMode.MANDATORY_HITL


@pytest.mark.parametrize("risk", [RiskClass.WRITE, RiskClass.FINANCIAL])
def test_write_and_financial_pass_with_operator_approval(permissive_guardrails, risk):
    decision = permissive_guardrails.authorize(_meta(risk))
    assert decision.allowed is True
    assert decision.requires_human_review is True


def test_enforce_raises_on_denial(strict_guardrails):
    with pytest.raises(ApprovalRequiredError) as exc_info:
        strict_guardrails.enforce(_meta(RiskClass.WRITE), context="update live title tags")
    assert exc_info.value.tool == "test.write"


def test_callback_provider_receives_context(settings):
    seen: list[str] = []

    def approver(metadata: ToolMetadata, context: str) -> bool:
        seen.append(f"{metadata.name}|{context}")
        return True

    engine = GuardrailEngine(CallbackApprovalProvider(approver), settings=settings)
    assert engine.authorize(_meta(RiskClass.WRITE), "ctx").allowed is True
    assert seen == ["test.write|ctx"]


def test_broken_approver_denies_rather_than_approves(settings):
    """A crashing approval channel must never be read as consent."""

    def approver(metadata: ToolMetadata, context: str) -> bool:
        raise RuntimeError("approval service down")

    engine = GuardrailEngine(CallbackApprovalProvider(approver), settings=settings)
    assert engine.authorize(_meta(RiskClass.FINANCIAL)).allowed is False


def test_callback_provider_requires_callable():
    with pytest.raises(TypeError):
        CallbackApprovalProvider("not callable")


def test_config_can_relax_write_policy_to_review_only(tmp_path):
    settings = Settings(
        _env_file=None,
        audit_log_path=tmp_path / "audit.jsonl",
        require_approval_for_writes=False,
    )
    engine = GuardrailEngine(settings=settings)
    assert engine.policy_for(RiskClass.WRITE) is ApprovalMode.OPERATOR_REVIEW
    # Spend policy is independent and stays locked down.
    assert engine.policy_for(RiskClass.FINANCIAL) is ApprovalMode.MANDATORY_HITL


def test_disabling_guardrails_allows_everything_in_development(tmp_path):
    settings = Settings(
        _env_file=None,
        environment=Environment.DEVELOPMENT,
        audit_log_path=tmp_path / "audit.jsonl",
        guardrails_enabled=False,
    )
    engine = GuardrailEngine(settings=settings)
    assert engine.authorize(_meta(RiskClass.FINANCIAL)).allowed is True


def test_auto_approve_provider_is_accepted_in_development(settings):
    engine = GuardrailEngine(AutoApproveProvider(), settings=settings)
    assert engine.authorize(_meta(RiskClass.WRITE)).allowed is True
