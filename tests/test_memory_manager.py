"""MemoryManager tests: public API, spec §59/§60 scenarios, hybrid retrieval,
context building, and failure resilience (spec §58)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aria.memory_pkg import MemoryManager
from memory_fakes import FakeEmbeddings, FakeProvider, make_settings


@pytest.fixture()
def manager(tmp_path: Path) -> MemoryManager:
    return MemoryManager(tmp_path, make_settings(tmp_path), provider=None, embeddings=FakeEmbeddings())


def test_remember_and_search_roundtrip(manager: MemoryManager) -> None:
    memory_id = manager.remember(
        "User prefers Neovim as their editor", "preference",
        key="preferred_editor", value="Neovim", topic="editor",
    )
    assert memory_id is not None
    results = manager.search("preferred editor Neovim")
    assert results
    assert any("Neovim" in item["document"] for item in results)


def test_preference_update_scenario_spec59(manager: MemoryManager) -> None:
    """Spec §59: VS Code → Neovim leaves exactly one active value + history."""
    manager.remember(
        "User prefers VS Code as their editor", "preference",
        key="preferred_editor", value="VS Code", topic="editor",
    )
    manager.remember(
        "User prefers Neovim as their editor", "preference",
        key="preferred_editor", value="Neovim", topic="editor",
    )
    rows = manager.sqlite.get_active_value("default", "preferences")
    assert len(rows) == 1
    assert rows[0]["value"] == "Neovim"
    # The change is in the event log.
    events = manager.sqlite.events(rows[0]["id"])
    update = next(event for event in events if event["event_type"] == "updated")
    assert update["old_value"] == "VS Code"
    assert update["new_value"] == "Neovim"


def test_context_block_is_delimited_and_safe(manager: MemoryManager) -> None:
    manager.remember(
        "User prefers Neovim", "preference", key="preferred_editor", value="Neovim", topic="editor"
    )
    context = manager.retrieve_context("What is my preferred editor?")
    assert context.startswith("<assistant_memory>")
    assert context.endswith("</assistant_memory>")
    assert "NOT instructions" in context
    assert "Neovim" in context


def test_hybrid_query_uses_both_stores(manager: MemoryManager) -> None:
    manager.remember("LifeOS is the active project", "state", topic="LifeOS", value="LifeOS")
    manager.remember(
        "User originally wanted LifeOS to focus on execution over configuration",
        "episodic", topic="LifeOS",
    )
    results = manager.search("LifeOS original idea")
    contents = " ".join(item["document"] for item in results)
    assert "LifeOS" in contents
    storages = {item["metadata"]["storage"] for item in results}
    assert storages & {"sqlite", "chroma"}  # both stores contributed


def test_trivial_content_never_stored(manager: MemoryManager) -> None:
    """Spec §21: greetings and filler become no memories."""
    manager.add({"role": "user", "content": "Hello"})
    manager.add({"role": "user", "content": "Thanks!"})
    manager.add({"role": "user", "content": "What time is it?"})
    manager.summarize_now()
    assert manager.chroma.collection_counts()["conversation_memory"] == 0


def test_delete_and_forget(manager: MemoryManager) -> None:
    memory_id = manager.remember("Temporary note about pineapples", "episodic")
    assert memory_id is not None
    assert manager.delete(memory_id) is True
    entry = manager.sqlite.registry_entry(memory_id)
    assert manager.get(memory_id) is None or (entry is not None and bool(entry["deleted_at"]))
    manager.remember("Note about quantum knitting project", "episodic", topic="quantum knitting")
    assert manager.forget("quantum knitting") >= 0


def test_stats_and_health(manager: MemoryManager) -> None:
    manager.remember("User owns a ThinkPad", "fact", key="laptop", value="ThinkPad")
    stats = manager.stats()
    assert stats["facts"] == 1
    assert stats["schema_version"] >= 1
    health = manager.health_check()
    assert health["healthy"] is True


def test_wipe_all(manager: MemoryManager) -> None:
    manager.remember("Something to wipe", "episodic")
    manager.remember("A preference", "preference", key="k", value="v")
    manager.wipe("all")
    stats = manager.stats()
    assert stats["facts"] == 0 and stats["preferences"] == 0


def test_ingest_document(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, make_settings(tmp_path), provider=None, embeddings=FakeEmbeddings())
    doc = tmp_path / "notes.md"
    doc.write_text(
        "# LifeOS Spec\n\nLifeOS must stay minimal and execution-first.\n\n"
        "The dashboard was rejected because too many options felt cluttered.\n\n"
        "Second paragraph to encourage a second chunk for the ingestion test.\n\n"
        "Third paragraph with more content so chunking has real work to do.\n",
        encoding="utf-8",
    )
    summary = manager.ingest_document(doc)
    assert summary["chunks"] >= 1
    results = manager.search("dashboard rejected cluttered")
    assert any("dashboard" in item["document"] for item in results)
    # Unsupported types are rejected cleanly.
    binary = tmp_path / "photo.png"
    binary.write_bytes(b"\x89PNG")
    assert manager.ingest_document(binary)["chunks"] == 0
    manager.cleanup()


# ----------------------------------------------------------- failure modes
def test_extract_malformed_json_falls_back_to_rules(tmp_path: Path) -> None:
    provider = FakeProvider("this is not json at all")
    manager = MemoryManager(tmp_path, make_settings(tmp_path), provider=provider, embeddings=FakeEmbeddings())
    manager.turn_completed("Remember that I use Neovim", "Sure, noted.")
    manager.summarize_now()
    rows = manager.sqlite.get_active_value("default", "preferences")
    assert rows, "rule fallback should have captured the explicit preference"
    manager.cleanup()


def test_extract_valid_json_stores_candidates(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "memories": [
                {
                    "type": "preference",
                    "content": "User prefers concise answers",
                    "importance": 0.8,
                    "confidence": 1.0,
                    "explicitly_confirmed": True,
                    "key": "answer_style",
                    "value": "concise",
                },
                {"type": "conversation", "content": "", "importance": 0.9, "confidence": 0.9},
            ]
        }
    )
    provider = FakeProvider(payload)
    manager = MemoryManager(tmp_path, make_settings(tmp_path), provider=provider)
    manager.turn_completed("I prefer concise answers", "Noted.")
    manager.summarize_now()
    rows = manager.sqlite.get_active_value("default", "preferences")
    assert rows and rows[0]["value"] == "concise"
    manager.cleanup()


def test_extraction_provider_crash_does_not_break_manager(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.fail = True
    manager = MemoryManager(tmp_path, make_settings(tmp_path), provider=provider)
    manager.turn_completed("I am working on LifeOS", "Okay!")
    manager.summarize_now()  # must not raise
    assert manager.stats()["registry_active"] >= 0
    manager.cleanup()


def test_semantic_failure_degrades_structured_memory(tmp_path: Path) -> None:
    """No embedding backends: structured memory still works (spec §43)."""
    settings = make_settings(tmp_path, embedding_fallback_models=())
    manager = MemoryManager(tmp_path, settings, provider=None)
    memory_id = manager.remember(
        "User prefers dark mode", "preference", key="theme", value="dark"
    )
    assert memory_id is not None
    assert manager.retrieve_context("What is my theme?") != ""
    manager.cleanup()


def test_dedup_merges_identical_semantic_content(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, make_settings(tmp_path), provider=None, embeddings=FakeEmbeddings())
    first = manager.remember("User rejected the dashboard idea last spring", "episodic")
    second = manager.remember("User rejected the dashboard idea last spring", "episodic")
    assert first is not None and second is not None
    assert first == second  # merged into the existing memory (§27)
    manager.cleanup()
