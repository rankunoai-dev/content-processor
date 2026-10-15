"""Tests for the tool catalogue."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from src.core.base_tool import BaseTool
from src.core.registry import ToolRegistry
from src.core.schemas import RiskClass, ToolMetadata


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _make_tool(name: str, risk: RiskClass) -> type[BaseTool]:
    class _Tool(BaseTool[_In, _Out]):
        metadata = ToolMetadata(name=name, summary="x", risk_class=risk)
        input_model = _In
        output_model = _Out

        def execute(self, payload: _In) -> _Out:
            return _Out()

    return _Tool


def test_register_and_get():
    reg = ToolRegistry()
    tool = _make_tool("seo.audit", RiskClass.READ)
    assert reg.register(tool) is tool
    assert reg.get("seo.audit") is tool


def test_duplicate_names_are_rejected():
    """Silent shadowing would let a rename redirect an agent to the wrong tool."""
    reg = ToolRegistry()
    reg.register(_make_tool("seo.audit", RiskClass.READ))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_make_tool("seo.audit", RiskClass.WRITE))


def test_re_registering_the_same_class_is_idempotent():
    reg = ToolRegistry()
    tool = _make_tool("seo.audit", RiskClass.READ)
    reg.register(tool)
    reg.register(tool)
    assert reg.names() == ["seo.audit"]


def test_unknown_name_lists_what_is_available():
    reg = ToolRegistry()
    reg.register(_make_tool("seo.audit", RiskClass.READ))
    with pytest.raises(KeyError, match="seo.audit"):
        reg.get("seo.nope")


def test_describe_filters_by_risk_class():
    """This is the query a reviewer runs to audit the write/spend surface."""
    reg = ToolRegistry()
    reg.register(_make_tool("seo.audit", RiskClass.READ))
    reg.register(_make_tool("seo.publish", RiskClass.WRITE))
    reg.register(_make_tool("ads.spend", RiskClass.FINANCIAL))

    assert [m.name for m in reg.describe()] == ["ads.spend", "seo.audit", "seo.publish"]
    assert [m.name for m in reg.describe(RiskClass.WRITE)] == ["seo.publish"]


def test_clear_empties_the_registry():
    reg = ToolRegistry()
    reg.register(_make_tool("seo.audit", RiskClass.READ))
    reg.clear()
    assert reg.names() == []
