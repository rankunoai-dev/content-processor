"""Tool registry - the single catalogue of platform capabilities.

Serves two audiences:

* **Agents**, which need to enumerate what they can do and read each tool's risk
  class before choosing an action.
* **Reviewers**, who need one command to answer "what in this repo can spend
  money or mutate a client site?".

Registration is explicit rather than import-magic: a capability that nobody
registered does not silently become available to an agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.logger import get_logger
from src.core.schemas import RiskClass, ToolMetadata

if TYPE_CHECKING:
    from src.core.base_tool import BaseTool

__all__ = ["ToolRegistry", "registry"]

_logger = get_logger("core.registry")


class ToolRegistry:
    """Maps tool name to tool class."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._tools: dict[str, type[BaseTool[Any, Any]]] = {}

    def register(self, tool_cls: type[BaseTool[Any, Any]]) -> type[BaseTool[Any, Any]]:
        """Register a tool class. Usable as a decorator.

        Args:
            tool_cls: A concrete `BaseTool` subclass.

        Returns:
            The same class, so `@registry.register` works.

        Raises:
            ValueError: If the name is already taken. Silent shadowing would let
                a rename quietly redirect an agent to the wrong capability.
        """
        name = tool_cls.metadata.name
        existing = self._tools.get(name)
        if existing is not None and existing is not tool_cls:
            msg = (
                f"Tool name '{name}' is already registered to "
                f"{existing.__module__}.{existing.__qualname__}."
            )
            raise ValueError(msg)

        self._tools[name] = tool_cls
        _logger.debug(
            "tool_registered",
            extra={"tool": name, "risk": tool_cls.metadata.risk_class},
        )
        return tool_cls

    def get(self, name: str) -> type[BaseTool[Any, Any]]:
        """Look up a registered tool class by name.

        Raises:
            KeyError: If no tool is registered under that name.
        """
        try:
            return self._tools[name]
        except KeyError:
            known = ", ".join(sorted(self._tools)) or "<none>"
            msg = f"No tool named '{name}'. Registered: {known}"
            raise KeyError(msg) from None

    def names(self) -> list[str]:
        """All registered tool names, sorted."""
        return sorted(self._tools)

    def describe(self, risk_class: RiskClass | None = None) -> list[ToolMetadata]:
        """Return metadata for registered tools.

        Args:
            risk_class: Restrict to a single risk class. Use this to audit the
                platform's write and financial surface.

        Returns:
            Metadata for the matching tools, sorted by name.
        """
        items = [cls.metadata for cls in self._tools.values()]
        if risk_class is not None:
            items = [m for m in items if m.risk_class is risk_class]
        return sorted(items, key=lambda m: m.name)

    def clear(self) -> None:
        """Empty the registry. Tests only."""
        self._tools.clear()


registry = ToolRegistry()
"""Process-wide registry. Import this rather than constructing a new one."""
