"""Deprecated compatibility facade; use :mod:`aria.core.agent` instead."""

from ..core.agent import AriaAgent, BaseAgent, register_deploy_coder_tool
from ..services.coder import CoderAgent, CoderReport, CoderService

__all__ = ["AriaAgent", "BaseAgent", "register_deploy_coder_tool", "CoderAgent", "CoderReport", "CoderService"]
