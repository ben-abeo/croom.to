"""
Room settings on the touch Pi (spec docs/superpowers/specs/2026-10-29-room-settings-design.md): what lets a person with the PIN
change the room from its touch screen. The PIN is `croom.settings.pin`.

This package imports nothing from `croom.control`, so the control service can import it without the two importing each other.
"""

from croom.settings.pin import PinError, PinLocked, PinStore

__all__ = ["PinError", "PinLocked", "PinStore"]
