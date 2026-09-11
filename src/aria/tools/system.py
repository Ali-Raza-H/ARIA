"""Explicit local media, system, service, and Docker controls."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from ..config import DockerConfig, MediaConfig, SystemConfig
from ..logging.setup import log_error
from .core.base import Tool, ToolContext, ToolResult
from .core.confirmation import ConfirmationManager
from .core.registry import ToolRegistry


class SystemToolService:
    def __init__(self, system: SystemConfig, media: MediaConfig, docker: DockerConfig, confirmation: ConfirmationManager | None = None) -> None:
        self.system = system
        self.media = media
        self.docker_config = docker
        self.confirmation = confirmation

    def _run(self, executable: str, args: list[str], timeout: float) -> str:
        if not shutil.which(executable) and not executable.startswith("/"):
            raise RuntimeError(f"utility is not installed: {executable}")
        result = subprocess.run([executable, *args], capture_output=True, text=True, check=False, timeout=timeout)
        output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
        output = output.strip()
        if result.returncode:
            raise RuntimeError(f"{executable} exited {result.returncode}: {output[:2000]}")
        return output or "(no output)"

    def _confirm(self, tool: str, args: dict[str, Any], description: str, context: ToolContext) -> ToolResult | None:
        if self.confirmation is None:
            return None
        message = self.confirmation.require(tool, args, description, context.user_request)
        return ToolResult(message, is_error=True) if message else None

    def media_status(self, _args: dict[str, Any]) -> str:
        return self._run(self.media.playerctl_command, ["-a", "metadata", "--format", "{{playerName}}\t{{status}}\t{{artist}}\t{{title}}"], self.media.timeout_seconds)

    def media_control(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(args.get("action", ""))
        if action not in {"play", "pause", "play-pause", "stop", "next", "previous", "seek", "volume", "mute"}:
            return ToolResult("invalid media action", is_error=True)
        player = str(args.get("player", "")).strip()
        prefix = ["-p", player] if player else ["-a"]
        action_args = [action]
        if action == "seek": action_args += [str(args.get("seconds", 0))]
        if action == "volume": action_args += [str(args.get("value", 0))]
        if action == "mute": action_args += [str(args.get("value", "toggle"))]
        return ToolResult(self._run(self.media.playerctl_command, [*prefix, *action_args], self.media.timeout_seconds))

    def media_launch(self, args: dict[str, Any], _context: ToolContext) -> ToolResult:
        name = str(args.get("player", "")).strip()
        command = self.media.players.get(name)
        if not command:
            return ToolResult(f"media player route is not configured: {name}", is_error=True)
        executable = command[0]
        if not shutil.which(executable) and not executable.startswith("/"):
            return ToolResult(f"media player executable is not installed: {executable}", is_error=True)
        try:
            subprocess.Popen(
                list(command),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return ToolResult(f"could not launch media player {name}: {exc}", is_error=True)
        return ToolResult(f"launched media player {name}")

    def monitor(self, args: dict[str, Any]) -> str:
        subject = str(args.get("subject", "overview"))
        commands = {
            "cpu": ["-b", "1", "1"],
            "memory": ["-b", "-o", "-e"],
            "disk": ["-h"],
            "temperatures": [],
            "gpu": [],
            "battery": [],
            "network": [],
            "processes": ["aux"],
        }
        if subject == "overview":
            return json.dumps({"cpu": self._run(self.system.vmstat_command, ["-s"], self.system.timeout_seconds), "memory": self._run(self.system.free_command, ["-h"], self.system.timeout_seconds), "disk": self._run(self.system.df_command, ["-h"], self.system.timeout_seconds), "network": self._run(self.system.ip_command, ["-brief", "address"], self.system.timeout_seconds)})
        executable = self.system.ps_command if subject == "processes" else self.system.free_command if subject == "memory" else self.system.df_command if subject == "disk" else self.system.ip_command if subject == "network" else self.system.sensors_command if subject == "temperatures" else self.system.nvidia_smi_command if subject == "gpu" else self.system.upower_command if subject == "battery" else self.system.vmstat_command
        argv = commands.get(subject, [])
        if subject == "network": argv = ["-brief", "address"]
        return self._run(executable, argv, self.system.timeout_seconds)

    def systemctl(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(args.get("action", "")); service = str(args.get("service", "")).strip()
        allowed = {"status", "is-active", "is-enabled", "list-units", "list-failed", "start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "logs"}
        if action not in allowed: return ToolResult("invalid systemctl action", is_error=True)
        if action in {"stop", "restart", "reload", "enable", "disable", "mask", "unmask", "start"}:
            blocked = self._confirm("systemctl", args, f"systemctl {action} {service or 'services'}", context)
            if blocked: return blocked
        if action == "logs": return ToolResult(self._run(self.system.journalctl_command, ["-u", service, "-n", str(args.get("lines", 100)), "--no-pager"], self.system.timeout_seconds))
        argv = [action] + ([service] if service else [])
        return ToolResult(self._run(self.system.systemctl_command, argv, self.system.timeout_seconds))

    def docker_control(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(args.get("action", "")); target = str(args.get("target", "")).strip()
        read_only = {"ps", "images", "volumes", "networks", "stats", "inspect", "logs", "top", "events", "version", "info", "compose-ps"}
        disruptive = {"start", "stop", "restart", "pause", "unpause", "kill", "rm", "rmi", "volume-rm", "network-rm", "pull", "exec", "compose-up", "compose-down", "compose-restart", "system-prune"}
        if action not in read_only | disruptive: return ToolResult("invalid docker action", is_error=True)
        if action in disruptive:
            blocked = self._confirm("docker", args, f"docker {action} {target}".strip(), context)
            if blocked: return blocked
        if action == "compose-up": argv = ["compose", "-f", str(args["file"]), "up", "-d"]
        elif action == "compose-down": argv = ["compose", "-f", str(args["file"]), "down"]
        elif action == "compose-restart": argv = ["compose", "-f", str(args["file"]), "restart"]
        elif action == "compose-ps": argv = ["compose", "-f", str(args["file"]), "ps"]
        elif action == "logs": argv = ["logs", "--tail", str(args.get("lines", 100)), target]
        elif action == "exec": argv = ["exec", target, *[str(x) for x in args.get("command", [])]]
        elif action in {"volume-rm", "network-rm"}: argv = [action.split("-", 1)[0], "rm", target]
        elif action == "system-prune": argv = ["system", "prune", "--force"]
        else: argv = action.split("-") if "-" in action else [action]; argv += ([target] if target else [])
        return ToolResult(self._run(self.docker_config.docker_command, argv, self.docker_config.timeout_seconds))


def register_system_tools(registry: ToolRegistry, system: SystemConfig, media: MediaConfig, docker: DockerConfig, confirmation: ConfirmationManager | None = None) -> SystemToolService:
    service = SystemToolService(system, media, docker, confirmation)
    obj = {"type": "object", "additionalProperties": False}
    def call(method: str):
        def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            try:
                value = getattr(service, method)(args) if method in {"monitor", "media_status"} else getattr(service, method)(args, ctx)
                return value if isinstance(value, ToolResult) else ToolResult(value)
            except Exception as exc:
                log_error(f"{method} failed: {type(exc).__name__}: {exc}"); return ToolResult(f"{method} failed: {type(exc).__name__}: {exc}", is_error=True)
        return handler
    if media.enabled:
        registry.register(Tool("media_status", "Show all running media players and current metadata.", {**obj, "properties": {}}, call("media_status")))
        registry.register(Tool("media_control", "Control play/pause/stop/next/previous/seek/volume/mute for media players.", {**obj, "properties": {"action": {"type": "string", "enum": ["play", "pause", "play-pause", "stop", "next", "previous", "seek", "volume", "mute"]}, "player": {"type": "string"}, "seconds": {"type": "number"}, "value": {}}, "required": ["action"]}, call("media_control")))
        registry.register(Tool("media_launch", "Launch a configured media player route such as yt, spotify, or termusic.", {**obj, "properties": {"player": {"type": "string"}}, "required": ["player"]}, call("media_launch")))
    if system.enabled:
        registry.register(Tool("system_monitor", "Read CPU, memory, disk, temperatures, GPU, battery, network, or process metrics.", {**obj, "properties": {"subject": {"type": "string", "enum": ["overview", "cpu", "memory", "disk", "temperatures", "gpu", "battery", "network", "processes"]}}, "required": ["subject"]}, call("monitor")))
        registry.register(Tool("systemctl_control", "Read or control systemd services and view bounded journal logs.", {**obj, "properties": {"action": {"type": "string", "enum": ["status", "is-active", "is-enabled", "list-units", "list-failed", "start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "logs"]}, "service": {"type": "string"}, "lines": {"type": "integer", "minimum": 1, "maximum": 1000}}, "required": ["action"]}, call("systemctl")))
    if docker.enabled:
        registry.register(Tool("docker_control", "Monitor and operate Docker containers, images, volumes, networks, Compose projects, logs, and container exec.", {**obj, "properties": {"action": {"type": "string"}, "target": {"type": "string"}, "file": {"type": "string"}, "lines": {"type": "integer", "minimum": 1, "maximum": 1000}, "command": {"type": "array", "items": {"type": "string"}}}, "required": ["action"]}, call("docker_control")))
    return service
