"""
Wake-on-LAN: the magic packet that turns the TV on from standby. A TV that is off has no socket to be told anything on,
so this is the one control that does not go through the TV's client. LG enables it with the TV's "Mobile TV On" setting.
"""

import re

from croom.tv.errors import TvError

# Six pairs of hex digits, joined by one kind of separator or by none: AA:BB:CC:DD:EE:FF, AA-BB-CC-DD-EE-FF, AABBCCDDEEFF.
_MAC = re.compile(r"[0-9A-Fa-f]{12}|[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}")


async def wake(mac: str) -> None:
    """Send the magic packet for this MAC address. Raises TvError, in words for the table, when it is not a MAC address,
    when the library that sends the packet is not installed, or when the packet cannot be sent. The library is imported
    here and not at the top, so a room without a TV runs without it."""
    mac = str(mac or "").strip()
    if not mac:
        raise TvError("The TV's MAC address is not set (tv.mac in the room's config), so the TV cannot be turned on from "
                      "the table")
    if not _MAC.fullmatch(mac):
        # (a MAC of digits only, such as 10:20:30:40:50:59, is a number to YAML unless it is in quotes)
        raise TvError(f"{mac!r} is not a MAC address (tv.mac in the room's config wants one like \"AA:BB:CC:DD:EE:FF\", in "
                      "quotes), so the TV cannot be turned on from the table")
    try:
        import wakeonlan
    except ImportError as e:
        raise TvError("The Wake-on-LAN library is not installed on this device: pip install wakeonlan") from e
    # wakeonlan 4 has wake() and deprecates send_magic_packet(); the pin is wakeonlan>=3.0, which has only the latter
    send = getattr(wakeonlan, "wake", None) or getattr(wakeonlan, "send_magic_packet", None)
    if send is None:
        raise TvError("The Wake-on-LAN library on this device cannot send a wake-up call: pip install -U wakeonlan")
    try:
        send(mac)
    except (OSError, ValueError) as e:     # the network is down, or the library does not like the address after all
        raise TvError(f"The wake-up call could not be sent to {mac}: {e}") from e
