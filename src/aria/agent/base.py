"""Shared agent loop: model rounds, tool execution, memory."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..images import ImageAttachment, prepare_image_message
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
        self._active_attachments: list[ImageAttachment] = []
        self.tool_attachments: list[ImageAttachment] = []
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
        attachments: list[ImageAttachment] | None = None,
        image_fallback_provider: Provider | None = None,
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
        self._active_attachments = []
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

            request_messages = self._messages_for_provider()
            current_attachments = [*(attachments or [])] if iteration == 0 else []
            current_attachments.extend(self._active_attachments)
            if current_attachments:
                request_messages = self._messages_for_provider(
                    image_content=prepare_image_message(
                        self.provider,
                        user_text,
                        current_attachments,
                        fallback_provider=image_fallback_provider,
                    )
                )
            response = self.provider.complete(
                request_messages,
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
                if self.tool_attachments:
                    self._active_attachments.extend(self.tool_attachments)
                    self.tool_attachments.clear()
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
        self._active_attachments = []
        return message

    @staticmethod
    def _message_size(message: dict[str, Any]) -> int:
        """Approximate request size of one message, including tool calls.

        An assistant message's ``tool_calls`` payload also reaches the wire, so
        it must count toward the compaction budget; measuring only ``content``
        used to let a turn be split right after the assistant message.
        """
        size = len(str(message.get("content", "")))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            size += len(json.dumps(tool_calls, ensure_ascii=True))
        return size

    @staticmethod
    def _turn_boundary(messages: list[dict[str, Any]], start: int) -> int:
        """Return the exclusive end of the turn that starts at index *start*.

        A turn is one user message, or one assistant message together with the
        tool results that answer it. Truncation must never split a turn:
        sending a ``tool`` message whose matching assistant ``tool_calls``
        message was dropped makes the request invalid ("Unexpected role 'tool'
        after role 'system'").
        """
        role = messages[start].get("role")
        end = start + 1
        if role == "assistant" and messages[start].get("tool_calls"):
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
        return end

    def _messages_for_provider(
        self,
        max_chars: int = 80_000,
        image_content: str | list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Keep requests bounded even when a session remains open for days.

        The durable memory layer supplies retrieved continuity; sending every
        historic tool payload again is both expensive and a common cause of
        context-window failures. Keep the system prompt and the newest turns
        that fit the budget, replacing dropped history with an explicit notice
        merged into the system message (never a separate system message before
        a tool result, which OpenAI-compatible endpoints reject).
        """
        messages = self.memory.messages
        if sum(self._message_size(message) for message in messages) <= max_chars:
            result = list(messages)
            if image_content is not None:
                self._attach_image_content(result, image_content)
            return result
        system = messages[:1] if messages and messages[0].get("role") == "system" else []
        notice = "\n\nEarlier turn/tool details were compacted; use persistent memory or tools to re-check facts."
        if system:
            head: list[dict[str, Any]] = [
                {**system[0], "content": str(system[0].get("content", "")) + notice}
            ]
        else:
            head = [{"role": "system", "content": notice.strip()}]
        body = messages[len(system) :]
        kept: list[dict[str, Any]] = []
        used = sum(self._message_size(message) for message in head)
        index = len(body)
        while index > 0:
            # One turn = user message, or assistant + its tool results.
            if body[index - 1].get("role") == "tool":
                # Find the assistant message that owns the trailing tool results.
                owner = index - 1
                while owner > 0 and body[owner - 1].get("role") == "tool":
                    owner -= 1
                owner -= 1
                turn_start = owner if owner >= 0 and body[owner].get("role") == "assistant" else index - 1
            else:
                turn_start = index - 1
            turn = body[turn_start:index]
            turn_size = sum(self._message_size(message) for message in turn)
            if kept and used + turn_size > max_chars:
                break
            kept[:0] = turn
            used += turn_size
            index = turn_start
        result = [*head, *kept]
        if image_content is not None:
            self._attach_image_content(result, image_content)
        return result

    @staticmethod
    def _attach_image_content(messages: list[dict[str, Any]], image_content: str | list[dict[str, Any]]) -> None:
        """Attach images without overwriting a tool-result user message."""
        if messages and messages[-1].get("role") == "user" and isinstance(messages[-1].get("content"), str):
            # A custom-protocol tool result is also represented as a user
            # message. If the previous message is an assistant response, keep
            # that result and append a distinct image turn.
            if len(messages) < 2 or messages[-2].get("role") != "assistant":
                messages[-1] = {**messages[-1], "content": image_content}
                return
        messages.append({"role": "user", "content": image_content})

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
