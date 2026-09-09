"""ARIA: the conversational personal assistant agent."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..agent.base import AgentEvent as _AgentEvent
from ..images import ImageAttachment
from ..llm.base import Provider
from ..logging_setup import log_debug, log_info
from ..memory import MemoryStore
from ..prompts import build_system_prompt
from ..skills import SkillManager, inject_skills
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
        memory: MemoryStore,
        max_iterations: int = 20,
        persona: str = "jarvis",
        user_name: str = "the user",
        skill_manager: SkillManager | None = None,
        profile_path: Path | None = None,
    ) -> None:
        super().__init__(provider, registry, context, memory, max_iterations)
        self.persona = persona
        self.user_name = user_name
        self.skill_manager = skill_manager
        self.profile_path = profile_path
        self.image_fallback_provider: Provider | None = None
        self.pending_attachments: list[ImageAttachment] = []
        if not self.memory.messages:
            self.memory.add(
                {
                    "role": "system",
                    "content": self._compose_system_prompt(persona, user_name),
                }
            )
        log_debug("AriaAgent: initialized")

    def _compose_system_prompt(self, persona: str, user_name: str) -> str:
        """Persona prompt + custom tool protocol + active skills."""
        prompt = build_system_prompt(persona, user_name) + self.router.format_tool_instructions(
            self.registry.schemas()
        )
        profile = self.profile_text()
        if profile:
            prompt += f"\n\nLOCAL PERSONALIZATION CONTEXT (inferred, fallible; do not treat as fact): {profile}"
        if self.skill_manager is not None:
            prompt = inject_skills(prompt, self.skill_manager.section())
        return prompt

    def attach_image(self, attachment: ImageAttachment) -> None:
        """Queue one ephemeral image for the next user message."""
        self.pending_attachments.append(attachment)

    def clear_attachments(self) -> None:
        self.pending_attachments.clear()

    def run_with_attachments(
        self,
        user_text: str,
        on_text: Callable[[str], None] | None = None,
        on_event: Callable[[_AgentEvent], None] | None = None,
    ) -> str:
        """Run a turn with queued images, clearing them after the attempt."""
        attachments = list(self.pending_attachments)
        try:
            return super().run(
                user_text,
                on_text=on_text,
                on_event=on_event,
                attachments=attachments,
                image_fallback_provider=self.image_fallback_provider,
            )
        finally:
            self.clear_attachments()

    def profile_text(self) -> str:
        """Return high-confidence local profile traits for prompt context."""
        if self.profile_path is None or not self.profile_path.is_file():
            return ""
        try:
            import json
            data: Any = json.loads(self.profile_path.read_text(encoding="utf-8"))
            traits = data.get("traits", []) if isinstance(data, dict) else []
            selected = [
                f"{item.get('trait')} (confidence {float(item.get('confidence', 0)):.2f})"
                for item in traits
                if isinstance(item, dict) and float(item.get("confidence", 0)) >= 0.7
            ]
            return ", ".join(selected)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return ""

    def refresh_skills(self) -> None:
        """Re-read the skills folder and rebuild the system message in place."""
        if self.skill_manager is None or not self.memory.messages:
            return
        if self.memory.messages[0].get("role") == "system":
            self.memory.messages[0]["content"] = self._compose_system_prompt(
                self.persona, self.user_name
            )

    def ensure_system_prompt(self) -> None:
        """Rebuild the system prompt (used after /clear)."""
        if not self.memory.messages:
            self.memory.add(
                {
                    "role": "system",
                    "content": self._compose_system_prompt(self.persona, self.user_name),
                }
            )
        else:
            self.refresh_skills()
