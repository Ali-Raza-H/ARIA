from typing import Any

import ollama

from aria.llm.factory import ProviderManager
from aria.llm.ollama_provider import OllamaProvider
from aria.llm.openai_compat import OpenAICompatProvider


def test_mistral_uses_shared_openai_compatible_provider(monkeypatch) -> None:
    created: list[dict[str, Any]] = []

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setattr("aria.llm.openai_compat.OpenAI", FakeOpenAI)

    manager = ProviderManager({})
    provider = manager.create("mistral", "mistral-small-latest")

    assert isinstance(provider, OpenAICompatProvider)
    assert provider.name == "mistral"
    assert provider.model == "mistral-small-latest"
    assert created == [{"api_key": "test-key", "base_url": "https://api.mistral.ai/v1"}]


def test_ollama_uses_custom_tools_by_default(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []

    class FakeClient:
        def chat(self, **request: Any):
            requests.append(request)
            return [
                {
                    "message": {
                        "content": (
                            '<tool_call>{"name":"read_file",'
                            '"arguments":{"path":"a.txt"}}</tool_call>'
                        )
                    }
                }
            ]

    monkeypatch.setattr(ollama, "Client", lambda **kwargs: FakeClient())
    provider = OllamaProvider("gemma2:9b", {"host": "http://localhost:11434"})
    response = provider.complete([], [{"type": "function"}])

    assert "tools" not in requests[0]
    assert response.tool_calls == []
    assert "<tool_call>" in response.content


def test_ollama_native_tools_are_opt_in(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []

    class FakeClient:
        def chat(self, **request: Any):
            requests.append(request)
            return []

    monkeypatch.setattr(ollama, "Client", lambda **kwargs: FakeClient())
    provider = OllamaProvider("tool-model", {"native_tools": True})
    provider.complete([], [{"type": "function"}])

    assert requests[0]["tools"] == [{"type": "function"}]
