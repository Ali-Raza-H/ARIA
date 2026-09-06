"""Provider construction and runtime model/provider management."""

from __future__ import annotations

from typing import Any

from ..logging_setup import log_debug, log_error, log_info
from .base import Provider, list_models
from .ollama_provider import OllamaProvider
from .openai_compat import OpenAICompatProvider

# Providers that ride the shared OpenAI-compatible adapter.
OPENAI_COMPATIBLE = {"gemini", "nvidia", "openrouter", "openai", "mistral"}

# Well-known defaults; config.yaml can override or extend any of them.
DEFAULT_PROVIDER_SETTINGS: dict[str, dict[str, Any]] = {
    "gemini": {
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
    },
    "nvidia": {
        "api_key_env": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models": ["meta/llama-3.3-70b-instruct", "nvidia/llama-3.3-nemotron-super-49b-v1"],
    },
    "openrouter": {
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["anthropic/claude-sonnet-4.5", "openai/gpt-4.1-mini"],
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4.1", "gpt-4.1-mini", "o4-mini"],
    },
    "mistral": {
        "api_key_env": "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1",
        "models": ["mistral-large-latest", "mistral-small-latest", "codestral-latest"],
    },
    "ollama": {
        "host": "http://127.0.0.1:11434",
        "models": ["gemma2:9b", "qwen2.5-coder:7b"],
    },
}


class ProviderManager:
    """Builds providers on demand and tracks the active model per provider.

    Model state is kept per provider so switching back and forth restores the
    last model used with that provider.
    """

    def __init__(self, provider_settings: dict[str, dict[str, Any]]) -> None:
        # Merge user settings over the built-in defaults.
        self._settings: dict[str, dict[str, Any]] = {}
        for name, defaults in DEFAULT_PROVIDER_SETTINGS.items():
            merged = dict(defaults)
            merged.update(provider_settings.get(name, {}))
            self._settings[name] = merged
        for name, values in provider_settings.items():
            if name not in self._settings:
                self._settings[name] = dict(values)
        self._active_model: dict[str, str] = {}
        log_debug(f"ProviderManager initialized with providers: {sorted(self._settings)}")

    def names(self) -> list[str]:
        return sorted(self._settings)

    def models_for(self, provider: str) -> list[str]:
        """Configured models for *provider*; falls back to a live API listing."""
        settings = self._settings.get(provider, {})
        models = [str(m) for m in settings.get("models", []) if str(m).strip()]
        if not models:
            models = self._remote_models(provider)
        return models

    def _remote_models(self, provider: str) -> list[str]:
        try:
            probe = self.create(provider, self._fallback_model(provider))
            return list_models(probe)
        except Exception as exc:
            log_error(f"ProviderManager: could not list models for {provider}: {exc}")
            return []

    def _fallback_model(self, provider: str) -> str:
        settings = self._settings.get(provider, {})
        return str(settings.get("model", "")) or "default"

    def active_model(self, provider: str) -> str | None:
        return self._active_model.get(provider)

    def set_active_model(self, provider: str, model: str) -> None:
        self._active_model[provider] = model
        log_info(f"ProviderManager: active model for {provider} set to {model}")

    def create(self, provider: str, model: str) -> Provider:
        """Instantiate the adapter for *provider*/*model*."""
        if provider not in self._settings:
            raise ValueError(f"Unknown provider: {provider}. Available: {', '.join(self.names())}")
        settings = self._settings[provider]
        if provider == "ollama":
            return OllamaProvider(model, settings)
        if provider in OPENAI_COMPATIBLE:
            return OpenAICompatProvider(provider, model, settings)
        raise ValueError(f"Provider '{provider}' has no adapter implementation")

    def describe(self) -> str:
        lines = []
        for name in self.names():
            models = self.models_for(name)
            marker = "*" if self._active_model.get(name) else " "
            current = f" -> {self._active_model[name]}" if marker == "*" else ""
            shown = ", ".join(models[:6]) + (f" (+{len(models) - 6} more)" if len(models) > 6 else "")
            lines.append(f"{marker} {name}{current}: {shown or '(no models configured)'}")
        return "\n".join(lines)
