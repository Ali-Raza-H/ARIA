"""Deprecated compatibility facade; use :mod:`aria.services.vision`."""

from .services.vision import ImageAttachment, capture_clipboard, capture_screen, load_image, prepare_image_message, register_image_tools

__all__ = ["ImageAttachment", "capture_clipboard", "capture_screen", "load_image", "prepare_image_message", "register_image_tools"]
