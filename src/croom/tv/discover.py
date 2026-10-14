"""
Finding LG webOS TVs on the network (spec 2026-10-13 TV remote and touchpad, section 3: Find TVs). A webOS TV answers an
SSDP search for LG's second-screen service, and gives itself a name in the UPnP description its answer points to. The search
goes out through `send` and the description is read through `fetch`, which a test replaces, so that the tests put nothing on
the network. Whatever answers is a device on the network that nobody vetted: what it says is read, not believed (it is
read from its own address only, as much as a description needs, and what it names itself is printable).
"""

import asyncio
import ipaddress
import logging
import socket
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

from croom.tv.errors import TvError

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

SEARCHES = 2                            # UDP can lose a datagram, and UPnP has a search sent more than once
RESEND_AFTER_S = 0.5                    # this long after the one before (less when the window is short)
SEND_GRACE_S = 1.0                      # a search that has not ended this long after its window is cut off
FETCH_TIMEOUT_S = 2.0                   # a description has this long to arrive
MAX_DESCRIPTION_BYTES = 65536           # and is read as far as this
MAX_TVS = 64                            # more answers than this from different addresses are not a network of TVs
MAX_NAME_LENGTH = 100

Answer = Callable[[bytes, str], None]                      # a datagram that came back, and the address it came from
Send = Callable[[bytes, Answer, float], Awaitable[None]]   # put the search out, hand each answer on, end after the window
Fetch = Callable[[str], Awaitable[str]]                    # the page at an address, as text


async def discover_tvs(timeout: float = 3.0, send: Optional[Send] = None, fetch: Optional[Fetch] = None) -> List[Dict[str, str]]:
    """The LG webOS TVs on the local network that answer within `timeout` seconds, each as {"host", "name", "location"}: the
    address it answered from, the name it gives itself (empty when its description cannot be read) and where that description
    is (empty when it is not a web address on that host). One TV is listed once, however often it answers; a device that is not
    a webOS TV is left out. Named TVs come first, by name, and by address among TVs of one name.

    `send` puts the search on the network and hands each answer to a callback until `timeout` is up; `fetch` gets a page. A
    search that has not ended a second after its window is cut off and the answers that came stand. Raises TvError, in words,
    when the search cannot be sent at all: that is not the same as no TV answering."""
    send, fetch = send or _multicast, fetch or _get
    found: Dict[str, str] = {}              # by the address that answered: where its answer says its description is

    def on_answer(datagram: bytes, host: str) -> None:
        location = _location_of(datagram)
        if location is None or (host not in found and len(found) >= MAX_TVS):
            return
        found[host] = found.get(host) or _location_at(location, host)

    try:
        await asyncio.wait_for(send(SEARCH, on_answer, timeout), timeout + SEND_GRACE_S)
    except asyncio.TimeoutError:
        logger.debug("The search for TVs did not end in time; the answers that came stand")
    answered = list(found.items())
    names = await asyncio.gather(*(_name(fetch, host, location) for host, location in answered))
    tvs = [{"host": host, "name": name, "location": location} for (host, location), name in zip(answered, names)]
    tvs.sort(key=lambda tv: (not tv["name"], tv["name"].casefold(), _address_key(tv["host"])))
    return tvs


def _location_of(datagram: bytes) -> Optional[str]:
    """The LOCATION in an answer from a webOS TV ("" when it names none); None when the datagram is not such an answer (another
    device, an announcement, noise): the ST header says what a device is answering to."""
    headers: Dict[str, str] = {}
    for line in datagram.decode("utf-8", errors="replace").splitlines():
        name, colon, value = line.partition(":")
        if colon:
            headers.setdefault(name.strip().upper(), value.strip())
    if headers.get("ST", "").lower() != SEARCH_TARGET:
        return None
    return headers.get("LOCATION", "")


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
    return location if parts.scheme in ("http", "https") and hostname == host else ""


async def _name(fetch: Fetch, host: str, location: str) -> str:
    """The name the TV at `host` gives itself in its description; "" when it has none or the description cannot be read."""
    if not location:
        return ""
    try:
        return _friendly_name(await asyncio.wait_for(fetch(location), FETCH_TIMEOUT_S))
    except Exception as e:  # noqa: BLE001 - no answer, an error page, no XML: the TV is listed all the same, without its name
        logger.debug(f"The description of the TV at {host} could not be read: {str(e) or type(e).__name__}")
        return ""


def _friendly_name(description: str) -> str:
    """The friendlyName of a UPnP device description (the first, which is the TV's own), on one printable line; "" when it has
    none. Raises ET.ParseError when the text is not XML (the caller takes any failure for no name). A description has no DTD,
    so a text that has one is not read: nothing in it is expanded."""
    if "<!DOCTYPE" in description:
        return ""
    root = ET.fromstring(description)
    for element in root.iter():
        if element.tag.rpartition("}")[2] == "friendlyName":        # (whatever the namespace is called)
            name = " ".join((element.text or "").split())
            return "".join(char for char in name if char.isprintable())[:MAX_NAME_LENGTH]
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
    more). A redirect is not followed, and an error page is an error. aiohttp is imported here, as the other services do."""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url, allow_redirects=False) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body += chunk
                if len(body) > MAX_DESCRIPTION_BYTES:
                    raise ValueError(f"the page at {url} is longer than {MAX_DESCRIPTION_BYTES} bytes")
            return body.decode("utf-8", errors="replace")
