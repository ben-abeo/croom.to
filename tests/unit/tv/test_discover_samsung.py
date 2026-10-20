"""Samsung discovery and MAC lookup stay within each answering device's address."""
import asyncio
import json

import pytest

from croom.tv import discover
from croom.tv.errors import TvError
from tests.unit.tv.test_discover import LG, LG_LOCATION, answering, description, serving, ssdp_answer

SAMSUNG = "10.0.0.55"
LOCATION = f"http://{SAMSUNG}:9197/dmr"
API = f"http://{SAMSUNG}:8001/api/v2/"
RENDERING = "urn:schemas-upnp-org:service:RenderingControl:1"
REMOTE = "urn:samsung.com:device:RemoteControlReceiver:1"
AGENT = "urn:samsung.com:service:MainTVAgent2:1"


def samsung_description(manufacturer="Samsung Electronics", name="Living room S90HD"):
    return (
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        "<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
        f"<friendlyName>{name}</friendlyName><manufacturer>{manufacturer}</manufacturer>"
        "<modelName>Samsung S90HD</modelName></device></root>"
    )


def api_description(**device_fields):
    return json.dumps({"device": {
        "type": "Samsung SmartTV", "name": "Living room S90HD", "modelName": "QN65S90H",
        "ip": SAMSUNG, "networkType": "wireless", "wifiMac": "a8-bb-cc-dd-ee-10",
        **device_fields,
    }})


async def no_mac(host):
    return ""


@pytest.mark.parametrize("target", [REMOTE, AGENT, RENDERING])
async def test_each_samsung_service_finds_a_manufacturer_verified_tv(target):
    fetch = serving({LOCATION: samsung_description(), API: api_description()})
    found = await discover.discover_tvs(
        send=answering((ssdp_answer(LOCATION, target), SAMSUNG)), fetch=fetch)
    assert found == [{
        "host": SAMSUNG, "name": "Living room S90HD", "location": LOCATION,
        "brand": "samsung", "mac": "a8:bb:cc:dd:ee:10",
    }]


@pytest.mark.parametrize("page", [
    samsung_description("Sonos"),
    samsung_description("LG Electronics"),
    samsung_description("Not Samsung Electronics"),
    samsung_description(""),
    "<root><device></root>",
    '<!DOCTYPE root [<!ENTITY name "Samsung">]><root><device><manufacturer>&name;</manufacturer></device></root>',
    ConnectionError("unreachable"),
])
async def test_samsung_services_require_a_safe_description_naming_the_manufacturer(page):
    fetch = serving({LOCATION: page, API: api_description()})
    found = await discover.discover_tvs(
        send=answering((ssdp_answer(LOCATION, RENDERING), SAMSUNG)), fetch=fetch)
    assert found == []
    assert fetch.asked == [LOCATION]


@pytest.mark.parametrize("location", [
    "http://10.0.0.99:9197/dmr", "http://samsung.example/dmr",
    "file:///etc/passwd", "http://10.0.0.55/\x1b[2J", "",
    "http://user:password@10.0.0.55/dmr",
])
async def test_an_unsafe_samsung_location_is_not_followed_or_used_for_brand_validation(location):
    fetch = serving({location: samsung_description(), API: api_description()})
    assert await discover.discover_tvs(
        send=answering((ssdp_answer(location, RENDERING), SAMSUNG)), fetch=fetch) == []
    assert fetch.asked == []


async def test_multiple_services_and_repeated_responses_are_one_tv_with_one_description():
    fetch = serving({LOCATION: samsung_description(), API: api_description()})
    send = answering(*[(ssdp_answer(LOCATION, target), SAMSUNG) for target in (RENDERING, REMOTE, AGENT, REMOTE)])
    found = await discover.discover_tvs(send=send, fetch=fetch)
    assert len(found) == 1
    assert fetch.asked == [LOCATION, API]


async def test_lg_and_samsung_searches_run_together_and_both_brands_are_named():
    started = set()
    all_started = asyncio.Event()

    async def send(request, on_answer, window):
        target = next(line[4:] for line in request.decode().splitlines() if line.startswith("ST: "))
        started.add(target)
        if len(started) == 4:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), 0.5)
        if target == discover.SEARCH_TARGET:
            on_answer(ssdp_answer(LG_LOCATION), LG)
        elif target == RENDERING:
            on_answer(ssdp_answer(LOCATION, RENDERING), SAMSUNG)

    found = await discover.discover_tvs(send=send, fetch=serving({
        LG_LOCATION: description(), LOCATION: samsung_description(), API: api_description(),
    }))
    assert started == {discover.SEARCH_TARGET, REMOTE, AGENT, RENDERING}
    assert [(tv["host"], tv["brand"]) for tv in found] == [(LG, "lg"), (SAMSUNG, "samsung")]


async def test_lg_specific_response_wins_over_generic_rendering_control_for_same_host():
    fetch = serving({LG_LOCATION: description()})
    found = await discover.discover_tvs(
        send=answering((ssdp_answer(LG_LOCATION, RENDERING), LG), (ssdp_answer(LG_LOCATION), LG)),
        fetch=fetch,
    )
    assert found == [{"host": LG, "name": "Boardroom TV", "location": LG_LOCATION, "brand": "lg", "mac": ""}]


async def test_a_failed_brand_search_does_not_discard_other_brands_answers():
    async def send(request, on_answer, window):
        if discover.SEARCH_TARGET.encode() in request:
            raise TvError("LG search failed")
        on_answer(ssdp_answer(LOCATION, REMOTE), SAMSUNG)

    found = await discover.discover_tvs(
        send=send, fetch=serving({LOCATION: samsung_description(), API: api_description()}))
    assert [(tv["host"], tv["brand"]) for tv in found] == [(SAMSUNG, "samsung")]


async def test_neighbor_mac_for_the_discovered_host_wins_over_reported_wifi_mac():
    hosts = []

    async def resolver(host):
        hosts.append(host)
        return "AA-BB-CC-DD-EE-22"

    found = await discover.discover_tvs(
        send=answering((ssdp_answer(LOCATION, RENDERING), SAMSUNG)),
        fetch=serving({LOCATION: samsung_description(), API: api_description()}), mac_resolver=resolver)
    assert found[0]["mac"] == "aa:bb:cc:dd:ee:22"
    assert hosts == [SAMSUNG]


async def test_neighbor_mac_is_also_enriched_for_lg():
    async def resolver(host):
        return "aabbccddee22"

    found = await discover.discover_tvs(
        send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: description()}),
        mac_resolver=resolver)
    assert found[0]["mac"] == "aa:bb:cc:dd:ee:22"


@pytest.mark.parametrize("device_fields", [
    {"networkType": "wired"},
    {"networkType": ""},
    {"ip": "10.0.0.99"},
    {"type": "Samsung Soundbar"},
    {"wifiMac": "none"},
    {"wifiMac": "00:00:00:00:00:00"},
    {"wifiMac": "ff:ff:ff:ff:ff:ff"},
    {"wifiMac": "01:00:5e:00:00:01"},
    {"wifiMac": "not a mac"},
])
async def test_unverified_wifi_mac_does_not_become_the_wake_address(device_fields):
    found = await discover.discover_tvs(
        send=answering((ssdp_answer(LOCATION, RENDERING), SAMSUNG)),
        fetch=serving({LOCATION: samsung_description(), API: api_description(**device_fields)}))
    assert found[0]["mac"] == ""


@pytest.mark.parametrize("page", ["broken json", "[]", '{"device":[]}', ConnectionError("closed")])
async def test_bad_samsung_api_data_leaves_a_usable_tv_without_a_mac(page):
    found = await discover.discover_tvs(
        send=answering((ssdp_answer(LOCATION, REMOTE), SAMSUNG)),
        fetch=serving({LOCATION: samsung_description(), API: page}))
    assert found[0]["brand"] == "samsung"
    assert found[0]["mac"] == ""


async def test_injected_discovery_never_launches_a_real_mac_lookup(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("injected discovery attempted a real subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected)
    found = await discover.discover_tvs(
        send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: description()}))
    assert found[0]["mac"] == ""


async def test_a_slow_mac_lookup_is_cut_off_without_losing_the_tv(monkeypatch):
    monkeypatch.setattr(discover, "MAC_TIMEOUT_S", 0.05, raising=False)

    async def slow(host):
        await asyncio.sleep(30)

    found = await asyncio.wait_for(discover.discover_tvs(
        send=answering((ssdp_answer(LG_LOCATION), LG)), fetch=serving({LG_LOCATION: description()}),
        mac_resolver=slow), 0.5)
    assert found[0]["mac"] == ""


async def test_a_slow_samsung_api_is_cut_off_without_losing_the_tv(monkeypatch):
    monkeypatch.setattr(discover, "FETCH_TIMEOUT_S", 0.05)

    async def fetch(url):
        if url == LOCATION:
            return samsung_description()
        await asyncio.sleep(30)

    found = await asyncio.wait_for(discover.discover_tvs(
        send=answering((ssdp_answer(LOCATION, RENDERING), SAMSUNG)), fetch=fetch), 0.5)
    assert found[0]["mac"] == ""
    assert found[0]["name"] == "Living room S90HD"


class NeighborProcess:
    def __init__(self, output=b"", delay=0, returncode=0):
        self.output, self.delay, self.exit_code = output, delay, returncode
        self.returncode = None
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(self.delay)
        self.returncode = self.exit_code
        return self.output, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        self.returncode = self.exit_code
        return self.returncode


@pytest.mark.parametrize("output, expected", [
    (b"10.0.0.55 dev eth0 lladdr A8:BB:CC:DD:EE:10 REACHABLE\n", "a8:bb:cc:dd:ee:10"),
    (b"10.0.0.55 dev wlan0 lladdr a8:bb:cc:dd:ee:10 STALE\n", "a8:bb:cc:dd:ee:10"),
    (b"10.0.0.99 dev eth0 lladdr a8:bb:cc:dd:ee:10 REACHABLE\n", ""),
    (b"10.0.0.55 dev eth0 INCOMPLETE\n", ""),
    (b"10.0.0.55 dev eth0 lladdr a8:bb:cc:dd:ee:10 FAILED\n", ""),
    (b"10.0.0.55 dev eth0 lladdr ff:ff:ff:ff:ff:ff REACHABLE\n", ""),
    (b"", ""),
])
async def test_resolve_mac_reads_only_the_matching_host_from_linux_neighbors(monkeypatch, output, expected):
    calls = []

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return NeighborProcess(output)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await discover.resolve_mac(SAMSUNG) == expected
    assert calls == [(("ip", "neigh", "show", "to", SAMSUNG), {
        "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.DEVNULL,
    })]


@pytest.mark.parametrize("host", ["--help", "10.0.0.55;whoami", "tv.example", "127.0.0.1", "224.0.0.1", "0.0.0.0"])
async def test_mac_lookup_rejects_non_device_addresses_without_running_a_command(monkeypatch, host):
    async def create(*args, **kwargs):
        pytest.fail("unsafe host reached subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await discover.resolve_mac(host) == ""


async def test_unavailable_neighbor_command_leaves_mac_unknown(monkeypatch):
    async def create(*args, **kwargs):
        raise FileNotFoundError("ip not installed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await discover.resolve_mac(SAMSUNG) == ""


async def test_a_hung_neighbor_command_is_killed_and_reaped(monkeypatch):
    monkeypatch.setattr(discover, "MAC_TIMEOUT_S", 0.05, raising=False)
    process = NeighborProcess(delay=30)

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await asyncio.wait_for(discover.resolve_mac(SAMSUNG), 0.5) == ""
    assert process.killed
