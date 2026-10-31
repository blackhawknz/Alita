"""Compatibility shim — the voice implementation lives in :mod:`alita.voice`."""

from ..voice import VoiceError, VoiceService, speakable_text, truncate_speech  # noqa: F401
