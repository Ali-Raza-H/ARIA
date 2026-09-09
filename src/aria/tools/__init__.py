"""Agent tools."""

from .core import CustomToolRouter, RoutedResponse, Tool, ToolContext, ToolRegistry, ToolResult
from .browser import BrowserToolService, register_browser_tools
from .desktop import DesktopToolService, register_desktop_tools
from .filesystem import register_filesystem_tools
from .lifeos import LifeOSTool, register_lifeos_tool
from ..config import LifeOSConfig
from .shell import register_shell_tool
from .web import WebToolService, register_web_tools

__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "BrowserToolService",
    "DesktopToolService",
    "CustomToolRouter",
    "RoutedResponse",
    "LifeOSConfig",
    "LifeOSTool",
    "register_browser_tools",
    "register_desktop_tools",
    "register_filesystem_tools",
    "register_lifeos_tool",
    "register_shell_tool",
    "WebToolService",
    "register_web_tools",
]
