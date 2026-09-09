"""LLM-callable memory tools (spec §71/§41).

The assistant's orchestration layer retrieves memory automatically each turn;
these tools cover the explicit cases: the user says "remember that...", asks
what ARIA remembers, requests a lookup, or asks to forget. Raw tool output is
never auto-stored (§63) — only deliberate saves land in long-term memory.

The MemoryManager is captured in the handler closures at registration time,
keeping the ToolContext dataclass untouched (spec §3: no core coupling).
"""

from __future__ import annotations

from typing import Any

from ..tools.core.base import Tool, ToolContext, ToolResult
from ..tools.core.registry import ToolRegistry
from .manager import MemoryManager


def _remember_handler(manager: MemoryManager):
    def handler(arguments: dict[str, Any], _context: ToolContext) -> ToolResult:
        """remember_fact: store an explicit, confirmed memory (§24)."""
        content = str(arguments.get("content", "")).strip()
        if not content:
            return ToolResult("content is required", is_error=True)
        memory_type = str(arguments.get("memory_type", "fact")).strip().lower()
        if memory_type not in {"fact", "preference", "state", "goal", "task", "episodic"}:
            memory_type = "fact"
        key = str(arguments.get("key", "")).strip()
        value = str(arguments.get("value", "")).strip()
        topic = str(arguments.get("topic", "")).strip()
        memory_id = manager.remember(
            content,
            memory_type,
            importance=0.9,
            confidence=1.0,
            confirmed=True,
            topic=topic,
            key=key,
            value=value or content,
            source="user_explicit",
        )
        if memory_id is None:
            return ToolResult("Could not store the memory (storage unavailable).", is_error=True)
        return ToolResult(f"Remembered ({memory_type}, id {memory_id}).")

    return handler


def _search_handler(manager: MemoryManager):
    def handler(arguments: dict[str, Any], _context: ToolContext) -> ToolResult:
        """search_memory: hybrid retrieval over all memory."""
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolResult("query is required", is_error=True)
        results = manager.search(query, limit=8)
        if not results:
            return ToolResult("No memories matched that query.")
        lines = [
            f"- [{item['score']:.2f}] ({item['metadata']['memory_type']}) {item['document'][:200]}"
            for item in results
        ]
        return ToolResult("\n".join(lines))

    return handler


def _list_handler(manager: MemoryManager):
    def handler(_arguments: dict[str, Any], _context: ToolContext) -> ToolResult:
        """list_memories: what do I remember about the user (§41)."""
        lines: list[str] = ["STRUCTURED"]
        lines.extend(f"- {line}" for line in manager.facts_text().splitlines()[:20])
        recent = manager.list_recent(limit=10)
        if recent:
            lines.append("RECENT SEMANTIC")
            lines.extend(f"- ({item['type']}) {item['content'][:140]}" for item in recent)
        return ToolResult("\n".join(lines))

    return handler


def _forget_handler(manager: MemoryManager):
    def handler(arguments: dict[str, Any], _context: ToolContext) -> ToolResult:
        """forget_memory: delete by id or topic (§41/§67)."""
        target = str(arguments.get("memory_id_or_topic", "")).strip()
        if not target:
            return ToolResult("memory_id_or_topic is required", is_error=True)
        count = manager.forget(target)
        return ToolResult(f"Forgot {count} memory item(s) matching '{target}'.")

    return handler


def _confirm_handler(manager: MemoryManager):
    def handler(arguments: dict[str, Any], _context: ToolContext) -> ToolResult:
        """confirm_memory: raise a memory to confirmed/confidence 1.0 (§68)."""
        memory_id = str(arguments.get("memory_id", "")).strip()
        if not memory_id:
            return ToolResult("memory_id is required", is_error=True)
        ok = manager.confirm(memory_id)
        return ToolResult("Memory confirmed." if ok else "Memory id not found.", is_error=not ok)

    return handler


def register_memory_tools(registry: ToolRegistry, manager: MemoryManager) -> None:
    """Register the five memory tools, closing over the manager instance."""
    registry.register(
        Tool(
            name="remember_fact",
            description=(
                "Store a lasting memory the user explicitly asked to remember "
                "(preferences, facts about the user or their projects, decisions). "
                "Do NOT use for greetings, trivial chatter, or one-off tool output."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The memory in one or two sentences"},
                    "memory_type": {
                        "type": "string",
                        "enum": ["fact", "preference", "state", "goal", "task", "episodic"],
                        "description": "Memory category (default fact)",
                    },
                    "key": {"type": "string", "description": "Optional structured key, e.g. preferred_editor"},
                    "value": {"type": "string", "description": "Optional structured value, e.g. Neovim"},
                    "topic": {"type": "string", "description": "Optional topic label, e.g. LifeOS"},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            handler=_remember_handler(manager),
        )
    )
    registry.register(
        Tool(
            name="search_memory",
            description=(
                "Search long-term memory (facts, preferences, project state, history, "
                "ingested documents) for information relevant to the current question."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for"}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_search_handler(manager),
        )
    )
    registry.register(
        Tool(
            name="list_memories",
            description="List everything remembered about the user: structured facts plus recent semantic memories.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_list_handler(manager),
        )
    )
    registry.register(
        Tool(
            name="forget_memory",
            description="Delete memories by id or by topic text when the user asks to forget something.",
            parameters={
                "type": "object",
                "properties": {
                    "memory_id_or_topic": {"type": "string", "description": "A memory id or topic text to forget"}
                },
                "required": ["memory_id_or_topic"],
                "additionalProperties": False,
            },
            handler=_forget_handler(manager),
        )
    )
    registry.register(
        Tool(
            name="confirm_memory",
            description="Confirm an inferred memory as true after the user validates it (raises confidence to 1.0).",
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "The memory id to confirm"}
                },
                "required": ["memory_id"],
                "additionalProperties": False,
            },
            handler=_confirm_handler(manager),
        )
    )
