"""Helpers for updating the frozen AppConfig at runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config import AppConfig, CoderConfig, SpeechConfig


def replace_config(config: AppConfig, **changes: Any) -> AppConfig:
    """Return a new AppConfig with *changes* applied (validated lightly)."""
    if "workspace" in changes and isinstance(changes["workspace"], Path):
        changes["workspace"] = changes["workspace"]
    if "coder" in changes and not isinstance(changes["coder"], CoderConfig):
        changes["coder"] = CoderConfig(**changes["coder"])
    if "speech" in changes and not isinstance(changes["speech"], SpeechConfig):
        changes["speech"] = SpeechConfig(**changes["speech"])
    return replace(config, **changes)
