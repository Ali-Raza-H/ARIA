"""Deduplication and conflict resolution (spec §27, §28, §29, §67).

Deduplication: before inserting a semantic memory, search for similar
memories; exact-content and >= duplicate_threshold hits merge into the
existing memory, related hits are kept separately, unrelated hits insert.

Conflict resolution: structured stores enforce one active value per key
(upsert semantics); the resolver records the superseded value as a historical
Chroma memory so the *story* survives while SQLite holds only current truth
(spec §1, §28), and applies source priority (§29) when scores compete.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .chroma import ChromaStore
from ..classifier import CandidateMemory
from ..models import MemoryType, SemanticMemory, utc_now
from ...logging.setup import log_debug, log_info


class DedupVerdict(str, Enum):
    """What to do with a candidate after similarity search (spec §27)."""

    INSERT = "insert"
    MERGE = "merge"
    RELATED = "related"


@dataclass
class DedupResult:
    """The deduplicator's decision plus the conflicting memory, if any."""

    verdict: DedupVerdict
    existing_id: str | None = None
    similarity: float = 0.0
    reason: str = ""


class Deduplicator:
    """Semantic duplicate detection against the existing corpus (spec §27)."""

    def __init__(self, chroma: ChromaStore, duplicate_threshold: float = 0.90, related_threshold: float = 0.75) -> None:
        self._chroma = chroma
        self._duplicate_threshold = duplicate_threshold
        self._related_threshold = related_threshold

    def check(self, candidate: CandidateMemory, user_id: str) -> DedupResult:
        """Decide insert/merge/related for one candidate (§27 flowchart)."""
        memory_type = _DECISION_TYPE.get(candidate.type, MemoryType.EPISODIC)
        if memory_type not in _SEMANTIC_TYPES:
            # Structured memories dedupe by key at the store layer (§27 note).
            return DedupResult(DedupVerdict.INSERT, reason="structured type: key-based dedup")
        try:
            hits = self._chroma.search(
                candidate.content,
                memory_types=[memory_type],
                user_id=user_id,
                limit=3,
            )
        except Exception as exc:  # noqa: BLE001 - dedup must never block writes
            log_debug(f"Memory dedup: similarity search failed, inserting: {exc}")
            return DedupResult(DedupVerdict.INSERT, reason="search unavailable")
        if not hits:
            return DedupResult(DedupVerdict.INSERT, reason="no similar memories")
        best = hits[0]
        similarity = float(best.get("similarity", 0.0))
        existing_id = str(best.get("metadata", {}).get("memory_id") or "")
        if similarity >= self._duplicate_threshold:
            return DedupResult(
                DedupVerdict.MERGE,
                existing_id=existing_id or None,
                similarity=similarity,
                reason=f"similarity {similarity:.2f} >= {self._duplicate_threshold}",
            )
        if similarity >= self._related_threshold:
            return DedupResult(
                DedupVerdict.RELATED,
                existing_id=existing_id or None,
                similarity=similarity,
                reason=f"similarity {similarity:.2f} within related band",
            )
        return DedupResult(DedupVerdict.INSERT, similarity=similarity, reason="distinct memory")


# Source priority ladder (spec §29): higher wins on conflicts.
SOURCE_PRIORITY = {
    "user_explicit": 6,
    "conversation_explicit": 5,
    "confirmed": 4,
    "conversation": 3,
    "inferred": 2,
    "system": 1,
    "document": 2,
    "web": 1,
    "tool": 1,
    "import": 2,
}


def source_rank(source: str | None) -> int:
    """Priority for a source label; unknown sources rank lowest (§29)."""
    return SOURCE_PRIORITY.get((source or "").lower(), 0)


class ConflictResolver:
    """Resolves competing values for structured memories (spec §28/§29/§67).

    The structured stores enforce one-active-value-per-key; this resolver
    decides whether an incoming candidate should win against the existing
    record and produces the historical memory describing the change.
    """

    def should_replace(
        self,
        existing_value: str,
        existing_source: str | None,
        existing_confirmed: bool,
        new_value: str,
        new_source: str | None,
        new_confirmed: bool,
    ) -> bool:
        """Apply the §29 ladder: explicit current statement beats everything."""
        if new_value.strip().lower() == existing_value.strip().lower():
            return False
        if new_confirmed:
            return True
        if existing_confirmed and not new_confirmed:
            # Only a correction (explicit) overrides a confirmed record (§67).
            return source_rank(new_source) >= source_rank("user_explicit")
        return source_rank(new_source) >= source_rank(existing_source)

    def historical_memory(
        self, category: str, key: str, old_value: str, new_value: str, user_id: str
    ) -> SemanticMemory:
        """Build the Chroma memory that preserves the old value's story (§28)."""
        content = f"User previously had {key} = {old_value} ({category}) but this changed to {new_value}."
        return SemanticMemory(
            id=f"mem_{user_id}_{utc_now().strftime('%Y%m%d%H%M%S')}",
            user_id=user_id,
            type=MemoryType.EPISODIC,
            content=content,
            importance=0.6,
            confidence=1.0,
            source="system",
            source_reference=f"conflict:{category}.{key}",
            confirmed=True,
            topic=key,
        )


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
    "ignore": MemoryType.EPISODIC,
}

_SEMANTIC_TYPES = {MemoryType.EPISODIC, MemoryType.CONVERSATION, MemoryType.KNOWLEDGE, MemoryType.PROJECT_CONTEXT}
