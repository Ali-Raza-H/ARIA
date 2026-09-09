"""Ephemeral JSON conversation memory."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Protocol

from ..logging.setup import log_debug, log_error


class MemoryStore(Protocol):
    """Minimal message-store contract shared by ARIA and the coder agent."""

    messages: list[dict[str, Any]]
    @property
    def path(self) -> Path: ...

    def add(self, message: dict[str, Any]) -> None: ...

    def extend(self, messages: list[dict[str, Any]]) -> None: ...

    def cleanup(self) -> None: ...


class SessionMemory:
    """Keep provider messages in a temporary JSON file for the active session."""

    def __init__(self, directory: Path, label: str = "aria") -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{label}-session-{uuid.uuid4().hex}.json"
        self.messages: list[dict[str, Any]] = []
        self._persist()

    def add(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self._persist()

    def extend(self, messages: list[dict[str, Any]]) -> None:
        self.messages.extend(messages)
        self._persist()

    def _persist(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(self.messages, ensure_ascii=True, indent=2), encoding="utf-8"
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except OSError as exc:
            log_error(f"SessionMemory: persist failed for {self.path.name}: {exc}")

    def cleanup(self) -> None:
        """Delete session data; cleanup is intentionally best effort."""
        try:
            self.path.unlink(missing_ok=True)
            self.path.with_suffix(".tmp").unlink(missing_ok=True)
            log_debug(f"SessionMemory: cleaned up {self.path.name}")
        except OSError as exc:
            log_error(f"SessionMemory: cleanup failed for {self.path.name}: {exc}")
