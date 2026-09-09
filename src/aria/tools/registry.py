"""Tool registry with execution logging."""

from __future__ import annotations

import time
from typing import Any

from ..logging.setup import log_debug, log_error, log_info
from .base import Tool, ToolContext, ToolResult


class ToolRegistry:
    """Collection of tools exposed to the model."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        log_debug(f"ToolRegistry: registered tool '{tool.name}'")

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def begin_turn(self) -> None:
        """Reset optional stateful tools before a new user request."""
        for tool in self._tools.values():
            reset = getattr(tool.handler, "begin_turn", None)
            if callable(reset):
                reset()

    def execute(self, name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            log_error(f"ToolRegistry: model requested unknown tool '{name}'")
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        started = time.monotonic()
        try:
            result = tool.handler(arguments, context)
        except Exception as exc:  # Tool failures should be visible to the model, not kill the loop.
            log_error(f"ToolRegistry: '{name}' raised {type(exc).__name__}: {exc}")
            return ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)
        elapsed = time.monotonic() - started
        status = "error" if result.is_error else "ok"
        log_info(f"ToolRegistry: '{name}' -> {status} in {elapsed:.2f}s")
        log_debug(f"ToolRegistry: '{name}' arguments={arguments} output={result.output[:400]!r}")
        return result
