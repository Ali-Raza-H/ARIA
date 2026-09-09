"""Structured memory exceptions.

Memory failures must never crash the assistant (spec §43/§44): callers catch
:class:`MemoryError` and continue in degraded mode. Every failure path in the
memory subsystem raises one of these; nothing else escapes on purpose.
"""

from __future__ import annotations


class MemoryError(RuntimeError):
    """Base class for memory subsystem failures."""


class StorageUnavailableError(MemoryError):
    """A storage backend (SQLite or Chroma) could not be reached or initialized."""


class ValidationError(MemoryError):
    """A memory record failed validation (bad scores, unknown type, bad timestamps)."""


class ConflictError(MemoryError):
    """A structured-memory update conflicts with existing authoritative state."""
