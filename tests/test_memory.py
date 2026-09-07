import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import aria.persistent_memory as persistent_memory
from aria.config import MemoryConfig, load_config
from aria.persistent_memory import EmbeddingService, PersistentMemory


class FakeCollection:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def add(self, *, ids, documents, embeddings, metadatas) -> None:
        for index, item_id in enumerate(ids):
            if item_id in self.records:
                raise ValueError("duplicate id")
            self.records[item_id] = {
                "document": documents[index],
                "embedding": embeddings[index],
                "metadata": dict(metadatas[index]),
            }

    def upsert(self, *, ids, documents, embeddings, metadatas) -> None:
        for index, item_id in enumerate(ids):
            self.records[item_id] = {
                "document": documents[index],
                "embedding": embeddings[index],
                "metadata": dict(metadatas[index]),
            }

    def get(self, *, ids=None, where=None, limit=None, include=None):
        records = list(self.records.items())
        if ids is not None:
            wanted = set(ids if isinstance(ids, list) else [ids])
            records = [(item_id, value) for item_id, value in records if item_id in wanted]
        if where:
            records = [
                (item_id, value)
                for item_id, value in records
                if all(value["metadata"].get(key) == expected for key, expected in where.items())
            ]
        if limit is not None:
            records = records[:limit]
        return {
            "ids": [item_id for item_id, _ in records],
            "documents": [value["document"] for _, value in records],
            "metadatas": [value["metadata"] for _, value in records],
        }

    def query(self, *, query_embeddings, n_results):
        records = list(self.records.items())[:n_results]
        return {
            "ids": [[item_id for item_id, _ in records]],
            "documents": [[value["document"] for _, value in records]],
            "metadatas": [[value["metadata"] for _, value in records]],
            "distances": [[0.1 for _ in records]],
        }

    def update(self, *, ids, documents=None, metadatas=None) -> None:
        for index, item_id in enumerate(ids):
            if documents is not None:
                self.records[item_id]["document"] = documents[index]
            if metadatas is not None:
                self.records[item_id]["metadata"] = dict(metadatas[index])

    def delete(self, *, ids=None, where=None) -> None:
        if ids is not None:
            for item_id in ids:
                self.records.pop(item_id, None)
        elif where:
            for item_id, value in list(self.records.items()):
                if all(value["metadata"].get(key) == expected for key, expected in where.items()):
                    del self.records[item_id]


class FakeChromaClient:
    def __init__(self, path: str) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def get_or_create_collection(self, *, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())


class FakeEmbeddings:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.inputs.extend(texts)
        return [[0.1, 0.2] for _ in texts]


class SummaryProvider:
    def complete(self, messages, tools, on_text=None):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "summary": "The user is building a local memory system.",
                    "facts": [
                        {
                            "namespace": "goals",
                            "key": "project",
                            "value": "memory system",
                            "confidence": 0.95,
                            "importance": 0.9,
                        }
                    ],
                }
            )
        )


def test_memory_redacts_secrets_and_paths() -> None:
    redacted = PersistentMemory.redact(
        "api_key=secret123 token:abc password = xyz Bearer abc123 "
        "/home/alice/private/project"
    )

    assert "secret123" not in redacted
    assert "abc123" not in redacted
    assert "xyz" not in redacted
    assert "/home/alice" not in redacted
    assert "[REDACTED]" in redacted
    assert "[PATH]" in redacted


def test_memory_config_defaults_and_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "provider: ollama\n"
        "model: gemma2:9b\n"
        "workspace: .\n"
        "providers:\n"
        "  ollama:\n"
        "    host: http://127.0.0.1:11434\n"
        "memory:\n"
        "  context_token_budget: 1234\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARIA_MEMORY_TIER2_LIMIT", "3")

    config = load_config(config_path, tmp_path)

    assert config.memory.embedding_fallback_models[0] == "qwen3-embedding:0.6b"
    assert config.memory.context_token_budget == 1234
    assert config.memory.tier2_limit == 3


def test_embedding_service_tries_ollama_models_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    class Backend:
        name = "ollama"

        def __init__(self, host: str, model: str) -> None:
            self.model = model

        def embed(self, texts: list[str]) -> list[list[float]]:
            attempts.append(self.model)
            if self.model == "first":
                raise RuntimeError("unavailable")
            return [[1.0, 2.0] for _ in texts]

    monkeypatch.setattr(persistent_memory, "OllamaEmbeddingBackend", Backend)
    service = EmbeddingService(
        MemoryConfig(
            embedding_fallback_models=("first", "second"),
        )
    )

    assert service.embed(["hello"]) == [[1.0, 2.0]]
    assert attempts == ["first", "second"]


def test_persistent_memory_indexes_summarizes_promotes_and_wipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeChromaClient(str(tmp_path / "chroma"))
    monkeypatch.setattr(
        persistent_memory,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda path: client),
    )
    config = MemoryConfig(
        directory=Path("memory"),
        chroma_directory=Path("chroma"),
        promotion_repetitions=1,
    )
    memory = PersistentMemory(tmp_path, config, SummaryProvider())
    memory._stop_event.set()
    memory._queue_event.set()
    memory._worker.join(timeout=2)
    embeddings = FakeEmbeddings()
    memory._embeddings = cast(Any, embeddings)

    memory.add({"role": "system", "content": "system"})
    memory.add({"role": "user", "content": "My API key=do-not-store and goal is memory."})
    memory.add({"role": "assistant", "content": "I will remember the goal."})
    memory.turn_completed("My goal is memory.", "I will remember the goal.")

    while True:
        row = memory._next_job()
        if row is None:
            break
        memory._process_job(row)
        with memory._db_lock:
            memory._db.execute("UPDATE jobs SET status='done' WHERE id=?", (row["id"],))
            memory._db.commit()

    assert client.collections["chat_history"].records
    assert client.collections["semantic_memories"].records
    assert "[REDACTED]" in PersistentMemory.redact("API key=do-not-store")
    assert memory.list_facts()[0]["value"] == "memory system"
    assert "RELEVANT MEMORIES" in memory.build_context("memory")

    memory.wipe("all")
    assert not client.collections["chat_history"].records
    assert not client.collections["semantic_memories"].records
    assert memory.list_facts() == []
    memory.cleanup()
