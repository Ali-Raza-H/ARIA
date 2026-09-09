"""Shared fakes for memory subsystem tests."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from aria.memory.config import MemorySettings


class FakeEmbeddings:
    """Deterministic bag-of-words embeddings: identical text → identical vector.

    Words are hashed into a fixed-size vector, giving real cosine similarity
    behavior (identical/near-identical strings land near 1.0, unrelated near
    0) without any model download. Vector norm is unit-normalized so Chroma
    distances behave like the real backend.
    """

    def __init__(self, dims: int = 256) -> None:
        self.dims = dims
        self.embed_calls = 0

    name = "fake"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in text.lower().split():
            digest = hashlib.md5(word.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dims
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def make_settings(tmp_path: Path, **overrides: Any) -> MemorySettings:
    """Memory settings pointed at a temp directory with no network backends."""
    defaults: dict[str, Any] = {
        "root": tmp_path,
        "sqlite_path": tmp_path / "assistant.db",
        "chroma_path": tmp_path / "chroma",
        "embedding_fallback_models": (),  # no network in tests
    }
    defaults.update(overrides)
    return MemorySettings(**defaults)


class FakeProvider:
    """Minimal LLM provider double for extraction tests (spec §58)."""

    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[list[dict[str, Any]]] = []
        self.fail = False

    def complete(self, messages: list[dict[str, Any]], tools: list[Any], on_text: Any = None) -> Any:
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("provider unavailable")

        class _Response:
            content = self.response

        return _Response()
