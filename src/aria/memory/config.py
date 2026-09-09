"""Memory subsystem configuration (spec §51).

Extends the existing :class:`~aria.config.MemoryConfig` with the new
settings; paths are resolved by the manager so nothing is hard-coded.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace as dataclass_replace
from pathlib import Path

from ..config import MemoryConfig


@dataclass(frozen=True)
class MemorySettings:
    """Everything the memory subsystem needs, derived from MemoryConfig."""

    # Core paths (spec §51: no hard-coded paths)
    root: Path = Path(".")
    sqlite_path: Path = Path("data/memory/assistant.db")
    chroma_path: Path = Path("data/memory/chroma")

    # Retrieval (spec §30/§37)
    max_results: int = 10
    max_memory_tokens: int = 5000

    # Ranking weights (spec §34; all configurable)
    similarity_weight: float = 0.45
    importance_weight: float = 0.25
    confidence_weight: float = 0.20
    recency_weight: float = 0.10
    access_weight: float = 0.05

    # Deduplication thresholds (spec §27; configurable, not universal truths)
    duplicate_threshold: float = 0.90
    related_threshold: float = 0.75

    # Embeddings (spec §17: configurable, replaceable provider)
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key_env: str = "MEMORY_EMBEDDING_API_KEY"
    embedding_ollama_host: str = "http://127.0.0.1:11434"
    embedding_fallback_models: tuple[str, ...] = (
        "qwen3-embedding:0.6b",
        "nomic-embed-text",
        "mxbai-embed-large",
        "bge-m3",
        "snowflake-arctic-embed",
    )

    # Extraction / classification
    extraction_enabled: bool = True
    min_importance_to_store: float = 0.25

    # Lifecycle (spec §39)
    lifecycle_enabled: bool = True
    episodic_retention_days: int = 180
    conversation_retention_days: int = 365
    knowledge_retention_days: int = 0  # 0 = never expires
    decay_half_life_days: int = 90

    # Query classification keywords (spec §70: query-aware retrieval)
    structured_keywords: tuple[str, ...] = (
        "what is my",
        "what are my",
        "current",
        "my tasks",
        "my goals",
        "my projects",
        "incomplete",
        "pending",
        "my preferred",
        "status",
        "list my",
    )
    semantic_keywords: tuple[str, ...] = (
        "why",
        "how did",
        "what did i",
        "history",
        "previously",
        "remember when",
        "compare",
        "originally",
        "before",
        "used to",
    )

    @classmethod
    def from_config(cls, config: MemoryConfig, root: Path) -> "MemorySettings":
        """Build settings from the existing MemoryConfig plus new fields.

        Legacy fields keep their meaning: ``directory`` becomes the sqlite
        parent directory, ``context_token_budget`` maps to max_memory_tokens,
        and the rank_* weights map onto the new weight names.
        """
        sqlite_path = config.directory / "assistant.db" if config.directory else Path("data/memory/assistant.db")
        return cls(
            root=root,
            sqlite_path=sqlite_path,
            chroma_path=config.chroma_directory,
            max_memory_tokens=config.context_token_budget,
            similarity_weight=config.rank_similarity,
            recency_weight=config.rank_recency,
            importance_weight=config.rank_importance,
            confidence_weight=config.rank_confidence,
            access_weight=config.rank_frequency,
            duplicate_threshold=0.90,
            related_threshold=0.75,
            extraction_enabled=True,
            lifecycle_enabled=True,
            episodic_retention_days=config.episodic_retention_days,
            conversation_retention_days=config.conversation_retention_days,
            knowledge_retention_days=config.knowledge_retention_days,
            embedding_base_url=config.embedding_base_url,
            embedding_model=config.embedding_model,
            embedding_api_key_env=config.embedding_api_key_env,
            embedding_ollama_host=config.embedding_ollama_host,
            embedding_fallback_models=config.embedding_fallback_models,
        )

    def with_overrides(self, **overrides: object) -> "MemorySettings":
        """Return a copy with validated overrides (spec §49)."""
        valid = {f.name for f in fields(self)}
        unknown = set(overrides) - valid
        if unknown:
            raise ValueError(f"unknown memory settings: {', '.join(sorted(unknown))}")
        return dataclass_replace(self, **overrides)  # type: ignore[arg-type]
