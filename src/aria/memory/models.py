"""Typed memory models shared across the memory subsystem (spec §19).

Dataclasses keep the subsystem dependency-free; validation happens in
:meth:`MemoryBase.validate` and at store boundaries so malformed records are
rejected before touching SQLite or Chroma (spec §49).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .exceptions import ValidationError


class MemoryType(str, Enum):
    """Logical memory types (spec §5)."""

    FACT = "fact"
    PREFERENCE = "preference"
    STATE = "state"
    GOAL = "goal"
    TASK = "task"
    EPISODIC = "episodic"
    CONVERSATION = "conversation"
    KNOWLEDGE = "knowledge"
    PROJECT_CONTEXT = "project_context"


# Memory types whose authoritative copy lives in SQLite (spec §5).
STRUCTURED_TYPES = frozenset(
    {MemoryType.FACT, MemoryType.PREFERENCE, MemoryType.STATE, MemoryType.GOAL, MemoryType.TASK}
)

# Memory types whose primary copy lives in Chroma (spec §5).
SEMANTIC_TYPES = frozenset(
    {MemoryType.EPISODIC, MemoryType.CONVERSATION, MemoryType.KNOWLEDGE, MemoryType.PROJECT_CONTEXT}
)


class StorageType(str, Enum):
    """Where a memory's authoritative copy lives."""

    SQLITE = "sqlite"
    CHROMA = "chroma"


def utc_now() -> datetime:
    """Timezone-aware UTC now (all stored timestamps are ISO-8601 UTC)."""
    return datetime.now(timezone.utc)


def clamp_score(value: float, name: str) -> float:
    """Validate a 0.0-1.0 score (spec §49)."""
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a number: {value!r}") from exc
    if not 0.0 <= score <= 1.0:
        raise ValidationError(f"{name} must be within 0.0-1.0, got {score}")
    return score


@dataclass
class MemoryBase:
    """Fields shared by every memory record."""

    id: str
    user_id: str
    type: MemoryType
    content: str
    importance: float = 0.5
    confidence: float = 0.5
    source: str | None = None
    source_reference: str | None = None
    confirmed: bool = False
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None

    def validate(self) -> None:
        """Reject malformed records before they reach storage (spec §49)."""
        if not self.id:
            raise ValidationError("memory id must not be empty")
        if not self.user_id:
            raise ValidationError("memory user_id must not be empty")
        if not isinstance(self.type, MemoryType):
            raise ValidationError(f"unknown memory type: {self.type!r}")
        if not self.content or not self.content.strip():
            raise ValidationError("memory content must not be empty")
        clamp_score(self.importance, "importance")
        clamp_score(self.confidence, "confidence")
        for name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if not isinstance(value, datetime):
                raise ValidationError(f"{name} must be a datetime")
            if value.tzinfo is None:
                raise ValidationError(f"{name} must be timezone-aware")
        if self.expires_at is not None and not isinstance(self.expires_at, datetime):
            raise ValidationError("expires_at must be a datetime or None")


@dataclass
class Fact(MemoryBase):
    """A stable explicit fact stored authoritatively in SQLite (spec §5.1)."""

    category: str = "general"
    key: str = ""

    def __post_init__(self) -> None:
        self.type = MemoryType.FACT


@dataclass
class Preference(MemoryBase):
    """A user preference stored authoritatively in SQLite (spec §5.2)."""

    category: str = "general"
    key: str = ""

    def __post_init__(self) -> None:
        self.type = MemoryType.PREFERENCE


@dataclass
class Project(MemoryBase):
    """Structured project state in SQLite (spec §5.9, §10)."""

    name: str = ""
    status: str = "active"
    priority: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        self.type = MemoryType.STATE


@dataclass
class Goal(MemoryBase):
    """A structured objective stored in SQLite (spec §5.4, §11)."""

    title: str = ""
    status: str = "active"
    priority: int = 0
    project_id: str | None = None
    target_date: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        self.type = MemoryType.GOAL
        if not self.title:
            self.title = self.content


@dataclass
class Task(MemoryBase):
    """A structured actionable item stored in SQLite (spec §5.5, §12)."""

    title: str = ""
    status: str = "pending"
    priority: int = 0
    project_id: str | None = None
    goal_id: str | None = None
    due_date: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        self.type = MemoryType.TASK
        if not self.title:
            self.title = self.content


@dataclass
class SemanticMemory(MemoryBase):
    """A semantic/episodic memory stored in Chroma (spec §5.6-5.8, §16)."""

    topic: str = ""
    chroma_id: str | None = None

    def __post_init__(self) -> None:
        if self.type not in SEMANTIC_TYPES:
            self.type = MemoryType.EPISODIC


@dataclass
class MemoryResult:
    """One scored retrieval hit (spec §34)."""

    memory_id: str
    content: str
    memory_type: MemoryType
    storage_type: StorageType
    score: float = 0.0
    similarity: float = 0.0
    importance: float = 0.5
    confidence: float = 0.5
    source: str | None = None
    source_reference: str | None = None
    created_at: datetime | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class MemoryContext:
    """The assembled, token-bounded context block for the LLM (spec §36/§37)."""

    text: str
    facts: list[MemoryResult] = field(default_factory=list)
    semantic: list[MemoryResult] = field(default_factory=list)
    truncated: bool = False
    total_results: int = 0
