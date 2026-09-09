"""OpenAI-compatible adapter shared by hosted model providers.

All of these services speak the OpenAI chat-completions protocol; switching
between them only swaps the base URL, model, and API key.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from typing import Any, cast

from openai import OpenAI

from ..logging_setup import log_debug, log_error, log_info
from .base import AssistantMessage, ToolCall


class OpenAICompatProvider:
    """Adapter for any OpenAI-compatible chat-completions endpoint."""

    name = "openai_compat"

    def __init__(
        self,
        provider_name: str,
        model: str,
        settings: dict[str, Any],
        *,
        credential_role: str = "aria",
    ) -> None:
        if credential_role == "coder":
            api_key_env = str(settings.get("coder_api_key_env", ""))
            api_key = os.getenv(api_key_env) if api_key_env else None
            role_name = "coder"
        elif credential_role not in {"aria", ""}:
            api_key_env = str(settings.get("background_api_key_env", "") or settings.get("aria_api_key_env", ""))
            api_key = os.getenv(api_key_env) if api_key_env else None
            role_name = credential_role
            # Background/vision credentials may be dedicated or reuse ARIA's key.
        else:
            api_key_env = str(settings.get("aria_api_key_env", ""))
            api_key = os.getenv(api_key_env) if api_key_env else None
            role_name = "ARIA"
        if not api_key:
            raise ValueError(
                f"Provider '{provider_name}' needs an API key for {role_name}: "
                f"set {api_key_env or 'the provider credential'} in .env"
            )
        base_url = str(settings.get("base_url", ""))
        if not base_url:
            raise ValueError(f"Provider '{provider_name}' is missing base_url in config.yaml")
        self.name = provider_name
        self.model = model
        image_models = settings.get("image_input_models", [])
        self.supports_image_input = model in image_models if isinstance(image_models, list) else False
        self.supports_image_generation = model in (settings.get("image_generation_models", []) or []) if isinstance(settings.get("image_generation_models", []), list) else False
        log_debug(f"OpenAICompatProvider[{provider_name}]: endpoint={base_url} model={model}")
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> AssistantMessage:
        log_debug(f"OpenAICompatProvider[{self.name}]: request model={self.model} messages={len(messages)}")
        try:
            # The OpenAI SDK's typed message params are stricter than our normalized
            # dicts; the endpoint accepts them, so the cast is safe at runtime.
            request: dict[str, Any] = {
                "model": self.model,
                "messages": cast("Any", list(messages)),
                "stream": True,
            }
            if tools:
                request["tools"] = cast("Any", list(tools))
            stream = self.client.chat.completions.create(**request)
        except Exception as exc:
            log_error(f"OpenAICompatProvider[{self.name}]: request failed: {type(exc).__name__}: {exc}")
            raise

        content_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        for chunk in stream:
            choices = getattr(chunk, "choices", [])
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                if on_text:
                    on_text(text)
            for index, raw_call in enumerate(getattr(delta, "tool_calls", None) or []):
                position = getattr(raw_call, "index", index)
                entry = calls.setdefault(position, {"id": "", "name": "", "arguments": ""})
                call_id = getattr(raw_call, "id", None)
                if call_id:
                    entry["id"] = call_id
                function = getattr(raw_call, "function", None)
                if function is not None:
                    name = getattr(function, "name", None)
                    arguments = getattr(function, "arguments", None)
                    if name:
                        entry["name"] = name
                    if arguments:
                        entry["arguments"] += arguments

        normalized_calls: list[ToolCall] = []
        for index, raw_call in sorted(calls.items()):
            try:
                arguments = json.loads(raw_call["arguments"] or "{}")
            except json.JSONDecodeError:
                log_error(f"OpenAICompatProvider[{self.name}]: unparseable tool arguments for {raw_call['name']}")
                arguments = {}
            normalized_calls.append(
                ToolCall(
                    id=raw_call["id"] or f"call_{index}",
                    name=raw_call["name"],
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        log_info(f"OpenAICompatProvider[{self.name}]: completed, {len(normalized_calls)} tool call(s)")
        return AssistantMessage("".join(content_parts), normalized_calls)

    def generate_image(self, prompt: str, output_path: str) -> str:
        """Generate one image through an OpenAI-compatible images endpoint."""
        if not self.supports_image_generation:
            raise RuntimeError(f"model {self.model} is not marked image-generation capable")
        response = self.client.images.generate(model=self.model, prompt=prompt, n=1, size="1024x1024")
        if not response.data:
            raise RuntimeError("image provider returned no image data")
        item = response.data[0]
        import base64
        import pathlib
        data = getattr(item, "b64_json", None)
        if data:
            pathlib.Path(output_path).write_bytes(base64.b64decode(data))
        else:
            import httpx
            url = getattr(item, "url", None)
            if not url:
                raise RuntimeError("image provider returned neither b64_json nor url")
            pathlib.Path(output_path).write_bytes(httpx.get(url, timeout=30).content)
        return output_path

    def list_models(self) -> list[str]:
        """Return model ids available on this endpoint, best effort."""
        try:
            page = self.client.models.list()
            return sorted(str(model.id) for model in page)
        except Exception as exc:
            log_error(f"OpenAICompatProvider[{self.name}]: list_models failed: {exc}")
            return []
