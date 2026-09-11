"""Shell command tool."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from ...logging.setup import log_debug, log_info
from ..core.base import Tool, ToolContext, ToolResult
from ..core.registry import ToolRegistry


def _run_shell(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return ToolResult("command must be a non-empty string", is_error=True)
    blocked = context.blocked_launch_commands
    first_token = command.strip().split(maxsplit=1)[0]
    if first_token in blocked:
        return ToolResult(
            f"'{first_token}' is a configured desktop launcher. Use desktop_launch "
            "with its named route instead of run_shell_command.",
            is_error=True,
        )

    shell_executable = os.environ.get("COMSPEC") if os.name == "nt" else os.environ.get("SHELL")
    # Tools do not need provider credentials. Do not make `env` an API-key
    # exfiltration primitive for the model or put those values in its context.
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION"))
    }
    timeout = context.command_timeout_seconds or 60.0
    log_debug(f"Shell: running {command!r} in {context.workspace}")
    try:
        completed = subprocess.run(
            command,
            shell=True,
            executable=shell_executable,
            cwd=context.workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env,
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
            f"Command timed out after {timeout}s: {exc.cmd}",
            is_error=True,
        )


def register_shell_tool(registry: ToolRegistry, blocked_launch_commands: tuple[str, ...] = ()) -> None:
    """Register shell access while reserving configured GUI launch commands."""
    def handler(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return _run_shell(arguments, context)

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
            handler=handler,
        )
    )
