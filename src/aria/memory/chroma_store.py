"""Chroma semantic memory store (spec §15-16, §27, §32).

Four collections isolate memory types while sharing one embedding space:
conversation_memory, episodic_memory, knowledge, project_context. The choice
is documented per spec §15: separate collections give clean metadata scoping
and per-type lifecycle deletes without filter gymnastics, and the number of
collections is small and stable.

Embeddings are always supplied explicitly (Chroma's default embedding
function is never invoked), and every upsert is dimension-aware: a model
switch creates a ``<name>_d<dims>`` collection rather than corrupting the
existing one.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .embeddings import EmbeddingProvider
from .exceptions import MemoryError, StorageUnavailableError
from .models import MemoryType, SemanticMemory, clamp_score
from ..logging.setup import log_error, log_info

try:
    import chromadb
except ImportError:  # pragma: no cover - exercised by startup validation
    chromadb = None  # type: ignore[assignment]

COLLECTION_BY_TYPE: dict[MemoryType, str] = {
    MemoryType.CONVERSATION: "conversation_memory",
    MemoryType.EPISODIC: "episodic_memory",
    MemoryType.KNOWLEDGE: "knowledge",
    MemoryType.PROJECT_CONTEXT: "project_context",
}


def collection_name_for(memory_type: MemoryType) -> str:
    """Map a semantic memory type to its collection (spec §15)."""
    return COLLECTION_BY_TYPE.get(memory_type, "episodic_memory")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class ChromaStore:
    """Semantic store behind the EmbeddingProvider abstraction (§17)."""

    def __init__(self, path: Path, embeddings: EmbeddingProvider) -> None:
        if chromadb is None:
            raise MemoryError("chromadb is required for semantic memory; run uv sync")
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)
        try:
            self._client = chromadb.PersistentClient(path=str(self._path))
        except Exception as exc:  # pragma: no cover - native client failure
            raise StorageUnavailableError(f"could not initialize Chroma at {self._path}: {exc}") from exc
        self._embeddings = embeddings
        self._lock = threading.RLock()
        self._collections: dict[str, Any] = {}
        # Dimension-specific collection overrides after an embedding switch.
        self._collection_overrides: dict[str, str] = {}

    # ------------------------------------------------------------ helpers
    def _get_collection(self, base_name: str) -> Any:
        with self._lock:
            name = self._collection_overrides.get(base_name, base_name)
            if name not in self._collections:
                self._collections[name] = self._client.get_or_create_collection(name=name)
            return self._collections[name]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return self._embeddings.embed(texts)

    def _upsert_with_dimension_fallback(
        self, base_name: str, ids: list[str], documents: list[str], embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """Upsert into the active collection, switching if dimensions changed."""
        collection = self._get_collection(base_name)
        try:
            collection.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)
            return
        except Exception as exc:
            if "dimension" not in str(exc).lower():
                raise
        dims = len(embeddings[0])
        fallback_name = f"{base_name}_d{dims}"
        with self._lock:
            self._collection_overrides[base_name] = fallback_name
            self._collections.pop(fallback_name, None)
        log_info(f"Memory chroma: embedding dimension changed; using collection {fallback_name}")
        collection = self._get_collection(base_name)
        collection.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)

    @staticmethod
    def _metadata_for(memory: SemanticMemory) -> dict[str, Any]:
        """Chroma metadata per spec §16 (flat scalars only)."""
        return {
            "memory_id": memory.id,
            "user_id": memory.user_id,
            "memory_type": memory.type.value,
            "topic": memory.topic or "",
            "importance": float(memory.importance),
            "confidence": float(memory.confidence),
            "source": memory.source or "unknown",
            "source_reference": memory.source_reference or "",
            "confirmed": int(memory.confirmed),
            "created_at": _iso(memory.created_at),
            "last_accessed_at": "",
            "access_count": 0,
        }

    @staticmethod
    def _normalize(result: Any) -> list[dict[str, Any]]:
        """Flatten Chroma's query/get response into scored result dicts."""
        data: dict[str, Any] = result or {}
        ids = data.get("ids") or []
        if ids and isinstance(ids[0], list):
            ids = ids[0]
        documents = data.get("documents") or []
        if documents and isinstance(documents[0], list):
            documents = documents[0]
        metadatas = data.get("metadatas") or []
        if metadatas and isinstance(metadatas[0], list):
            metadatas = metadatas[0]
        distances = data.get("distances") or []
        if distances and isinstance(distances[0], list):
            distances = distances[0]
        items: list[dict[str, Any]] = []
        for index, item_id in enumerate(ids):
            metadata = dict(metadatas[index]) if index < len(metadatas) else {}
            distance = float(distances[index]) if index < len(distances) else 1.0
            items.append(
                {
                    "chroma_id": str(item_id),
                    "document": str(documents[index]) if index < len(documents) else "",
                    "metadata": metadata,
                    # Convert distance to a bounded similarity for ranking (§34).
                    "similarity": 1.0 / (1.0 + max(distance, 0.0)),
                }
            )
        return items

    # ------------------------------------------------------------ writes
    def add(self, memory: SemanticMemory) -> str:
        """Insert one semantic memory; returns the Chroma id (spec §16)."""
        memory.validate()
        clamp_score(memory.importance, "importance")
        clamp_score(memory.confidence, "confidence")
        chroma_id = memory.chroma_id or f"vec_{memory.id}"
        embedding = self._embed([memory.content])[0]
        metadata = self._metadata_for(memory)
        base_name = collection_name_for(memory.type)
        self._upsert_with_dimension_fallback(
            base_name, [chroma_id], [memory.content], [embedding], [metadata]
        )
        return chroma_id

    def update_content(self, memory: SemanticMemory) -> str:
        """Replace a memory's content/embedding (spec §9: memory updates)."""
        return self.add(memory)

    def update_metadata(self, memory_id: str, memory_type: MemoryType, **fields: Any) -> bool:
        """Update stored metadata without re-embedding (spec §38/§39)."""
        collection = self._get_collection(collection_name_for(memory_type))
        try:
            existing = collection.get(ids=[f"vec_{memory_id}"], include=["metadatas"])
        except Exception as exc:
            log_error(f"Memory chroma: metadata lookup failed for {memory_id}: {exc}")
            return False
        items = self._normalize(existing)
        if not items:
            return False
        metadata = dict(items[0]["metadata"])
        for key, value in fields.items():
            metadata[key] = value
        collection.update(ids=[f"vec_{memory_id}"], metadatas=[metadata])
        return True

    def delete(self, memory_id: str, memory_type: MemoryType) -> bool:
        """Delete a vector (spec §40). Returns True if a vector was removed."""
        collection = self._get_collection(collection_name_for(memory_type))
        chroma_id = f"vec_{memory_id}"
        existing = self._normalize(collection.get(ids=[chroma_id], include=["metadatas"]))
        if not existing:
            return False
        collection.delete(ids=[chroma_id])
        return True

    # ------------------------------------------------------------ reads
    def search(
        self,
        query: str,
        *,
        memory_types: list[MemoryType] | None = None,
        user_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Semantic search with metadata filtering (spec §16/§32)."""
        types = memory_types or [MemoryType.EPISODIC, MemoryType.CONVERSATION]
        embedding = self._embed([query])[0]
        aggregated: list[dict[str, Any]] = []
        for memory_type in types:
            collection = self._get_collection(collection_name_for(memory_type))
            where = {"user_id": user_id} if user_id else None
            try:
                result = collection.query(
                    query_embeddings=[embedding], n_results=limit, where=where
                )
            except Exception as exc:
                log_error(f"Memory chroma: query failed on {memory_type.value}: {exc}")
                continue
            for item in self._normalize(result):
                item["memory_type"] = memory_type.value
                aggregated.append(item)
        aggregated.sort(key=lambda item: float(item.get("similarity", 0.0)), reverse=True)
        return aggregated[:limit]

    def get(self, memory_id: str, memory_type: MemoryType) -> dict[str, Any] | None:
        """Fetch one vector by registry id (spec §42: inspect)."""
        collection = self._get_collection(collection_name_for(memory_type))
        items = self._normalize(collection.get(ids=[f"vec_{memory_id}"], include=["metadatas", "documents"]))
        if not items:
            return None
        items[0]["memory_type"] = memory_type.value
        return items[0]

    def exists(self, memory_id: str, memory_type: MemoryType) -> bool:
        """True when the vector is present (used by health checks, §55)."""
        collection = self._get_collection(collection_name_for(memory_type))
        return bool(self._normalize(collection.get(ids=[f"vec_{memory_id}"], include=["metadatas"])))

    def all_ids(self, memory_type: MemoryType) -> list[dict[str, Any]]:
        """All records in a collection — lifecycle/health scan input (§54/§55)."""
        collection = self._get_collection(collection_name_for(memory_type))
        try:
            items = self._normalize(collection.get(limit=100000, include=["metadatas", "documents"]))
        except Exception as exc:
            log_error(f"Memory chroma: scan failed on {memory_type.value}: {exc}")
            return []
        for item in items:
            item["memory_type"] = memory_type.value
        return items

    def delete_all(self, memory_type: MemoryType, user_id: str | None = None) -> int:
        """Delete records by type (and optionally user); returns count (§41)."""
        collection = self._get_collection(collection_name_for(memory_type))
        items = self._normalize(collection.get(limit=100000, include=["metadatas"]))
        targets = [
            item["chroma_id"]
            for item in items
            if user_id is None or str(item["metadata"].get("user_id")) == user_id
        ]
        if targets:
            collection.delete(ids=targets)
        return len(targets)

    def collection_counts(self) -> dict[str, int]:
        """Per-collection document counts for stats (spec §52)."""
        counts: dict[str, int] = {}
        for memory_type, name in COLLECTION_BY_TYPE.items():
            try:
                collection = self._get_collection(name)
                counts[name] = int(collection.count())
            except Exception as exc:
                log_error(f"Memory chroma: count failed for {name}: {exc}")
                counts[name] = -1
        return counts
