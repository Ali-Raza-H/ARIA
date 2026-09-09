"""Deprecated compatibility facade; use :mod:`aria.services.coder`."""

from ..services.coder import CoderAgent, CoderReport, CoderService

__all__ = ["CoderAgent", "CoderReport", "CoderService"]
