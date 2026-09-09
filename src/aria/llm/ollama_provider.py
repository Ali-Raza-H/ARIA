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
        image_models = settings.get("image_input_models", [])
        self.supports_image_input = model in image_models if isinstance(image_models, list) else False
        self.supports_image_generation = False
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
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [self._normalize_message(message) for message in messages],
            "stream": True,
        }
        if tools and self.native_tools:
            request["tools"] = list(tools)

        for attempt in range(2):
            try:
                # Buffer callbacks until the stream finishes. This prevents a
                # partial response from being rendered twice if the first
                # attempt runs out of VRAM and is retried.
                pending_text: list[str] = []
                content_parts: list[str] = []
                raw_calls: list[Any] = []
                response = self.client.chat(**request)
                for chunk in response:
                    message = _get(chunk, "message")
                    if message is None:
                        continue
                    text = _get(message, "content", "")
                    if text:
                        content_parts.append(text)
                        if on_text and self.native_tools:
                            pending_text.append(text)
                    raw_calls.extend(_get(message, "tool_calls", None) or [])
                if on_text and self.native_tools:
                    for text in pending_text:
                        on_text(text)
                break
            except Exception as exc:
                if attempt == 0 and self._is_oom(exc):
                    log_error("OllamaProvider: out of memory; clearing VRAM before one retry")
                    try:
                        self.clear_vram()
                    except Exception as clear_exc:
                        log_error(
                            f"OllamaProvider: VRAM cleanup failed: "
                            f"{type(clear_exc).__name__}: {clear_exc}"
                        )
                    continue
                log_error(f"OllamaProvider: chat failed: {type(exc).__name__}: {exc}")
                raise
        else:  # pragma: no cover - the loop either returns or raises
            raise RuntimeError("Ollama request failed without an exception")

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

    @staticmethod
    def _normalize_message(message: dict[str, Any]) -> dict[str, Any]:
        """Convert provider-neutral image parts to Ollama's message shape."""
        content = message.get("content")
        if not isinstance(content, list):
            return dict(message)
        text_parts: list[str] = []
        images: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            elif part.get("type") == "image_url":
                image_url = part.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if isinstance(url, str) and "," in url:
                    images.append(url.split(",", 1)[1])
        normalized = dict(message)
        normalized["content"] = "\n".join(text_parts)
        if images:
            normalized["images"] = images
        return normalized

    @staticmethod
    def _is_oom(error: Exception) -> bool:
        """Return whether Ollama reported a GPU/CPU out-of-memory failure."""
        message = str(error).lower()
        return "out of memory" in message or "outofmemory" in message or "oom" in message

    def clear_vram(self) -> int:
        """Unload every currently running Ollama model and return its count."""
        response = self.client.ps()
        models = _get(response, "models", []) or []
        unloaded = 0
        for model in models:
            name = _get(model, "name") or _get(model, "model")
            if not name:
                continue
            # Ollama unloads a model when an empty generate request uses
            # keep_alive=0 (the supported API equivalent of `ollama stop`).
            self.client.generate(model=str(name), prompt="", keep_alive=0)
            unloaded += 1
        log_info(f"OllamaProvider: unloaded {unloaded} model(s) from VRAM")
        return unloaded

    def list_models(self) -> list[str]:
        """Return locally installed Ollama models."""
        try:
            return sorted(str(m.model) for m in self.client.list().models)
        except Exception as exc:
            log_error(f"OllamaProvider: list_models failed: {exc}")
            return []
