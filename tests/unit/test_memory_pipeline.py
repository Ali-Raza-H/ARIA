"""Memory pipeline tests (spec §58): classification, ranking, retrieval routing,
lifecycle expiry/maintenance, and health checks."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from memory_fakes import FakeEmbeddings, make_settings  # noqa: E402

from aria.memory.classifier import MemoryClassifier, MemoryDecisionType  # noqa: E402
from aria.memory.config import MemorySettings  # noqa: E402
from aria.memory.maintenance.lifecycle import LifecycleManager  # noqa: E402
from aria.memory.manager import MemoryManager  # noqa: E402
from aria.memory.models import MemoryResult, MemoryType, StorageType  # noqa: E402
from aria.memory.semantic.retriever import QueryKind, classify_query  # noqa: E402
from aria.memory.models import utc_now  # noqa: E402
from aria.memory.semantic.reranker import Reranker  # noqa: E402


# ---------------------------------------------------------------- classifier
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Remember that I use Neovim", MemoryDecisionType.PREFERENCE),
        ("I've switched from VS Code to Neovim", MemoryDecisionType.PREFERENCE),
        ("I prefer concise answers", MemoryDecisionType.PREFERENCE),
        ("I'm currently working on LifeOS", MemoryDecisionType.STATE),
        ("I want to finish the MVP", MemoryDecisionType.GOAL),
        ("I need to fix the login bug", MemoryDecisionType.TASK),
        ("I tried implementing the dashboard and it failed", MemoryDecisionType.EPISODIC),
        ("Hello", MemoryDecisionType.IGNORE),
        ("Thanks", MemoryDecisionType.IGNORE),
        ("Okay", MemoryDecisionType.IGNORE),
    ],
)
def test_classifier_rules(text: str, expected: MemoryDecisionType) -> None:
    assert MemoryClassifier().classify(text).decision is expected


def test_explicit_statement_is_confirmed() -> None:
    decision = MemoryClassifier().classify("Remember that I use Neovim")
    assert decision.explicitly_confirmed is True
    assert decision.confidence == 1.0


# ---------------------------------------------------------------- reranker
def _result(**overrides: object) -> MemoryResult:
    base: dict[str, object] = {
        "memory_id": "mem_x",
        "content": "content",
        "memory_type": MemoryType.EPISODIC,
        "storage_type": StorageType.CHROMA,
        "similarity": 0.9,
        "importance": 0.8,
        "confidence": 0.9,
    }
    base.update(overrides)
    return MemoryResult(**base)  # type: ignore[arg-type]


def test_rank_orders_by_weighted_score() -> None:
    settings = MemorySettings()
    reranker = Reranker(settings)
    # High similarity, low everything else vs. moderate similarity, high value.
    weak = _result(memory_id="weak", similarity=0.99, importance=0.1, confidence=0.1)
    strong = _result(memory_id="strong", similarity=0.7, importance=0.9, confidence=0.9)
    ranked = reranker.rank([weak, strong])
    assert ranked[0].memory_id == "strong"


def test_frequently_accessed_low_quality_does_not_dominate() -> None:
    """Spec §34: access frequency must not let bad memories rule forever."""
    settings = MemorySettings()
    reranker = Reranker(settings)
    noisy = _result(memory_id="noisy", similarity=0.5, importance=0.1, confidence=0.1, metadata={"access_count": 100})
    good = _result(memory_id="good", similarity=0.8, importance=0.8, confidence=0.8, metadata={"access_count": 0})
    ranked = reranker.rank([noisy, good])
    assert ranked[0].memory_id == "good"


# ---------------------------------------------------------------- retrieval routing
def test_structured_queries_skip_embeddings() -> None:
    settings = make_settings(Path("/tmp"), embedding_fallback_models=())
    assert classify_query("What is my current project?", settings) is QueryKind.STRUCTURED
    assert classify_query("List my tasks", settings) is QueryKind.STRUCTURED


def test_semantic_queries_use_chroma() -> None:
    settings = make_settings(Path("/tmp"), embedding_fallback_models=())
    assert classify_query("Why did I reject the previous architecture?", settings) is QueryKind.SEMANTIC
    assert classify_query("What did I originally want from LifeOS?", settings) is QueryKind.SEMANTIC


# ---------------------------------------------------------------- lifecycle
def _backdate_registry(settings: MemorySettings, memory_id: str, days: int) -> None:
    """Backdate a registry row so retention/decay windows have passed."""
    import sqlite3

    db = sqlite3.connect(settings.sqlite_path)
    with db:
        db.execute(
            "UPDATE memory_registry SET created_at=? WHERE id=?",
            ((utc_now() - timedelta(days=days)).isoformat(), memory_id),
        )
    db.close()


def _backdate_vector(
    manager: MemoryManager,
    memory_id: str,
    memory_type: MemoryType,
    days: int,
) -> None:
    """Backdate a Chroma vector's metadata (decay reads it from there)."""
    old = (utc_now() - timedelta(days=days)).isoformat()
    assert manager.chroma.update_metadata(
        memory_id=memory_id, memory_type=memory_type, created_at=old
    )


def test_retention_decay_soft_deletes_stale_memory(tmp_path: Path) -> None:
    """Spec §39: stale low-value unconfirmed memories decay and are removed."""
    settings = make_settings(
        tmp_path,
        embedding_fallback_models=(),
        conversation_retention_days=1,
    )
    manager = MemoryManager(tmp_path, settings, provider=None, embeddings=FakeEmbeddings())
    try:
        # Low importance (0.3) so one decay step reaches the §39 floor.
        memory_id = manager.remember(
            "A temporary note about yesterdays experiment", "conversation",
            importance=0.3, confirmed=False,
        )
        assert memory_id is not None
        assert manager.chroma.collection_counts()["conversation_memory"] == 1
        _backdate_vector(manager, memory_id, MemoryType.CONVERSATION, days=5)

        decayed = manager.lifecycle.apply_decay()
        assert decayed == 1
        assert manager.chroma.collection_counts()["conversation_memory"] == 0
        entry = manager.sqlite.registry_entry(memory_id)
        assert entry is not None and bool(entry["deleted_at"])
    finally:
        manager.cleanup()


def test_decay_spares_confirmed_and_important(tmp_path: Path) -> None:
    """Spec §39: confirmed/high-value memories are never decayed."""
    settings = make_settings(
        tmp_path,
        embedding_fallback_models=(),
        conversation_retention_days=1,
    )
    manager = MemoryManager(tmp_path, settings, provider=None, embeddings=FakeEmbeddings())
    try:
        memory_id = manager.remember(
            "User keeps their notes in Obsidian", "conversation", confirmed=True
        )
        assert memory_id is not None
        _backdate_vector(manager, memory_id, MemoryType.CONVERSATION, days=30)
        assert manager.lifecycle.apply_decay() == 0
        assert manager.chroma.collection_counts()["conversation_memory"] == 1
    finally:
        manager.cleanup()


def test_health_check_reports_issues(tmp_path: Path) -> None:
    """Spec §55: diagnostics detect a chroma vector without a registry entry."""
    settings = make_settings(tmp_path, embedding_fallback_models=())
    manager = MemoryManager(tmp_path, settings, provider=None, embeddings=FakeEmbeddings())
    try:
        memory_id = manager.remember("User owns a purple calculator", "episodic")
        assert memory_id is not None
        healthy = manager.lifecycle.health_check()
        assert healthy["healthy"] is True and healthy["issues"] == []
        # Delete the vector behind the registry's back -> inconsistency (§55).
        manager.chroma.delete(memory_id, MemoryType.EPISODIC)
        broken = manager.lifecycle.health_check()
        assert broken["healthy"] is False and broken["issues"]
    finally:
        manager.cleanup()


def test_backup_zip_roundtrip(tmp_path: Path) -> None:
    """Spec §57: CLI backup zips SQLite + Chroma and stays readable."""
    settings = make_settings(tmp_path, embedding_fallback_models=())
    manager = MemoryManager(tmp_path, settings, provider=None, embeddings=FakeEmbeddings())
    try:
        assert manager.remember("User prefers dark themes", "preference", key="theme", value="dark")
        from aria.memory import __main__ as memory_cli

        target = tmp_path / "backup.zip"
        backup_stores = memory_cli.backup_stores
        result = backup_stores(manager, target)
        assert result == target and target.exists() and target.stat().st_size > 0
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as unpack:
            import zipfile

            with zipfile.ZipFile(target) as archive:
                archive.extractall(unpack)
            db = sqlite3.connect(Path(unpack) / "sqlite" / settings.sqlite_path.name)
            count = db.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]
            db.close()
        assert count >= 1
    finally:
        manager.cleanup()
