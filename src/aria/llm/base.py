"""Provider contracts used by the agent loops."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    """A normalized model request to execute one tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantMessage:
    """The normalized assistant response from any provider."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class Provider(Protocol):
    """Interface implemented by every model backend."""

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> AssistantMessage:
        """Generate one response, optionally streaming text to on_text."""
        ...


def list_models(provider: Provider) -> list[str]:
    """Return the adapter's models when it supports listing, else []."""
    listing = getattr(provider, "list_models", None)
    if callable(listing):
        found: Any = listing()
        if isinstance(found, (list, tuple, set)):
            return [str(model) for model in found]
    return []
