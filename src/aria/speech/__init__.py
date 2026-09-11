"""Deprecated compatibility facade; use :mod:`aria.services.speech`."""

from ..services.speech.controller import (
    ChatterboxBackend,
    KokoroBackend,
    KokoroHFBackend,
    KokoroLocalBackend,
    SpeechController,
    conversationalize_for_speech,
    strip_markdown_for_speech,
)

__all__ = ["ChatterboxBackend", "KokoroBackend", "KokoroHFBackend", "KokoroLocalBackend", "SpeechController", "conversationalize_for_speech", "strip_markdown_for_speech"]
