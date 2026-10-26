"""Samsung wire protocol and lifecycle, with a TV transport that never opens a socket."""

import asyncio
import importlib.util
from copy import deepcopy

import aiohttp
import pytest
from yarl import URL

from croom.tv.errors import TvError


class TvSocket:
    def __init__(self, events):
        self.incoming = asyncio.Queue()
        for event in events:
            self.incoming.put_nowait(event)
        self.sent = []
        self.closed = False
        self.close_started = asyncio.Event()
        self.hang_on_close = False

    async def receive(self):
        event = await self.incoming.get()
        if event is None:
            self.closed = True
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)
        import json
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(event), None)

    async def send_json(self, payload):
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(deepcopy(payload))

    async def close(self):
        self.close_started.set()
        if self.hang_on_close:
            await asyncio.Event().wait()
        self.closed = True


class DeviceResponse:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        if self.session.rest_error:
            raise self.session.rest_error
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        return None

    async def json(self, **kwargs):
        self.session.read_started.set()
        if self.session.rest_gate is not None:
            try:
                await self.session.rest_gate.wait()
            except asyncio.CancelledError:
                self.session.read_cancelled.set()
                raise
        return deepcopy(self.session.device_info)


class TvSession:
    def __init__(self, socket):
        self.socket = socket
        self.closed = False
        self.opened = []
        self.requested = []
        self.rest_error = None
        self.rest_gate = None
        self.read_started = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.device_info = {
            "device": {
                "name": "[TV] Samsung S90HD",
                "modelName": "QN65S90H",
                "PowerState": "on",
            }
        }

    async def ws_connect(self, url, **kwargs):
        self.opened.append((URL(url), kwargs))
        return self.socket

    def get(self, url, **kwargs):
        self.requested.append((URL(url), kwargs))
        return DeviceResponse(self)

    async def close(self):
        self.closed = True


def make_client(monkeypatch, events=None, key=None):
    assert importlib.util.find_spec("croom.tv.samsung") is not None, "Samsung adapter is missing"
    from croom.tv import samsung

    socket = TvSocket(events if events is not None else [
        {"event": "ms.channel.connect", "data": {"token": "issued-123"}},
    ])
    session = TvSession(socket)
    monkeypatch.setattr(samsung.aiohttp, "ClientSession", lambda **kwargs: session)
    return samsung.SamsungClient("192.0.2.41", key), session, socket


async def test_pairs_on_secure_local_channel_and_retains_token_and_device_name(monkeypatch):
    client, session, socket = make_client(monkeypatch, [
        {"event": "ed.edenTV.update", "data": {}},
        {"event": "ms.channel.connect", "data": {"token": "issued-123"}},
    ])
    try:
        assert await client.connect() is True
        assert client.is_connected()
        assert client.client_key == "issued-123"
        url, options = session.opened[0]
        assert (url.scheme, url.host, url.port, url.path) == (
            "wss", "192.0.2.41", 8002, "/api/v2/channels/samsung.remote.control",
        )
        assert dict(url.query) == {"name": "Q3J5c3RhbCBNZWV0"}
        assert options["ssl"] is False
        assert client.tv_info.hello_info == {"deviceName": "[TV] Samsung S90HD"}
        assert client.tv_info.system_info == {"modelName": "QN65S90H"}
        assert client.tv_state.is_on is True
    finally:
        await client.disconnect()
    assert socket.closed and session.closed and not client.is_connected()


async def test_reconnect_sends_saved_token_and_preserves_it_when_no_new_token_arrives(monkeypatch):
    client, session, _ = make_client(monkeypatch, [
        {"event": "ms.channel.connect", "data": {}},
    ], key="saved +/&")
    try:
        await client.connect()
        assert session.opened[0][0].query["token"] == "saved +/&"
        assert client.client_key == "saved +/&"
        await client.connect()
        assert len(session.opened) == 1
    finally:
        await client.disconnect()


async def test_declined_pairing_closes_every_resource(monkeypatch):
    client, session, socket = make_client(monkeypatch, [
        {"event": "ms.channel.unauthorized", "data": {}},
    ])
    with pytest.raises(ConnectionRefusedError):
        await client.connect()
    assert not client.is_connected()
    assert session.closed and socket.closed


async def test_cancelled_pairing_closes_socket_and_session(monkeypatch):
    client, session, socket = make_client(monkeypatch, [])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.connect(), .02)
    assert not client.is_connected()
    assert session.closed and socket.closed


async def test_remote_close_notifies_service_without_waiting_for_next_command(monkeypatch):
    client, session, socket = make_client(monkeypatch)
    gone = asyncio.Event()

    async def on_state(state):
        if not client.is_connected():
            gone.set()

    await client.register_state_update_callback(on_state)
    try:
        await client.connect()
        socket.incoming.put_nowait(None)
        await asyncio.wait_for(gone.wait(), 1)
        assert client.tv_state.is_on is False
        assert not client.is_connected()
        with pytest.raises(ConnectionError):
            await client.button("HOME")
    finally:
        await client.disconnect()
    assert session.closed


async def test_disconnect_is_bounded_when_tv_never_finishes_close(monkeypatch):
    client, session, socket = make_client(monkeypatch)
    await client.connect()
    socket.hang_on_close = True
    await asyncio.wait_for(client.disconnect(), 3)
    assert session.closed
    assert not client.is_connected()


@pytest.mark.parametrize("name,wire", [
    ("LEFT", "KEY_LEFT"), ("RIGHT", "KEY_RIGHT"), ("UP", "KEY_UP"), ("DOWN", "KEY_DOWN"),
    ("ENTER", "KEY_ENTER"), ("BACK", "KEY_RETURN"), ("HOME", "KEY_HOME"),
    ("MENU", "KEY_MENU"), ("QMENU", "KEY_TOOLS"), ("EXIT", "KEY_EXIT"),
    ("INFO", "KEY_INFO"), ("GUIDE", "KEY_GUIDE"), ("LIST", "KEY_CH_LIST"),
    ("INPUT_HUB", "KEY_SOURCE"), ("MUTE", "KEY_MUTE"),
    ("VOLUMEUP", "KEY_VOLUP"), ("VOLUMEDOWN", "KEY_VOLDOWN"),
    ("CHANNELUP", "KEY_CHUP"), ("CHANNELDOWN", "KEY_CHDOWN"),
    ("PLAY", "KEY_PLAY"), ("PAUSE", "KEY_PAUSE"), ("STOP", "KEY_STOP"),
    ("REWIND", "KEY_REWIND"), ("FASTFORWARD", "KEY_FF"),
    ("RED", "KEY_RED"), ("GREEN", "KEY_GREEN"), ("YELLOW", "KEY_YELLOW"),
    ("BLUE", "KEY_BLUE"), ("7", "KEY_7"), ("POWER", "KEY_POWEROFF"),
])
async def test_remote_buttons_use_samsung_wire_names(monkeypatch, name, wire):
    client, _, socket = make_client(monkeypatch)
    try:
        await client.connect()
        await client.button(name)
        assert socket.sent == [{
            "method": "ms.remote.control",
            "params": {"Cmd": "Click", "DataOfCmd": wire, "Option": "false",
                       "TypeOfRemote": "SendRemoteKey"},
        }]
        assert name in client.supported_keys
    finally:
        await client.disconnect()


async def test_commands_never_invent_volume_mute_or_input_readbacks(monkeypatch):
    client, _, socket = make_client(monkeypatch)
    try:
        await client.connect()
        await client.volume_up()
        await client.volume_down()
        await client.channel_up()
        await client.channel_down()
        await client.button("MUTE")
        await client.set_input("SOURCE")
        assert [message["params"]["DataOfCmd"] for message in socket.sent] == [
            "KEY_VOLUP", "KEY_VOLDOWN", "KEY_CHUP", "KEY_CHDOWN", "KEY_MUTE", "KEY_SOURCE",
        ]
        assert await client.get_volume() is None
        assert await client.get_muted() is None
        assert await client.get_input() is None
        assert await client.get_current_app() is None
        assert await client.get_current_channel() == {}
        assert client.tv_state.volume is None and client.tv_state.muted is None
        assert client.tv_state.current_input is None
        assert await client.get_inputs() == [{"id": "SOURCE", "label": "Source menu"}]
    finally:
        await client.disconnect()


@pytest.mark.parametrize("method,args,match", [
    ("set_volume", (30,), "volume"),
    ("set_mute", (True,), "mute"),
    ("set_input", ("HDMI_2",), "Source"),
    ("move", (10, 20), "pointer"),
    ("click", (), "pointer"),
    ("scroll", (0, 10), "pointer"),
    ("send_message", ("Meeting starts",), "message"),
    ("button", ("LIVE_ZOOM",), "key"),
    ("button", ("KEY_FACTORY",), "key"),
])
async def test_unsupported_commands_report_the_limit_without_sending_anything(
    monkeypatch, method, args, match,
):
    client, _, socket = make_client(monkeypatch)
    try:
        await client.connect()
        with pytest.raises(TvError, match=match):
            await getattr(client, method)(*args)
        assert socket.sent == []
    finally:
        await client.disconnect()


async def test_power_on_never_toggles_and_power_off_uses_discrete_key(monkeypatch):
    client, _, socket = make_client(monkeypatch)
    try:
        await client.connect()
        await client.power_on()
        assert socket.sent == []
        await client.power_off()
        assert socket.sent[0]["params"]["DataOfCmd"] == "KEY_POWEROFF"
    finally:
        await client.disconnect()


async def test_power_readback_distinguishes_standby_from_an_open_socket(monkeypatch):
    client, session, _ = make_client(monkeypatch)
    try:
        await client.connect()
        assert await client.get_power_state() == {"state": "Active"}
        session.device_info["device"]["PowerState"] = "standby"
        assert await client.get_power_state() == {"state": "Active Standby"}
        assert client.tv_state.is_on is False
        assert client.is_connected()
        session.device_info["device"].pop("PowerState")
        assert await client.get_power_state() == {}
        assert client.tv_state.is_on is None
    finally:
        await client.disconnect()


async def test_optional_rest_failure_does_not_lose_a_successful_pairing(monkeypatch):
    client, session, _ = make_client(monkeypatch)
    session.rest_error = aiohttp.ClientConnectionError("REST unavailable")
    try:
        assert await client.connect()
        assert client.client_key == "issued-123"
        assert client.is_connected()
        assert await client.get_power_state() == {}
    finally:
        await client.disconnect()


async def test_numbered_channel_sends_digits_then_enter_without_inventing_channel(monkeypatch):
    client, _, socket = make_client(monkeypatch)
    try:
        await client.connect()
        await client.set_channel("12")
        assert [message["params"]["DataOfCmd"] for message in socket.sent] == [
            "KEY_1", "KEY_2", "KEY_ENTER",
        ]
        assert await client.get_current_channel() == {}
        socket.sent.clear()
        with pytest.raises(TvError, match="channel"):
            await client.set_channel("12-1")
        assert socket.sent == []
    finally:
        await client.disconnect()


async def test_installed_apps_and_launch_use_the_local_channel_protocol(monkeypatch):
    client, _, socket = make_client(monkeypatch)
    try:
        await client.connect()
        pending = asyncio.create_task(client.get_apps())
        while not socket.sent:
            await asyncio.sleep(0)
        assert socket.sent == [{
            "method": "ms.channel.emit",
            "params": {"event": "ed.installedApp.get", "to": "host"},
        }]
        socket.incoming.put_nowait({
            "event": "ed.installedApp.get",
            "data": {"data": [{"appId": "11101200001", "name": "YouTube", "app_type": 2}]},
        })
        assert await asyncio.wait_for(pending, 1) == [
            {"id": "11101200001", "title": "YouTube"},
        ]
        await client.launch_app("11101200001")
        assert socket.sent[-1] == {
            "method": "ms.channel.emit",
            "params": {"event": "ed.apps.launch", "to": "host", "data": {
                "action_type": "DEEP_LINK", "appId": "11101200001", "metaTag": "",
            }},
        }
        assert await client.get_current_app() is None
    finally:
        await client.disconnect()


async def test_no_app_list_response_is_unknown_and_does_not_hang(monkeypatch):
    client, _, _ = make_client(monkeypatch)
    try:
        await client.connect()
        monkeypatch.setattr(client, "APP_LIST_TIMEOUT", .02)
        assert await client.get_apps() is None
    finally:
        await client.disconnect()


async def test_service_keeps_unknown_state_and_uses_mute_toggle_without_guessing_input(
    monkeypatch, tmp_path,
):
    from tests.unit.tv.helpers import build_service

    client, _, socket = make_client(monkeypatch, key="saved-token")
    service = build_service(
        tmp_path, factory=lambda host, key: client, key="saved-token", brand="samsung",
    )
    try:
        await service.start()
        shown = service.status()
        assert shown["reachable"] and shown["on"]
        assert shown["name"] == "[TV] Samsung S90HD"
        assert shown["volume"] is None and shown["muted"] is None and shown["input"] is None
        assert shown["capabilities"]["pointer"] is False
        assert "HOME" in shown["capabilities"]["supported_keys"]
        await service.control("mute")
        assert [message["params"]["DataOfCmd"] for message in socket.sent] == ["KEY_MUTE"]
        assert service.status()["muted"] is None
        socket.sent.clear()
        assert await service.before_join()
        await service.control("power", "on")
        assert socket.sent == []
        assert service.status()["input"] is None
    finally:
        await service.stop()


async def test_service_marks_a_revoked_samsung_pairing_token_refused(monkeypatch, tmp_path):
    from tests.unit.tv.helpers import build_service

    client, session, socket = make_client(monkeypatch, [
        {"event": "ms.channel.unauthorized", "data": {}},
    ], key="revoked-token")
    service = build_service(
        tmp_path, factory=lambda host, key: client, key="revoked-token",
        brand="samsung", keep_on=False,
    )
    try:
        await service.start()
        assert service.status()["refused"] is True
        assert service.status()["reachable"] is False
        assert session.closed and socket.closed
    finally:
        await service.stop()


async def test_connected_service_reports_unknown_power_without_blocking_remote_keys(monkeypatch, tmp_path):
    from tests.unit.tv.helpers import build_service

    client, session, socket = make_client(monkeypatch, key="saved-token")
    session.rest_error = aiohttp.ClientError("device information unavailable")
    service = build_service(
        tmp_path, factory=lambda host, key: client, key="saved-token",
        brand="samsung", keep_on=False,
    )
    try:
        await service.start()
        assert service.status()["reachable"]
        assert service.status()["power_known"] is False
        await service.control("key", "HOME")
        assert socket.sent[-1]["params"]["DataOfCmd"] == "KEY_HOME"
        service.MANUAL_TURN_ON_GRACE_S = .01
        await service.control("power", "on")
        assert service.status()["power_known"] is False
        session.rest_error = None
        await service.control("power", "on")
        assert service.status()["power_known"] is True
    finally:
        await service.stop()


@pytest.mark.parametrize("initial,changed,expected", [
    ("standby", "on", {"state": "Active"}),
    ("on", "standby", {"state": "Active Standby"}),
])
async def test_power_poll_reports_changes_while_the_same_remote_socket_stays_open(
    monkeypatch, initial, changed, expected,
):
    from tests.unit.tv.helpers import until

    client, session, socket = make_client(monkeypatch)
    monkeypatch.setattr(client, "POWER_POLL_INTERVAL", .01, raising=False)
    session.device_info["device"]["PowerState"] = initial
    updates = []

    async def on_state(state):
        updates.append(deepcopy(state.power_state))

    await client.register_state_update_callback(on_state)
    try:
        await client.connect()
        session.device_info["device"]["PowerState"] = changed
        await until(lambda: updates, timeout=.3)
        assert updates == [expected]
        assert client.tv_state.power_state == expected
        assert client.is_connected()
        assert len(session.opened) == 1 and socket.sent == []
        requests_after_change = len(session.requested)
        await until(lambda: len(session.requested) >= requests_after_change + 3, timeout=.3)
        assert updates == [expected], "Unchanged power must not trigger duplicate callbacks"
    finally:
        await client.disconnect()


@pytest.mark.parametrize("remote_close", [False, True])
async def test_power_poll_is_cancelled_with_the_connection_even_during_a_rest_read(
    monkeypatch, remote_close,
):
    from tests.unit.tv.helpers import until

    client, session, socket = make_client(monkeypatch)
    monkeypatch.setattr(client, "POWER_POLL_INTERVAL", .01, raising=False)
    await client.connect()
    session.read_started.clear()
    session.rest_gate = asyncio.Event()
    try:
        await until(session.read_started.is_set, timeout=.3)
        if remote_close:
            socket.incoming.put_nowait(None)
            await until(lambda: not client.is_connected(), timeout=.3)
        else:
            await asyncio.wait_for(client.disconnect(), .3)
        await until(session.read_cancelled.is_set, timeout=.3)
        requests_at_close = len(session.requested)
        await asyncio.sleep(.04)
        assert len(session.requested) == requests_at_close
    finally:
        session.rest_gate.set()
        await client.disconnect()
    assert session.closed and not client.is_connected()
