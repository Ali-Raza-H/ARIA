"""Confirmation policy for disruptive local and remote actions."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PendingConfirmation:
    tool_name: str
    arguments_json: str
    description: str


class ConfirmationManager:
    """Allow explicit current-turn intent and otherwise require one reply."""

    _AFFIRMATIVE = re.compile(r"^\s*(yes|y|confirm|confirmed|do it|go ahead|proceed|okay|ok)\s*[.!]*\s*$", re.I)

    def __init__(self) -> None:
        self._pending: PendingConfirmation | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {token.lower() for token in re.findall(r"[a-zA-Z0-9_.@:/-]+", value)}

    def _direct_intent(self, request: str, action: str, target: str) -> bool:
        """Conservative conversational-intent match for the current request."""
        request_lower = request.lower()
        action_words = {
            "send": ("send", "mail", "email"),
            "reply": ("reply", "respond"),
            "forward": ("forward",),
            "trash": ("trash", "delete", "remove"),
            "delete": ("permanently delete", "delete forever", "purge"),
            "modify": ("label", "archive", "star", "mark", "move"),
            "start": ("start", "bring up", "launch"),
            "stop": ("stop", "shut down", "bring down", "halt"),
            "restart": ("restart", "reboot", "bounce"),
            "reload": ("reload",),
            "enable": ("enable",),
            "disable": ("disable",),
            "pause": ("pause",),
            "unpause": ("unpause", "resume"),
            "remove": ("remove", "delete", "prune"),
            "exec": ("exec", "execute", "run inside"),
            "pull": ("pull", "download image"),
            "compose": ("compose",),
        }
        if not any(word in request_lower for word in action_words.get(action, (action,))):
            return False
        target_tokens = {token for token in self._tokens(target) if len(token) > 1}
        if not target_tokens:
            return True
        request_tokens = self._tokens(request)
        return bool(target_tokens & request_tokens) or any(token in request_lower for token in target_tokens)

    def allow(self, tool_name: str, arguments: dict[str, Any], description: str, request: str) -> bool:
        arguments_json = json.dumps(arguments, sort_keys=True, ensure_ascii=True)
        with self._lock:
            pending = self._pending
            if pending is not None:
                if self._AFFIRMATIVE.match(request) and pending.tool_name == tool_name and pending.arguments_json == arguments_json:
                    self._pending = None
                    return True
                # A new non-affirmative request replaces stale pending state.
                self._pending = None
            action = str(arguments.get("action") or arguments.get("operation") or "")
            # Some explicit tools (gmail_send, gmail_trash, media_launch) do
            # not need an action property in their schema. Infer the verb from
            # the tool name so a clear conversational request can bypass the
            # extra prompt without making inferred/model actions implicit.
            if not action and tool_name.startswith("gmail_"):
                action = tool_name.removeprefix("gmail_")
            if not action and tool_name.startswith("media_"):
                action = tool_name.removeprefix("media_")
            target = str(
                arguments.get("service")
                or arguments.get("container")
                or arguments.get("container_name")
                or arguments.get("to")
                or arguments.get("message_id")
                or arguments.get("thread_id")
                or arguments.get("image")
                or arguments.get("project")
                or arguments.get("command")
                or ""
            )
            if action and self._direct_intent(request, action.lower(), target):
                return True
            self._pending = PendingConfirmation(tool_name, arguments_json, description)
            return False

    def pending_description(self) -> str | None:
        with self._lock:
            return self._pending.description if self._pending else None

    def require(self, tool_name: str, arguments: dict[str, Any], description: str, request: str) -> str | None:
        if self.allow(tool_name, arguments, description, request):
            return None
        return (
            "CONFIRMATION_REQUIRED: " + description + ". Ask the user to confirm explicitly, "
            "then repeat the exact same tool call after they confirm."
        )
