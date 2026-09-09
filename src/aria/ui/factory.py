"""Select the terminal interface: urwid (modern) or rich (legacy)."""

from __future__ import annotations

from aria.config import AppConfig

from .repl import Repl

AVAILABLE_BACKENDS = ("urwid", "rich")


def create_repl(agent, console=None, *, config: AppConfig | None = None, **kwargs):
    """Build the configured Repl backend.

    ``ui.backend`` picks the implementation: ``urwid`` (default) or ``rich``
    (legacy). If the urwid extra is not installed, ARIA falls back to the
    Rich REPL with a warning instead of failing to start. The urwid module is
    imported lazily so importing this factory never requires the extra.
    """
    backend = (config.ui_backend if config else "urwid") or "urwid"
    backend = backend.strip().lower()
    if backend == "urwid":
        try:
            import urwid  # noqa: F401
        except ImportError:
            from ..logging.setup import log_error

            log_error("ui.backend=urwid but the urwid extra is missing; falling back to rich")
            backend = "rich"
    if backend == "urwid":
        from .urwid_tui import UrwidRepl

        return UrwidRepl(agent, console, config=config, **kwargs)
    if backend != "rich":
        raise ValueError(f"ui.backend must be one of {', '.join(AVAILABLE_BACKENDS)}")
    return Repl(agent, console, config=config, **kwargs)
