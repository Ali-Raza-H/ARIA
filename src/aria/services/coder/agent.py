"""The independent coding agent ARIA deploys for heavy work.

The coder runs with its own provider session, its own memory, and a much
higher iteration budget, so massive multi-file operations never consume or
distort ARIA's conversational context. ARIA only receives the final report.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast

from ...llm.base import Provider
from ...logging.setup import log_debug, log_error, log_info
from ...memory import SessionMemory
from ...prompts import build_coder_prompt
from ...tools.core.base import ToolContext
from ...tools.core.registry import ToolRegistry
from ...telemetry import TelemetryRecorder
from ...tools.filesystem import register_filesystem_tools
from ...tools.shell import register_shell_tool
from ...core.agent.base import AgentEvent, BaseAgent


class ProviderFactory(Protocol):
    """Anything that can build a provider from a name and model id."""

    def create(self, provider: str, model: str) -> Provider: ...


@dataclass(frozen=True)
class CoderReport:
    """Structured result returned to ARIA after a deployment."""

    ok: bool
    summary: str
    elapsed_seconds: float
    iterations_used: int = 0
    error: str | None = None
    details: dict = field(default_factory=dict)


class CoderAgent(BaseAgent):
    """A self-contained agent specialized in coding and system tasks."""

    def __init__(
        self,
        provider: Provider,
        context: ToolContext,
        max_iterations: int = 60,
        user_name: str = "the user",
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        registry = ToolRegistry()
        register_filesystem_tools(registry)
        register_shell_tool(registry)
        memory = SessionMemory(Path("data/sessions"), label="aria-coder")  # relative to cwd
        super().__init__(provider, registry, context, memory, max_iterations, telemetry=telemetry, telemetry_role="coder")
        # Replace the default empty system prompt with the coder persona.
        self.memory.messages.clear()
        self.memory.add(
            {
                "role": "system",
                "content": build_coder_prompt(user_name)
                + self.router.format_tool_instructions(self.registry.schemas()),
            }
        )
        log_debug("CoderAgent: initialized with filesystem + shell tools")


class CoderService:
    """Builds and runs CoderAgents on demand, one deployment at a time."""

    def __init__(
        self,
        provider_manager: ProviderFactory,
        provider_name: str,
        model: str,
        max_iterations: int = 60,
        max_output_chars: int | None = None,
        user_name: str = "the user",
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        self.provider_manager = provider_manager
        self.provider_name = provider_name
        self.model = model
        self.max_iterations = max_iterations
        self.max_output_chars = max_output_chars
        self._user_name = user_name
        self.telemetry = telemetry
        self._lock = threading.Lock()
        self._active: CoderAgent | None = None
        log_debug(f"CoderService: initialized provider={provider_name} model={model}")

    def set_model(self, provider_name: str, model: str) -> None:
        self.provider_name = provider_name
        self.model = model
        log_info(f"CoderService: backend set to {provider_name}/{model}")

    def is_busy(self) -> bool:
        return self._lock.locked()

    def provider_label(self) -> str:
        return f"{self.provider_name} / {self.model}"

    def deploy(
        self,
        task: str,
        context: ToolContext,
        on_text: Callable[[str], None] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> CoderReport:
        """Run *task* in an independent agent and return its report to ARIA.

        ``on_event`` receives the coder's AgentEvent stream so the UI can show
        its execution trace while it works.
        """
        log_info(f"CoderService: deployment requested ({len(task)} chars of instructions)")
        acquired = self._lock.acquire(timeout=1.0)
        if not acquired:
            log_error("CoderService: a deployment is already running")
            return CoderReport(
                ok=False,
                summary="A coding deployment is already running. Wait for it to finish.",
                elapsed_seconds=0.0,
                error="busy",
            )
        started = time.monotonic()
        try:
            create_coder = getattr(self.provider_manager, "create_coder", None)
            if callable(create_coder):
                provider = cast(Provider, create_coder(self.provider_name, self.model))
            else:
                # Small test doubles and third-party factories from older
                # integrations may only expose create(). The real
                # ProviderManager always takes the dedicated coder path.
                provider = self.provider_manager.create(self.provider_name, self.model)
            coder_context = replace(
                context,
                max_command_output_chars=(
                    self.max_output_chars
                    if self.max_output_chars is not None
                    else context.max_command_output_chars
                ),
            )
            agent = CoderAgent(
                provider,
                coder_context,
                max_iterations=self.max_iterations,
                user_name=self._user_name,
                telemetry=self.telemetry,
            )
            self._active = agent
            summary = agent.run(task, on_text=on_text, on_event=on_event)
            elapsed = time.monotonic() - started
            log_info(f"CoderService: deployment finished in {elapsed:.1f}s")
            return CoderReport(ok=True, summary=summary, elapsed_seconds=elapsed)
        except Exception as exc:
            elapsed = time.monotonic() - started
            log_error(f"CoderService: deployment failed: {type(exc).__name__}: {exc}")
            return CoderReport(
                ok=False,
                summary=f"The coding agent failed: {type(exc).__name__}: {exc}",
                elapsed_seconds=elapsed,
                error=str(exc),
            )
        finally:
            if self._active is not None:
                self._active.memory.cleanup()
                self._active = None
            self._lock.release()
