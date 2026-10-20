"""Validation and storage names for a TV chosen on the room's table."""
import hashlib
import ipaddress
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict

from croom.core.config import TvConfig
from croom.tv.errors import TvError


def connection_settings(current: TvConfig, changes: Dict[str, Any]) -> TvConfig:
    """Only the two settings a person can correct without selecting another TV."""
    if set(changes) - {"mac", "pi_input"}:
        raise TvError("Only the MAC address and HDMI input can be changed here; use Find TVs to choose another TV")
    mac = changes.get("mac", current.mac)
    if not isinstance(mac, str):
        raise TvError("The MAC address must be text")
    mac = mac.strip().replace("-", ":").upper()
    if re.fullmatch(r"[0-9A-F]{12}", mac):
        mac = ":".join(mac[index:index + 2] for index in range(0, 12, 2))
    if mac:
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
            raise TvError("Enter the TV's MAC address as six pairs, like AA:BB:CC:DD:EE:02")
        if mac == "00:00:00:00:00:00" or int(mac[:2], 16) & 1:
            raise TvError("Enter the TV's own MAC address, not a broadcast or multicast address")
    pi_input = changes.get("pi_input", current.pi_input)
    if not isinstance(pi_input, str) or not re.fullmatch(r"HDMI_[1-9][0-9]?", pi_input):
        raise TvError("Choose the HDMI input the room Pi is connected to")
    return replace(current, mac=mac, pi_input=pi_input)


def selected_config(current: TvConfig, device: Dict[str, Any]) -> TvConfig:
    brand, host = device.get("brand", "lg"), device.get("host")
    if brand not in ("lg", "samsung"):
        raise TvError("Choose an LG or Samsung TV from Find TVs")
    try:
        address = ipaddress.IPv4Address(host)
    except (ValueError, TypeError):
        raise TvError("Choose a TV with a valid local IPv4 address") from None
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_reserved:
        raise TvError("Choose a TV on the room's network")
    chosen = replace(current, enabled=True, brand=brand, host=str(address))
    return connection_settings(chosen, {
        "mac": device.get("mac", ""),
        "pi_input": device.get("pi_input", current.pi_input),
    })


def key_path(directory: Path, config: TvConfig) -> Path:
    """Pairing belongs to this brand and address, never to a different selected TV."""
    identity = hashlib.sha256(f"{config.brand}:{config.host}".encode("utf-8")).hexdigest()[:24]
    return directory / f"tv-{identity}-key"
