"""Contracts for modular agent tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolContext:
    """Resources and limits shared by tool handlers."""

    workspace: Path
    command_timeout_seconds: float | None = None
    max_command_output_chars: int | None = None


@dataclass(frozen=True)
class ToolResult:
    """Serializable result returned to the model."""

    output: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    """A callable tool plus its provider-neutral function schema."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], ToolResult]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
