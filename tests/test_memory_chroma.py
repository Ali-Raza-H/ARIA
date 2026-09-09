"""Chroma semantic store tests (spec §58: insert/search/filter/delete)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aria.memory.chroma_store import ChromaStore, collection_name_for
from aria.memory.models import MemoryType, SemanticMemory
from memory_fakes import FakeEmbeddings


@pytest.fixture()
def store(tmp_path: Path) -> ChromaStore:
    return ChromaStore(tmp_path / "chroma", FakeEmbeddings())


def _memory(content: str, memory_type: MemoryType = MemoryType.EPISODIC, **kwargs: object) -> SemanticMemory:
    defaults: dict[str, object] = {
        "id": f"mem_test_{abs(hash(content)) % 10**10}",
        "user_id": "default",
        "type": memory_type,
        "content": content,
        "importance": 0.7,
        "confidence": 0.8,
        "source": "conversation",
        "topic": "test",
    }
    defaults.update(kwargs)
    return SemanticMemory(**defaults)  # type: ignore[arg-type]


def test_collection_mapping() -> None:
    assert collection_name_for(MemoryType.CONVERSATION) == "conversation_memory"
    assert collection_name_for(MemoryType.EPISODIC) == "episodic_memory"
    assert collection_name_for(MemoryType.KNOWLEDGE) == "knowledge"
    assert collection_name_for(MemoryType.PROJECT_CONTEXT) == "project_context"


def test_insert_and_semantic_search(store: ChromaStore) -> None:
    store.add(_memory("User rejected the complex dashboard because it felt cluttered"))
    store.add(_memory("User prefers dark interfaces for late-night coding"))
    hits = store.search("dashboard cluttered", user_id="default", limit=2)
    assert hits
    assert "dashboard" in hits[0]["document"]
    # Exact match should outrank the unrelated memory.
    assert hits[0]["similarity"] > hits[-1]["similarity"]


def test_metadata_filtering_by_user(store: ChromaStore) -> None:
    store.add(_memory("LifeOS started as an execution-first productivity app", user_id="default"))
    hits = store.search("LifeOS", user_id="someone-else", limit=5)
    assert hits == []


def test_delete_vector(store: ChromaStore) -> None:
    memory = _memory("A memory destined for deletion")
    store.add(memory)
    assert store.exists(memory.id, MemoryType.EPISODIC) is True
    assert store.delete(memory.id, MemoryType.EPISODIC) is True
    assert store.exists(memory.id, MemoryType.EPISODIC) is False
    assert store.delete(memory.id, MemoryType.EPISODIC) is False


def test_update_metadata_without_reembedding(store: ChromaStore) -> None:
    memory = _memory("User chose SQLite over PostgreSQL for the assistant")
    store.add(memory)
    assert store.update_metadata(memory.id, MemoryType.EPISODIC, importance=0.95) is True
    item = store.get(memory.id, MemoryType.EPISODIC)
    assert item is not None
    assert float(item["metadata"]["importance"]) == 0.95


def test_delete_all_for_type(store: ChromaStore) -> None:
    store.add(_memory("Episodic one", memory_type=MemoryType.EPISODIC))
    store.add(_memory("Episodic two", memory_type=MemoryType.EPISODIC))
    store.add(_memory("Knowledge doc chunk", memory_type=MemoryType.KNOWLEDGE))
    assert store.delete_all(MemoryType.EPISODIC) == 2
    assert store.collection_counts()[collection_name_for(MemoryType.EPISODIC)] == 0
    assert store.collection_counts()[collection_name_for(MemoryType.KNOWLEDGE)] == 1
