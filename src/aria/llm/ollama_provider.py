"""Ollama provider adapter for local models."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import ollama

from ..logging_setup import log_debug, log_error, log_info
from .base import AssistantMessage, ToolCall


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


class OllamaProvider:
    """Adapter for local models served by Ollama."""

    def __init__(self, model: str, settings: dict[str, Any]) -> None:
        host = settings.get("host")
        self.client = ollama.Client(host=host) if host else ollama.Client()
        self.model = model
        # Many Ollama models do not implement native function calling. The
        # assistant's text protocol is therefore the safe default; native
        # tools can be enabled explicitly for models that support them.
        self.native_tools = bool(settings.get("native_tools", False))
        log_debug(
            f"OllamaProvider: host={host or 'default'} model={model} "
            f"native_tools={self.native_tools}"
        )

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> AssistantMessage:
        log_debug(f"OllamaProvider: request model={self.model} messages={len(messages)}")
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": list(messages),
                "stream": True,
            }
            if tools and self.native_tools:
                request["tools"] = list(tools)
            response = self.client.chat(**request)
        except Exception as exc:
            log_error(f"OllamaProvider: chat failed: {type(exc).__name__}: {exc}")
            raise

        content_parts: list[str] = []
        raw_calls: list[Any] = []
        for chunk in response:
            message = _get(chunk, "message")
            if message is None:
                continue
            text = _get(message, "content", "")
            if text:
                content_parts.append(text)
                # In custom-protocol mode, wait until the complete response is
                # available so <tool_call> tags are never partially rendered.
                if on_text and self.native_tools:
                    on_text(text)
            raw_calls.extend(_get(message, "tool_calls", None) or [])

        calls: list[ToolCall] = []
        for index, raw_call in enumerate(raw_calls):
            function = _get(raw_call, "function", {})
            arguments = _get(function, "arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    log_error(f"OllamaProvider: unparseable arguments for {_get(function, 'name', '')}")
                    arguments = {}
            calls.append(
                ToolCall(
                    id=str(_get(raw_call, "id", None) or f"call_{index}"),
                    name=str(_get(function, "name", "")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        log_info(f"OllamaProvider: completed model={self.model}, {len(calls)} tool call(s)")
        return AssistantMessage("".join(content_parts), calls)

    def list_models(self) -> list[str]:
        """Return locally installed Ollama models."""
        try:
            return sorted(str(m.model) for m in self.client.list().models)
        except Exception as exc:
            log_error(f"OllamaProvider: list_models failed: {exc}")
            return []
