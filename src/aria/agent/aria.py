"""Deprecated compatibility facade; use :mod:`aria.core.agent.aria`."""

from ..core.agent.aria import AriaAgent, DeployCoderTool, register_deploy_coder_tool

__all__ = ["AriaAgent", "DeployCoderTool", "register_deploy_coder_tool"]
