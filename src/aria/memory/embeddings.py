"""Embedding provider abstraction (spec §17).

``EmbeddingProvider`` is the only embedding interface the memory subsystem
sees; concrete backends (OpenAI-compatible endpoint, Ollama models) plug in
via configuration and are chosen with fallback. Nothing else in the package
hard-codes a provider or model.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from typing import Protocol

from .config import MemorySettings
from .exceptions import MemoryError
from ..logging_setup import log_debug, log_error, log_info


class EmbeddingProvider(Protocol):
    """The embedding interface used by the semantic store and retriever."""

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    """OpenAI-compatible embeddings endpoint (configurable base URL + model)."""

    name = "openai-compatible"

    def __init__(self, base_url: str, model: str, api_key_env: str) -> None:
        from openai import OpenAI

        api_key = os.getenv(api_key_env) if api_key_env else None
        if not base_url or not model or not api_key:
            raise ValueError("OpenAI-compatible embedding settings are incomplete")
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.model, input=texts)
        return [list(map(float, item.embedding)) for item in response.data]


class OllamaEmbeddingProvider:
    """Local Ollama embedding model."""

    name = "ollama"

    def __init__(self, host: str, model: str) -> None:
        import ollama

        self.model = model
        self.client = ollama.Client(host=host) if host else ollama.Client()

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embed(model=self.model, input=texts)
        embeddings = getattr(response, "embeddings", None)
        if embeddings is None and isinstance(response, dict):
            embeddings = response.get("embeddings")
        if not embeddings:
            raise MemoryError(f"Ollama returned no embeddings for {self.model}")
        return [list(map(float, vector)) for vector in embeddings]


class FallbackEmbeddingProvider:
    """Try the configured remote backend first, then Ollama fallback models.

    Keeps whichever backend last succeeded and records permanently failed
    candidates so a dead endpoint is not retried on every call (spec §72:
    embedding failures must not stall the assistant). When every candidate
    fails, the provider enters a degraded state with a retry backoff so a
    down Ollama produces one error per interval instead of one per call
    (bugReport BR-4).
    """

    name = "fallback"

    # Seconds before retrying candidates after a full sweep failed.
    RETRY_INTERVAL_SECONDS = 300.0

    def __init__(self, settings: MemorySettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._active: EmbeddingProvider | None = None
        self._failed: set[str] = set()
        self._degraded_until: float = 0.0
        models = list(settings.embedding_fallback_models)
        if settings.embedding_model and settings.embedding_model not in models:
            models.insert(0, settings.embedding_model)
        self._models = models

    def _candidates(self) -> Iterator[EmbeddingProvider]:
        settings = self._settings
        if settings.embedding_base_url and settings.embedding_model:
            try:
                yield OpenAIEmbeddingProvider(
                    settings.embedding_base_url,
                    settings.embedding_model,
                    settings.embedding_api_key_env,
                )
            except Exception as exc:  # noqa: BLE001 - backend probes
                log_debug(f"Memory embeddings: remote backend unavailable: {exc}")
        for model in self._models:
            if model in self._failed:
                continue
            try:
                yield OllamaEmbeddingProvider(settings.embedding_ollama_host, model)
            except Exception as exc:  # noqa: BLE001 - ollama import/connection errors
                self._failed.add(model)
                log_debug(f"Memory embeddings: Ollama backend {model} unavailable: {exc}")

    @property
    def backend_name(self) -> str:
        """Name of the backend that produced the last successful embedding."""
        with self._lock:
            return self._active.name if self._active else "none"

    @property
    def degraded(self) -> bool:
        """True when no backend has succeeded and retries are paused.

        A fresh provider (no attempt yet) is not degraded — callers can use
        this to report memory health at startup without a live probe.
        """
        with self._lock:
            return self._active is None and time.monotonic() < self._degraded_until

    def probe(self) -> bool:
        """Attempt one real embedding; True when a backend works (BR-4).

        Used at startup so memory health is reported once instead of being
        discovered through repeated failures during turns. An explicit probe
        overrides the retry backoff (it is a deliberate health check), and a
        failure feeds the same degraded/backoff state as normal calls.
        """
        with self._lock:
            self._degraded_until = 0.0  # a probe always retries immediately
        try:
            self.embed(["probe"])
            return True
        except MemoryError:
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts, falling back through candidates on failure."""
        if not texts:
            return []
        with self._lock:
            active = self._active
            if active is not None:
                try:
                    return active.embed(texts)
                except Exception as exc:  # noqa: BLE001 - fall through to candidates
                    log_error(f"Memory embeddings: backend {active.name} failed: {exc}")
                    self._failed.add(getattr(active, "model", active.name))
                    self._active = None
            elif time.monotonic() < self._degraded_until:
                raise MemoryError(
                    "No embedding backend is available (retrying in "
                    f"{int(self._degraded_until - time.monotonic())}s)"
                )
            last_error: Exception | None = None
            for provider in self._candidates():
                try:
                    result = provider.embed(texts)
                    self._active = provider
                    self._degraded_until = 0.0
                    log_info(f"Memory embeddings: using {provider.name} backend")
                    return result
                except Exception as exc:  # noqa: BLE001 - try the next candidate
                    last_error = exc
                    self._failed.add(str(getattr(provider, "model", provider.name)))
                    log_error(f"Memory embeddings: {getattr(provider, 'model', provider.name)} failed: {exc}")
            self._degraded_until = time.monotonic() + self.RETRY_INTERVAL_SECONDS
            raise MemoryError(f"No embedding backend is available: {last_error}")
