"""Relevance ranking (spec §34/§35).

Ranking is never similarity-only: every result is scored with configurable
weights over similarity, importance, confidence, recency, and access
frequency. Recency decay is per memory type (§35): permanent facts barely
decay, episodic memories fade faster.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import MemorySettings
from .models import MemoryResult, MemoryType, utc_now

# Per-type recency decay half-lives in days (spec §35: memory types specify
# their decay behavior). None means no decay.
_DECAY_HALF_LIFE_DAYS: dict[str, int | None] = {
    "fact": None,
    "preference": None,
    "state": None,
    "goal": None,
    "task": 60,
    "episodic": 90,
    "conversation": 60,
    "knowledge": None,
    "project_context": 120,
}


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


class Reranker:
    """Combined weighted scoring over retrieved candidates (spec §34)."""

    def __init__(self, settings: MemorySettings) -> None:
        self._settings = settings

    def score(self, result: MemoryResult) -> float:
        """Weighted score for one candidate (spec §34 formula)."""
        s = self._settings
        recency = self._recency(result)
        frequency = min(result.metadata.get("access_count", 0) / 10.0, 1.0)  # type: ignore[arg-type]
        score = (
            result.similarity * s.similarity_weight
            + result.importance * s.importance_weight
            + result.confidence * s.confidence_weight
            + recency * s.recency_weight
            + frequency * s.access_weight
        )
        return round(score, 4)

    def _recency(self, result: MemoryResult) -> float:
        """Type-aware time decay (spec §35)."""
        created = result.created_at or _parse_iso(result.metadata.get("created_at"))
        if created is None:
            return 0.5
        now = utc_now()
        age_days = max((now - created).total_seconds() / 86400.0, 0.0)
        memory_type = (
            result.memory_type.value if isinstance(result.memory_type, MemoryType) else str(result.memory_type)
        )
        half_life = _DECAY_HALF_LIFE_DAYS.get(memory_type, 90)
        if half_life is None:
            return 1.0
        return 0.5 ** (age_days / float(half_life))

    def rank(self, results: list[MemoryResult]) -> list[MemoryResult]:
        """Score and sort candidates; ties break toward newer (§34)."""
        for result in results:
            result.score = self.score(result)
        return sorted(results, key=lambda r: (r.score, r.created_at or utc_now().replace(tzinfo=None)), reverse=True)
