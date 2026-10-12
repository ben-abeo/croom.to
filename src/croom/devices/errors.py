"""Why a device command could not be done; the control API answers 409 with the message."""


class DeviceUnavailable(RuntimeError):
    """No usable device, or the device stopped answering."""


class NotReady(RuntimeError):
    """The device is there but the action needs something first (a home, a known position, a saved preset)."""


class Interrupted(RuntimeError):
    """A long camera move was cut short by another command."""
