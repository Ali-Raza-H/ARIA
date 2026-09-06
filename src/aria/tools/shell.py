"""Shell command tool."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from ..logging_setup import log_debug, log_info
from .base import Tool, ToolContext, ToolResult
from .registry import ToolRegistry


def _run_shell(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return ToolResult("command must be a non-empty string", is_error=True)

    shell_executable = os.environ.get("COMSPEC") if os.name == "nt" else os.environ.get("SHELL")
    log_debug(f"Shell: running {command!r} in {context.workspace}")
    try:
        completed = subprocess.run(
            command,
            shell=True,
            executable=shell_executable,
            cwd=context.workspace,
            capture_output=True,
            text=True,
            timeout=context.command_timeout_seconds,
            env=os.environ.copy(),
        )
        output = completed.stdout
        if completed.stderr:
            output += ("\n" if output else "") + completed.stderr
        output = output or "(no output)"
        if context.max_command_output_chars is not None:
            limit = context.max_command_output_chars
            if len(output) > limit:
                output = output[:limit] + f"\n[output truncated at {limit} characters]"
        log_info(f"Shell: exit_code={completed.returncode} for {command[:80]!r}")
        prefix = f"exit_code={completed.returncode}\n"
        return ToolResult(prefix + output, is_error=completed.returncode != 0)
    except subprocess.TimeoutExpired as exc:
        return ToolResult(
            f"Command timed out after {context.command_timeout_seconds}s: {exc.cmd}",
            is_error=True,
        )


def register_shell_tool(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="run_shell_command",
            description="Run a command using the operating system's current shell in the workspace.",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=_run_shell,
        )
    )
