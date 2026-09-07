"""ARIA's rotating file logging system.

Three independent rotating files are maintained:

- ``data/logs/debug.log``: DEBUG and above (the complete data flow).
- ``data/logs/info.log``: INFO and above only.
- ``data/logs/error.log``: ERROR and above only.

Each file keeps ``backup_count`` rotated backups of ``max_bytes`` bytes.
A ``log_call`` decorator records function entry, exit, arguments, results and
exceptions so the logs reconstruct both the call graph and the data flow.
"""

from __future__ import annotations

import functools
import logging
import os
import re
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TypeVar

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_LOGGER_NAME = "aria"
_configured: bool = False

# Secrets that must never reach disk, whatever a caller tries to log.
_REDACTED_KEYS = {"api_key", "apikey", "key", "token", "authorization", "password", "secret"}
_REDACTED_SUBSTRINGS = ("AIza", "sk-", "nvapi-", "Bearer ")
_TAIL_KEEP = 4
_SECRET_VALUE = re.compile(
    r"(?i)\b(api[ _-]?key|token|password|secret|authorization)\s*[:=]\s*([^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")

F = TypeVar("F", bound=Callable[..., Any])


def _redact(value: Any, _depth: int = 0) -> Any:
    """Replace secret-looking values with a masked tail before logging."""
    if _depth > 4:
        return "..."
    if isinstance(value, str):
        value = _SECRET_VALUE.sub(r"\1=[REDACTED]", value)
        value = _BEARER_VALUE.sub("Bearer [REDACTED]", value)
        if value.startswith(_REDACTED_SUBSTRINGS):
            return "***" + value[-_TAIL_KEEP:] if len(value) > _TAIL_KEEP else "***"
        return value
    if isinstance(value, dict):
        return {
            key: ("***" if str(key).lower() in _REDACTED_KEYS and isinstance(item, str) else _redact(item, _depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted = [_redact(item, _depth + 1) for item in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    return value


def configure_logging(
    directory: Path,
    *,
    console_level: int = logging.WARNING,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    """Attach the three rotating file handlers (and a console handler)."""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    formatter = logging.Formatter(_FORMAT)
    for filename, level in (
        ("debug.log", logging.DEBUG),
        ("info.log", logging.INFO),
        ("error.log", logging.ERROR),
    ):
        handler = RotatingFileHandler(
            directory / filename, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        try:
            os.chmod(directory / filename, 0o600)
        except OSError:
            pass
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(formatter)
    logger.addHandler(console)
    logger.propagate = False
    _configured = True

    logger.debug("Logging configured. Files in %s", directory)


def is_configured() -> bool:
    return _configured


def _get_logger() -> logging.Logger:
    """Return the ARIA logger; auto-configure when not set up yet."""
    if not _configured:
        configure_logging(Path(os.getenv("ARIA_LOG_DIR", "data/logs")))
    return logging.getLogger(_LOGGER_NAME)


def log(level: str, message: str) -> None:
    """Log *message* at *level* ('debug', 'info', 'error') - CIEL-style helper."""
    numeric = getattr(logging, level.upper(), logging.DEBUG)
    _get_logger().log(numeric, _redact(message))


def log_debug(message: str) -> None:
    log("debug", message)


def log_info(message: str) -> None:
    log("info", message)


def log_error(message: str) -> None:
    log("error", message)


def _summarize(value: Any, limit: int = 160) -> str:
    text = repr(_redact(value))
    if len(text) > limit:
        text = text[:limit] + f"...({len(text)} chars total)"
    return text


def log_call(func: F) -> F:
    """Trace calls: entry with arguments, exit with result, errors with tracebacks."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        name = f"{func.__module__}:{func.__qualname__}"
        shown_args = ", ".join(
            [_summarize(a) for a in args[1:]] + [f"{k}={_summarize(v)}" for k, v in kwargs.items()]
        )
        log_debug(f"call -> {name}({shown_args})")
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            log("error", f"{name} raised {type(exc).__name__}: {exc}")
            log_debug(f"call <- {name} (raised)")
            raise
        log_debug(f"call <- {name} -> {_summarize(result)}")
        return result

    return wrapper  # type: ignore[return-value]
