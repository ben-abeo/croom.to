"""
Find LG webOS and Samsung Tizen TVs through their SSDP services. Descriptions and
Samsung device information are bounded and fetched only from the answering host.
MAC discovery prefers the local neighbor table, which identifies the active interface.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

from croom.tv.errors import TvError
from croom.tv.names import one_line

logger = logging.getLogger(__name__)

SSDP_ADDRESS = ("239.255.255.250", 1900)
SEARCH_TARGET = "urn:lge-com:service:webos-second-screen:1"   # what a webOS TV answers to, and nothing else does
MX = 2                                  # a device answers after a random time up to this many seconds (SSDP's MX header)
SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}\r\n"
    'MAN: "ssdp:discover"\r\n'
    f"MX: {MX}\r\n"
    f"ST: {SEARCH_TARGET}\r\n"
    "\r\n"
).encode("ascii")
SAMSUNG_TARGETS = (
    "urn:samsung.com:device:RemoteControlReceiver:1",
    "urn:samsung.com:service:MainTVAgent2:1",
    "urn:schemas-upnp-org:service:RenderingControl:1",
)
TARGETS = (SEARCH_TARGET, *SAMSUNG_TARGETS)

SEARCHES = 2                            # UDP can lose a datagram, and UPnP has a search sent more than once
RESEND_AFTER_S = 0.5                    # this long after the one before (less when the window is short)
SEND_GRACE_S = 1.0                      # a search that has not ended this long after its window is cut off
FETCH_TIMEOUT_S = 2.0                   # a description has this long to arrive
MAC_TIMEOUT_S = 1.0
MAX_DESCRIPTION_BYTES = 65536           # and is read as far as this
MAX_TVS = 64                            # more answers than this from different addresses are not a network of TVs

Answer = Callable[[bytes, str], None]                      # a datagram that came back, and the address it came from
Send = Callable[[bytes, Answer, float], Awaitable[None]]   # put the search out, hand each answer on, end after the window
Fetch = Callable[[str], Awaitable[str]]                    # the page at an address, as text
MacResolver = Callable[[str], Awaitable[str]]


async def discover_tvs(
    timeout: float = 3.0, send: Optional[Send] = None, fetch: Optional[Fetch] = None,
    mac_resolver: Optional[MacResolver] = None,
) -> List[Dict[str, str]]:
    """Return {host, name, location, brand, mac}, deduplicated by the answering IP.

    LG's dedicated service remains discoverable without its description; Samsung
    candidates require a description identifying their manufacturer. An unknown
    MAC is "". Searches and per-device probes run concurrently with bounded waits.
    Injecting send/fetch disables the real MAC lookup unless explicitly supplied.
    """
    if mac_resolver is None and send is None and fetch is None:
        mac_resolver = resolve_mac
    send, fetch = send or _multicast, fetch or _get
    found: Dict[str, Tuple[str, str]] = {}

    def on_answer(datagram: bytes, host: str) -> None:
        answer = _answer_of(datagram)
        if answer is None or (host not in found and len(found) >= MAX_TVS):
            return
        brand, location = answer
        previous = found.get(host)
        # LG may also advertise RenderingControl. Its dedicated service wins.
        if previous and (previous[0] == brand or previous[0] == "lg"):
            found[host] = previous[0], previous[1] or _location_at(location, host)
        else:
            found[host] = brand, _location_at(location, host)

    async def search(target: str) -> Optional[Exception]:
        request = SEARCH.replace(SEARCH_TARGET.encode(), target.encode())
        try:
            await asyncio.wait_for(send(request, on_answer, timeout), timeout + SEND_GRACE_S)
        except asyncio.TimeoutError:
            logger.debug("The search for TVs did not end in time; the answers that came stand")
        except TvError as error:
            return error
        return None

    errors = await asyncio.gather(*(search(target) for target in TARGETS))
    if all(error is not None for error in errors):
        raise errors[0]
    candidates = await asyncio.gather(*(
        _device(fetch, host, brand, location, mac_resolver)
        for host, (brand, location) in found.items()
    ))
    tvs = [tv for tv in candidates if tv is not None]
    tvs.sort(key=lambda tv: (not tv["name"], tv["name"].casefold(), _address_key(tv["host"])))
    return tvs


def _answer_of(datagram: bytes) -> Optional[Tuple[str, str]]:
    """Read only successful SSDP responses for the services searched."""
    lines = datagram.decode("utf-8", errors="replace").splitlines()
    status_line = lines[0].split() if lines else []
    if len(status_line) < 2 or not status_line[0].startswith("HTTP/1.") or status_line[1] != "200":
        return None
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers.setdefault(name.strip().upper(), value.strip())
    target = headers.get("ST", "").lower()
    if target not in {value.lower() for value in TARGETS}:
        return None
    return ("lg" if target == SEARCH_TARGET else "samsung"), headers.get("LOCATION", "")


def _location_at(location: str, host: str) -> str:
    """The address of a description, when it is a web address on the host that answered, and one a terminal can print: a device
    can say anything in LOCATION, and a description is read from the address that answered and from no other, so that a device
    cannot have this one ask something else on the network for it. Else ""."""
    if not (location.isascii() and location.isprintable()):
        return ""
    try:
        parts = urlsplit(location)
        hostname = parts.hostname
    except ValueError:
        return ""
    return location if (
        parts.scheme in ("http", "https") and hostname == host
        and parts.username is None and parts.password is None
    ) else ""


async def _device(
    fetch: Fetch, host: str, brand: str, location: str, mac_resolver: Optional[MacResolver],
) -> Optional[Dict[str, str]]:
    name, manufacturer = "", ""
    if location:
        try:
            page = await asyncio.wait_for(fetch(location), FETCH_TIMEOUT_S)
            name, manufacturer = _description(page)
        except Exception as error:  # noqa: BLE001 - one unreadable description does not lose the other TVs
            logger.debug("The description at %s could not be read: %s", host, error)
    if brand == "samsung" and not re.match(r"^samsung(?:\s|$)", manufacturer.casefold()):
        return None

    async def neighbor_mac() -> str:
        if mac_resolver is None:
            return ""
        try:
            return _normalize_mac(await asyncio.wait_for(mac_resolver(host), MAC_TIMEOUT_S))
        except Exception:  # noqa: BLE001 - neighbor information is optional
            return ""

    if brand == "samsung":
        mac, reported_mac = await asyncio.gather(neighbor_mac(), _samsung_mac(fetch, host))
        mac = mac or reported_mac
    else:
        mac = await neighbor_mac()
    return {"host": host, "name": name, "location": location, "brand": brand, "mac": mac}


def _description(description: str) -> Tuple[str, str]:
    """Read the root device's own name/manufacturer, without DTD expansion."""
    if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES or "<!DOCTYPE" in description:
        return "", ""
    root = ET.fromstring(description)
    device = next((element for element in root.iter() if element.tag.rpartition("}")[2] == "device"), None)
    if device is None:
        return "", ""
    fields = {element.tag.rpartition("}")[2]: one_line(element.text) for element in device}
    return fields.get("friendlyName", ""), fields.get("manufacturer", "")


async def _samsung_mac(fetch: Fetch, host: str) -> str:
    """A reported Wi-Fi MAC is useful only for a verified wireless TV at this IP."""
    try:
        address = ipaddress.ip_address(host)
        authority = f"[{address}]" if address.version == 6 else str(address)
        page = await asyncio.wait_for(fetch(f"http://{authority}:8001/api/v2/"), FETCH_TIMEOUT_S)
        if len(page.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
            return ""
        device = json.loads(page)["device"]
        if (
            device.get("type") == "Samsung SmartTV" and device.get("ip") == host
            and device.get("networkType", "").casefold() in ("wireless", "wifi", "wi-fi")
        ):
            return _normalize_mac(device.get("wifiMac", ""))
    except Exception:  # noqa: BLE001 - API unavailable or not a Samsung TV; discovery can still list it
        pass
    return ""


def _normalize_mac(value: str) -> str:
    """A usable unicast address, normalized for config and Wake-on-LAN."""
    if not isinstance(value, str):
        return ""
    raw = re.sub(r"[:-]", "", value.strip())
    if not re.fullmatch(r"[0-9a-fA-F]{12}", raw) or int(raw, 16) == 0 or int(raw[:2], 16) & 1:
        return ""
    return ":".join(raw[index:index + 2].lower() for index in range(0, 12, 2))


async def resolve_mac(host: str) -> str:
    """Read Linux's existing neighbor entry for this IP; never scan or use a shell."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if address.is_loopback or address.is_multicast or address.is_unspecified:
        return ""
    process = None
    try:
        process = await asyncio.wait_for(asyncio.create_subprocess_exec(
            "ip", "neigh", "show", "to", str(address),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        ), MAC_TIMEOUT_S)
        output, _ = await asyncio.wait_for(process.communicate(), MAC_TIMEOUT_S)
        if process.returncode != 0:
            return ""
        for line in output.decode("ascii", errors="replace").splitlines():
            fields = line.split()
            if not fields or fields[0] != str(address) or "FAILED" in fields or "INCOMPLETE" in fields:
                continue
            if "lladdr" in fields and fields.index("lladdr") + 1 < len(fields):
                return _normalize_mac(fields[fields.index("lladdr") + 1])
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    return ""


def _address_key(host: str) -> Tuple[int, Union[int, str]]:
    """Addresses in the order a person counts them (10.0.0.9 before 10.0.0.12); what is not an address comes after them."""
    try:
        return 0, int(ipaddress.ip_address(host))
    except ValueError:
        return 1, host


class _Answers(asyncio.DatagramProtocol):
    """The listening end of a search: what comes back to the port that the search went out from."""

    def __init__(self, on_answer: Answer):
        self._on_answer = on_answer
        self.error: Optional[Exception] = None

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        self._on_answer(data, addr[0])

    def error_received(self, exc: Exception) -> None:
        self.error = exc                    # (the loop hands a send that failed here, and does not raise it)


async def _multicast(request: bytes, on_answer: Answer, window: float) -> None:
    """The real `send`: the search goes out as a datagram to the SSDP group, SEARCHES times and RESEND_AFTER_S apart, from a
    port of its own (1900 is for whatever else listens there), and each answer that comes back within `window` seconds is
    handed to `on_answer`. The group is reached the way the system routes it, which is by the default route's interface.
    Raises TvError, in words, when the network will not take the search."""
    loop = asyncio.get_running_loop()
    try:
        transport, answers = await loop.create_datagram_endpoint(lambda: _Answers(on_answer), local_addr=("0.0.0.0", 0))
    except OSError as e:
        raise TvError(f"The search for TVs could not be started: {e}") from e
    try:
        transport.get_extra_info("socket").setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        end = loop.time() + window
        for _ in range(SEARCHES):
            transport.sendto(request, SSDP_ADDRESS)
            if answers.error is not None:                   # no route, no interface: no answers will come, so none are waited for
                raise answers.error
            await asyncio.sleep(min(RESEND_AFTER_S, window / SEARCHES))
        await asyncio.sleep(max(0.0, end - loop.time()))
    except OSError as e:
        raise TvError(f"The search for TVs could not be sent: {e}") from e
    finally:
        transport.close()


async def _get(url: str) -> str:
    """The real `fetch`: the page at this address as text, as far as MAX_DESCRIPTION_BYTES of it (ValueError when there is
    more). Only a 200 is a description: a redirect is not followed, and neither it nor an error page is read. What the device
    sends is not decompressed on the way (a few kilobytes can unpack to gigabytes), a description being plain XML, and it is
    asked for as it is: aiohttp offers gzip unless the request says otherwise, and a device that compresses on request would
    lose its name. aiohttp is imported here, as the other services do."""
    import aiohttp
    async with aiohttp.ClientSession(auto_decompress=False, headers={"Accept-Encoding": "identity"}) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"the page at {url} answered {response.status}")
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body += chunk
                if len(body) > MAX_DESCRIPTION_BYTES:
                    raise ValueError(f"the page at {url} is longer than {MAX_DESCRIPTION_BYTES} bytes")
            return body.decode("utf-8", errors="replace")
