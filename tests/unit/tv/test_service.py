import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from croom.core.config import Config, TvConfig
from croom.core.service import ServiceManager, ServiceState
from croom.tv.errors import TvError
from croom.tv.keystore import KeyStore
from croom.tv.service import TvService
from tests.unit.tv.fake_client import FakeTvClient


def service_for(tmp_path, **overrides):
    config = TvConfig(**dict({"enabled": True, "host": "10.0.0.41", "mac": "AA:BB:CC:DD:EE:FF"}, **overrides))
    woken = []

    async def waker(mac):
        woken.append(mac)
    FakeTvClient.instances.clear()
    service = TvService(config, KeyStore(tmp_path / "tv-client-key"), FakeTvClient, waker)
    service.woken = woken
    return service


async def until(condition, timeout=2.0):
    """Let the loop run until the condition holds: the reconnect loop works in the background."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "the condition never held"
        await asyncio.sleep(0.01)


async def test_pairing_stores_the_key_privately_and_forget_removes_it(tmp_path):
    service = service_for(tmp_path)
    assert service.status()["paired"] is False and service.status()["configured"] is True
    await service.pair()
    key = tmp_path / "tv-client-key"
    assert key.read_text() == "key-1" and oct(key.stat().st_mode & 0o777) == "0o600"
    assert service.status()["paired"] is True and service.status()["reachable"] is True
    await service.forget()
    assert not key.exists() and service.status()["paired"] is False


async def test_start_connects_with_the_stored_key_and_reads_the_state(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        assert ("connect", "key-9") in client.calls
        s = service.status()
        assert s == {"configured": True, "paired": True, "on": True, "reachable": True, "name": "Boardroom TV", "volume": 24, "muted": False,
                     "input": "HDMI_1", "inputs": [{"id": "HDMI_1", "label": "HDMI 1"}, {"id": "HDMI_2", "label": "HDMI 2"}],
                     "app": "com.webos.app.hdmi1", "channel": "7", "keep_on": True, "held_off": False}
        await client.update(volume=30, muted=True)
        assert service.status()["volume"] == 30 and service.status()["muted"] is True
    finally:
        await service.stop()


async def test_an_unconfigured_room_has_a_quiet_service(tmp_path):
    service = service_for(tmp_path, enabled=False, host="")
    await service.start()
    assert service.status()["configured"] is False and FakeTvClient.instances == []
    with pytest.raises(TvError) as failure:
        await service.control("volume_up")
    assert "Options" in str(failure.value) or "not set up" in str(failure.value)
    await service.stop()


async def test_a_tv_that_refuses_is_unreachable_in_words_and_retried(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    service.RECONNECT_FIRST_S = 0.05
    FakeTvClient.refuse_next = True
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        client.refuse = True
        await service._connect_once()
        assert service.status()["reachable"] is False
        with pytest.raises(TvError) as failure:
            await service.control("volume_up")
        assert "10.0.0.41" in str(failure.value)
        client.refuse = False
        await service._connect_once()
        assert service.status()["reachable"] is True
    finally:
        await service.stop()


# What follows covers the service's own behaviour: the connection loop, what pairing and forgetting do while it runs,
# the state as the real library words it, and the wiring from the config.

async def test_a_tv_that_is_off_at_start_is_found_when_it_answers(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    service.RECONNECT_FIRST_S = 0.02
    FakeTvClient.refuse_next = True                       # off when the agent starts, answering on the next try
    await service.start()
    try:
        assert service.status()["reachable"] is False and service.status()["on"] is False
        await until(lambda: service.status()["reachable"])
        assert service.status()["on"] is True and service.status()["name"] == "Boardroom TV"
        assert len(FakeTvClient.instances) == 1           # the one client tries again: its callbacks stay registered
    finally:
        await service.stop()


async def test_the_reconnect_backs_off_up_to_a_cap(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    class Refusing(FakeTvClient):
        def __init__(self, host, key):
            super().__init__(host, key)
            self.refuse = True

    async def waker(mac):
        pass
    service = TvService(TvConfig(enabled=True, host="10.0.0.41"), KeyStore(tmp_path / "tv-client-key"), Refusing, waker, loop_sleep=sleep)
    service.RECONNECT_FIRST_S, service.RECONNECT_MAX_S = 1.0, 5.0
    await service.start()
    try:
        await until(lambda: len(sleeps) >= 5)
    finally:
        await service.stop()
    assert sleeps[:5] == [1.0, 2.0, 4.0, 5.0, 5.0]


async def test_stop_does_not_wait_out_a_reconnect_back_off(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)                       # the default back-off: the next try is two seconds away
    FakeTvClient.refuse_next = True
    await service.start()
    assert service.status()["reachable"] is False
    await asyncio.wait_for(service.stop(), 0.5)
    assert ("disconnect",) in FakeTvClient.instances[-1].calls


async def test_a_connection_that_drops_is_made_again_with_the_stored_key(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    service.WATCH_S = service.RECONNECT_FIRST_S = 0.02
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        client.refuse, client.connected = True, False     # the TV is switched off: its socket closes and it stops answering
        await client.update(power_state={})               # the library reports a cleared state as it lets go
        await until(lambda: len([c for c in client.calls if c[0] == "connect"]) >= 2)     # a try the TV refuses
        assert service.status()["on"] is False and service.status()["reachable"] is False
        client.tv_state.power_state, client.refuse = {"state": "Active"}, False           # and switched on again
        await until(lambda: service.status()["reachable"])
        assert service.status()["on"] is True
        assert {c for c in client.calls if c[0] == "connect"} == {("connect", "key-9")}   # every try with the stored key
    finally:
        await service.stop()


async def test_pairing_a_running_service_starts_the_connection(tmp_path):
    service = service_for(tmp_path)                       # started before the TV was paired
    service.WATCH_S = service.RECONNECT_FIRST_S = 0.02
    await service.start()
    try:
        assert FakeTvClient.instances == [] and service.status()["paired"] is False
        with pytest.raises(TvError) as failure:
            await service.control("volume_up")
        assert "Pair the TV" in str(failure.value)
        await service.pair()
        client = FakeTvClient.instances[-1]
        client.connected = False                          # a drop: the loop that pairing started brings it back
        await until(lambda: service.status()["reachable"])
        assert ("connect", "key-1") in client.calls       # with the key the pairing stored
    finally:
        await service.stop()


async def test_forget_while_connected_lets_go_of_the_tv_and_stops_reconnecting(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    service.WATCH_S = service.RECONNECT_FIRST_S = 0.02
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await service.forget()
        assert client.connected is False and not (tmp_path / "tv-client-key").exists()
        s = service.status()
        assert (s["paired"], s["reachable"], s["on"], s["name"]) == (False, False, False, "")
        connects = len([c for c in client.calls if c[0] == "connect"])
        await asyncio.sleep(0.15)
        assert len([c for c in client.calls if c[0] == "connect"]) == connects
    finally:
        await service.stop()


async def test_a_pairing_the_tv_does_not_complete_is_explained_and_stores_nothing(tmp_path):
    service = service_for(tmp_path)
    FakeTvClient.refuse_next = True                       # the TV is off, or the person declined the prompt
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "10.0.0.41" in str(failure.value) and "prompt" in str(failure.value)
    assert not (tmp_path / "tv-client-key").exists() and service.status()["paired"] is False
    assert ("disconnect",) in FakeTvClient.instances[-1].calls


async def test_pairing_needs_a_configured_tv(tmp_path):
    service = service_for(tmp_path, enabled=False, host="")
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "not set up" in str(failure.value) and FakeTvClient.instances == []


async def test_a_pairing_that_yields_no_key_is_explained(tmp_path):
    class Keyless(FakeTvClient):
        async def connect(self):                          # connects, but the TV never hands a key over
            self.calls.append(("connect", self.client_key))
            self.connected = True
            return True

    async def waker(mac):
        pass
    service = TvService(TvConfig(enabled=True, host="10.0.0.41"), KeyStore(tmp_path / "tv-client-key"), Keyless, waker)
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "did not hand out a key" in str(failure.value) and "10.0.0.41" in str(failure.value)
    assert service.status()["paired"] is False and service.status()["reachable"] is False
    assert ("disconnect",) in FakeTvClient.instances[-1].calls


async def test_a_key_that_cannot_be_kept_is_explained_and_the_connection_let_go(tmp_path):
    (tmp_path / "blocker").write_text("a file where the data dir should be")
    FakeTvClient.instances.clear()

    async def waker(mac):
        pass
    service = TvService(TvConfig(enabled=True, host="10.0.0.41"), KeyStore(tmp_path / "blocker" / "tv-client-key"), FakeTvClient, waker)
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "key could not be kept" in str(failure.value) and "tv-client-key" in str(failure.value)
    assert service.status()["paired"] is False and service.status()["reachable"] is False
    assert ("disconnect",) in FakeTvClient.instances[-1].calls


async def test_a_connect_that_answers_false_is_not_a_connection(tmp_path, caplog):
    class Closed(FakeTvClient):
        async def connect(self):                          # what the library answers when the connection is closed as it is made
            self.calls.append(("connect", self.client_key))
            return False

    async def waker(mac):
        pass
    (tmp_path / "tv-client-key").write_text("key-9")
    service = TvService(TvConfig(enabled=True, host="10.0.0.41"), KeyStore(tmp_path / "tv-client-key"), Closed, waker)
    await service.start()
    try:
        assert service.status()["reachable"] is False and "closed before it was made" in caplog.text
        with pytest.raises(TvError) as failure:
            await service.control("volume_up")
        assert "10.0.0.41" in str(failure.value)
    finally:
        await service.stop()


class LibraryShapedClient(FakeTvClient):
    """The state as aiowebostv's WebOsTvInfo and WebOsTvState have it, which names things differently from the fake."""

    def __init__(self, host, key):
        super().__init__(host, key)
        self.tv_info = SimpleNamespace(hello={"deviceOS": "webOS"}, system={"modelName": "OLED65C6"}, software={}, connection={})
        self.tv_state = SimpleNamespace(
            power_state={"state": "Active"}, current_app_id="com.webos.app.hdmi2", muted=None, volume=12, apps={},
            inputs={"com.webos.app.hdmi1": {"id": "HDMI_1", "label": "HDMI 1", "appId": "com.webos.app.hdmi1", "connected": True},
                    "com.webos.app.hdmi2": {"id": "HDMI_2", "label": "HDMI 2", "appId": "com.webos.app.hdmi2", "connected": False}},
            current_channel=None, is_on=True)


async def test_the_state_is_read_in_the_names_the_real_library_uses(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")

    async def waker(mac):
        pass
    service = TvService(TvConfig(enabled=True, host="10.0.0.41"), KeyStore(tmp_path / "tv-client-key"), LibraryShapedClient, waker)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        s = service.status()
        assert s["name"] == "OLED65C6"                                      # the hello names no device: the model stands in
        assert s["input"] == "HDMI_2" and s["app"] == "com.webos.app.hdmi2"    # the input is the one whose app is on screen
        assert s["inputs"] == [{"id": "HDMI_1", "label": "HDMI 1"}, {"id": "HDMI_2", "label": "HDMI 2"}]
        assert s["volume"] == 12 and s["muted"] is False and s["channel"] is None and s["on"] is True
        await client.update(current_app_id="com.webos.app.livetv", current_channel={"channelNumber": "11", "channelName": "News"})
        assert service.status()["input"] is None and service.status()["channel"] == "11"    # live TV is not an input
        await client.update(power_state={}, volume=None, inputs={}, current_app_id=None, current_channel=None)   # the library's cleared state
        assert service.status()["on"] is False and service.status()["volume"] is None
    finally:
        await service.stop()


async def test_a_state_the_service_cannot_read_never_reaches_the_library(tmp_path, caplog):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await client.update(power_state="Active")         # not the dict the library sends: reading it fails, and must not raise here
        assert "could not be read" in caplog.text
        assert service.status()["volume"] == 24 and service.status()["on"] is True      # the state stays as it was
    finally:
        await service.stop()


async def test_the_service_runs_under_the_service_manager(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    manager = ServiceManager()
    manager.register(service)
    assert await manager.start_all() is True
    try:
        assert service.state == ServiceState.RUNNING            # the manager sets this: the service's own model must not be in the way
        assert service.status()["reachable"] is True and service.status()["name"] == "Boardroom TV"
    finally:
        await manager.stop_all()
    assert service.state == ServiceState.STOPPED and service.status()["reachable"] is False


def test_from_config_keeps_the_key_in_the_data_dir(tmp_path, monkeypatch):
    async def wake(mac):
        pass
    monkeypatch.setitem(sys.modules, "croom.tv.wake", types.SimpleNamespace(wake=wake))    # that module comes with the controls
    config = Config(data_dir=str(tmp_path / "data"))
    config.tv = TvConfig(enabled=True, host="10.0.0.41")
    service = TvService.from_config(config)
    assert service.name == "tv" and service.status()["configured"] is True and service.status()["paired"] is False
    (tmp_path / "data" / "tv-client-key").write_text("key-9")
    assert service.status()["paired"] is True
