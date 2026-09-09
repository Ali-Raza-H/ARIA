"""Deprecated compatibility facade; use :mod:`aria.services.proactive`."""

from .services.proactive import ProfileStore, ProactiveService

__all__ = ["ProfileStore", "ProactiveService"]
