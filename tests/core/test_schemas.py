"""Contract tests for the shared data models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.schemas import (
    ExecutionStatus,
    RiskClass,
    StrictModel,
    ToolMetadata,
    ToolResult,
)


class _Sample(StrictModel):
    value: int


def test_strict_model_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        _Sample(value=1, typo_field=2)


def test_tool_metadata_rejects_invalid_name():
    with pytest.raises(ValidationError):
        ToolMetadata(name="Bad Name!", summary="x", risk_class=RiskClass.READ)


def test_paid_tool_must_be_financial_risk_class():
    """A tool that spends money cannot hide behind a READ classification."""
    with pytest.raises(ValidationError):
        ToolMetadata(
            name="seo.paid_lookup",
            summary="Costs money.",
            risk_class=RiskClass.READ,
            estimated_cost_usd=0.05,
        )


def test_financial_tool_may_declare_cost():
    meta = ToolMetadata(
        name="ads.spend",
        summary="Spends budget.",
        risk_class=RiskClass.FINANCIAL,
        estimated_cost_usd=0.05,
    )
    assert meta.estimated_cost_usd == pytest.approx(0.05)


def test_tool_result_ok_property():
    ok = ToolResult[_Sample](status=ExecutionStatus.SUCCESS, tool="t", data=_Sample(value=1))
    failed = ToolResult[_Sample](status=ExecutionStatus.FAILED, tool="t", error="boom")
    assert ok.ok is True
    assert failed.ok is False


def test_tool_result_rejects_negative_duration():
    with pytest.raises(ValidationError):
        ToolResult[_Sample](status=ExecutionStatus.SUCCESS, tool="t", duration_ms=-1.0)
