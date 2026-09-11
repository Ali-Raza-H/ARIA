"""Durable, privacy-aware runtime telemetry for ARIA.

Telemetry records operational metadata by default: timings, provider/model,
message and tool counts, estimated context size, provider-reported token usage,
errors, and tool outcomes. Prompt, response, and tool contents are never stored
unless an explicit future opt-in is added; this keeps telemetry useful without
turning it into a transcript archive.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config.loader import TelemetryConfig


@dataclass(frozen=True)
class TelemetryEvent:
    kind: str
    timestamp: float
    data: dict[str, Any] = field(default_factory=dict)


class TelemetryRecorder:
    """Thread-safe SQLite telemetry sink shared by ARIA and the coder agent."""

    def __init__(self, config: TelemetryConfig, root: Path | None = None) -> None:
        self.config = config
        self.root = (root or Path.cwd()).resolve()
        self.path = config.database if config.database.is_absolute() else self.root / config.database
        self._lock = threading.RLock()
        self._listeners: list[Callable[[TelemetryEvent], None]] = []
        self._configured_model_contexts: list[dict[str, Any]] = []
        self._connection: sqlite3.Connection | None = None
        if config.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._create_schema()

    def _create_schema(self) -> None:
        assert self._connection is not None
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    duration_ms REAL,
                    user_chars INTEGER NOT NULL DEFAULT 0,
                    response_chars INTEGER NOT NULL DEFAULT 0,
                    iterations INTEGER NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS model_calls (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    duration_ms REAL,
                    provider TEXT,
                    model TEXT,
                    role TEXT,
                    iteration INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    context_chars INTEGER NOT NULL DEFAULT 0,
                    context_tokens INTEGER NOT NULL DEFAULT 0,
                    context_window INTEGER,
                    estimated_input_tokens INTEGER,
                    estimated_output_tokens INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    cached_tokens INTEGER,
                    tool_count INTEGER NOT NULL DEFAULT 0,
                    streamed INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    duration_ms REAL,
                    name TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    argument_chars INTEGER NOT NULL DEFAULT 0,
                    result_chars INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_calls_started ON model_calls(started_at);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_started ON tool_calls(started_at);
                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
                """
            )
            columns = {row[1] for row in self._connection.execute("PRAGMA table_info(model_calls)").fetchall()}
            if "context_tokens" not in columns:
                self._connection.execute("ALTER TABLE model_calls ADD COLUMN context_tokens INTEGER NOT NULL DEFAULT 0")
            if "context_window" not in columns:
                self._connection.execute("ALTER TABLE model_calls ADD COLUMN context_window INTEGER")
            self._connection.commit()

    @staticmethod
    def estimate_tokens(value: Any) -> int:
        """Conservative rough token estimate used when providers omit usage."""
        if isinstance(value, str):
            return max(0, (len(value) + 3) // 4)
        if isinstance(value, (dict, list, tuple)):
            return max(0, (len(json.dumps(value, ensure_ascii=False, default=str)) + 3) // 4)
        return max(0, (len(str(value)) + 3) // 4)

    @staticmethod
    def _chars(value: Any) -> int:
        if isinstance(value, str):
            return len(value)
        return len(json.dumps(value, ensure_ascii=False, default=str))

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def register_model_contexts(self, providers: dict[str, dict[str, Any]]) -> None:
        """Register configured context limits so telemetry can show every model.

        A model need not have been called yet to appear in `/telemetry`; this
        is populated from the static provider configuration at startup.
        """
        contexts: list[dict[str, Any]] = []
        for provider, settings in providers.items():
            raw_models = settings.get("models", [])
            models = raw_models if isinstance(raw_models, list) else []
            raw_windows = settings.get("context_windows", {})
            windows = raw_windows if isinstance(raw_windows, dict) else {}
            default_window = settings.get("context_window", 0)
            for model in models:
                try:
                    window = int(windows.get(model, default_window) or 0)
                except (TypeError, ValueError):
                    window = 0
                contexts.append({"provider": str(provider), "model": str(model), "context_window": window})
        self._configured_model_contexts = contexts

    def add_listener(self, listener: Callable[[TelemetryEvent], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[TelemetryEvent], None]) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if not self.config.live:
            return
        event = TelemetryEvent(kind, time.time(), data)
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:
                pass

    def emit_live(self, kind: str, data: dict[str, Any]) -> None:
        """Publish a non-persistent live event, useful for context refreshes."""
        self._emit(kind, data)

    def _event(self, kind: str, data: dict[str, Any]) -> None:
        if self._connection is not None:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT INTO events(timestamp, kind, payload_json) VALUES (?, ?, ?)",
                    (time.time(), kind, json.dumps(data, ensure_ascii=True, default=str)),
                )
        self._emit(kind, data)

    def start_turn(self, user_text: str) -> str:
        turn_id = uuid.uuid4().hex
        now = time.time()
        data = {"turn_id": turn_id, "user_chars": len(user_text)}
        if self._connection is not None:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT INTO turns(id, started_at, user_chars) VALUES (?, ?, ?)",
                    (turn_id, now, len(user_text)),
                )
        self._event("turn_started", data)
        return turn_id

    def finish_turn(self, turn_id: str, *, response: str, iterations: int, tool_calls: int, error: str | None = None) -> None:
        now = time.time()
        if self._connection is not None:
            with self._lock, self._connection:
                self._connection.execute(
                    "UPDATE turns SET finished_at=?, duration_ms=(? - started_at) * 1000, response_chars=?, iterations=?, tool_calls=?, error=? WHERE id=?",
                    (now, now, len(response), iterations, tool_calls, error, turn_id),
                )
        self._event("turn_finished", {"turn_id": turn_id, "response_chars": len(response), "iterations": iterations, "tool_calls": tool_calls, "error": error})

    def start_model_call(self, turn_id: str, *, provider: str, model: str, role: str, iteration: int, messages: list[dict[str, Any]], tool_count: int, context_window: int | None = None) -> tuple[str, float, dict[str, Any]]:
        call_id = uuid.uuid4().hex
        started = time.time()
        context_chars = self._chars(messages)
        context_tokens = self.estimate_tokens(messages)
        estimates = {
            "message_count": len(messages),
            "context_chars": context_chars,
            "context_tokens": context_tokens,
            "context_window": context_window,
            "estimated_input_tokens": context_tokens if self.config.record_estimates else None,
            "tool_count": tool_count,
        }
        if self._connection is not None:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT INTO model_calls(id, turn_id, started_at, provider, model, role, iteration, message_count, context_chars, context_tokens, context_window, estimated_input_tokens, tool_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (call_id, turn_id, started, provider, model, role, iteration, len(messages), context_chars, context_tokens, context_window, estimates["estimated_input_tokens"], tool_count),
                )
        self._event("context_update", {"call_id": call_id, "turn_id": turn_id, "provider": provider, "model": model, **estimates})
        self._event("model_started", {"call_id": call_id, "turn_id": turn_id, "provider": provider, "model": model, "role": role, "iteration": iteration, **estimates})
        return call_id, started, estimates

    def finish_model_call(self, call_id: str, started: float, *, response: Any = None, usage: dict[str, Any] | None = None, streamed: bool = False, error: str | None = None) -> None:
        now = time.time()
        usage = usage or {}
        input_tokens = self._int_usage(usage, "input_tokens", "prompt_tokens", "prompt_eval_count")
        output_tokens = self._int_usage(usage, "output_tokens", "completion_tokens", "eval_count")
        total_tokens = self._int_usage(usage, "total_tokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        estimated_output_tokens = self.estimate_tokens(response.content if hasattr(response, "content") else response) if self.config.record_estimates else None
        metadata = {key: value for key, value in usage.items() if key not in {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "prompt_eval_count", "eval_count", "total_tokens"}}
        if self._connection is not None:
            with self._lock, self._connection:
                self._connection.execute(
                    "UPDATE model_calls SET finished_at=?, duration_ms=(? - started_at) * 1000, estimated_output_tokens=?, input_tokens=?, output_tokens=?, total_tokens=?, cached_tokens=?, streamed=?, error=?, metadata_json=? WHERE id=?",
                    (now, now, estimated_output_tokens, input_tokens, output_tokens, total_tokens, self._int_usage(usage, "cached_tokens", "cache_read_input_tokens"), int(streamed), error, json.dumps(metadata, default=str), call_id),
                )
        self._event("model_finished", {"call_id": call_id, "duration_ms": round((now - started) * 1000, 2), "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens, "estimated_output_tokens": estimated_output_tokens, "error": error})

    @staticmethod
    def _int_usage(usage: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return None

    def record_tool(self, turn_id: str, *, name: str, arguments: dict[str, Any], result: str, ok: bool, started: float, error: str | None = None) -> None:
        now = time.time()
        data = {"tool_id": uuid.uuid4().hex, "turn_id": turn_id, "name": name, "ok": ok, "duration_ms": round((now - started) * 1000, 2), "argument_chars": self._chars(arguments), "result_chars": len(result), "argument_hash": self._digest(arguments)}
        if self._connection is not None:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT INTO tool_calls(id, turn_id, started_at, finished_at, duration_ms, name, ok, argument_chars, result_chars, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (data["tool_id"], turn_id, started, now, (now - started) * 1000, name, int(ok), data["argument_chars"], len(result), error),
                )
        self._event("tool_finished", data)

    def snapshot(self) -> dict[str, Any]:
        if self._connection is None:
            return {"enabled": False}
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(total_tokens),0), COALESCE(SUM(estimated_input_tokens),0), COALESCE(SUM(estimated_output_tokens),0), COALESCE(SUM(duration_ms),0) FROM model_calls").fetchone()
            latest = self._connection.execute("SELECT provider, model, context_chars, context_tokens, context_window FROM model_calls ORDER BY started_at DESC LIMIT 1").fetchone()
            model_contexts = self._connection.execute("SELECT provider, model, MAX(context_window) AS context_window FROM model_calls WHERE context_window IS NOT NULL GROUP BY provider, model ORDER BY provider, model").fetchall()
            tools = self._connection.execute("SELECT COUNT(*), COALESCE(SUM(ok),0), COALESCE(SUM(duration_ms),0) FROM tool_calls").fetchone()
            turns = self._connection.execute("SELECT COUNT(*), COALESCE(SUM(duration_ms),0) FROM turns").fetchone()
        latest_data = {
            "provider": latest[0], "model": latest[1], "context_chars": int(latest[2] or 0),
            "context_tokens": int(latest[3] or 0), "context_window": int(latest[4] or 0),
        } if latest else {"provider": "", "model": "", "context_chars": 0, "context_tokens": 0, "context_window": 0}
        observed_contexts = [
            {"provider": row[0], "model": row[1], "context_window": int(row[2] or 0)}
            for row in model_contexts
        ]
        merged_contexts = {(item["provider"], item["model"]): item for item in self._configured_model_contexts}
        merged_contexts.update({(item["provider"], item["model"]): item for item in observed_contexts})
        latest_data["model_contexts"] = sorted(merged_contexts.values(), key=lambda item: (item["provider"], item["model"]))
        return {"enabled": True, "model_calls": int(row[0]), "input_tokens": int(row[1]), "output_tokens": int(row[2]), "total_tokens": int(row[3]), "estimated_input_tokens": int(row[4]), "estimated_output_tokens": int(row[5]), "model_ms": round(float(row[6]), 1), "tool_calls": int(tools[0]), "successful_tools": int(tools[1]), "tool_ms": round(float(tools[2]), 1), "turns": int(turns[0]), "turn_ms": round(float(turns[1]), 1), **latest_data}

    def prune(self) -> None:
        if self._connection is None or self.config.retention_days <= 0:
            return
        cutoff = time.time() - self.config.retention_days * 86400
        with self._lock, self._connection:
            for table in ("events", "model_calls", "tool_calls", "turns"):
                column = "timestamp" if table == "events" else "started_at"
                self._connection.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))

    def close(self) -> None:
        if self._connection is not None:
            with self._lock:
                self._connection.close()
                self._connection = None
