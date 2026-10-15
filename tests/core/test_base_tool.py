"""Tests for the governed execution pipeline in `BaseTool`.

Covers the contract every domain tool inherits: declaration is validated at
import, input and output are validated at the boundary, guardrails and budget
are applied before any side effect, and `run()` never raises.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from src.core.base_tool import BaseTool
from src.core.rate_limiter import CostLedger
from src.core.schemas import ExecutionStatus, RiskClass, ToolMetadata


class EchoIn(BaseModel):
    text: str


class EchoOut(BaseModel):
    text: str
    length: int


class EchoTool(BaseTool[EchoIn, EchoOut]):
    """Harmless read-only tool used across these tests."""

    metadata = ToolMetadata(name="test.echo", summary="Echo.", risk_class=RiskClass.READ)
    input_model = EchoIn
    output_model = EchoOut

    def execute(self, payload: EchoIn) -> EchoOut:
        return EchoOut(text=payload.text, length=len(payload.text))


class WriteTool(EchoTool):
    """Same body, but classified as mutating external state."""

    metadata = ToolMetadata(name="test.write_echo", summary="Write.", risk_class=RiskClass.WRITE)


class PaidTool(EchoTool):
    metadata = ToolMetadata(
        name="test.paid",
        summary="Paid.",
        risk_class=RiskClass.FINANCIAL,
        estimated_cost_usd=0.4,
    )


class ExplodingTool(EchoTool):
    metadata = ToolMetadata(name="test.explode", summary="Fails.", risk_class=RiskClass.READ)

    def execute(self, payload: EchoIn) -> EchoOut:
        raise RuntimeError("upstream exploded")


class WrongOutputTool(EchoTool):
    metadata = ToolMetadata(name="test.wrong_out", summary="Bad.", risk_class=RiskClass.READ)

    def execute(self, payload: EchoIn) -> EchoOut:
        return "not a model"  # type: ignore[return-value]


class TestDeclarationContract:
    def test_missing_metadata_fails_at_class_creation(self):
        """A malformed tool must fail at import, not mid-agent-run."""
        with pytest.raises(TypeError, match="metadata"):

            class Broken(BaseTool[EchoIn, EchoOut]):
                input_model = EchoIn
                output_model = EchoOut

                def execute(self, payload: EchoIn) -> EchoOut:
                    return EchoOut(text="", length=0)

    def test_metadata_must_be_tool_metadata_instance(self):
        with pytest.raises(TypeError, match="ToolMetadata"):

            class Broken(BaseTool[EchoIn, EchoOut]):
                metadata = "test.broken"
                input_model = EchoIn
                output_model = EchoOut

                def execute(self, payload: EchoIn) -> EchoOut:
                    return EchoOut(text="", length=0)


class TestHappyPath:
    def test_returns_validated_output(self, permissive_guardrails, ledger):
        result = EchoTool(permissive_guardrails, ledger).run({"text": "rankuno"})
        assert result.ok
        assert result.status is ExecutionStatus.SUCCESS
        assert result.data.length == 7
        assert result.trace_id

    def test_accepts_a_model_instance(self, permissive_guardrails, ledger):
        result = EchoTool(permissive_guardrails, ledger).run(EchoIn(text="abc"))
        assert result.ok

    def test_read_only_tool_is_not_flagged_for_review(self, strict_guardrails, ledger):
        result = EchoTool(strict_guardrails, ledger).run({"text": "abc"})
        assert result.requires_human_review is False


class TestBoundaryValidation:
    def test_invalid_input_fails_without_executing(self, permissive_guardrails, ledger):
        result = EchoTool(permissive_guardrails, ledger).run({"wrong_key": 1})
        assert result.status is ExecutionStatus.FAILED
        assert result.data is None

    def test_wrong_model_type_is_rejected(self, permissive_guardrails, ledger):
        class Other(BaseModel):
            text: str

        result = EchoTool(permissive_guardrails, ledger).run(Other(text="x"))
        assert result.status is ExecutionStatus.FAILED

    def test_bad_output_type_is_caught_at_the_boundary(self, permissive_guardrails, ledger):
        """An unvalidated shape must not escape into a report or another tool."""
        result = WrongOutputTool(permissive_guardrails, ledger).run({"text": "x"})
        assert result.status is ExecutionStatus.FAILED
        assert "EchoOut" in result.error


class TestGuardrailIntegration:
    def test_write_tool_is_blocked_without_approval(self, strict_guardrails, ledger):
        result = WriteTool(strict_guardrails, ledger).run({"text": "x"})
        assert result.status is ExecutionStatus.BLOCKED_PENDING_APPROVAL
        assert result.data is None

    def test_write_tool_proceeds_with_approval(self, permissive_guardrails, ledger):
        result = WriteTool(permissive_guardrails, ledger).run({"text": "x"})
        assert result.ok
        assert result.requires_human_review is True

    def test_blocked_tool_is_not_charged(self, strict_guardrails):
        """Guardrails run before the ledger, so a refused action costs nothing."""
        ledger = CostLedger(ceiling_usd=10.0)
        PaidTool(strict_guardrails, ledger).run({"text": "x"})
        assert ledger.spent_usd == pytest.approx(0.0)

    def test_default_construction_denies_writes(self):
        """A tool wired with no explicit engine must not be able to write."""
        result = WriteTool().run({"text": "x"})
        assert result.status is ExecutionStatus.BLOCKED_PENDING_APPROVAL


class TestBudgetIntegration:
    def test_paid_tool_charges_the_ledger(self, permissive_guardrails):
        ledger = CostLedger(ceiling_usd=10.0)
        result = PaidTool(permissive_guardrails, ledger).run({"text": "x"})
        assert result.ok
        assert ledger.spent_usd == pytest.approx(0.4)
        assert result.cost_usd == pytest.approx(0.4)

    def test_exhausted_budget_stops_execution(self, permissive_guardrails):
        ledger = CostLedger(ceiling_usd=0.5)
        tool = PaidTool(permissive_guardrails, ledger)
        assert tool.run({"text": "x"}).ok
        second = tool.run({"text": "x"})
        assert second.status is ExecutionStatus.BUDGET_EXCEEDED


class TestFailureHandling:
    def test_execute_exceptions_become_results_not_raises(self, permissive_guardrails, ledger):
        result = ExplodingTool(permissive_guardrails, ledger).run({"text": "x"})
        assert result.status is ExecutionStatus.FAILED
        assert "upstream exploded" in result.error
        assert result.duration_ms >= 0.0
