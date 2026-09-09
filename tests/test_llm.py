from typing import Any

import ollama
import pytest

from aria.llm.factory import ProviderManager
from aria.llm.ollama_provider import OllamaProvider
from aria.llm.openai_compat import OpenAICompatProvider


def test_mistral_uses_shared_openai_compatible_provider(monkeypatch) -> None:
    created: list[dict[str, Any]] = []

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setenv("ARIA_MISTRAL_API_KEY", "test-key")
    monkeypatch.setattr("aria.llm.openai_compat.OpenAI", FakeOpenAI)

    manager = ProviderManager({})
    provider = manager.create("mistral", "mistral-small-latest")

    assert isinstance(provider, OpenAICompatProvider)
    assert provider.name == "mistral"
    assert provider.model == "mistral-small-latest"
    assert created == [{"api_key": "test-key", "base_url": "https://api.mistral.ai/v1"}]


@pytest.mark.parametrize(
    ("provider_name", "aria_env", "coder_env", "base_url", "model"),
    [
        (
            "cerebras",
            "ARIA_CEREBRAS_API_KEY",
            "CODER_CEREBRAS_API_KEY",
            "https://api.cerebras.ai/v1",
            "qwen-3.8-27b",
        ),
        (
            "groq",
            "ARIA_GROQ_API_KEY",
            "CODER_GROQ_API_KEY",
            "https://api.groq.com/openai/v1",
            "llama-3.1-8b-instant",
        ),
    ],
)
def test_cerebras_and_groq_support_aria_and_coder_roles(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    aria_env: str,
    coder_env: str,
    base_url: str,
    model: str,
) -> None:
    created: list[dict[str, Any]] = []

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setenv(aria_env, "aria-key")
    monkeypatch.setenv(coder_env, "coder-key")
    monkeypatch.setattr("aria.llm.openai_compat.OpenAI", FakeOpenAI)

    manager = ProviderManager({})
    aria_provider = manager.create(provider_name, model)
    coder_provider = manager.create_coder(provider_name, model)

    assert isinstance(aria_provider, OpenAICompatProvider)
    assert isinstance(coder_provider, OpenAICompatProvider)
    assert aria_provider.name == provider_name
    assert coder_provider.name == provider_name
    assert created == [
        {"api_key": "aria-key", "base_url": base_url},
        {"api_key": "coder-key", "base_url": base_url},
    ]


def test_zai_uses_shared_openai_compatible_provider(monkeypatch) -> None:
    created: list[dict[str, Any]] = []

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setenv("ARIA_ZAI_API_KEY", "test-key")
    monkeypatch.setattr("aria.llm.openai_compat.OpenAI", FakeOpenAI)

    manager = ProviderManager({})
    provider = manager.create("zai", "glm-4.7")

    assert isinstance(provider, OpenAICompatProvider)
    assert provider.name == "zai"
    assert provider.model == "glm-4.7"
    assert created == [{"api_key": "test-key", "base_url": "https://api.z.ai/api/paas/v4"}]


def test_zai_coder_role_uses_dedicated_key(monkeypatch) -> None:
    created: list[dict[str, Any]] = []

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setenv("ARIA_ZAI_API_KEY", "aria-key")
    monkeypatch.setenv("CODER_ZAI_API_KEY", "coder-key")
    monkeypatch.setattr("aria.llm.openai_compat.OpenAI", FakeOpenAI)

    manager = ProviderManager({})
    provider = manager.create_coder("zai", "glm-4.7")

    assert isinstance(provider, OpenAICompatProvider)
    assert created == [{"api_key": "coder-key", "base_url": "https://api.z.ai/api/paas/v4"}]


def test_coder_uses_a_dedicated_provider_key(monkeypatch) -> None:
    created: list[dict[str, Any]] = []

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setenv("ARIA_MISTRAL_API_KEY", "aria-key")
    monkeypatch.setenv("CODER_MISTRAL_API_KEY", "coder-key")
    monkeypatch.setattr("aria.llm.openai_compat.OpenAI", FakeOpenAI)

    manager = ProviderManager({})
    provider = manager.create_coder("mistral", "codestral-latest")

    assert isinstance(provider, OpenAICompatProvider)
    assert created == [{"api_key": "coder-key", "base_url": "https://api.mistral.ai/v1"}]


def test_coder_key_does_not_fall_back_to_aria_key(monkeypatch) -> None:
    monkeypatch.setenv("ARIA_MISTRAL_API_KEY", "aria-key")
    monkeypatch.delenv("CODER_MISTRAL_API_KEY", raising=False)

    manager = ProviderManager({})
    try:
        manager.create_coder("mistral", "codestral-latest")
    except ValueError as exc:
        assert "CODER_MISTRAL_API_KEY" in str(exc)
    else:
        raise AssertionError("coder provider unexpectedly used ARIA's key")


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


def test_ollama_clears_all_loaded_models(monkeypatch) -> None:
    unloaded: list[dict[str, Any]] = []

    class FakeClient:
        def ps(self):
            return {"models": [{"name": "gemma2:9b"}, {"name": "qwen2.5-coder:7b"}]}

        def generate(self, **request: Any):
            unloaded.append(request)
            return {}

    monkeypatch.setattr(ollama, "Client", lambda **kwargs: FakeClient())
    provider = OllamaProvider("gemma2:9b", {})

    assert provider.clear_vram() == 2
    assert unloaded == [
        {"model": "gemma2:9b", "prompt": "", "keep_alive": 0},
        {"model": "qwen2.5-coder:7b", "prompt": "", "keep_alive": 0},
    ]


def test_ollama_retries_once_after_oom(monkeypatch) -> None:
    calls = 0
    cleared = 0

    class FakeClient:
        def chat(self, **request: Any):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("CUDA out of memory")
            return [{"message": {"content": "recovered"}}]

        def ps(self):
            return {"models": []}

    monkeypatch.setattr(ollama, "Client", lambda **kwargs: FakeClient())
    provider = OllamaProvider("gemma2:9b", {})
    original_clear = provider.clear_vram

    def clear() -> int:
        nonlocal cleared
        cleared += 1
        return original_clear()

    provider.clear_vram = clear
    response = provider.complete([], [])

    assert response.content == "recovered"
    assert calls == 2
    assert cleared == 1


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
