"""SQLite structured store tests (spec §58: create/retrieve/update/delete/
duplicate prevention/transactions/conflict resolution)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aria.memory.exceptions import ValidationError
from aria.memory.sqlite_store import SQLiteStore


@pytest.fixture()
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "assistant.db")


def test_migration_creates_schema(store: SQLiteStore) -> None:
    stats = store.stats()
    assert stats["schema_version"] >= 1
    assert store.path.exists()


def test_ensure_user_idempotent(store: SQLiteStore) -> None:
    store.ensure_user("default")
    store.ensure_user("default")
    assert store.stats()["users"] == 1


def test_fact_create_retrieve_update_delete(store: SQLiteStore) -> None:
    memory_id, created = store.upsert_fact("default", "profile", "editor", "Neovim")
    assert created is False
    rows = store.get_active_value("default", "facts")
    assert rows[0]["value"] == "Neovim"

    # Update with a new value: same key, new row, old soft-deleted (§28).
    updated_id, created = store.upsert_fact("default", "profile", "editor", "VS Code")
    assert created is True
    assert updated_id != memory_id
    rows = store.get_active_value("default", "facts")
    assert len(rows) == 1 and rows[0]["value"] == "VS Code"

    # Soft delete (§40).
    assert store.set_deleted(updated_id, reason="test") is True
    assert store.get_active_value("default", "facts") == []


def test_duplicate_fact_prevented(store: SQLiteStore) -> None:
    store.upsert_fact("default", "profile", "editor", "Neovim")
    store.upsert_fact("default", "profile", "editor", "Neovim")  # same value: refresh
    rows = store.get_active_value("default", "facts")
    assert len(rows) == 1


def test_fact_uniqueness_enforced_in_db(store: SQLiteStore) -> None:
    """The partial unique index backs the application-level rule."""
    memory_id, _ = store.upsert_fact("default", "profile", "editor", "Neovim")
    store.set_deleted(memory_id)
    store.upsert_fact("default", "profile", "editor", "VS Code")
    store.upsert_fact("default", "profile", "editor", "Neovim")  # re-create after delete
    rows = store.get_active_value("default", "facts")
    assert len(rows) == 1 and rows[0]["value"] == "Neovim"


def test_conflict_resolution_records_events(store: SQLiteStore) -> None:
    first_id, _ = store.upsert_fact("default", "profile", "editor", "VS Code")
    second_id, _ = store.upsert_fact("default", "profile", "editor", "Neovim", confirmed=True)
    events = store.events(second_id)
    kinds = [event["event_type"] for event in events]
    assert "updated" in kinds
    update_event = next(event for event in events if event["event_type"] == "updated")
    assert update_event["old_value"] == "VS Code"
    assert update_event["new_value"] == "Neovim"
    # Old memory carries a superseded/deleted event trail.
    old_events = store.events(first_id)
    assert any(event["event_type"] == "deleted" for event in old_events)


def test_preference_conflict_same_semantics(store: SQLiteStore) -> None:
    store.upsert_preference("default", "style", "answers", "concise")
    store.upsert_preference("default", "style", "answers", "verbose")
    rows = store.get_active_value("default", "preferences")
    assert len(rows) == 1 and rows[0]["value"] == "verbose"


def test_project_goal_task_upserts(store: SQLiteStore) -> None:
    project_id, created = store.upsert_project("default", "LifeOS", status="active")
    assert created is False
    store.upsert_project("default", "LifeOS", status="paused")
    projects = store.get_projects("default")
    assert len(projects) == 1 and projects[0]["status"] == "paused"

    goal_id, _ = store.upsert_goal("default", "Finish MVP", project_id=project_id)
    task_id, _ = store.upsert_task("default", "Write spec", project_id=project_id, goal_id=goal_id)
    assert len(store.get_goals("default", status="active")) == 1
    assert len(store.get_tasks("default", status="pending")) == 1
    store.upsert_task("default", "Write spec", status="completed")
    assert store.get_tasks("default", status="pending") == []
    assert len(store.get_tasks("default")) == 1
    assert task_id


def test_registry_and_access_tracking(store: SQLiteStore) -> None:
    memory_id, _ = store.upsert_fact("default", "profile", "editor", "Neovim")
    entry = store.registry_entry(memory_id)
    assert entry is not None and entry["storage_type"] == "sqlite"
    store.record_access([memory_id, memory_id])
    entry = store.registry_entry(memory_id)
    assert int(entry["access_count"]) == 2  # type: ignore[index]
    assert entry["last_accessed_at"] is not None  # type: ignore[index]


def test_confirm_raises_confidence(store: SQLiteStore) -> None:
    memory_id, _ = store.upsert_fact("default", "profile", "editor", "Neovim", confidence=0.6)
    assert store.confirm(memory_id) is True
    row = store.get_active_value("default", "facts")[0]
    assert float(row["confidence"]) == 1.0 and int(row["confirmed"]) == 1


def test_invalid_scores_rejected(store: SQLiteStore) -> None:
    with pytest.raises(ValidationError):
        store.upsert_fact("default", "profile", "editor", "Neovim", importance=1.5)
    with pytest.raises(ValidationError):
        store.upsert_preference("default", "style", "answers", "concise", confidence=-0.1)
    with pytest.raises(ValidationError):
        store.upsert_fact("default", "profile", "", "value")  # empty key


def test_orphan_detection_and_repair(store: SQLiteStore) -> None:
    memory_id, _ = store.upsert_fact("default", "profile", "editor", "Neovim")
    # Simulate a registry outage by deleting the registry row directly.
    with store._lock:
        store._db.execute("DELETE FROM memory_registry WHERE id=?", (memory_id,))
        store._db.commit()
    orphans = store.orphan_scan()
    assert memory_id in orphans["storage_without_registry"]
    assert store.repair_orphans() == 1
    assert store.registry_entry(memory_id) is not None


def test_expired_memory_detection(store: SQLiteStore) -> None:
    from datetime import timedelta

    from aria.memory.models import utc_now

    memory_id, _ = store.upsert_fact("default", "profile", "temp", "value")
    past = (utc_now() - timedelta(days=1)).isoformat()
    with store._lock:
        store._db.execute("UPDATE memory_registry SET expires_at=? WHERE id=?", (past, memory_id))
        store._db.commit()
    assert store.expired_memory_ids(utc_now()) == [memory_id]
