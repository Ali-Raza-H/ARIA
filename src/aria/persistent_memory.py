"""Persistent three-tier memory for ARIA.

The existing :class:`SessionMemory` remains the lightweight JSON memory used by
short-lived agent sessions. This module is the durable memory manager used by
the conversational ARIA process:

* SQLite stores hard facts, provenance, sessions, and pending jobs.
* Chroma stores semantic summaries (Tier 2) and raw chat records (Tier 3).
* A small worker performs extraction after each completed turn.

All external embedding calls receive redacted text. Chroma is intentionally
initialized eagerly so a configured ARIA process cannot appear healthy while
its durable memory backend is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from openai import OpenAI

from .config import MemoryConfig
from .logging_setup import log_debug, log_error, log_info

try:  # Keep module importable for the existing lightweight memory tests.
    import chromadb
except ImportError:  # pragma: no cover - exercised by startup validation
    chromadb = None  # type: ignore[assignment]


class ProviderLike(Protocol):
    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        on_text: Any = None,
    ) -> Any: ...


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)
_PATH_PATTERN = re.compile(r"(?i)(?:/home/[^\s]+|/Users/[^\s]+|[A-Za-z]:\\[^\s]+)")


class MemoryError(RuntimeError):
    """Raised when required persistent memory infrastructure cannot initialize."""


class EmbeddingBackend(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingBackend:
    name = "openai-compatible"

    def __init__(self, base_url: str, model: str, api_key_env: str) -> None:
        api_key = os.getenv(api_key_env) if api_key_env else None
        if not base_url or not model or not api_key:
            raise ValueError("OpenAI-compatible embedding settings are incomplete")
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.model, input=texts)
        return [list(map(float, item.embedding)) for item in response.data]


class OllamaEmbeddingBackend:
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
            raise RuntimeError(f"Ollama returned no embeddings for {self.model}")
        return [list(map(float, vector)) for vector in embeddings]


class EmbeddingService:
    """Try the configured remote embedding backend, then Ollama fallbacks."""

    def __init__(self, config: MemoryConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._backend: EmbeddingBackend | None = None
        self._failed_backends: set[str] = set()
        self._models = list(config.embedding_fallback_models)
        if config.embedding_model and config.embedding_model not in self._models:
            self._models.insert(0, config.embedding_model)

    def _candidates(self) -> Iterable[EmbeddingBackend]:
        if self._config.embedding_base_url and self._config.embedding_model:
            try:
                yield OpenAIEmbeddingBackend(
                    self._config.embedding_base_url,
                    self._config.embedding_model,
                    self._config.embedding_api_key_env,
                )
            except ValueError as exc:
                log_debug(f"Memory embeddings: remote backend unavailable: {exc}")
        for model in self._models:
            if model in self._failed_backends:
                continue
            try:
                yield OllamaEmbeddingBackend(self._config.embedding_ollama_host, model)
            except Exception as exc:
                self._failed_backends.add(model)
                log_debug(f"Memory embeddings: Ollama backend {model} unavailable: {exc}")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            if self._backend is not None:
                try:
                    return self._backend.embed(texts)
                except Exception as exc:
                    log_error(f"Memory embeddings: backend {self._backend.name} failed: {exc}")
                    self._failed_backends.add(getattr(self._backend, "model", self._backend.name))
                    self._backend = None
            last_error: Exception | None = None
            for backend in self._candidates():
                try:
                    result = backend.embed(texts)
                    self._backend = backend
                    log_info(f"Memory embeddings: using {backend.name} backend")
                    return result
                except Exception as exc:
                    last_error = exc
                    key = getattr(backend, "model", backend.name)
                    self._failed_backends.add(str(key))
                    log_error(f"Memory embeddings: {key} failed: {exc}")
            raise RuntimeError(f"No embedding backend is available: {last_error}")


class PersistentMemory:
    """Durable memory manager implementing the agent memory interface."""

    def __init__(self, root: Path, config: MemoryConfig, provider: ProviderLike) -> None:
        if chromadb is None:
            raise MemoryError("chromadb is required for persistent memory; run uv sync")
        self.root = root.resolve()
        self.config = config
        self.provider = provider
        self.session_id = uuid.uuid4().hex
        self.directory = self.root / config.directory
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        self.db_path = self.directory / "memory.sqlite3"
        self.chroma_path = self.root / config.chroma_directory
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.chroma_path, 0o700)
        except OSError:
            pass
        self._db_lock = threading.RLock()
        self._closed = False
        self._queue_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="aria-memory-worker",
            daemon=True,
        )
        self.messages: list[dict[str, Any]] = []
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        self._db.row_factory = sqlite3.Row
        self._initialize_schema()
        try:
            self._chroma = chromadb.PersistentClient(path=str(self.chroma_path))
            self._semantic = self._chroma.get_or_create_collection(name="semantic_memories")
            self._history = self._chroma.get_or_create_collection(name="chat_history")
        except Exception as exc:
            self._db.close()
            raise MemoryError(f"Could not initialize Chroma at {self.chroma_path}: {exc}") from exc
        self._embeddings = EmbeddingService(config)
        self._create_session()
        self._worker.start()
        log_info(f"PersistentMemory: session {self.session_id} initialized")

    @property
    def path(self) -> Path:
        """Compatibility path used by the existing REPL status display."""
        return self.db_path

    def _initialize_schema(self) -> None:
        with self._db_lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    summary TEXT
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    namespace TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    value_type TEXT NOT NULL DEFAULT 'text',
                    confidence REAL NOT NULL,
                    importance REAL NOT NULL,
                    source_session TEXT,
                    source_memory_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS fact_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    importance REAL NOT NULL,
                    source_session TEXT,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS fact_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    namespace TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    importance REAL NOT NULL,
                    source_session TEXT NOT NULL,
                    source_memory_id TEXT,
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, next_attempt_at);
                CREATE INDEX IF NOT EXISTS fact_observations_lookup
                    ON fact_observations(namespace, fact_key, value);
                CREATE UNIQUE INDEX IF NOT EXISTS active_fact_key
                    ON facts(namespace, fact_key) WHERE active=1;
                """
            )
            self._db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _create_session(self) -> None:
        now = self._now()
        with self._db_lock:
            self._db.execute(
                "INSERT INTO sessions(session_id, started_at, last_activity) VALUES (?, ?, ?)",
                (self.session_id, now, now),
            )
            self._db.commit()

    def add(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if message.get("role") == "system":
            return
        self._record_history_message(message)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            self.add(message)

    def _record_history_message(self, message: dict[str, Any]) -> None:
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return
        message_id = f"{self.session_id}:{len(self.messages)}:{uuid.uuid4().hex}"
        metadata = {
            "session_id": self.session_id,
            "role": str(message.get("role", "unknown")),
            "created_at": self._now(),
            "archived": False,
            "importance": 0.5,
            "access_count": 0,
        }
        # Indexing is deliberately asynchronous so a slow or unavailable
        # embedding backend never blocks the chat turn.
        # Vector stores are durable local data. Redact before storing, not just
        # before embedding, so secrets cannot be retrieved in a later session.
        self._queue_job("history", {"id": message_id, "content": self.redact(content), "metadata": metadata})

    def _queue_job(self, kind: str, payload: dict[str, Any], error: str = "") -> None:
        with self._db_lock:
            self._db.execute(
                "INSERT INTO jobs(session_id, kind, payload, next_attempt_at, last_error) VALUES (?, ?, ?, ?, ?)",
                (self.session_id, kind, json.dumps(payload, ensure_ascii=True), self._now(), error),
            )
            self._db.commit()
        self._queue_event.set()

    def turn_completed(self, user_text: str, response_text: str) -> None:
        """Queue semantic extraction after a completed conversational turn."""
        self._queue_job(
            "summarize",
            {"session_id": self.session_id, "user": self.redact(user_text), "assistant": self.redact(response_text)},
        )

    def prepare_context(self, query: str = "") -> None:
        """Refresh the system message with hard facts and relevant memories."""
        if not self.messages or self.messages[0].get("role") != "system":
            return
        context = self.build_context(query)
        base = str(self.messages[0].get("content", ""))
        marker = "\n\nCURRENT MEMORY CONTEXT\n"
        base = base.split(marker, 1)[0]
        self.messages[0]["content"] = base + (marker + context if context else "")

    def build_context(self, query: str = "") -> str:
        sections: list[str] = []
        facts = self.list_facts(limit=32)
        if facts:
            sections.append("HARD FACTS\n" + "\n".join(f"- {item['namespace']}.{item['key']}: {item['value']}" for item in facts))
        memories = self.search(query, limit=self.config.tier2_limit, tier="semantic") if query else self._recent_semantic()
        history = self.search(query, limit=self.config.tier3_limit, tier="history") if query else self._recent_history()
        if memories:
            sections.append("RELEVANT MEMORIES\n" + "\n".join(f"- {item['document']}" for item in memories))
        if history:
            sections.append("RECENT CHAT\n" + "\n".join(f"- {item['metadata'].get('role', 'unknown')}: {item['document']}" for item in history))
        text = "\n\n".join(sections)
        return self._truncate_tokens(text, self.config.context_token_budget)

    def _recent_semantic(self) -> list[dict[str, Any]]:
        try:
            result = self._semantic.get(limit=self.config.tier2_limit)
            items = self._normalize_chroma(result)
            return sorted(items, key=lambda item: str(item["metadata"].get("created_at", "")), reverse=True)
        except Exception as exc:
            log_error(f"PersistentMemory: semantic warm-up failed: {exc}")
            return []

    def _recent_history(self) -> list[dict[str, Any]]:
        try:
            items = self._normalize_chroma(self._history.get(limit=100000))
            items.sort(key=lambda item: str(item["metadata"].get("created_at", "")))
            return items[-max(self.config.warmup_turns * 2, 2) :]
        except Exception as exc:
            log_error(f"PersistentMemory: history warm-up failed: {exc}")
            return []

    @staticmethod
    def _truncate_tokens(text: str, budget: int) -> str:
        words = text.split()
        return " ".join(words[: max(budget * 4, 1)])

    @staticmethod
    def redact(text: str) -> str:
        """Remove credentials and machine-specific paths before remote embedding."""
        redacted = _PATH_PATTERN.sub("[PATH]", text)
        redacted = re.sub(
            r"(?i)(api[\s_-]*key|token|password|secret|authorization)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            redacted,
        )
        redacted = re.sub(
            r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+",
            "Bearer [REDACTED]",
            redacted,
        )
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", redacted)

    def search(self, query: str, limit: int | None = None, tier: str = "semantic") -> list[dict[str, Any]]:
        collection = self._semantic if tier == "semantic" else self._history
        limit = limit or (self.config.tier2_limit if tier == "semantic" else self.config.tier3_limit)
        try:
            embedding = self._embeddings.embed([self.redact(query)])[0]
            result = collection.query(query_embeddings=[embedding], n_results=limit)
            items = self._normalize_query(result)
            return self._rank(items, query, tier)
        except Exception as exc:
            log_error(f"PersistentMemory: {tier} search unavailable: {exc}")
            return []

    @staticmethod
    def _normalize_chroma(result: Any) -> list[dict[str, Any]]:
        data = cast(dict[str, Any], result)
        ids = data.get("ids", []) or []
        documents = data.get("documents", []) or []
        metadatas = data.get("metadatas", []) or []
        if ids and not isinstance(ids[0], list) and not documents:
            documents = [""] * len(ids)
        if ids and not isinstance(ids[0], list) and not metadatas:
            metadatas = [{}] * len(ids)
        if ids and isinstance(ids[0], list):
            ids, documents, metadatas = ids[0], documents[0], metadatas[0]
        return [
            {"id": str(item_id), "document": str(documents[index] or ""), "metadata": dict(metadatas[index] or {})}
            for index, item_id in enumerate(ids)
        ]

    @classmethod
    def _normalize_query(cls, result: Any) -> list[dict[str, Any]]:
        data = cast(dict[str, Any], result)
        items = cls._normalize_chroma(data)
        distances = data.get("distances", []) or []
        if distances and isinstance(distances[0], list):
            distances = distances[0]
        for index, item in enumerate(items):
            if index < len(distances):
                # Chroma returns distances; convert the common non-negative
                # distance into a bounded similarity signal for ranking.
                item["metadata"]["similarity"] = 1.0 / (1.0 + float(distances[index]))
        return items

    def _rank(self, items: list[dict[str, Any]], query: str, tier: str) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        for item in items:
            metadata = item["metadata"]
            try:
                age_days = max((now - datetime.fromisoformat(str(metadata.get("created_at"))).astimezone(timezone.utc)).days, 0)
            except (TypeError, ValueError):
                age_days = 365
            recency = 0.5 ** (age_days / 365)
            frequency = min(float(metadata.get("access_count", 0)) / 10, 1)
            importance = float(metadata.get("importance", 0.5))
            explicit = 1.0 if query.lower() in item["document"].lower() else 0.0
            item["score"] = (
                float(metadata.get("similarity", 0.0)) * self.config.rank_similarity
                + recency * self.config.rank_recency
                + frequency * self.config.rank_frequency
                + importance * self.config.rank_importance
                + explicit * self.config.rank_explicit
            )
        ranked = sorted(items, key=lambda item: float(item.get("score", 0)), reverse=True)
        # Persist retrieval frequency in the vector metadata; it is otherwise a
        # misleading, permanently-zero ranking signal.
        for item in ranked:
            metadata = dict(item["metadata"])
            metadata["access_count"] = int(metadata.get("access_count", 0)) + 1
            try:
                collection = self._semantic if tier == "semantic" else self._history
                collection.update(ids=[item["id"]], metadatas=[metadata])
                item["metadata"] = metadata
            except Exception as exc:
                log_debug(f"PersistentMemory: could not update retrieval count: {exc}")
        return ranked

    def list_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db_lock:
            rows = self._db.execute(
                "SELECT namespace, fact_key AS key, value, confidence, importance, updated_at FROM facts WHERE active=1 ORDER BY importance DESC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def facts_text(self) -> str:
        facts = self.list_facts()
        return "\n".join(f"{item['namespace']}.{item['key']} = {item['value']}" for item in facts) or "No hard facts stored."

    def _extract(self, payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        prompt = (
            "Return JSON only with keys summary and facts. summary is a concise semantic summary. "
            "facts is a list of objects with namespace, key, value, confidence, importance. "
            "Only include stable user facts worth remembering; never include secrets.\n\n"
            + json.dumps(payload, ensure_ascii=True)
        )
        response = self.provider.complete([{"role": "user", "content": prompt}], [], on_text=None)
        content = str(getattr(response, "content", response))
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            data = json.loads(match.group(0)) if match else {"summary": content, "facts": []}
        summary = str(data.get("summary", "")).strip()
        facts = data.get("facts", [])
        return summary, [item for item in facts if isinstance(item, dict)]

    def _process_job(self, row: sqlite3.Row) -> None:
        payload = json.loads(str(row["payload"]))
        kind = str(row["kind"])
        if kind == "history":
            embedding = self._embeddings.embed([self.redact(str(payload["content"]))])[0]
            self._upsert_vector("history", payload["id"], payload["content"], embedding, payload["metadata"])
        elif kind == "summarize":
            summary, facts = self._extract(payload)
            if summary:
                memory_id = f"semantic:{payload['session_id']}:{uuid.uuid4().hex}"
                embedding = self._embeddings.embed([self.redact(summary)])[0]
                importances: list[float] = []
                for item in facts:
                    try:
                        value = float(item.get("importance", 0.5))
                    except (TypeError, ValueError):
                        continue
                    if 0 <= value <= 1:
                        importances.append(value)
                metadata = {
                    "session_id": payload["session_id"],
                    "created_at": self._now(),
                    "importance": max(importances, default=0.5),
                    "access_count": 0,
                    "archived": False,
                    "summary": True,
                }
                self._upsert_vector("semantic", memory_id, self.redact(summary), embedding, metadata)
                with self._db_lock:
                    self._db.execute("UPDATE sessions SET summary=?, status='summarized', last_activity=? WHERE session_id=?", (summary, self._now(), payload["session_id"]))
                    self._db.commit()
                if self.config.detail_mode == "delete_after_summary":
                    self._history.delete(where={"session_id": payload["session_id"]})
            for fact in facts:
                self._promote_fact(fact, payload["session_id"], memory_id if summary else "")

    def _upsert_vector(
        self, tier: str, item_id: str, document: str, embedding: list[float], metadata: dict[str, Any]
    ) -> None:
        """Upsert into a collection compatible with the active embedding size.

        Chroma fixes collection dimensionality at first insert. Switching an
        Ollama fallback model used to leave every pending job retrying forever.
        Dimension-specific collections keep old memories readable while new
        embeddings receive a compatible index.
        """
        attr = "_semantic" if tier == "semantic" else "_history"
        collection = getattr(self, attr)
        try:
            collection.upsert(ids=[item_id], documents=[document], embeddings=[embedding], metadatas=[metadata])
            return
        except Exception as exc:
            if "dimension" not in str(exc).lower():
                raise
            name = f"{tier}_memories_d{len(embedding)}" if tier == "semantic" else f"chat_history_d{len(embedding)}"
            log_info(f"PersistentMemory: using {name} after embedding dimension change")
            collection = self._chroma.get_or_create_collection(name=name)
            setattr(self, attr, collection)
            collection.upsert(ids=[item_id], documents=[document], embeddings=[embedding], metadatas=[metadata])

    def _promote_fact(self, fact: dict[str, Any], session_id: str, source_memory_id: str) -> None:
        namespace = str(fact.get("namespace", "profile"))
        key = str(fact.get("key", "")).strip()
        value = self.redact(str(fact.get("value", "")).strip())
        try:
            confidence = float(fact.get("confidence", 0))
            importance = float(fact.get("importance", 0))
        except (TypeError, ValueError):
            log_error("PersistentMemory: ignored fact with non-numeric confidence or importance")
            return
        if not 0 <= confidence <= 1 or not 0 <= importance <= 1:
            log_error("PersistentMemory: ignored fact with out-of-range confidence or importance")
            return
        if not key or not value or confidence < self.config.promotion_confidence or importance < self.config.promotion_importance:
            return
        now = self._now()
        with self._db_lock:
            self._db.execute(
                "INSERT INTO fact_observations(namespace, fact_key, value, confidence, importance, source_session, source_memory_id, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (namespace, key, value, confidence, importance, session_id, source_memory_id, now),
            )
            observation_count = int(self._db.execute(
                "SELECT COUNT(DISTINCT source_session) FROM fact_observations WHERE namespace=? AND fact_key=? AND value=? AND observed_at>=?",
                (namespace, key, value, (datetime.now(timezone.utc) - timedelta(days=self.config.promotion_recency_days)).isoformat()),
            ).fetchone()[0])
            if observation_count < self.config.promotion_repetitions:
                self._db.commit()
                return
            row = self._db.execute(
                "SELECT * FROM facts WHERE namespace=? AND fact_key=? AND active=1",
                (namespace, key),
            ).fetchone()
            if row is None:
                cursor = self._db.execute(
                    "INSERT INTO facts(namespace, fact_key, value, confidence, importance, source_session, source_memory_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (namespace, key, value, confidence, importance, session_id, source_memory_id, self._now(), self._now()),
                )
            else:
                fact_id = int(row["id"])
                self._db.execute("UPDATE facts SET active=0 WHERE id=?", (fact_id,))
                cursor = self._db.execute(
                    "INSERT INTO facts(namespace, fact_key, value, confidence, importance, source_session, source_memory_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (namespace, key, value, confidence, importance, session_id, source_memory_id, row["created_at"], self._now()),
                )
                new_id = int(cursor.lastrowid or 0)
                self._db.execute(
                    "INSERT INTO fact_history(fact_id, value, confidence, importance, source_session, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (new_id, str(row["value"]), float(row["confidence"]), float(row["importance"]), row["source_session"], self._now()),
                )
            self._db.commit()

    def _archive_session_history(self, session_id: str, summary: str | None = None) -> None:
        """Mark a session's raw history archived and optionally replace content."""
        try:
            items = self._normalize_chroma(self._history.get(limit=100000))
            for item in items:
                if str(item["metadata"].get("session_id")) != session_id:
                    continue
                metadata = dict(item["metadata"])
                metadata["archived"] = True
                metadata["importance"] = min(float(metadata.get("importance", 0.5)), 0.2)
                document = summary if summary and self.config.detail_mode == "raw_then_summary" else item["document"]
                self._history.update(ids=[item["id"]], documents=[document], metadatas=[metadata])
        except Exception as exc:
            log_error(f"PersistentMemory: could not archive session {session_id}: {exc}")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            row = self._next_job()
            if row is None:
                self.retention()
                self._queue_event.wait(timeout=60)
                self._queue_event.clear()
                continue
            try:
                self._process_job(row)
                with self._db_lock:
                    self._db.execute("UPDATE jobs SET status='done', last_error=NULL WHERE id=?", (row["id"],))
                    self._db.commit()
            except Exception as exc:
                if str(row["kind"]) == "summarize":
                    payload = json.loads(str(row["payload"]))
                    self._archive_session_history(str(payload.get("session_id", "")))
                attempts = int(row["attempts"]) + 1
                next_time = datetime.now(timezone.utc) + timedelta(seconds=self.config.retry_interval_seconds)
                with self._db_lock:
                    self._db.execute(
                        "UPDATE jobs SET status='pending', attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
                        (attempts, next_time.isoformat(), str(exc), row["id"]),
                    )
                    self._db.commit()
                log_error(f"PersistentMemory: job {row['kind']} failed: {exc}")
                self._queue_event.wait(timeout=self.config.retry_interval_seconds)
                self._queue_event.clear()

    def _next_job(self) -> sqlite3.Row | None:
        with self._db_lock:
            return self._db.execute(
                "SELECT * FROM jobs WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT 1",
                (self._now(),),
            ).fetchone()

    def summarize_now(self) -> int:
        with self._db_lock:
            rows = self._db.execute("SELECT * FROM jobs WHERE kind='summarize' AND status='pending' ORDER BY id").fetchall()
        for row in rows:
            try:
                self._process_job(row)
                with self._db_lock:
                    self._db.execute("UPDATE jobs SET status='done', last_error=NULL WHERE id=?", (row["id"],))
                    self._db.commit()
            except Exception as exc:
                payload = json.loads(str(row["payload"]))
                self._archive_session_history(str(payload.get("session_id", "")))
                log_error(f"PersistentMemory: manual summary failed: {exc}")
        return len(rows)

    def retention(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        archived = 0
        deleted = 0
        semantic_deleted = 0
        try:
            result = self._history.get(limit=100000)
            items = self._normalize_chroma(result)
            for item in items:
                created = datetime.fromisoformat(str(item["metadata"].get("created_at"))).astimezone(timezone.utc)
                age = (now - created).days
                metadata = item["metadata"]
                if age >= self.config.raw_retention_days and not metadata.get("archived"):
                    metadata["archived"] = True
                    metadata["importance"] = min(float(metadata.get("importance", 0.5)), 0.2)
                    replacement = None
                    if self.config.detail_mode == "raw_then_summary":
                        with self._db_lock:
                            session_row = self._db.execute(
                                "SELECT summary FROM sessions WHERE session_id=?",
                                (metadata.get("session_id"),),
                            ).fetchone()
                        if session_row and session_row["summary"]:
                            replacement = str(session_row["summary"])
                    if replacement:
                        self._history.update(
                            ids=[item["id"]],
                            documents=[replacement],
                            metadatas=[metadata],
                        )
                    else:
                        self._history.update(ids=[item["id"]], metadatas=[metadata])
                    archived += 1
                if age >= self.config.archive_retention_days:
                    self._history.delete(ids=[item["id"]])
                    deleted += 1
            semantic_result = self._semantic.get(limit=100000)
            for item in self._normalize_chroma(semantic_result):
                created = datetime.fromisoformat(str(item["metadata"].get("created_at"))).astimezone(timezone.utc)
                if (now - created).days >= self.config.tier2_retention_days:
                    self._semantic.delete(ids=[item["id"]])
                    semantic_deleted += 1
        except Exception as exc:
            log_error(f"PersistentMemory: retention failed: {exc}")
        return {"archived": archived, "deleted": deleted, "semantic_deleted": semantic_deleted}

    def status(self) -> dict[str, Any]:
        with self._db_lock:
            facts = int(self._db.execute("SELECT COUNT(*) FROM facts WHERE active=1").fetchone()[0])
            pending = int(self._db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0])
            sessions = int(self._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            observations = int(self._db.execute("SELECT COUNT(*) FROM fact_observations").fetchone()[0])
        return {"session_id": self.session_id, "facts": facts, "pending_jobs": pending, "sessions": sessions, "observations": observations}

    @staticmethod
    def _delete_all(collection: Any) -> None:
        """Delete every record without relying on an invalid empty filter."""
        result = collection.get(limit=100000, include=["metadatas"])
        items = PersistentMemory._normalize_chroma(result)
        ids = [item["id"] for item in items]
        if ids:
            collection.delete(ids=ids)

    def set_provider(self, provider: ProviderLike) -> None:
        """Use the currently selected ARIA provider for background extraction."""
        self.provider = provider

    def promote_pending(self) -> int:
        """Process queued summaries and apply any qualifying hard facts."""
        return self.summarize_now()

    def clear_current_session(self) -> None:
        """Delete this session's records and start a clean persistent session."""
        old_session_id = self.session_id
        self._history.delete(where={"session_id": old_session_id})
        self._semantic.delete(where={"session_id": old_session_id})
        with self._db_lock:
            self._db.execute("DELETE FROM jobs WHERE session_id=?", (old_session_id,))
            self._db.execute("DELETE FROM sessions WHERE session_id=?", (old_session_id,))
            self._db.commit()
        self.messages.clear()
        self.session_id = uuid.uuid4().hex
        self._create_session()

    def wipe(self, scope: str) -> None:
        scope = scope.lower()
        if scope == "all":
            self.messages.clear()
            with self._db_lock:
                self._db.execute("DELETE FROM facts")
                self._db.execute("DELETE FROM fact_history")
                self._db.execute("DELETE FROM fact_observations")
                self._db.execute("DELETE FROM jobs")
                self._db.execute("DELETE FROM sessions")
                self._db.commit()
            self._delete_all(self._semantic)
            self._delete_all(self._history)
            self.session_id = uuid.uuid4().hex
            self._create_session()
        elif scope == "session":
            self.messages.clear()
            old_session_id = self.session_id
            self._history.delete(where={"session_id": old_session_id})
            self._semantic.delete(where={"session_id": old_session_id})
            with self._db_lock:
                self._db.execute("DELETE FROM jobs WHERE session_id=?", (old_session_id,))
                self._db.execute("DELETE FROM sessions WHERE session_id=?", (old_session_id,))
                self._db.commit()
            self.session_id = uuid.uuid4().hex
            self._create_session()
        elif scope in {"1", "2", "3"}:
            if scope == "1":
                with self._db_lock:
                    self._db.execute("DELETE FROM facts")
                    self._db.execute("DELETE FROM fact_history")
                    self._db.execute("DELETE FROM fact_observations")
                    self._db.commit()
            elif scope == "2":
                self._delete_all(self._semantic)
                with self._db_lock:
                    self._db.execute("DELETE FROM jobs WHERE kind='summarize'")
                    self._db.commit()
            else:
                self._delete_all(self._history)
                with self._db_lock:
                    self._db.execute("DELETE FROM jobs WHERE kind='history'")
                    self._db.commit()
        else:
            raise ValueError("scope must be all, session, or tier 1/2/3")

    def cleanup(self) -> None:
        self._stop_event.set()
        self._queue_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2)
        with self._db_lock:
            if self._closed:
                return
            self._db.execute("UPDATE sessions SET status='closed', last_activity=? WHERE session_id=?", (self._now(), self.session_id))
            self._db.commit()
            self._db.close()
            self._closed = True
        log_debug(f"PersistentMemory: closed session {self.session_id}")
