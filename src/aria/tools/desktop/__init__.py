"""Optional desktop-control capability."""

# Keep the legacy monkeypatch/import surface available while the implementation
# lives in ``service.py``. These module objects are shared with the service,
# so patching ``aria.tools.desktop.shutil`` still affects runtime behavior.
import shutil
import subprocess

from .service import DesktopToolService, register_desktop_tools

__all__ = ["DesktopToolService", "register_desktop_tools", "shutil", "subprocess"]
