"""SQLite structured memory store (spec §6-14, §31, §45-47).

The authoritative source for facts, preferences, projects, goals, tasks, the
cross-store memory registry, and the event log. Implements soft-delete (spec
§40), transactional writes (§46), atomic update+event+registry sequences (§47),
and deterministic retrieval (§31). The assistant core never touches this
module directly — only the MemoryManager does (§76).
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .exceptions import ValidationError
from .migrations import apply_migrations, connect
from .models import MemoryType, utc_now
from ..logging.setup import log_error

_ID_PREFIX = {
    MemoryType.FACT: "fact_",
    MemoryType.PREFERENCE: "pref_",
    MemoryType.GOAL: "goal_",
    MemoryType.TASK: "task_",
    MemoryType.EPISODIC: "mem_",
    MemoryType.CONVERSATION: "mem_",
    MemoryType.KNOWLEDGE: "doc_",
    MemoryType.PROJECT_CONTEXT: "mem_",
}


def new_id(memory_type: MemoryType) -> str:
    """Stable unique ID with a type prefix; never derived from content (§50)."""
    return f"{_ID_PREFIX.get(memory_type, 'mem_')}{uuid.uuid4().hex}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def content_hash(text: str) -> str:
    """Stable content hash for dedup bookkeeping."""
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


class SQLiteStore:
    """Authoritative structured memory behind one RLock-guarded connection.

    Every public method takes the lock and commits before releasing; WAL mode
    keeps readers responsive (spec §45).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = connect(str(self._path))
        apply_migrations(self._db)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ helpers
    @contextmanager
    def _txn(self, user_id: str | None = None) -> Iterator[sqlite3.Connection]:
        """Transaction scope: commit on success, rollback on error (§46).

        When ``user_id`` is given the user row is provisioned first, so
        structured writes never trip the foreign key on a new user.
        """
        with self._lock:
            try:
                if user_id is not None:
                    self._db.execute(
                        "INSERT OR IGNORE INTO users(id, created_at, updated_at) VALUES (?, ?, ?)",
                        (user_id, _iso(utc_now()), _iso(utc_now())),
                    )
                yield self._db
                self._db.commit()
            except sqlite3.Error:
                self._db.rollback()
                raise

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _check_scores(importance: float, confidence: float) -> None:
        for name, value in (("importance", importance), ("confidence", confidence)):
            if not 0.0 <= float(value) <= 1.0:
                raise ValidationError(f"{name} must be 0.0-1.0, got {value}")

    def _record_event(
        self,
        db: sqlite3.Connection,
        memory_id: str,
        event_type: str,
        old_value: str | None = None,
        new_value: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Insert one event row (spec §14)."""
        db.execute(
            "INSERT INTO memory_events(id, memory_id, event_type, old_value, new_value, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"evt_{uuid.uuid4().hex}", memory_id, event_type, old_value, new_value, reason, _iso(utc_now())),
        )

    def _upsert_registry(
        self,
        db: sqlite3.Connection,
        memory_id: str,
        user_id: str,
        memory_type: MemoryType,
        storage_type: str,
        storage_id: str,
        importance: float,
        confidence: float,
        hash_value: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        """Insert or refresh the registry row for a memory (spec §13).

        Auto-provisions the user row so writes never fail on a missing
        foreign key; users are cheap and idempotent.
        """
        now = _iso(utc_now())
        db.execute(
            "INSERT OR IGNORE INTO users(id, created_at, updated_at) VALUES (?, ?, ?)",
            (user_id, now, now),
        )
        db.execute(
            """
            INSERT INTO memory_registry(
                id, user_id, memory_type, storage_type, storage_id, content_hash,
                importance, confidence, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                storage_id=excluded.storage_id,
                importance=excluded.importance,
                confidence=excluded.confidence,
                content_hash=excluded.content_hash,
                updated_at=excluded.updated_at,
                expires_at=excluded.expires_at,
                deleted_at=NULL
            """,
            (
                memory_id,
                user_id,
                memory_type.value,
                storage_type,
                storage_id,
                hash_value,
                importance,
                confidence,
                now,
                now,
                _iso(expires_at) if expires_at else None,
            ),
        )

    def _touch_registry_access(self, db: sqlite3.Connection, memory_ids: list[str]) -> None:
        """Batch access tracking (spec §38): last_accessed_at + access_count."""
        now = _iso(utc_now())
        for memory_id in memory_ids:
            db.execute(
                "UPDATE memory_registry SET last_accessed_at=?, access_count=access_count+1 WHERE id=?",
                (now, memory_id),
            )

    # ------------------------------------------------------------ users
    def ensure_user(self, user_id: str = "default") -> None:
        """Create the user row if missing (spec §7: default local user)."""
        with self._txn(user_id):
            pass

    # ------------------------------------------------------------ facts/preferences
    def upsert_fact(
        self,
        user_id: str,
        category: str,
        key: str,
        value: str,
        *,
        importance: float = 0.5,
        confidence: float = 1.0,
        source: str | None = None,
        source_reference: str | None = None,
        confirmed: bool = False,
    ) -> tuple[str, bool]:
        """Insert or update one fact; returns (memory_id, updated_existing).

        Conflict resolution (spec §28): one active value per (user, category,
        key). A new value soft-deletes the old row, writes fact_history, and
        records an 'updated' event — all in one transaction (§47).
        """
        self._check_scores(importance, confidence)
        if not key.strip() or not value.strip():
            raise ValidationError("fact key and value must be non-empty")
        now = _iso(utc_now())
        with self._txn(user_id) as db:
            old = db.execute(
                "SELECT id, value FROM facts WHERE user_id=? AND category=? AND key=? AND deleted=0",
                (user_id, category, key),
            ).fetchone()
            if old is not None and str(old["value"]) == value:
                memory_id = str(old["id"])
                db.execute(
                    "UPDATE facts SET confidence=?, importance=?, confirmed=?, updated_at=? WHERE id=?",
                    (confidence, importance, int(confirmed), now, memory_id),
                )
                self._upsert_registry(
                    db, memory_id, user_id, MemoryType.FACT, "sqlite", memory_id, importance, confidence,
                    hash_value=content_hash(f"{category}.{key}={value}"),
                )
                return memory_id, True
            if old is not None:
                db.execute("UPDATE facts SET deleted=1, updated_at=? WHERE id=?", (now, str(old["id"])))
            memory_id = new_id(MemoryType.FACT)
            db.execute(
                "INSERT INTO facts(id, user_id, category, key, value, confidence, importance,"
                " source, source_reference, confirmed, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, user_id, category, key, value, confidence, importance,
                 source, source_reference, int(confirmed), now, now),
            )
            self._record_event(
                db, memory_id, "updated" if old is not None else "created",
                old_value=str(old["value"]) if old is not None else None,
                new_value=value,
                reason="conflict resolution" if old is not None else None,
            )
            if old is not None:
                self._record_event(db, str(old["id"]), "deleted", old_value=str(old["value"]), reason="superseded")
            self._upsert_registry(
                db, memory_id, user_id, MemoryType.FACT, "sqlite", memory_id, importance, confidence,
                hash_value=content_hash(f"{category}.{key}={value}"),
            )
            return memory_id, old is not None

    def upsert_preference(
        self,
        user_id: str,
        category: str,
        key: str,
        value: str,
        *,
        importance: float = 0.5,
        confidence: float = 1.0,
        source: str | None = None,
        source_reference: str | None = None,
        confirmed: bool = False,
    ) -> tuple[str, bool]:
        """Insert or update one preference (same conflict rules as facts)."""
        self._check_scores(importance, confidence)
        if not key.strip() or not value.strip():
            raise ValidationError("preference key and value must be non-empty")
        now = _iso(utc_now())
        with self._txn(user_id) as db:
            old = db.execute(
                "SELECT id, value FROM preferences WHERE user_id=? AND category=? AND key=? AND deleted=0",
                (user_id, category, key),
            ).fetchone()
            if old is not None and str(old["value"]) == value:
                memory_id = str(old["id"])
                db.execute(
                    "UPDATE preferences SET confidence=?, importance=?, confirmed=?, updated_at=? WHERE id=?",
                    (confidence, importance, int(confirmed), now, memory_id),
                )
                self._upsert_registry(
                    db, memory_id, user_id, MemoryType.PREFERENCE, "sqlite", memory_id, importance, confidence,
                    hash_value=content_hash(f"{category}.{key}={value}"),
                )
                return memory_id, True
            if old is not None:
                db.execute("UPDATE preferences SET deleted=1, updated_at=? WHERE id=?", (now, str(old["id"])))
            memory_id = new_id(MemoryType.PREFERENCE)
            db.execute(
                "INSERT INTO preferences(id, user_id, category, key, value, confidence, importance,"
                " source, source_reference, confirmed, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, user_id, category, key, value, confidence, importance,
                 source, source_reference, int(confirmed), now, now),
            )
            self._record_event(
                db, memory_id, "updated" if old is not None else "created",
                old_value=str(old["value"]) if old is not None else None,
                new_value=value,
                reason="conflict resolution" if old is not None else None,
            )
            if old is not None:
                self._record_event(db, str(old["id"]), "deleted", old_value=str(old["value"]), reason="superseded")
            self._upsert_registry(
                db, memory_id, user_id, MemoryType.PREFERENCE, "sqlite", memory_id, importance, confidence,
                hash_value=content_hash(f"{category}.{key}={value}"),
            )
            return memory_id, old is not None

    def get_active_value(
        self, user_id: str, table: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        """Deterministic structured retrieval (spec §31).

        ``table`` is ``facts`` or ``preferences``; returns active rows with
        their memory ids. Optional category filter.
        """
        if table not in {"facts", "preferences"}:
            raise ValueError(f"unsupported structured table: {table}")
        with self._lock:
            if category:
                rows = self._db.execute(
                    f"SELECT id, category, key, value, confidence, importance, confirmed, updated_at"
                    f" FROM {table} WHERE user_id=? AND category=? AND deleted=0"
                    f" ORDER BY importance DESC, updated_at DESC",
                    (user_id, category),
                ).fetchall()
            else:
                rows = self._db.execute(
                    f"SELECT id, category, key, value, confidence, importance, confirmed, updated_at"
                    f" FROM {table} WHERE user_id=? AND deleted=0"
                    f" ORDER BY importance DESC, updated_at DESC",
                    (user_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ projects
    def upsert_project(
        self,
        user_id: str,
        name: str,
        *,
        description: str = "",
        status: str = "active",
        priority: int = 0,
    ) -> tuple[str, bool]:
        """Insert or update a project keyed by (user, name) (spec §10)."""
        if not name.strip():
            raise ValidationError("project name must be non-empty")
        now = _iso(utc_now())
        with self._txn(user_id) as db:
            old = db.execute(
                "SELECT id FROM projects WHERE user_id=? AND name=? AND deleted=0",
                (user_id, name),
            ).fetchone()
            if old is not None:
                memory_id = str(old["id"])
                db.execute(
                    "UPDATE projects SET description=?, status=?, priority=?, updated_at=? WHERE id=?",
                    (description, status, priority, now, memory_id),
                )
                self._record_event(db, memory_id, "updated", new_value=f"{name}:{status}")
                self._upsert_registry(
                    db, memory_id, user_id, MemoryType.STATE, "sqlite", memory_id, 0.7, 1.0,
                    hash_value=content_hash(f"project:{name}"),
                )
                return memory_id, True
            memory_id = new_id(MemoryType.STATE)
            db.execute(
                "INSERT INTO projects(id, user_id, name, description, status, priority, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, user_id, name, description, status, priority, now, now),
            )
            self._record_event(db, memory_id, "created", new_value=f"{name}:{status}")
            self._upsert_registry(
                db, memory_id, user_id, MemoryType.STATE, "sqlite", memory_id, 0.7, 1.0,
                hash_value=content_hash(f"project:{name}"),
            )
            return memory_id, False

    def get_projects(self, user_id: str, status: str | None = None) -> list[dict[str, Any]]:
        """Deterministic project listing (spec §31)."""
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT id, name, description, status, priority, updated_at FROM projects"
                    " WHERE user_id=? AND status=? AND deleted=0 ORDER BY priority DESC, updated_at DESC",
                    (user_id, status),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id, name, description, status, priority, updated_at FROM projects"
                    " WHERE user_id=? AND deleted=0 ORDER BY priority DESC, updated_at DESC",
                    (user_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ goals
    def upsert_goal(
        self,
        user_id: str,
        title: str,
        *,
        description: str = "",
        status: str = "active",
        priority: int = 0,
        project_id: str | None = None,
        target_date: str | None = None,
        importance: float = 0.7,
    ) -> tuple[str, bool]:
        """Insert or update a goal keyed by (user, title) (spec §11)."""
        if not title.strip():
            raise ValidationError("goal title must be non-empty")
        self._check_scores(importance, 1.0)
        now = _iso(utc_now())
        with self._txn(user_id) as db:
            old = db.execute(
                "SELECT id FROM goals WHERE user_id=? AND title=? AND deleted=0",
                (user_id, title),
            ).fetchone()
            if old is not None:
                memory_id = str(old["id"])
                db.execute(
                    "UPDATE goals SET description=?, status=?, priority=?, project_id=?, target_date=?, updated_at=?"
                    " WHERE id=?",
                    (description, status, priority, project_id, target_date, now, memory_id),
                )
                self._record_event(db, memory_id, "updated", new_value=f"{title}:{status}")
                self._upsert_registry(
                    db, memory_id, user_id, MemoryType.GOAL, "sqlite", memory_id, importance, 1.0,
                    hash_value=content_hash(f"goal:{title}"),
                )
                return memory_id, True
            memory_id = new_id(MemoryType.GOAL)
            db.execute(
                "INSERT INTO goals(id, user_id, project_id, title, description, status, priority, target_date,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, user_id, project_id, title, description, status, priority, target_date, now, now),
            )
            self._record_event(db, memory_id, "created", new_value=f"{title}:{status}")
            self._upsert_registry(
                db, memory_id, user_id, MemoryType.GOAL, "sqlite", memory_id, importance, 1.0,
                hash_value=content_hash(f"goal:{title}"),
            )
            return memory_id, False

    def get_goals(self, user_id: str, status: str | None = None) -> list[dict[str, Any]]:
        """Deterministic goal listing (spec §31)."""
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT id, title, description, status, priority, project_id, target_date, updated_at"
                    " FROM goals WHERE user_id=? AND status=? AND deleted=0"
                    " ORDER BY priority DESC, updated_at DESC",
                    (user_id, status),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id, title, description, status, priority, project_id, target_date, updated_at"
                    " FROM goals WHERE user_id=? AND deleted=0 ORDER BY priority DESC, updated_at DESC",
                    (user_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ tasks
    def upsert_task(
        self,
        user_id: str,
        title: str,
        *,
        description: str = "",
        status: str = "pending",
        priority: int = 0,
        project_id: str | None = None,
        goal_id: str | None = None,
        due_date: str | None = None,
        importance: float = 0.6,
    ) -> tuple[str, bool]:
        """Insert or update a task keyed by (user, title) (spec §12)."""
        if not title.strip():
            raise ValidationError("task title must be non-empty")
        self._check_scores(importance, 1.0)
        now = _iso(utc_now())
        with self._txn(user_id) as db:
            old = db.execute(
                "SELECT id FROM tasks WHERE user_id=? AND title=? AND deleted=0",
                (user_id, title),
            ).fetchone()
            if old is not None:
                memory_id = str(old["id"])
                db.execute(
                    "UPDATE tasks SET description=?, status=?, priority=?, project_id=?, goal_id=?, due_date=?,"
                    " updated_at=? WHERE id=?",
                    (description, status, priority, project_id, goal_id, due_date, now, memory_id),
                )
                self._record_event(db, memory_id, "updated", new_value=f"{title}:{status}")
                self._upsert_registry(
                    db, memory_id, user_id, MemoryType.TASK, "sqlite", memory_id, importance, 1.0,
                    hash_value=content_hash(f"task:{title}"),
                )
                return memory_id, True
            memory_id = new_id(MemoryType.TASK)
            db.execute(
                "INSERT INTO tasks(id, user_id, project_id, goal_id, title, description, status, priority,"
                " due_date, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, user_id, project_id, goal_id, title, description, status, priority, due_date, now, now),
            )
            self._record_event(db, memory_id, "created", new_value=f"{title}:{status}")
            self._upsert_registry(
                db, memory_id, user_id, MemoryType.TASK, "sqlite", memory_id, importance, 1.0,
                hash_value=content_hash(f"task:{title}"),
            )
            return memory_id, False

    def get_tasks(self, user_id: str, status: str | None = None) -> list[dict[str, Any]]:
        """Deterministic task listing (spec §31)."""
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT id, title, description, status, priority, project_id, goal_id, due_date, updated_at"
                    " FROM tasks WHERE user_id=? AND status=? AND deleted=0"
                    " ORDER BY priority DESC, due_date IS NULL, due_date, updated_at DESC",
                    (user_id, status),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id, title, description, status, priority, project_id, goal_id, due_date, updated_at"
                    " FROM tasks WHERE user_id=? AND deleted=0"
                    " ORDER BY priority DESC, due_date IS NULL, due_date, updated_at DESC",
                    (user_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ registry
    def register_semantic(
        self,
        memory_id: str,
        user_id: str,
        memory_type: MemoryType,
        storage_id: str,
        *,
        importance: float = 0.5,
        confidence: float = 0.5,
        hash_value: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        """Add or refresh the registry row for a Chroma-backed memory (§13)."""
        with self._txn(user_id) as db:
            self._upsert_registry(
                db, memory_id, user_id, memory_type, "chroma", storage_id,
                importance, confidence, hash_value=hash_value, expires_at=expires_at,
            )

    def registry_entry(self, memory_id: str) -> dict[str, Any] | None:
        """Fetch one registry row (spec §42: inspect)."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM memory_registry WHERE id=?", (memory_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def find_registry_by_hash(self, user_id: str, hash_value: str) -> dict[str, Any] | None:
        """Exact-content registry lookup used as a fast dedup path (§27)."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM memory_registry WHERE user_id=? AND content_hash=? AND deleted_at IS NULL"
                " LIMIT 1",
                (user_id, hash_value),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_access(self, memory_ids: list[str]) -> None:
        """Batch access tracking for retrieved memories (spec §38)."""
        if not memory_ids:
            return
        with self._txn() as db:
            self._touch_registry_access(db, memory_ids)

    def set_deleted(self, memory_id: str, reason: str = "") -> bool:
        """Soft-delete a memory of any type (spec §40). Returns True if found."""
        now = _iso(utc_now())
        with self._txn() as db:
            for table in ("facts", "preferences"):
                row = db.execute(
                    f"SELECT id FROM {table} WHERE id=? AND deleted=0", (memory_id,)
                ).fetchone()
                if row is not None:
                    db.execute(f"UPDATE {table} SET deleted=1, updated_at=? WHERE id=?", (now, memory_id))
                    self._record_event(db, memory_id, "deleted", reason=reason)
                    db.execute(
                        "UPDATE memory_registry SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                        (now, memory_id),
                    )
                    return True
            for table in ("projects", "goals", "tasks"):
                row = db.execute(
                    f"SELECT id FROM {table} WHERE id=? AND deleted=0", (memory_id,)
                ).fetchone()
                if row is not None:
                    db.execute(f"UPDATE {table} SET deleted=1, updated_at=? WHERE id=?", (now, memory_id))
                    self._record_event(db, memory_id, "deleted", reason=reason)
                    db.execute(
                        "UPDATE memory_registry SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                        (now, memory_id),
                    )
                    return True
            row = db.execute(
                "SELECT id FROM memory_registry WHERE id=? AND deleted_at IS NULL", (memory_id,)
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE memory_registry SET deleted_at=?, updated_at=? WHERE id=?", (now, now, memory_id)
                )
                self._record_event(db, memory_id, "deleted", reason=reason)
                return True
        return False

    def confirm(self, memory_id: str) -> bool:
        """Mark a memory confirmed: confidence 1.0 (spec §24/§68)."""
        now = _iso(utc_now())
        with self._txn() as db:
            for table in ("facts", "preferences"):
                row = db.execute(
                    f"SELECT id FROM {table} WHERE id=? AND deleted=0", (memory_id,)
                ).fetchone()
                if row is not None:
                    db.execute(
                        f"UPDATE {table} SET confirmed=1, confidence=1.0, updated_at=? WHERE id=?",
                        (now, memory_id),
                    )
                    self._record_event(db, memory_id, "confirmed")
                    db.execute("UPDATE memory_registry SET confidence=1.0, updated_at=? WHERE id=?", (now, memory_id))
                    return True
            row = db.execute(
                "SELECT id FROM memory_registry WHERE id=? AND deleted_at IS NULL", (memory_id,)
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE memory_registry SET confidence=1.0, updated_at=? WHERE id=?", (now, memory_id)
                )
                self._record_event(db, memory_id, "confirmed")
                return True
        return False

    def active_structured(self, user_id: str) -> list[dict[str, Any]]:
        """All active structured memories for context building (§36/§37)."""
        results: list[dict[str, Any]] = []
        results.extend(self.get_active_value(user_id, "facts"))
        results.extend(self.get_active_value(user_id, "preferences"))
        for row in self.get_projects(user_id):
            row["kind"] = "project"
            results.append(row)
        for row in self.get_goals(user_id):
            row["kind"] = "goal"
            results.append(row)
        for row in self.get_tasks(user_id):
            row["kind"] = "task"
            results.append(row)
        return results

    def expired_memory_ids(self, now: datetime) -> list[str]:
        """Registry ids past their expiry, for lifecycle (spec §39/§54)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM memory_registry WHERE deleted_at IS NULL AND expires_at IS NOT NULL AND expires_at<=?",
                (_iso(now),),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    # ------------------------------------------------------------ events
    def events(self, memory_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Event log for auditing/debugging (spec §14/§42)."""
        with self._lock:
            if memory_id:
                rows = self._db.execute(
                    "SELECT * FROM memory_events WHERE memory_id=? ORDER BY created_at DESC LIMIT ?",
                    (memory_id, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM memory_events ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        """Counts for /memory status and health checks (spec §52)."""
        with self._lock:
            counts: dict[str, Any] = {}
            for table in ("users", "facts", "preferences", "projects", "goals", "tasks"):
                if table == "users":
                    counts[table] = int(self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                else:
                    counts[table] = int(
                        self._db.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted=0").fetchone()[0]
                    )
            counts["registry_active"] = int(
                self._db.execute("SELECT COUNT(*) FROM memory_registry WHERE deleted_at IS NULL").fetchone()[0]
            )
            counts["events"] = int(self._db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0])
        counts["schema_version"] = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        return counts

    def orphan_scan(self) -> dict[str, list[str]]:
        """Registry/storage consistency check inputs (spec §55)."""
        orphans: dict[str, list[str]] = {"registry_without_storage": [], "storage_without_registry": []}
        with self._lock:
            rows = self._db.execute(
                "SELECT id, storage_type FROM memory_registry WHERE deleted_at IS NULL"
            ).fetchall()
            for row in rows:
                if str(row["storage_type"]) == "chroma":
                    # Chroma side is validated by the manager; sqlite side must self-exist.
                    continue
                found = False
                for table in ("facts", "preferences", "projects", "goals", "tasks"):
                    hit = self._db.execute(f"SELECT 1 FROM {table} WHERE id=?", (str(row["id"]),)).fetchone()
                    if hit is not None:
                        found = True
                        break
                if not found:
                    orphans["registry_without_storage"].append(str(row["id"]))
            for table in ("facts", "preferences", "projects", "goals", "tasks"):
                rows = self._db.execute(f"SELECT id FROM {table} WHERE deleted=0").fetchall()
                for row in rows:
                    hit = self._db.execute(
                        "SELECT 1 FROM memory_registry WHERE id=?", (str(row["id"]),)
                    ).fetchone()
                    if hit is None:
                        orphans["storage_without_registry"].append(str(row["id"]))
        return orphans

    def repair_orphans(self) -> int:
        """Re-register sqlite rows missing registry entries (spec §54/§55)."""
        repaired = 0
        orphans = self.orphan_scan()
        type_by_table = {
            "facts": MemoryType.FACT,
            "preferences": MemoryType.PREFERENCE,
            "projects": MemoryType.STATE,
            "goals": MemoryType.GOAL,
            "tasks": MemoryType.TASK,
        }
        with self._txn() as db:
            for memory_id in orphans["storage_without_registry"]:
                for table, memory_type in type_by_table.items():
                    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (memory_id,)).fetchone()
                    if row is not None:
                        importance = float(row["importance"]) if "importance" in row.keys() else 0.6
                        self._upsert_registry(
                            db, memory_id, str(row["user_id"]), memory_type, "sqlite", memory_id,
                            importance, 1.0,
                        )
                        repaired += 1
                        break
        return repaired

    def log_error_safe(self, message: str) -> None:
        """Last-ditch logging hook kept for parity with other stores."""
        log_error(message)

    def snapshot_to(self, target: Path) -> None:
        """Consistent online backup including WAL content (spec §57).

        Uses SQLite's backup API instead of copying the file: the main db can
        look nearly empty while recent writes sit in ``-wal`` journal files,
        and a plain copy of it would lose that data.
        """
        with self._lock:
            dest = sqlite3.connect(target)
            try:
                self._db.backup(dest)
                dest.commit()
            finally:
                dest.close()

    def registry_ids_with_storage(self, storage_type: str) -> set[str]:
        """Active registry IDs stored in the given backend (spec §55 diagnostics)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM memory_registry WHERE storage_type=? AND deleted_at IS NULL",
                (storage_type,),
            ).fetchall()
        return {str(row["id"]) for row in rows}

    def wipe_all(self) -> None:
        """Delete every structured row in place, keeping the schema (§41)."""
        with self._txn() as db:
            for table in ("memory_events", "memory_registry", "tasks", "goals", "projects", "preferences", "facts", "users"):
                db.execute(f"DELETE FROM {table}")
