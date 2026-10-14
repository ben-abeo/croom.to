"""Finding LG TVs on the network: the search a webOS TV answers, what is made of the answers, and the name a TV gives itself in
its UPnP description. Nothing here touches the network: the search goes through a `send` that is a stand-in, the descriptions are
read through a `fetch` that is one, and the real endpoint (`_multicast`) runs on a stand-in for the loop's datagram endpoint."""
import asyncio
import socket

import pytest
from croom.tv import discover
from croom.tv.discover import SEARCH_TARGET, SSDP_ADDRESS, discover_tvs
from croom.tv.errors import TvError
from tests.unit.tv.helpers import within

LG = "10.0.0.41"
LG_LOCATION = "http://10.0.0.41:1234/udhisapi.xml"


def ssdp_answer(location, st=SEARCH_TARGET):
    """What a device on the network sends back to a search: an HTTP-like answer (the status line, then headers)."""
    lines = ["HTTP/1.1 200 OK", "CACHE-CONTROL: max-age=1800", "EXT:"]
    if location is not None:
        lines.append(f"LOCATION: {location}")
    lines += ["SERVER: Linux/5.4 UPnP/1.0 webOS/6.0", f"ST: {st}", f"USN: uuid:6f1b2a3c-aabb-1000-8000-aabbccddeeff::{st}", "", ""]
    return "\r\n".join(lines).encode()


def description(name="Boardroom TV", tag="friendlyName"):
    """A UPnP device description, as a webOS TV serves it."""
    return ('<?xml version="1.0"?>\n<root xmlns="urn:schemas-upnp-org:device-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
            "<deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>"
            f"<{tag}>{name}</{tag}><manufacturer>LG Electronics</manufacturer><modelName>LG TV</modelName>"
            "</device></root>")


def answering(*answers):
    """A `send` that puts nothing on the network: it notes what it was asked to send, and answers with these datagrams, each
    from the address it came from."""
    sent = []

    async def send(request, on_answer, window):
        sent.append((request, window))
        for datagram, host in answers:
            on_answer(datagram, host)
    send.sent = sent
    return send


def serving(pages):
    """A `fetch` with these pages by their address; one it does not have is an error, as a TV that does not answer is."""
    asked = []

    async def fetch(url):
        asked.append(url)
        if url not in pages:
            raise ConnectionError(f"no answer from {url}")
        page = pages[url]
        if isinstance(page, BaseException):
            raise page
        return page
    fetch.asked = asked
    return fetch


# The search

def test_the_waits_and_the_limits_are_the_ones_settled():
    assert discover.MX == 2                                       # a TV answers within two seconds of the search ...
    assert discover.SEARCHES == 2 and discover.RESEND_AFTER_S == 0.5      # which is sent twice, half a second apart
    assert discover.SEND_GRACE_S == 1.0                           # a search that is not over a second after its window is cut off
    assert discover.FETCH_TIMEOUT_S == 2.0                        # a description has two seconds to arrive
    assert discover.MAX_DESCRIPTION_BYTES == 65536 and discover.MAX_TVS == 64      # (and names are 100 characters: test_names)


async def test_the_search_is_the_one_a_webos_tv_answers_to_and_goes_to_the_ssdp_group():
    send = answering()
    assert await discover_tvs(timeout=1.5, send=send, fetch=serving({})) == []
    [(request, window)] = send.sent
    assert request.decode().split("\r\n") == [
        "M-SEARCH * HTTP/1.1", "HOST: 239.255.255.250:1900", 'MAN: "ssdp:discover"', "MX: 2",
        "ST: urn:lge-com:service:webos-second-screen:1", "", ""]
    assert window == 1.5                                          # the window the answers are collected in is the one asked for
    assert SSDP_ADDRESS == ("239.255.255.250", 1900)


async def test_the_default_window_is_three_seconds():
    send = answering()
    await discover_tvs(send=send, fetch=serving({}))
    assert send.sent[0][1] == 3.0                                  # a TV answers within MX (2 s), and a second for the network


# What is made of the answers

async def test_an_lg_tv_is_found_by_its_answer_and_named_by_its_description_and_another_device_is_left_out():
    """The brief's own: two answers, one from an LG with a description that names it, one that is not an LG's."""
    other = ssdp_answer("http://10.0.0.7:49152/rootDesc.xml", st="urn:schemas-upnp-org:device:MediaRenderer:1")
    send = answering((ssdp_answer(LG_LOCATION), LG), (other, "10.0.0.7"))
    fetch = serving({LG_LOCATION: description("Boardroom TV"), "http://10.0.0.7:49152/rootDesc.xml": description("A speaker")})
    assert await discover_tvs(send=send, fetch=fetch) == [{"host": LG, "name": "Boardroom TV", "location": LG_LOCATION}]
    assert fetch.asked == [LG_LOCATION]                            # the other device's description was not even asked for


async def test_a_tv_that_answers_more_than_once_is_listed_once_and_read_once():
    send = answering((ssdp_answer(LG_LOCATION), LG), (ssdp_answer(LG_LOCATION), LG), (ssdp_answer(LG_LOCATION), LG))
    fetch = serving({LG_LOCATION: description()})
    assert await discover_tvs(send=send, fetch=fetch) == [{"host": LG, "name": "Boardroom TV", "location": LG_LOCATION}]
    assert fetch.asked == [LG_LOCATION]


async def test_the_tvs_are_listed_by_name_then_by_address_and_those_with_no_name_come_last():
    send = answering((ssdp_answer("http://10.0.0.50:1234/d.xml"), "10.0.0.50"), (ssdp_answer("http://10.0.0.9:1234/d.xml"), "10.0.0.9"),
                     (ssdp_answer("http://10.0.0.12:1234/d.xml"), "10.0.0.12"), (ssdp_answer("http://10.0.0.60:1234/d.xml"), "10.0.0.60"))
    fetch = serving({"http://10.0.0.50:1234/d.xml": description("annex"), "http://10.0.0.9:1234/d.xml": description("Boardroom TV"),
                     "http://10.0.0.12:1234/d.xml": description("Boardroom TV")})        # (the fourth cannot be read)
    found = await discover_tvs(send=send, fetch=fetch)
    assert [(tv["name"], tv["host"]) for tv in found] == [("annex", "10.0.0.50"), ("Boardroom TV", "10.0.0.9"),
                                                          ("Boardroom TV", "10.0.0.12"), ("", "10.0.0.60")]


async def test_the_header_names_may_be_in_any_case_and_the_target_too():
    answer = ssdp_answer(LG_LOCATION).decode().replace("ST:", "st:").replace("LOCATION:", "Location:").replace(
        SEARCH_TARGET, SEARCH_TARGET.upper()).encode()
    found = await discover_tvs(send=answering((answer, LG)), fetch=serving({LG_LOCATION: description()}))
    assert [tv["name"] for tv in found] == ["Boardroom TV"]


@pytest.mark.parametrize("datagram", [
    b"", b"\xff\xfe\x00garbage", b"HTTP/1.1 200 OK\r\n\r\n",                           # nothing, noise, an answer with no target
    b"NOTIFY * HTTP/1.1\r\nNT: " + SEARCH_TARGET.encode() + b"\r\nLOCATION: " + LG_LOCATION.encode() + b"\r\n\r\n",    # an announcement
    ssdp_answer(LG_LOCATION, st="ssdp:all"), ssdp_answer(LG_LOCATION, st=SEARCH_TARGET + ":extra"),
    ssdp_answer(LG_LOCATION, st="urn:schemas-upnp-org:device:MediaRenderer:1"),
    # carrying the target is not answering for it: a request is somebody's search or announcement, and an error is no TV
    b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: " + SEARCH_TARGET.encode()
    + b"\r\nLOCATION: " + LG_LOCATION.encode() + b"\r\n\r\n",
    b"NOTIFY * HTTP/1.1\r\nST: " + SEARCH_TARGET.encode() + b"\r\nLOCATION: " + LG_LOCATION.encode() + b"\r\n\r\n",
    ssdp_answer(LG_LOCATION).replace(b"200 OK", b"404 Not Found"), ssdp_answer(LG_LOCATION).replace(b"HTTP/1.1 200 OK\r\n", b""),
])
async def test_what_does_not_answer_the_search_for_a_webos_tv_is_ignored(datagram):
    fetch = serving({LG_LOCATION: description()})
    assert await discover_tvs(send=answering((datagram, LG)), fetch=fetch) == []
    assert fetch.asked == []


@pytest.mark.parametrize("how", ["bare newlines", "spaces before the colons", "both"])
async def test_an_answer_with_bare_newlines_or_spaces_before_the_colons_is_still_an_answer(how):
    """Devices are not all as careful as the specification: the lines may end in a newline alone, and a header's name may be
    followed by a space before its colon."""
    answer = ssdp_answer(LG_LOCATION)
    if how != "spaces before the colons":
        answer = answer.replace(b"\r\n", b"\n")
    if how != "bare newlines":
        answer = answer.replace(b"ST:", b"ST :").replace(b"LOCATION:", b"LOCATION :")
    found = await discover_tvs(send=answering((answer, LG)), fetch=serving({LG_LOCATION: description()}))
    assert found == [{"host": LG, "name": "Boardroom TV", "location": LG_LOCATION}]


# The name

async def test_a_description_that_cannot_be_fetched_gives_the_host_with_no_name():
    found = await discover_tvs(send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({}))
    assert found == [{"host": LG, "name": "", "location": LG_LOCATION}]


@pytest.mark.parametrize("page", [
    "not xml at all", "", "<root><device></root>", description("", tag="friendlyName"), description(tag="modelNumber"),
    ConnectionResetError("reset"), OSError("Network is unreachable"), ValueError("too long"),
])
async def test_a_description_with_no_name_in_it_gives_the_host_with_no_name(page):
    found = await discover_tvs(send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: page}))
    assert found == [{"host": LG, "name": "", "location": LG_LOCATION}]


async def test_a_description_that_never_arrives_is_cut_off_and_the_others_are_read(monkeypatch):
    monkeypatch.setattr(discover, "FETCH_TIMEOUT_S", 0.2)
    other = "http://10.0.0.50:1234/d.xml"

    async def fetch(url):
        if url == LG_LOCATION:
            await asyncio.sleep(30)                                # a TV that took the connection and says nothing
        return description("Training room")
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    found = await within(discover_tvs(send=answering((ssdp_answer(LG_LOCATION), LG), (ssdp_answer(other), "10.0.0.50")), fetch=fetch), 5.0)
    assert loop.time() - t0 < 1.0
    assert found == [{"host": "10.0.0.50", "name": "Training room", "location": other}, {"host": LG, "name": "", "location": LG_LOCATION}]


async def test_descriptions_are_read_together_so_that_a_search_takes_one_fetch_timeout_not_one_per_tv(monkeypatch):
    monkeypatch.setattr(discover, "FETCH_TIMEOUT_S", 0.3)

    async def fetch(url):
        await asyncio.sleep(30)                                   # TVs that take the connection and say nothing
    hosts = [f"10.0.0.{n}" for n in range(10, 20)]
    send = answering(*[(ssdp_answer(f"http://{h}:1234/d.xml"), h) for h in hosts])
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    found = await within(discover_tvs(send=send, fetch=fetch), 5.0)
    assert len(found) == 10 and loop.time() - t0 < 1.0            # ten in a row would be three seconds


@pytest.mark.parametrize("markup, name", [
    ("Boardroom TV", "Boardroom TV"),
    ("  Board\n   room\tTV  ", "Board room TV"),                                          # as it is shown: one line, one space
    ("Salle de réunion", "Salle de réunion"),
    ("Tom &amp; Jerry's &lt;TV&gt;", "Tom & Jerry's <TV>"),
    ("<![CDATA[Boardroom TV]]>", "Boardroom TV"),
    ("x" * 300, "x" * 100),                                                              # a line, not a page
])
async def test_the_name_is_tidied(markup, name):
    found = await discover_tvs(send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: description(markup)}))
    assert found[0]["name"] == name


@pytest.mark.parametrize("escape", ["\x1b[2J", "&#27;[2J"])
async def test_a_name_with_a_terminal_escape_in_it_is_no_name(escape):
    """Names are printed on a terminal (croom --check-tv): the escape is not XML, so the description is not one."""
    found = await discover_tvs(send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: description(f"Board{escape}room")}))
    assert found == [{"host": LG, "name": "", "location": LG_LOCATION}]


async def test_a_name_with_other_control_characters_in_it_is_printable_when_it_is_listed():
    page = description("Boardroom&#x7f;&#x9b; TV")                                          # DEL and a C1 escape, which XML allows
    found = await discover_tvs(send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: page}))
    assert found[0]["name"].isprintable() and found[0]["name"].startswith("Boardroom") and found[0]["name"].endswith("TV")


async def test_the_name_is_the_first_the_description_has_which_is_the_tvs_own():
    page = ("<root xmlns=\"urn:schemas-upnp-org:device-1-0\"><device><friendlyName>Boardroom TV</friendlyName><deviceList><device>"
            "<friendlyName>An embedded device</friendlyName></device></deviceList></device></root>")
    found = await discover_tvs(send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: page}))
    assert found[0]["name"] == "Boardroom TV"


async def test_the_name_may_come_with_a_namespace_prefix():
    page = ('<?xml version="1.0"?><u:root xmlns:u="urn:schemas-upnp-org:device-1-0"><u:device>'
            "<u:friendlyName>Boardroom TV</u:friendlyName></u:device></u:root>")
    found = await discover_tvs(send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: page}))
    assert found[0]["name"] == "Boardroom TV"


async def test_a_description_with_a_doctype_is_not_read_and_nothing_in_it_is_expanded():
    bomb = ('<?xml version="1.0"?><!DOCTYPE root [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            "<root><device><friendlyName>&b;</friendlyName></device></root>")
    found = await discover_tvs(send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: bomb}))
    assert found == [{"host": LG, "name": "", "location": LG_LOCATION}]


# Where a description is read from

async def test_an_answer_that_sends_the_reader_to_another_address_is_not_followed():
    """A device on the network can say anything in LOCATION: a description is read from the address that answered, and from
    no other, so that a device cannot have this one ask something else on the network for it."""
    elsewhere = "http://10.0.0.99:8080/admin"
    fetch = serving({elsewhere: description("Not the TV")})
    found = await discover_tvs(send=answering((ssdp_answer(elsewhere), LG)), fetch=fetch)
    assert found == [{"host": LG, "name": "", "location": ""}]
    assert fetch.asked == []


@pytest.mark.parametrize("location", ["ftp://10.0.0.41/d.xml", "file:///etc/passwd", "10.0.0.41:1234/d.xml", "http://[::1", "javascript:alert(1)"])
async def test_a_location_that_is_not_a_web_address_on_the_tv_is_not_followed(location):
    fetch = serving({location: description()})
    found = await discover_tvs(send=answering((ssdp_answer(location), LG)), fetch=fetch)
    assert found == [{"host": LG, "name": "", "location": ""}]
    assert fetch.asked == []


async def test_an_answer_with_no_location_is_listed_with_no_name():
    fetch = serving({})
    found = await discover_tvs(send=answering((ssdp_answer(None), LG)), fetch=fetch)
    assert found == [{"host": LG, "name": "", "location": ""}] and fetch.asked == []


async def test_a_second_answer_with_a_location_fills_in_for_a_first_that_had_none():
    send = answering((ssdp_answer(None), LG), (ssdp_answer(LG_LOCATION), LG))
    assert await discover_tvs(send=send, fetch=serving({LG_LOCATION: description()})) == [
        {"host": LG, "name": "Boardroom TV", "location": LG_LOCATION}]


async def test_a_second_answer_with_no_location_does_not_take_the_first_ones_away():
    send = answering((ssdp_answer(LG_LOCATION), LG), (ssdp_answer(None), LG))
    assert await discover_tvs(send=send, fetch=serving({LG_LOCATION: description()})) == [
        {"host": LG, "name": "Boardroom TV", "location": LG_LOCATION}]


async def test_a_location_with_characters_a_terminal_would_act_on_is_not_listed():
    location = "http://10.0.0.41/\x1b[2Jx"
    found = await discover_tvs(send=answering((ssdp_answer(location), LG)), fetch=serving({location: description()}))
    assert found == [{"host": LG, "name": "", "location": ""}]


async def test_a_flood_of_answers_does_not_become_a_flood_of_requests():
    answers = [(ssdp_answer(f"http://10.1.{n // 250}.{n % 250}:1234/d.xml"), f"10.1.{n // 250}.{n % 250}") for n in range(500)]
    fetch = serving({})
    found = await discover_tvs(send=answering(*answers), fetch=fetch)
    assert len(found) == len(fetch.asked) == discover.MAX_TVS


# A search that goes wrong

async def test_a_search_that_does_not_end_is_cut_off_and_what_came_before_stands(monkeypatch):
    monkeypatch.setattr(discover, "SEND_GRACE_S", 0.1)
    fetch = serving({LG_LOCATION: description()})

    async def send(request, on_answer, window):
        on_answer(ssdp_answer(LG_LOCATION), LG)
        await asyncio.sleep(30)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    found = await within(discover_tvs(timeout=0.1, send=send, fetch=fetch), 5.0)
    assert 0.15 <= loop.time() - t0 < 1.0
    assert found == [{"host": LG, "name": "Boardroom TV", "location": LG_LOCATION}]


async def test_a_search_that_cannot_be_sent_is_words_and_not_an_empty_list():
    async def send(request, on_answer, window):
        raise TvError("The search for TVs could not be sent: Network is unreachable")
    with pytest.raises(TvError) as failure:
        await discover_tvs(send=send, fetch=serving({}))
    assert "Network is unreachable" in str(failure.value)


async def test_a_cancelled_search_is_cancelled():
    async def send(request, on_answer, window):
        await asyncio.sleep(30)
    task = asyncio.ensure_future(discover_tvs(send=send, fetch=serving({})))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await within(task, 2.0)


# The real search: a datagram endpoint on the loop, which these tests replace with one that is not on a network

class FakeSocket:
    def __init__(self):
        self.options = []

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))


class FakeEndpoint:
    """What the loop's create_datagram_endpoint hands back: a transport that takes datagrams, and answers each one (as a network
    does) by handing the protocol the datagrams it was given to answer with."""

    def __init__(self, answers=(), send_error=None):
        self.answers, self.send_error = answers, send_error
        self.sent, self.closed, self.socket, self.kwargs, self.protocol = [], False, FakeSocket(), None, None

    async def create(self, factory, **kwargs):
        self.kwargs, self.protocol = kwargs, factory()
        return self, self.protocol

    def get_extra_info(self, name):
        return self.socket if name == "socket" else None

    def sendto(self, data, address):
        self.sent.append((data, address))
        if self.send_error is not None:
            self.protocol.error_received(self.send_error)       # as asyncio hands a send that failed to the protocol
            return
        for datagram, host in self.answers:
            self.protocol.datagram_received(datagram, (host, 1900))

    def close(self):
        self.closed = True


@pytest.fixture
def endpoint(monkeypatch):
    """Put a FakeEndpoint where the loop's datagram endpoint is: `endpoint.use(...)` says what it answers."""
    class Holder:
        def use(self, **kwargs):
            self.fake = FakeEndpoint(**kwargs)
            monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", self.fake.create)
            return self.fake
    return Holder()


async def test_the_real_search_goes_out_twice_to_the_ssdp_group_from_a_port_of_its_own_and_is_closed_after(endpoint):
    fake = endpoint.use(answers=[(ssdp_answer(LG_LOCATION), LG)])
    heard = []
    await discover._multicast(discover.SEARCH, lambda datagram, host: heard.append((datagram, host)), 0.3)
    assert fake.sent == [(discover.SEARCH, SSDP_ADDRESS)] * 2    # one datagram can be lost: UPnP has a search sent more than once
    assert heard == [(ssdp_answer(LG_LOCATION), LG)] * 2         # (and each is answered)
    assert fake.closed
    assert fake.kwargs == {"local_addr": ("0.0.0.0", 0)}          # a port of its own: 1900 belongs to whatever else listens there
    assert fake.socket.options == [(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)]


async def test_the_real_search_takes_the_window_it_is_given(endpoint):
    endpoint.use()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await discover._multicast(discover.SEARCH, lambda datagram, host: None, 0.4)
    assert 0.35 <= loop.time() - t0 < 0.9


async def test_the_second_search_goes_out_half_a_second_after_the_first_and_the_answers_are_waited_for_to_the_end_of_the_window(endpoint):
    fake = endpoint.use()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    task = asyncio.ensure_future(discover._multicast(discover.SEARCH, lambda datagram, host: None, 1.2))
    await asyncio.sleep(0.25)
    assert len(fake.sent) == 1                                    # the second is not sent at once ...
    await asyncio.sleep(0.55)
    assert len(fake.sent) == 2                                    # ... but half a second after the first
    await within(task, 3.0)
    assert 1.15 <= loop.time() - t0 < 1.9                         # and the window is the window, not the time it took to send


async def test_the_real_search_is_closed_when_it_is_cut_off(endpoint):
    fake = endpoint.use()
    task = asyncio.ensure_future(discover._multicast(discover.SEARCH, lambda datagram, host: None, 30))
    await asyncio.sleep(0.05)
    assert fake.sent and not fake.closed
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await within(task, 2.0)
    assert fake.closed


async def test_a_network_that_will_not_take_the_search_is_words(endpoint):
    fake = endpoint.use(send_error=OSError(101, "Network is unreachable"))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(TvError) as failure:
        await discover._multicast(discover.SEARCH, lambda datagram, host: None, 30)
    assert "could not be sent" in str(failure.value) and "Network is unreachable" in str(failure.value)
    assert loop.time() - t0 < 1.0 and fake.closed                # it did not wait out the window for answers that will not come


async def test_an_endpoint_that_cannot_be_made_is_words(monkeypatch):
    async def refuses(factory, **kwargs):
        raise OSError(24, "Too many open files")
    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", refuses)
    with pytest.raises(TvError) as failure:
        await discover._multicast(discover.SEARCH, lambda datagram, host: None, 0.1)
    assert "could not be started" in str(failure.value) and "Too many open files" in str(failure.value)


async def test_a_socket_that_will_not_take_its_options_is_words_and_is_closed(endpoint):
    fake = endpoint.use()
    fake.socket.setsockopt = lambda *args: (_ for _ in ()).throw(OSError(22, "Invalid argument"))
    with pytest.raises(TvError) as failure:
        await discover._multicast(discover.SEARCH, lambda datagram, host: None, 0.1)
    assert "Invalid argument" in str(failure.value) and fake.closed


async def test_the_real_search_through_discover_tvs_finds_a_tv(endpoint):
    endpoint.use(answers=[(ssdp_answer(LG_LOCATION), LG)])
    found = await discover_tvs(timeout=0.2, fetch=serving({LG_LOCATION: description()}))
    assert found == [{"host": LG, "name": "Boardroom TV", "location": LG_LOCATION}]


# The real fetch: the description, over HTTP, as much of it as a description needs

class FakeResponse:
    def __init__(self, chunks, status=200):
        self.chunks, self.status = chunks, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def content(self):
        return self

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def web(monkeypatch):
    """Put a stand-in for aiohttp's ClientSession where discover's real fetch looks for it; `web.respond(...)` says what it gets."""
    import aiohttp

    class Holder:
        asked = []
        response = FakeResponse([])
        session_args = None

        def respond(self, chunks, status=200):
            self.response = FakeResponse(chunks, status)

    holder = Holder()

    class Session:
        def __init__(self, **kwargs):
            holder.session_args = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            holder.asked.append((url, kwargs))
            return holder.response
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    return holder


async def test_the_real_fetch_gets_the_description_and_follows_no_redirect(web):
    web.respond([description("Boardroom").encode()[:50], description("Boardroom").encode()[50:]])
    assert await discover._get(LG_LOCATION) == description("Boardroom")
    assert web.asked == [(LG_LOCATION, {"allow_redirects": False})]       # a TV that says "look over there" is not followed


async def test_the_real_fetch_reads_the_description_as_utf_8_whatever_the_chunks_do_to_a_letter(web):
    raw = description("Salle de réunion").encode("utf-8")
    cut = raw.index("é".encode("utf-8")) + 1                                       # in the middle of the é
    web.respond([raw[:cut], raw[cut:]])
    assert await discover._get(LG_LOCATION) == description("Salle de réunion")


async def test_the_real_fetch_reads_no_more_than_a_description_needs(web):
    web.respond([b"x" * 30000, b"x" * 30000, b"x" * 30000])
    with pytest.raises(ValueError):
        await discover._get(LG_LOCATION)


@pytest.mark.parametrize("status", [301, 302, 204, 404, 500])
async def test_the_real_fetch_takes_nothing_but_a_200_for_a_description(web, status):
    """An error page is an error, and so is a redirect that was not followed: its body is not the description."""
    web.respond([description().encode()], status=status)
    with pytest.raises(Exception) as failure:
        await discover._get(LG_LOCATION)
    assert str(status) in str(failure.value)


async def test_the_real_fetch_does_not_decompress_what_a_device_sends(web):
    """A description is plain XML: a body that says it is compressed is not unpacked on the way (a few kilobytes can unpack to
    gigabytes), and is not a description either."""
    web.respond([description().encode()])
    await discover._get(LG_LOCATION)
    assert web.session_args == {"auto_decompress": False}


async def test_the_real_search_and_the_real_fetch_are_what_discover_tvs_uses_unless_it_is_told_otherwise(endpoint, web):
    endpoint.use(answers=[(ssdp_answer(LG_LOCATION), LG)])
    web.respond([description("Boardroom TV").encode()])
    assert await discover_tvs(timeout=0.2) == [{"host": LG, "name": "Boardroom TV", "location": LG_LOCATION}]
    assert [url for url, kwargs in web.asked] == [LG_LOCATION]
