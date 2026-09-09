"""Configuration validation public API.

The loader performs validation while constructing immutable models; this module
keeps the validation-facing exports in the architectural config boundary.
"""

from .loader import ConfigError, KNOWN_PROVIDERS

__all__ = ["ConfigError", "KNOWN_PROVIDERS"]
