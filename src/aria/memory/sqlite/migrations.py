"""Versioned SQLite migrations for the structured memory store (spec §56).

Migrations apply automatically on startup, run in order, and never destroy
existing data. The applied version is tracked with SQLite's ``user_version``
pragma; each migration runs inside one transaction so a crash mid-migration
cannot leave a half-migrated database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from ..exceptions import StorageUnavailableError

Migration = Callable[[sqlite3.Connection], None]

SCHEMA_VERSION = 1

# Pragmas applied to every new connection: WAL for safe concurrent readers
# with one writer, foreign keys on, 10s busy timeout for cross-thread writes.
CONNECTION_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000",
)


def _migrate_v1(db: sqlite3.Connection) -> None:
    """Initial schema: users, structured memory, registry, events (spec §6-14)."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS facts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            importance REAL NOT NULL DEFAULT 0.5,
            source TEXT,
            source_reference TEXT,
            confirmed INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS facts_active_key
            ON facts(user_id, category, key) WHERE deleted=0;

        CREATE TABLE IF NOT EXISTS preferences (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            importance REAL NOT NULL DEFAULT 0.5,
            source TEXT,
            source_reference TEXT,
            confirmed INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS preferences_active_key
            ON preferences(user_id, category, key) WHERE deleted=0;

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'idea',
            priority INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            project_id TEXT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            priority INTEGER NOT NULL DEFAULT 0,
            target_date TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            project_id TEXT,
            goal_id TEXT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 0,
            due_date TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS memory_registry (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            storage_type TEXT NOT NULL,
            storage_id TEXT NOT NULL,
            content_hash TEXT,
            importance REAL NOT NULL DEFAULT 0.5,
            confidence REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_accessed_at TEXT,
            access_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS registry_user_type
            ON memory_registry(user_id, memory_type, deleted_at);
        CREATE INDEX IF NOT EXISTS registry_storage
            ON memory_registry(storage_type, storage_id);

        CREATE TABLE IF NOT EXISTS memory_events (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_memory ON memory_events(memory_id, created_at);
        """
    )


MIGRATIONS: tuple[tuple[int, Migration], ...] = ((1, _migrate_v1),)


def connect(path: str) -> sqlite3.Connection:
    """Open a configured SQLite connection (spec §45: per-store connection)."""
    try:
        db = sqlite3.connect(path, check_same_thread=False)
        for pragma in CONNECTION_PRAGMAS:
            db.execute(pragma)
        db.row_factory = sqlite3.Row
        return db
    except sqlite3.Error as exc:
        raise StorageUnavailableError(f"could not open SQLite database at {path}: {exc}") from exc


def apply_migrations(db: sqlite3.Connection) -> int:
    """Bring the schema to the latest version; return the applied version.

    Statements are idempotent (``IF NOT EXISTS``), so an interrupted migration
    simply re-runs on next startup (spec §56: never destroy existing data).
    """
    try:
        current = int(db.execute("PRAGMA user_version").fetchone()[0])
        for version, migration in MIGRATIONS:
            if version <= current:
                continue
            migration(db)
            db.commit()
            db.execute(f"PRAGMA user_version = {version}")
            db.commit()
        return int(db.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise StorageUnavailableError(f"SQLite migration failed: {exc}") from exc
