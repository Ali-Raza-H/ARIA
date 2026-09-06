"""ARIA: the conversational personal assistant agent."""

from __future__ import annotations

from collections.abc import Callable

from ..agent.base import AgentEvent as _AgentEvent
from ..llm.base import Provider
from ..logging_setup import log_debug, log_info
from ..memory import SessionMemory
from ..prompts import build_system_prompt
from ..tools.base import Tool, ToolContext, ToolResult
from ..tools.registry import ToolRegistry
from .base import BaseAgent
from .coder import CoderReport, CoderService


class DeployCoderTool:
    """The bridge tool ARIA uses to hand work to the independent coding agent."""

    def __init__(
        self,
        coder_service: CoderService,
        on_text: Callable[[str], None] | None = None,
        on_event: Callable[[_AgentEvent], None] | None = None,
    ) -> None:
        self._coder_service = coder_service
        self._on_text = on_text
        self._on_event = on_event
        self.last_report: CoderReport | None = None

    def set_stream_hook(
        self,
        on_text: Callable[[str], None] | None = None,
        on_event: Callable[[_AgentEvent], None] | None = None,
    ) -> None:
        """Receive the coder's streaming output and events while it works."""
        self._on_text = on_text
        self._on_event = on_event

    def __call__(self, arguments: dict[str, object], context: ToolContext) -> ToolResult:
        task = arguments.get("task")
        if not isinstance(task, str) or not task.strip():
            return ToolResult("task must be a non-empty description of the work", is_error=True)
        log_info(f"DeployCoderTool: ARIA deployed the coder ({len(task)} chars)")
        report = self._coder_service.deploy(
            task, context, on_text=self._on_text, on_event=self._on_event
        )
        self.last_report = report
        if report.ok:
            return ToolResult(report.summary)
        return ToolResult(report.summary, is_error=True)


def register_deploy_coder_tool(
    registry: ToolRegistry, coder_service: CoderService
) -> DeployCoderTool:
    handler = DeployCoderTool(coder_service)
    registry.register(
        Tool(
            name="deploy_coder",
            description=(
                "Deploy the independent coding agent to perform substantial work on this machine: "
                "multi-file edits, large refactors, running builds or tests, installing things, or "
                "any long autonomous task. Pass a complete, self-contained description of the task; "
                "the coder cannot ask the user questions. Returns the coder's final report."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Full, unambiguous task brief for the coder"}
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            handler=handler,
        )
    )
    return handler


class AriaAgent(BaseAgent):
    """The assistant the user talks to."""

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        context: ToolContext,
        memory: SessionMemory,
        max_iterations: int = 20,
        persona: str = "jarvis",
        user_name: str = "the user",
    ) -> None:
        super().__init__(provider, registry, context, memory, max_iterations)
        self.persona = persona
        self.user_name = user_name
        if not self.memory.messages:
            self.memory.add(
                {
                    "role": "system",
                    "content": build_system_prompt(persona, user_name)
                    + self.router.format_tool_instructions(self.registry.schemas()),
                }
            )
        log_debug("AriaAgent: initialized")

    def ensure_system_prompt(self) -> None:
        """Rebuild the system prompt (used after /clear)."""
        if not self.memory.messages:
            self.memory.add(
                {
                    "role": "system",
                    "content": build_system_prompt(self.persona, self.user_name)
                    + self.router.format_tool_instructions(self.registry.schemas()),
                }
            )
