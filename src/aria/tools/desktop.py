"""Deprecated compatibility facade for desktop tools.

The implementation lives in :mod:`aria.tools.desktop.service`; module-level
aliases keep established integrations and tests working during migration.
"""

import shutil
import subprocess

from .desktop.service import DesktopToolService, register_desktop_tools

__all__ = ["DesktopToolService", "register_desktop_tools", "shutil", "subprocess"]
