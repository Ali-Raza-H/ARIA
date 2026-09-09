"""Desktop notifications for ARIA's scheduler and reminders."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable

from .config import NotificationConfig
from .logging.setup import log_error, log_info


class NotificationService:
    """Deliver concise notifications through notify-send or gdbus."""

    def __init__(self, config: NotificationConfig) -> None:
        self.config = config

    def send(self, title: str, body: str, urgency: str = "normal", tts: Callable[[str], None] | None = None) -> bool:
        if not self.config.enabled:
            return False
        if urgency not in {"low", "normal", "critical"}:
            urgency = self.config.default_urgency
        delivered = False
        try:
            if self.config.backend == "notify-send":
                command = self.config.notify_send_command
                if shutil.which(command) or command.startswith("/"):
                    completed = subprocess.run(
                        [command, "-a", self.config.app_name, "-u", urgency, "-t", str(self.config.default_timeout_ms), title, body],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    )
                    delivered = completed.returncode == 0
            else:
                command = self.config.dbus_command
                if shutil.which(command) or command.startswith("/"):
                    completed = subprocess.run(
                        [command, "call", "--session", "--dest", "org.freedesktop.Notifications", "--object-path", "/org/freedesktop/Notifications", "--method", "org.freedesktop.Notifications.Notify", self.config.app_name, "0", "", title, body, "[]", "{}", str(self.config.default_timeout_ms)],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    )
                    delivered = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_error(f"Notification: delivery failed: {type(exc).__name__}: {exc}")
        if self.config.tts_enabled and tts is not None:
            try:
                tts(f"{title}. {body}")
                delivered = True
            except Exception as exc:
                log_error(f"Notification: TTS delivery failed: {type(exc).__name__}: {exc}")
        log_info(f"Notification: {'delivered' if delivered else 'unavailable'} title={title[:100]!r}")
        return delivered
