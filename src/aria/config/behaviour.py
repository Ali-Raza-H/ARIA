"""User-editable behavior presets.

Change values here when you want source-controlled defaults that apply to every
installation. Per-machine settings belong in config.yaml; live behavior files
under skills/ and workflows/ remain the preferred no-code customization layer.
"""

from __future__ import annotations

from .loader import (
    KNOWN_PROVIDERS,
    SpeechConfig,
    VisionConfig,
)
from ..prompts import (
    CODER_PROMPT,
    JARVIS_PERSONA,
    MEMORY_EXTRACTION_PROMPT,
    PROFILE_INFERENCE_PROMPT,
    PROACTIVE_ANALYST_SYSTEM_PROMPT,
    VISION_DESCRIPTION_PROMPT,
)

# Prompt aliases are intentionally public and easy to edit/import.
ARIA_PROMPT = JARVIS_PERSONA
CODER_AGENT_PROMPT = CODER_PROMPT
MEMORY_PROMPT = MEMORY_EXTRACTION_PROMPT
PROACTIVE_PROMPT = PROACTIVE_ANALYST_SYSTEM_PROMPT
VISION_PROMPT = VISION_DESCRIPTION_PROMPT
PROFILE_PROMPT = PROFILE_INFERENCE_PROMPT

# Tool and UI behavior presets.
DEFAULT_UI_BACKEND = "urwid"
DEFAULT_PERSONA = "jarvis"
DEFAULT_DESKTOP_MODE = "managed"
DEFAULT_INPUT_BACKEND = "arch"
DEFAULT_CLIPBOARD_BACKEND = "auto"
DEFAULT_SPEECH_ENGINE = "kokoro_hf"
SUPPORTED_PROVIDERS = tuple(sorted(KNOWN_PROVIDERS))
SUPPORTED_SPEECH_ENGINES = ("kokoro_hf", "kokoro_local", "chatterbox")
SUPPORTED_DESKTOP_MODES = ("managed", "unrestricted")
SUPPORTED_INPUT_BACKENDS = ("arch", "pyautogui")
SUPPORTED_CLIPBOARD_BACKENDS = ("auto", "wl-paste", "xclip", "python")

# Named launcher route examples. Actual executable arrays belong in config.yaml.
DEFAULT_LAUNCHER_ROUTE_NAMES = ("terminal", "browser")

# Key names accepted by wtype/desktop_input. Modifier behavior is implemented
# by the desktop service; this list is a documentation/autocomplete surface.
KEYBIND_MODIFIERS = ("CTRL", "ALT", "SHIFT", "SUPER")
MEDIA_PLAYER_ROUTE_NAMES = ("yt", "spotify", "termusic")
SYSTEM_MONITOR_SUBJECTS = ("overview", "cpu", "memory", "disk", "temperatures", "gpu", "battery", "network", "processes")
DOCKER_READ_ACTIONS = ("ps", "images", "volumes", "networks", "stats", "inspect", "logs", "top", "events", "version", "info", "compose-ps")
DOCKER_DISRUPTIVE_ACTIONS = ("start", "stop", "restart", "pause", "unpause", "kill", "rm", "rmi", "volume-rm", "network-rm", "pull", "exec", "compose-up", "compose-down", "compose-restart", "system-prune")
SYSTEMCTL_ACTIONS = ("status", "is-active", "is-enabled", "list-units", "list-failed", "start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "logs")

__all__ = [
    "ARIA_PROMPT", "CODER_AGENT_PROMPT", "MEMORY_PROMPT", "PROACTIVE_PROMPT",
    "VISION_PROMPT", "PROFILE_PROMPT", "DEFAULT_UI_BACKEND", "DEFAULT_PERSONA",
    "DEFAULT_DESKTOP_MODE", "DEFAULT_INPUT_BACKEND", "DEFAULT_CLIPBOARD_BACKEND",
    "DEFAULT_SPEECH_ENGINE", "SUPPORTED_PROVIDERS", "SUPPORTED_SPEECH_ENGINES",
    "SUPPORTED_DESKTOP_MODES", "SUPPORTED_INPUT_BACKENDS",
    "SUPPORTED_CLIPBOARD_BACKENDS", "DEFAULT_LAUNCHER_ROUTE_NAMES",
    "KEYBIND_MODIFIERS", "MEDIA_PLAYER_ROUTE_NAMES", "SYSTEM_MONITOR_SUBJECTS", "DOCKER_READ_ACTIONS", "DOCKER_DISRUPTIVE_ACTIONS", "SYSTEMCTL_ACTIONS", "SpeechConfig", "VisionConfig",
]
