"""The MemoryManager: the only memory API the assistant sees (spec §18/§76).

Everything else in ``aria`` talks to this class; SQLite and Chroma stay
behind it. It also implements the agent's ``MemoryStore`` protocol (messages,
add/extend/cleanup) so it plugs into the existing agent loop, and it owns the
background extraction worker so memory processing never blocks a turn (spec
§25/§72). Every public method degrades gracefully: memory failures are logged
and swallowed, never propagated into the chat loop (spec §43/§44).
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from .chroma_store import ChromaStore
from .classifier import CandidateMemory, MemoryClassifier, MemoryDecisionType
from .config import MemorySettings
from .deduplicator import ConflictResolver, DedupVerdict, Deduplicator, source_rank
from .embeddings import FallbackEmbeddingProvider
from .exceptions import MemoryError
from .lifecycle import LifecycleManager
from .migrations import SCHEMA_VERSION
from .models import MemoryType, SemanticMemory, STRUCTURED_TYPES, utc_now
from .retriever import ContextBuilder, QueryKind, Retriever, classify_query
from .reranker import Reranker
from .sqlite_store import SQLiteStore, content_hash, new_id
from ..logging_setup import log_debug, log_error, log_info

# The agent's minimal memory contract (src/aria/memory.py MemoryStore).
_MESSAGE_KEYS = ("role", "content")


class MemoryManager:
    """Facade over SQLite + Chroma with extraction, retrieval, lifecycle."""

    def __init__(
        self,
        root: Path,
        settings: MemorySettings,
        provider: Any = None,
        session_label: str = "aria",
        embeddings: Any = None,
    ) -> None:
        self.root = root.resolve()
        self.settings = settings
        self.session_id = f"{session_label}-{utc_now().strftime('%Y%m%d-%H%M%S')}"
        self._provider = provider
        self._closed = False

        # Storage (spec §76: only this subsystem knows the stores).
        # ``embeddings`` allows injecting a replacement EmbeddingProvider
        # (spec §17/§84 dependency injection); default is the config-driven
        # fallback chain.
        self.sqlite = SQLiteStore(settings.sqlite_path)
        self.embeddings = embeddings if embeddings is not None else FallbackEmbeddingProvider(settings)
        self.chroma = ChromaStore(settings.chroma_path, self.embeddings)
        self.classifier = MemoryClassifier()
        self.deduplicator = Deduplicator(
            self.chroma,
            duplicate_threshold=settings.duplicate_threshold,
            related_threshold=settings.related_threshold,
        )
        self.resolver = ConflictResolver()
        self.retriever = Retriever(self.sqlite, self.chroma, Reranker(settings), settings)
        self.context_builder = ContextBuilder(settings)
        self.lifecycle = LifecycleManager(self.sqlite, self.chroma, settings)

        # Conversation buffer (agent MemoryStore contract).
        self.messages: list[dict[str, Any]] = []

        # Background extraction worker (spec §25: async where practical).
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, name="aria-memory-extractor", daemon=True)
        self._worker.start()
        self.sqlite.ensure_user("default")
        log_info(f"MemoryManager: initialized (schema v{SCHEMA_VERSION}) at {self.root}")

    # ------------------------------------------------- agent MemoryStore
    @property
    def path(self) -> Path:
        """Compatibility for the agent's memory status display."""
        return self.sqlite.path

    @property
    def degraded(self) -> bool:
        """True when no embedding backend is available (BR-4 degraded mode).

        In this mode structured facts/preferences keep working; semantic
        (vector) storage and recall are skipped quietly instead of logging an
        error on every operation. Supports both attribute/property flags and
        zero-argument methods on the embedding provider.
        """
        value = getattr(self.embeddings, "degraded", None)
        if value is None:
            return False
        try:
            return bool(value() if callable(value) else value)
        except Exception:  # noqa: BLE001 - status must never raise
            return False

    def add(self, message: dict[str, Any]) -> None:
        """Record one conversation message (agent MemoryStore contract)."""
        self.messages.append(message)
        if self._closed:
            return
        role = str(message.get("role", "unknown"))
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return
        if role == "system":
            return
        self._enqueue_history(role, content)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            self.add(message)

    def cleanup(self) -> None:
        """Stop the worker and close stores; pending items are processed
        best-effort first so a normal shutdown loses nothing (spec §74)."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._worker.join(timeout=5)
        try:
            self.sqlite.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must always finish
            log_error(f"MemoryManager: sqlite close failed: {exc}")
        log_info("MemoryManager: shut down cleanly")

    # ------------------------------------------------------- turn hooks
    def prepare_context(self, query: str = "") -> None:
        """Inject retrieved memory into the system message (spec §61/§75).

        Called by the agent loop before each turn. The injected block is
        wrapped in ``<assistant_memory>`` delimiters with the untrusted-data
        notice (§62) and replaced wholesale each turn so stale context never
        accumulates.
        """
        if not self.messages or self.messages[0].get("role") != "system":
            return
        try:
            context = self.retrieve_context(query)
        except Exception as exc:  # noqa: BLE001 - §44 fallback mode
            log_error(f"MemoryManager: prepare_context failed: {exc}")
            context = ""
        base = str(self.messages[0].get("content", ""))
        marker = f"\n\n{ContextBuilder.MARKER_START}"
        base = base.split(marker, 1)[0].rstrip()
        self.messages[0]["content"] = base + (f"\n\n{context}" if context else "")

    def turn_completed(self, user_text: str, response_text: str) -> None:
        """Queue extraction after a finished turn (spec §25)."""
        if self._closed or not self.settings.extraction_enabled:
            return
        self._queue.put({"kind": "extract", "user": user_text, "assistant": response_text})

    def _enqueue_history(self, role: str, content: str) -> None:
        self._queue.put(
            {
                "kind": "history",
                "role": role,
                "content": content,
                "created_at": utc_now().isoformat(),
            }
        )

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                if item["kind"] == "extract":
                    self._process_extraction(item["user"], item["assistant"])
                elif item["kind"] == "history":
                    self._process_history(item)
            except Exception as exc:  # noqa: BLE001 - §43: worker must never die
                log_error(f"MemoryManager: worker item failed: {type(exc).__name__}: {exc}")

    def _process_history(self, item: dict[str, Any]) -> None:
        """Store a conversation message as semantic memory (§5.7).

        Only messages that pass the classifier's triviality filter are kept
        (spec §21: do not store everything). While the memory subsystem is in
        degraded mode (no embedding backend), vector writes are skipped
        quietly (BR-4) — the conversation still flows through the agent.
        """
        if self.degraded:
            log_debug("MemoryManager: degraded mode; skipping semantic history write")
            return
        decision = self.classifier.classify(item["content"])
        if decision.decision is MemoryDecisionType.IGNORE and item["role"] == "assistant":
            # Assistant replies with real content stay as conversation memory.
            content = item["content"]
            if len(content) < 80 or content.startswith(("!", "$")):
                return
        elif decision.decision is MemoryDecisionType.IGNORE:
            return
        memory = SemanticMemory(
            id=new_id(MemoryType.CONVERSATION),
            user_id="default",
            type=MemoryType.CONVERSATION,
            content=item["content"][:2000],
            importance=0.4,
            confidence=0.7,
            source="conversation",
            source_reference=self.session_id,
            topic=decision.topic,
        )
        chroma_id = self.chroma.add(memory)
        self.sqlite.register_semantic(
            memory.id, "default", MemoryType.CONVERSATION, chroma_id,
            importance=memory.importance, confidence=memory.confidence,
            hash_value=content_hash(memory.content),
        )

    def _process_extraction(self, user_text: str, assistant_text: str) -> None:
        """Extraction pipeline: extract → classify → dedup → resolve → store (§25)."""
        extractor = self._get_extractor()
        if extractor is None:
            return
        candidates = extractor.extract(user_text, assistant_text)
        for candidate in candidates:
            try:
                self._store_candidate(candidate)
            except Exception as exc:  # noqa: BLE001 - one bad candidate stops nothing
                log_error(f"MemoryManager: candidate failed: {exc}")

    def _get_extractor(self) -> Any:
        if getattr(self, "_extractor", None) is None and self._provider is not None:
            from .classifier import MemoryExtractor

            self._extractor = MemoryExtractor(self._provider, self.classifier)
        return getattr(self, "_extractor", None)

    def set_provider(self, provider: Any) -> None:
        """Use the active provider for background extraction."""
        self._provider = provider
        self._extractor = None

    # ------------------------------------------------------- candidate flow
    def _store_candidate(self, candidate: CandidateMemory) -> str | None:
        """Route one candidate through dedup/conflict into storage (§25-§28)."""
        if candidate.importance < self.settings.min_importance_to_store:
            return None
        memory_type = _DECISION_TYPE.get(candidate.type.value, MemoryType.CONVERSATION)
        if memory_type in STRUCTURED_TYPES:
            return self._store_structured(candidate, memory_type)
        return self._store_semantic(candidate, memory_type)

    def _store_structured(self, candidate: CandidateMemory, memory_type: MemoryType) -> str | None:
        """Structured memories go to SQLite with conflict resolution (§28)."""
        category = candidate.topic or "general"
        key = candidate.key or self._derive_key(candidate)
        value = candidate.value or candidate.content
        source = "user_explicit" if candidate.explicitly_confirmed else "conversation"
        if memory_type is MemoryType.PREFERENCE:
            memory_id, _ = self.sqlite.upsert_preference(
                "default", category, key, value,
                importance=candidate.importance, confidence=candidate.confidence,
                source=source, confirmed=candidate.explicitly_confirmed,
            )
        elif memory_type is MemoryType.FACT:
            memory_id, _ = self.sqlite.upsert_fact(
                "default", category, key, value,
                importance=candidate.importance, confidence=candidate.confidence,
                source=source, confirmed=candidate.explicitly_confirmed,
            )
        elif memory_type is MemoryType.STATE:
            memory_id, _ = self.sqlite.upsert_project(
                "default", candidate.topic or value[:40],
                description=candidate.content, status="active",
            )
        elif memory_type is MemoryType.GOAL:
            memory_id, _ = self.sqlite.upsert_goal(
                "default", candidate.value or candidate.content[:80],
                description=candidate.content, importance=candidate.importance,
            )
        elif memory_type is MemoryType.TASK:
            memory_id, _ = self.sqlite.upsert_task(
                "default", candidate.value or candidate.content[:80],
                description=candidate.content, importance=candidate.importance,
            )
        else:
            return None
        # Preserve the old value's story in Chroma on conflict (§28).
        # Best-effort: structured truth must survive even when embeddings are
        # unavailable (spec §43 - the update itself already committed).
        try:
            events = self.sqlite.events(memory_id, limit=2)
            for event in events:
                if event["event_type"] == "updated" and event["old_value"]:
                    historical = self.resolver.historical_memory(
                        category, key, str(event["old_value"]), value, "default"
                    )
                    chroma_id = self.chroma.add(historical)
                    self.sqlite.register_semantic(
                        historical.id, "default", MemoryType.EPISODIC, chroma_id,
                        importance=historical.importance, confidence=historical.confidence,
                        hash_value=content_hash(historical.content),
                    )
                    break
        except Exception as exc:  # noqa: BLE001 - historical story is optional
            log_error(f"MemoryManager: historical memory skipped (embeddings unavailable?): {exc}")
        return memory_id

    def _store_semantic(self, candidate: CandidateMemory, memory_type: MemoryType) -> str | None:
        """Semantic memories pass dedup before Chroma insert (§27)."""
        if self.degraded:
            log_debug("MemoryManager: degraded mode; semantic memory not stored")
            return None
        verdict = self.deduplicator.check(candidate, "default")
        if verdict.verdict is DedupVerdict.MERGE and verdict.existing_id:
            # Update the existing memory in place (§27: merge/update).
            existing = self.sqlite.registry_entry(verdict.existing_id)
            memory_type_of_existing = (
                MemoryType(str(existing["memory_type"])) if existing else memory_type
            )
            self.chroma.delete(verdict.existing_id, memory_type_of_existing)
            memory = SemanticMemory(
                id=verdict.existing_id,
                user_id="default",
                type=memory_type,
                content=candidate.content,
                importance=max(candidate.importance, 0.5),
                confidence=max(candidate.confidence, 0.6),
                source=candidate.source,
                confirmed=candidate.explicitly_confirmed,
                topic=candidate.topic,
            )
            chroma_id = self.chroma.add(memory)
            self.sqlite.register_semantic(
                memory.id, "default", memory_type, chroma_id,
                importance=memory.importance, confidence=memory.confidence,
                hash_value=content_hash(memory.content),
            )
            return memory.id
        memory = SemanticMemory(
            id=new_id(memory_type),
            user_id="default",
            type=memory_type,
            content=candidate.content,
            importance=candidate.importance,
            confidence=candidate.confidence,
            source=candidate.source,
            source_reference=candidate.source_reference or self.session_id,
            confirmed=candidate.explicitly_confirmed,
            topic=candidate.topic,
        )
        chroma_id = self.chroma.add(memory)
        self.sqlite.register_semantic(
            memory.id, "default", memory_type, chroma_id,
            importance=memory.importance, confidence=memory.confidence,
            hash_value=content_hash(memory.content),
        )
        return memory.id

    @staticmethod
    def _derive_key(candidate: CandidateMemory) -> str:
        """Derive a structured key from content when the LLM gave none."""
        words = candidate.content.lower().split()
        stop = {"i", "my", "the", "a", "an", "is", "are", "was", "use", "prefer", "switched", "to", "from"}
        meaningful = [word.strip(".,!?;:'\"") for word in words if word not in stop]
        return "_".join(meaningful[:3]) or "unnamed"

    # ----------------------------------------------------------- public API
    def remember(
        self,
        content: str,
        memory_type: str = "fact",
        *,
        user_id: str = "default",
        importance: float = 0.8,
        confidence: float = 1.0,
        confirmed: bool = True,
        topic: str = "",
        key: str = "",
        value: str = "",
        source: str = "user_explicit",
        source_reference: str = "",
    ) -> str | None:
        """Explicit memory creation (spec §18/§41/§75): user said 'remember...'."""
        try:
            candidate = CandidateMemory(
                type=MemoryDecisionType(memory_type),
                content=content,
                importance=min(max(importance, 0.0), 1.0),
                confidence=min(max(confidence, 0.0), 1.0),
                explicitly_confirmed=confirmed,
                topic=topic,
                key=key,
                value=value,
                source=source,
                source_reference=source_reference,
            )
            return self._store_candidate(candidate)
        except (MemoryError, ValueError) as exc:
            log_error(f"MemoryManager: remember failed: {exc}")
            return None

    def search(self, query: str, limit: int | None = None, tier: str = "semantic", *, user_id: str = "default") -> list[dict[str, Any]]:
        """Hybrid search returning scored hits (spec §18/§42).

        The ``tier`` parameter exists for REPL compatibility: ``semantic``
        searches Chroma types, ``history`` returns recent conversation
        memories. Results use the legacy ``document``/``metadata`` shape the
        REPL renders.
        """
        try:
            # Explicit search always consults both stores: it is the §42
            # inspection API, unlike retrieve_context which classifies first.
            results = self.retriever.retrieve(query, user_id, query_kind=QueryKind.HYBRID)
            capped = results[: limit or self.settings.max_results]
            if tier == "history":
                capped = [r for r in results if r.memory_type is MemoryType.CONVERSATION][: limit or 8]
            return [
                {
                    "document": r.content,
                    "metadata": {
                        "memory_id": r.memory_id,
                        "memory_type": r.memory_type.value,
                        "storage": r.storage_type.value,
                        "score": r.score,
                        "role": "assistant" if "conversation" in r.memory_type.value else r.memory_type.value,
                        "created_at": str(r.metadata.get("created_at", "")),
                    },
                    "score": r.score,
                    "similarity": round(r.similarity, 3),
                    "importance": r.importance,
                    "confidence": r.confidence,
                }
                for r in capped
            ]
        except Exception as exc:  # noqa: BLE001 - §43/§44
            log_error(f"MemoryManager: search failed: {exc}")
            return []

    def retrieve_context(self, query: str, *, user_id: str = "default") -> str:
        """Token-bounded memory context for the LLM (spec §30/§37/§61/§75).

        Empty string when nothing relevant or on failure — callers must
        tolerate a missing block (§44 fallback mode).
        """
        try:
            results = self.retriever.retrieve(query, user_id)
            if not results:
                return ""
            context = self.context_builder.build(results)
            if not context.text:
                return ""
            self._record_access([r.memory_id for r in results if r.storage_type.value == "chroma"][:20])
            return self.context_builder.wrap(context)
        except Exception as exc:  # noqa: BLE001 - §43/§44
            log_error(f"MemoryManager: retrieve_context failed: {exc}")
            return ""

    def _record_access(self, memory_ids: list[str]) -> None:
        """Batch access tracking (spec §38: retrieval must stay cheap)."""
        try:
            self.sqlite.record_access(memory_ids)
        except Exception as exc:  # noqa: BLE001 - access tracking is best effort
            log_error(f"MemoryManager: access tracking failed: {exc}")

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """Fetch one memory with full provenance (spec §18/§42)."""
        try:
            entry = self.sqlite.registry_entry(memory_id)
            if entry is None:
                return None
            memory_type = MemoryType(str(entry["memory_type"]))
            if entry["storage_type"] == "chroma":
                vector = self.chroma.get(memory_id, memory_type)
                document = str(vector["document"]) if vector else ""
            else:
                rows = self.sqlite.get_active_value("default", "facts") + self.sqlite.get_active_value("default", "preferences")
                document = next((f"{r['category']}.{r['key']}: {r['value']}" for r in rows if r["id"] == memory_id), "")
            return {"registry": entry, "content": document}
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: get failed: {exc}")
            return None

    def update(self, memory_id: str, content: str, *, importance: float | None = None) -> bool:
        """Update a memory's content (spec §9/§18)."""
        try:
            entry = self.sqlite.registry_entry(memory_id)
            if entry is None:
                return False
            memory_type = MemoryType(str(entry["memory_type"]))
            if entry["storage_type"] == "chroma":
                memory = SemanticMemory(
                    id=memory_id, user_id=str(entry["user_id"]), type=memory_type,
                    content=content,
                    importance=float(importance if importance is not None else entry["importance"]),
                    confidence=max(float(entry["confidence"]), 0.6),
                    source="system",
                )
                chroma_id = self.chroma.update_content(memory)
                self.sqlite.register_semantic(
                    memory_id, str(entry["user_id"]), memory_type, chroma_id,
                    importance=memory.importance, confidence=memory.confidence,
                    hash_value=content_hash(content),
                )
                return True
            return False
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: update failed: {exc}")
            return False

    def delete(self, memory_id: str, *, reason: str = "user request") -> bool:
        """Soft-delete in SQLite, hard-delete the vector in Chroma (§40)."""
        try:
            entry = self.sqlite.registry_entry(memory_id)
            removed_vector = False
            if entry is not None:
                try:
                    removed_vector = self.chroma.delete(memory_id, MemoryType(str(entry["memory_type"])))
                except Exception as exc:  # noqa: BLE001 - vector delete is best effort
                    log_error(f"MemoryManager: vector delete failed: {exc}")
            removed_row = self.sqlite.set_deleted(memory_id, reason=reason)
            return removed_row or removed_vector
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: delete failed: {exc}")
            return False

    def confirm(self, memory_id: str) -> bool:
        """Explicit user confirmation: confidence 1.0 (spec §24/§68)."""
        try:
            return self.sqlite.confirm(memory_id)
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: confirm failed: {exc}")
            return False

    def forget(self, topic_or_id: str, *, user_id: str = "default") -> int:
        """Forget by id or by topic search (spec §41: user-controlled memory)."""
        try:
            if self.sqlite.registry_entry(topic_or_id) is not None:
                return 1 if self.delete(topic_or_id, reason="user forget") else 0
            hits = self.retriever.retrieve(topic_or_id, user_id)
            count = 0
            for result in hits[:10]:
                if self.delete(result.memory_id, reason="user forget by topic"):
                    count += 1
            return count
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: forget failed: {exc}")
            return 0

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent semantic memories, newest first (spec §42)."""
        try:
            items: list[dict[str, Any]] = []
            for memory_type in (MemoryType.EPISODIC, MemoryType.CONVERSATION, MemoryType.KNOWLEDGE, MemoryType.PROJECT_CONTEXT):
                for item in self.chroma.all_ids(memory_type):
                    metadata = item.get("metadata", {})
                    items.append(
                        {
                            "memory_id": str(metadata.get("memory_id", "")),
                            "content": str(item.get("document", ""))[:160],
                            "type": memory_type.value,
                            "created_at": str(metadata.get("created_at", "")),
                            "importance": float(metadata.get("importance", 0.5) or 0.5),
                        }
                    )
            items.sort(key=lambda item: item["created_at"], reverse=True)
            return items[:limit]
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: list_recent failed: {exc}")
            return []

    def stats(self) -> dict[str, Any]:
        """Combined statistics (spec §42/§52)."""
        try:
            result = dict(self.sqlite.stats())
            try:
                result["chroma"] = self.chroma.collection_counts()
            except Exception as exc:  # noqa: BLE001 - §43
                result["chroma"] = {"error": str(exc)}
            result["schema_version"] = SCHEMA_VERSION
            return result
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def status(self) -> dict[str, Any]:
        """Alias for stats() used by the REPL's duck-typed /memory command."""
        return self.stats()

    def health_check(self) -> dict[str, Any]:
        """Consistency diagnostics (spec §55)."""
        try:
            return self.lifecycle.health_check()
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "issues": [f"health check failed: {exc}"]}

    def run_maintenance(self) -> dict[str, int]:
        """Quality-control maintenance (spec §54)."""
        try:
            return self.lifecycle.run_maintenance()
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: maintenance failed: {exc}")
            return {}

    def ingest_document(self, path: Path, *, user_id: str = "default", topic: str = "") -> dict[str, Any]:
        """Ingest a text/markdown/code file into knowledge memory (§65).

        Text/md/code only in this version; chunking is paragraph-based with a
        configurable target size. Returns a summary dict for the caller.
        """
        try:
            from .ingestion import ingest_file

            return ingest_file(self, path, user_id=user_id, topic=topic)
        except Exception as exc:  # noqa: BLE001 - §43: ingestion must not crash
            log_error(f"MemoryManager: ingest_document failed: {exc}")
            return {"chunks": 0, "error": str(exc)}

    def facts_text(self) -> str:
        """Human-readable structured facts for /memory facts (REPL compat)."""
        try:
            lines: list[str] = []
            for row in self.sqlite.get_active_value("default", "facts"):
                lines.append(f"fact.{row['key']} = {row['value']} (confidence {row['confidence']:.2f})")
            for row in self.sqlite.get_active_value("default", "preferences"):
                lines.append(f"preference.{row['key']} = {row['value']} (confidence {row['confidence']:.2f})")
            for row in self.sqlite.get_projects("default"):
                lines.append(f"project.{row['name']} = {row['status']}")
            for row in self.sqlite.get_goals("default", status="active"):
                lines.append(f"goal: {row['title']} ({row['status']})")
            for row in self.sqlite.get_tasks("default", status="pending"):
                lines.append(f"task: {row['title']} (pending)")
            return "\n".join(lines) or "No structured memories stored yet."
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: facts_text failed: {exc}")
            return "Structured memory unavailable."

    def summarize_now(self) -> int:
        """Drain the extraction queue synchronously (REPL /memory summarize)."""
        processed = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            try:
                if item["kind"] == "extract":
                    self._process_extraction(item["user"], item["assistant"])
                elif item["kind"] == "history":
                    self._process_history(item)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                log_error(f"MemoryManager: summarize_now item failed: {exc}")
        return processed

    def retention(self) -> dict[str, int]:
        """Maintenance alias used by the REPL (spec §54)."""
        return self.run_maintenance()

    def promote_pending(self) -> int:
        """Flush pending extraction work (REPL /memory promote)."""
        return self.summarize_now()

    # ------------------------------------------------------- /clear support
    def clear_current_session(self) -> None:
        """Wipe the current session's conversation memories (REPL /clear)."""
        try:
            self.chroma.delete_all(MemoryType.CONVERSATION, user_id="default")
            self.messages.clear()
            log_info("MemoryManager: current session cleared")
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: clear_current_session failed: {exc}")

    def wipe(self, scope: str) -> None:
        """Destructive wipe for /memory wipe (REPL parity).

        Scopes: ``all`` (everything), ``session`` (current conversation),
        ``1`` (structured SQLite), ``2`` (semantic Chroma), ``3`` (raw
        conversation memories).
        """
        scope = scope.lower().removeprefix("tier")
        semantic_types = (MemoryType.EPISODIC, MemoryType.KNOWLEDGE, MemoryType.PROJECT_CONTEXT)
        try:
            if scope == "all":
                self.messages.clear()
                for memory_type in (*semantic_types, MemoryType.CONVERSATION):
                    self.chroma.delete_all(memory_type)
                self.sqlite.wipe_all()
                self.sqlite.ensure_user("default")
                log_info("MemoryManager: wiped all memory stores")
            elif scope == "session":
                self.clear_current_session()
            elif scope == "1":
                self.sqlite.wipe_all()
                self.sqlite.ensure_user("default")
            elif scope == "2":
                for memory_type in semantic_types:
                    self.chroma.delete_all(memory_type)
            elif scope == "3":
                self.chroma.delete_all(MemoryType.CONVERSATION)
            else:
                raise ValueError(f"unsupported wipe scope: {scope}")
        except Exception as exc:  # noqa: BLE001
            log_error(f"MemoryManager: wipe failed: {exc}")


_DECISION_TYPE: dict[str, MemoryType] = {
    "fact": MemoryType.FACT,
    "preference": MemoryType.PREFERENCE,
    "state": MemoryType.STATE,
    "goal": MemoryType.GOAL,
    "task": MemoryType.TASK,
    "episodic": MemoryType.EPISODIC,
    "conversation": MemoryType.CONVERSATION,
    "knowledge": MemoryType.KNOWLEDGE,
    "project_context": MemoryType.PROJECT_CONTEXT,
    "ignore": MemoryType.CONVERSATION,
}
