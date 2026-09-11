"""Hyprland desktop-control tools.

Managed mode uses a small allow-listed API. Unrestricted mode adds raw
``hyprctl dispatch`` and explicitly configured desktop commands. All subprocess
arguments remain argument arrays (never shell=True).
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...config import DesktopConfig
from ...logging.setup import log_error, log_info
from ..core.confirmation import ConfirmationManager
from ..core.base import Tool, ToolContext, ToolResult
from ..core.registry import ToolRegistry


class DesktopToolService:
    """Execute Hyprland operations using configured, auditable backends."""

    def __init__(self, config: DesktopConfig, confirmation: ConfirmationManager | None = None) -> None:
        self.config = config
        self.confirmation = confirmation

    def _confirmation_error(self, method: str, args: dict[str, Any], description: str, context: ToolContext) -> str | None:
        if self.confirmation is None:
            return None
        return self.confirmation.require(method, args, description, context.user_request)

    def _run(self, command: list[str], timeout: float | None = None) -> str:
        if not shutil.which(command[0]) and not Path(command[0]).is_file():
            raise RuntimeError(f"desktop utility is not installed: {command[0]}")
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout or self.config.command_timeout_seconds,
        )
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode:
            raise RuntimeError(f"{' '.join(command[:2])} exited {completed.returncode}: {output[:1000]}")
        return output

    def _hyprctl(self, *args: str) -> str:
        return self._run([self.config.hyprctl_command, *args])

    def monitors(self, _args: dict[str, Any]) -> str:
        return self._hyprctl("monitors", "-j")

    def workspaces(self, _args: dict[str, Any]) -> str:
        return self._hyprctl("workspaces", "-j")

    def clients(self, _args: dict[str, Any]) -> str:
        return self._hyprctl("clients", "-j")

    def active_window(self, _args: dict[str, Any]) -> str:
        return self._hyprctl("activewindow", "-j")

    def dispatch(self, args: dict[str, Any]) -> str:
        if self.config.mode != "unrestricted":
            raise PermissionError("raw Hyprland dispatch requires desktop.mode=unrestricted")
        dispatcher = args.get("dispatcher")
        values = args.get("arguments", [])
        if not isinstance(dispatcher, str) or not dispatcher.strip():
            raise ValueError("dispatcher must be a non-empty string")
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("arguments must be an array of strings")
        return self._hyprctl("dispatch", dispatcher.strip(), *values)

    def managed_dispatch(self, args: dict[str, Any]) -> str:
        dispatcher = args.get("dispatcher")
        values = args.get("arguments", [])
        allowed = {
            "killactive", "closewindow", "focuswindow", "movetoworkspace",
            "workspace", "movewindow", "resizeactive", "togglefloating", "fullscreen",
            "pin", "togglepseudotile", "togglespecialworkspace", "movetoworkspacesilent",
        }
        if not isinstance(dispatcher, str) or dispatcher.strip() not in allowed:
            raise ValueError(f"managed dispatcher must be one of: {', '.join(sorted(allowed))}")
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("arguments must be an array of strings")
        return self._hyprctl("dispatch", dispatcher.strip(), *values)

    def keybinds(self, _args: dict[str, Any]) -> str:
        """Return configured Hyprland binds; mutating config is intentionally out of scope."""
        return self._hyprctl("-j", "binds")

    def launch(self, args: dict[str, Any]) -> str:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty configured launcher name")
        configured = self.config.launchers.get(command.strip())
        if configured is None:
            raise PermissionError(f"launcher is not configured: {command}")
        try:
            process = subprocess.Popen(
                list(configured),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"could not launch configured route {command.strip()!r}: {exc}") from exc
        log_info(f"desktop.launcher name={command.strip()!r} pid={process.pid}")
        return f"launched {command.strip()} (pid {process.pid})"

    def send_input(self, args: dict[str, Any]) -> str:
        backend = self.config.input_backend
        action = args.get("action")
        if action not in {"type", "key", "click", "move"}:
            raise ValueError("action must be type, key, click, or move")
        if backend == "pyautogui":
            try:
                pyautogui = importlib.import_module("pyautogui")
            except ImportError as exc:
                raise RuntimeError("pyautogui backend is not installed") from exc
            if action == "type":
                text = args.get("text")
                if not isinstance(text, str):
                    raise ValueError("text is required for type")
                pyautogui.write(text, interval=float(args.get("interval", 0.0)))
            elif action == "key":
                key = args.get("key")
                if not isinstance(key, str) or not key:
                    raise ValueError("key is required for key")
                pyautogui.press(key)
            else:
                x, y = self._coordinates(args)
                pyautogui.moveTo(x, y)
                if action == "click":
                    pyautogui.click()
            return json.dumps({"backend": backend, "action": action})

        if action == "type":
            text = args.get("text")
            if not isinstance(text, str):
                raise ValueError("text is required for type")
            return self._run([self.config.wtype_command, text])
        if action == "key":
            key = args.get("key")
            if not isinstance(key, str) or not key:
                raise ValueError("key is required for key")
            modifiers = args.get("modifiers", [])
            if not isinstance(modifiers, list) or not all(isinstance(item, str) and item.strip() for item in modifiers):
                raise ValueError("modifiers must be an array of key names")
            # wtype supports a key press but not a held modifier sequence. Use
            # ydotool for explicit hold/key/release when a chord is requested.
            if modifiers:
                # wtype's modifier flags are not consistent across versions.
                # ydotool's key event protocol explicitly models press and
                # release, so a chord is sent as one atomic sequence.
                keycodes = {"CTRL": 29, "ALT": 56, "SHIFT": 42, "SUPER": 125}
                normalized = [modifier.upper() for modifier in modifiers]
                if all(modifier in keycodes for modifier in normalized):
                    events = [f"{keycodes[modifier]}:1" for modifier in normalized]
                    events.append(f"{key}:1")
                    events.append(f"{key}:0")
                    events.extend(f"{keycodes[modifier]}:0" for modifier in reversed(normalized))
                    return self._run([self.config.ydotool_command, "key", *events])
                raise ValueError("modifiers must be CTRL, ALT, SHIFT, or SUPER when using the arch backend")
            return self._run([self.config.wtype_command, "-k", key])
        x, y = self._coordinates(args)
        if action == "move":
            return self._run([self.config.ydotool_command, "mousemove", "--absolute", str(x), str(y)])
        return self._run([self.config.ydotool_command, "mousemove", "--absolute", str(x), str(y), "click", "0xC0"])

    @staticmethod
    def _coordinates(args: dict[str, Any]) -> tuple[int, int]:
        x, y = args.get("x"), args.get("y")
        if isinstance(x, bool) or not isinstance(x, int) or isinstance(y, bool) or not isinstance(y, int):
            raise ValueError("integer x and y coordinates are required")
        if not 0 <= x <= 10000 or not 0 <= y <= 10000:
            raise ValueError("coordinates must be between 0 and 10000")
        return x, y

    def set_keybind(self, args: dict[str, Any]) -> str:
        if self.config.mode != "unrestricted":
            raise PermissionError("changing keybinds requires desktop.mode=unrestricted")
        name = args.get("name")
        value = args.get("value")
        if not isinstance(name, str) or not name.strip() or not isinstance(value, str) or not value.strip():
            raise ValueError("name and value must be non-empty strings")
        config_path = self.config.hyprland_config_path
        if not config_path:
            raise ValueError("desktop.hyprland_config_path is not configured")
        path = Path(config_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Hyprland config does not exist: {path}")
        content = path.read_text(encoding="utf-8")
        marker = f"# ARIA_BIND {name.strip()}"
        line = f"bind = {value.strip()} # ARIA_BIND {name.strip()}"
        lines = content.splitlines()
        replaced = False
        for index, current in enumerate(lines):
            if marker in current:
                lines[index] = line
                replaced = True
                break
        if not replaced:
            lines.extend(["", line])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._hyprctl("reload")
        return f"{'updated' if replaced else 'added'} keybind {name.strip()}"

    def shell(self, args: dict[str, Any]) -> str:
        if self.config.mode != "unrestricted":
            raise PermissionError("desktop shell requires desktop.mode=unrestricted")
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty shell command")
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=self.config.command_timeout_seconds,
        )
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode:
            raise RuntimeError(f"desktop shell exited {completed.returncode}: {output[:1000]}")
        return output

    def exec_configured(self, args: dict[str, Any]) -> str:
        if self.config.mode != "unrestricted":
            raise PermissionError("desktop exec requires desktop.mode=unrestricted")
        name = args.get("command")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("command must be a configured command name")
        configured = self.config.launchers.get(name.strip())
        if configured is None:
            raise PermissionError(f"desktop command is not configured: {name}")
        return self._run(list(configured))

    def screenshot(self, args: dict[str, Any], context: ToolContext) -> str:
        raw = args.get("path", "desktop.png")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("path must be a non-empty string")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = context.workspace / path
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        output = self._run([self.config.screenshot_command, str(path)])
        return json.dumps({"path": str(path), "output": output})


def register_desktop_tools(registry: ToolRegistry, service: DesktopToolService) -> None:
    """Register desktop tools; only this registry is passed to ARIA."""

    def call(method: str, args: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            output = getattr(service, method)(args) if method != "screenshot" else service.screenshot(args, context)
            return ToolResult(output)
        except Exception as exc:
            log_error(f"desktop.{method} failed: {type(exc).__name__}: {exc}")
            return ToolResult(f"Desktop {method} failed: {type(exc).__name__}: {exc}", is_error=True)

    inspect_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    for name, method, description in (
        ("desktop_monitors", "monitors", "List Hyprland monitors as JSON."),
        ("desktop_workspaces", "workspaces", "List Hyprland workspaces as JSON."),
        ("desktop_clients", "clients", "List Hyprland windows/clients as JSON."),
        ("desktop_active_window", "active_window", "Inspect the active Hyprland window as JSON."),
        ("desktop_keybinds", "keybinds", "Inspect configured Hyprland keybinds."),
    ):
        registry.register(Tool(name, description, inspect_schema, lambda args, ctx, method=method: call(method, args, ctx)))

    registry.register(Tool(
        "desktop_dispatch", "Perform an allow-listed Hyprland desktop action in managed mode.",
        {"type": "object", "properties": {"dispatcher": {"type": "string"}, "arguments": {"type": "array", "items": {"type": "string"}}}, "required": ["dispatcher"], "additionalProperties": False},
        lambda args, ctx: call("managed_dispatch", args, ctx),
    ))
    registry.register(Tool(
        "desktop_launch", "Launch an application using a named command from desktop.launchers.",
        {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False},
        lambda args, ctx: call("launch", args, ctx),
    ))
    registry.register(Tool(
        "desktop_input", "Send configured keyboard or mouse input through Arch utilities or PyAutoGUI.",
        {"type": "object", "properties": {"action": {"type": "string", "enum": ["type", "key", "click", "move"]}, "text": {"type": "string"}, "key": {"type": "string"}, "modifiers": {"type": "array", "items": {"type": "string"}}, "x": {"type": "integer"}, "y": {"type": "integer"}, "interval": {"type": "number"}}, "required": ["action"], "additionalProperties": False},
        lambda args, ctx: call("send_input", args, ctx),
    ))
    registry.register(Tool(
        "desktop_screenshot", "Capture the desktop using the configured screenshot utility.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False},
        lambda args, ctx: call("screenshot", args, ctx),
    ))
    if service.config.mode == "unrestricted":
        registry.register(Tool(
            "desktop_raw_dispatch", "Run arbitrary hyprctl dispatch; available only in desktop.mode=unrestricted.",
            {"type": "object", "properties": {"dispatcher": {"type": "string"}, "arguments": {"type": "array", "items": {"type": "string"}}}, "required": ["dispatcher"], "additionalProperties": False},
            lambda args, ctx: call("dispatch", args, ctx),
        ))
        registry.register(Tool(
            "desktop_set_keybind", "Add or update a marked Hyprland keybind and reload Hyprland; unrestricted mode only.",
            {"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "string"}}, "required": ["name", "value"], "additionalProperties": False},
            lambda args, ctx: call("set_keybind", args, ctx),
        ))
        registry.register(Tool(
            "desktop_exec", "Run a named configured desktop command; available only in desktop.mode=unrestricted.",
            {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False},
            lambda args, ctx: call("exec_configured", args, ctx),
        ))
        registry.register(Tool(
            "desktop_shell", "Run an arbitrary desktop shell command; available only in desktop.mode=unrestricted.",
            {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False},
            lambda args, ctx: call("shell", args, ctx),
        ))
