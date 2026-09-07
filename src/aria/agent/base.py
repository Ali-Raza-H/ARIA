"""Shared agent loop: model rounds, tool execution, memory."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..llm.base import AssistantMessage, Provider, ToolCall
from ..logging_setup import log_debug, log_error, log_info
from ..memory import MemoryStore, SessionMemory
from ..tools.base import ToolContext, ToolResult
from ..tools.registry import ToolRegistry
from ..tools.router import CustomToolRouter, RoutedResponse


@dataclass(frozen=True)
class AgentEvent:
    """One observable step of the agent loop, for live chain-of-thought UI."""

    kind: str  # "round" | "text" | "tool_call" | "tool_result" | "limit"
    round: int = 0
    name: str = ""
    detail: str = ""
    ok: bool = True


class BaseAgent:
    """Coordinate model responses, tool execution, and ephemeral memory."""

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        context: ToolContext,
        memory: MemoryStore,
        max_iterations: int = 20,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.context = context
        self.memory = memory
        self.max_iterations = max_iterations
        self.router = CustomToolRouter()
        # Optional hook: receives streaming text from sub-agent deployments
        # (set by the UI so the coder's progress can be shown live).
        self.coder_on_text: Callable[[str], None] | None = None
        log_debug(f"BaseAgent initialized: iterations={max_iterations} tools={registry.names()}")

    @property
    def model_name(self) -> str:
        """The model id of the current provider, when the adapter exposes one."""
        return str(getattr(self.provider, "model", "unknown"))

    def run(
        self,
        user_text: str,
        on_text: Callable[[str], None] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> str:
        """Process one user message until the model has no more tool calls.

        ``on_event`` receives every intermediate step (rounds, reasoning text,
        tool calls, tool results) so the UI can show the chain of thought.
        """

        def emit(event: AgentEvent) -> None:
            if on_event:
                on_event(event)

        log_info(f"BaseAgent.run: starting turn with {len(user_text)} chars of user input")
        prepare_context = getattr(self.memory, "prepare_context", None)
        if callable(prepare_context):
            prepare_context(user_text)
        self.registry.begin_turn()
        self.memory.add({"role": "user", "content": user_text})
        final_content = ""

        for iteration in range(self.max_iterations):
            emit(AgentEvent(kind="round", round=iteration + 1))
            log_debug(f"BaseAgent.run: iteration {iteration + 1}/{self.max_iterations}")

            # Providers may stream custom <tool_call> protocol markup. Forward
            # only the safe prefix and let the routed final response reconcile
            # the complete answer once the provider finishes.
            streamed_text = ""
            emitted_text = ""
            protocol_seen = False

            def on_provider_text(chunk: str) -> None:
                nonlocal streamed_text, emitted_text, protocol_seen
                if not chunk:
                    return
                streamed_text += chunk
                if not on_text or protocol_seen:
                    return
                marker_positions = [
                    position
                    for position in (streamed_text.find("<tool_call"), streamed_text.find("<tool_result"))
                    if position >= 0
                ]
                if marker_positions:
                    protocol_seen = True
                    safe_prefix = streamed_text[: min(marker_positions)]
                    if len(safe_prefix) > len(emitted_text):
                        new_text = safe_prefix[len(emitted_text) :]
                        emitted_text = safe_prefix
                        on_text(new_text)
                    return
                emitted_text += chunk
                on_text(chunk)

            response = self.provider.complete(
                self._messages_for_provider(),
                self.registry.schemas(),
                on_text=on_provider_text,
            )
            routed = self.router.parse(response.content)
            calls = [*response.tool_calls, *routed.tool_calls]
            self.memory.add(self._assistant_message(response, routed))
            final_content = routed.content
            if on_text and routed.content:
                # Native providers already emitted their safe prefix. Emit the
                # full routed content only when no safe prefix was streamed;
                # custom protocol responses are otherwise emitted here once.
                if not streamed_text:
                    on_text(routed.content)
                elif protocol_seen and routed.content.startswith(emitted_text):
                    remainder = routed.content[len(emitted_text) :]
                    if remainder:
                        on_text(remainder)
                elif protocol_seen and routed.content != emitted_text:
                    on_text(routed.content)
            if routed.content:
                emit(AgentEvent(kind="text", round=iteration + 1, detail=routed.content))

            if not calls and not routed.errors:
                log_info("BaseAgent.run: turn complete")
                complete_turn = getattr(self.memory, "turn_completed", None)
                if callable(complete_turn):
                    complete_turn(user_text, final_content)
                return final_content

            native_results: list[dict[str, Any]] = []
            custom_results: list[tuple[ToolCall, ToolResult]] = []
            native_call_ids = {id(call) for call in response.tool_calls}
            for call in calls:
                emit(
                    AgentEvent(
                        kind="tool_call",
                        round=iteration + 1,
                        name=call.name,
                        detail=json.dumps(call.arguments, ensure_ascii=True),
                    )
                )
                log_debug(f"BaseAgent.run: executing tool '{call.name}'")
                result = self.registry.execute(call.name, call.arguments, self.context)
                emit(
                    AgentEvent(
                        kind="tool_result",
                        round=iteration + 1,
                        name=call.name,
                        detail=result.output,
                        ok=not result.is_error,
                    )
                )
                if id(call) in native_call_ids:
                    native_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": result.output,
                        }
                    )
                else:
                    custom_results.append((call, result))
            if native_results:
                self.memory.extend(native_results)
            if custom_results or routed.errors:
                self.memory.add(
                    {
                        "role": "user",
                        "content": self.router.format_results(custom_results, routed.errors),
                    }
                )
            for error in routed.errors:
                emit(AgentEvent(kind="tool_result", round=iteration + 1, name="tool_router", detail=error, ok=False))

        message = (
            f"Stopped after reaching the {self.max_iterations}-iteration safety limit. "
            "Review the work so far and tell me how to continue."
        )
        log_error("BaseAgent.run: hit the iteration safety limit")
        emit(AgentEvent(kind="limit", detail=message))
        self.memory.add({"role": "assistant", "content": message})
        complete_turn = getattr(self.memory, "turn_completed", None)
        if callable(complete_turn):
            complete_turn(user_text, message)
        return message

    def _messages_for_provider(self, max_chars: int = 80_000) -> list[dict[str, Any]]:
        """Keep requests bounded even when a session remains open for days.

        The durable memory layer supplies retrieved continuity; sending every
        historic tool payload again is both expensive and a common cause of
        context-window failures. Keep the system prompt and newest complete
        messages, replacing dropped history with an explicit notice.
        """
        messages = self.memory.messages
        if sum(len(str(message.get("content", ""))) for message in messages) <= max_chars:
            return list(messages)
        system = messages[:1] if messages and messages[0].get("role") == "system" else []
        kept: list[dict[str, Any]] = []
        used = sum(len(str(message.get("content", ""))) for message in system)
        for message in reversed(messages[len(system) :]):
            size = len(str(message.get("content", "")))
            if kept and used + size > max_chars:
                break
            kept.append(message)
            used += size
        notice = {"role": "system", "content": "Earlier turn/tool details were compacted; use persistent memory or tools to re-check facts."}
        return [*system, notice, *reversed(kept)]

    @staticmethod
    def _assistant_message(
        response: AssistantMessage, routed: RoutedResponse
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in response.tool_calls
            ]
        if routed.tool_calls or routed.errors:
            message["custom_tool_calls"] = [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in routed.tool_calls
            ]
            if routed.errors:
                message["custom_tool_errors"] = routed.errors
        return message
