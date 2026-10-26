import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from croom.core.config import Config, TvConfig
from croom.core.service import ServiceManager, ServiceState
from croom.tv.errors import TvError
from croom.tv.keystore import KeyStore
from croom.tv.service import TvService
from croom.tv.wake import wake
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import service_for


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
                     "app": "com.webos.app.hdmi1", "channel": "7", "keep_on": True, "held_off": False,
                     "refused": False, "prompting": False, "host": "10.0.0.41", "pi_input": "HDMI_1",
                     "brand": "lg", "mac": "AA:BB:CC:DD:EE:FF",
                     "capabilities": {"pointer": True, "direct_inputs": True, "mute_state": True}}
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


# The controls, Wake-on-LAN and keeping the TV on: the brief's own tests, as written, on service_for's waker, which brings
# the fake TV on as a real wake does (with a waker that only records the MAC address, the fake stays off for ever, and a
# manual On that finds the TV still off now says so, as the review asked: the second test would end in that TvError). More
# of the same, and the edges, are in test_controls.py and test_keep_on.py. One line differs from the brief: the first test's
# list of calls that are reads, not controls, gains the four reads that the review had every control followed by (the
# volume's and the power state's were there); what it asserts of the control calls themselves is as it was.

async def test_controls_map_to_the_tv_and_re_read_the_state(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await service.control("volume_up"); await service.control("volume", 55); await service.control("mute")
        await service.control("channel", "12"); await service.control("input", "pi"); await service.control("key", "HOME")
        await service.control("digit", "7"); await service.control("toast", "Hello")
        names = [c[0] for c in client.calls if c[0] not in ("connect", "get_inputs", "get_volume", "get_power_state", "get_muted", "get_input", "get_current_app", "get_current_channel")]
        assert names == ["volume_up", "set_volume", "set_mute", "set_channel", "set_input", "button", "button", "send_message"]
        assert ("set_input", "HDMI_1") in client.calls and ("button", "7") in client.calls and ("set_mute", True) in client.calls
        with pytest.raises(TvError):
            await service.control("key", "SELF_DESTRUCT")
        with pytest.raises(TvError):
            await service.control("volume", 150)
    finally:
        await service.stop()


async def test_power_off_holds_against_keep_on_and_on_wakes(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await service.control("power", "off")
        assert ("power_off",) in client.calls and service.status()["held_off"] is True
        await client.update(power_state={"state": "Off"})
        await service.ensure_on(timeout=0.2)                        # keep_on would wake; the hold says no
        assert service.woken == []
        await service.control("power", "on")
        assert service.woken == ["AA:BB:CC:DD:EE:FF"] and service.status()["held_off"] is False
    finally:
        await service.stop()


async def test_a_manual_off_holds_until_on_or_a_join(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    await service.start()
    try:
        await service.control("power", "off")
        await FakeTvClient.instances[-1].update(power_state={"state": "Off"})
        await service.before_join()                                 # a meeting wants the TV
        assert service.woken == ["AA:BB:CC:DD:EE:FF"] and service.status()["held_off"] is False
    finally:
        await service.stop()


async def test_a_join_waits_at_most_fifteen_seconds_for_a_tv_that_never_wakes(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    service.WAKE_TIMEOUT_S = 0.3
    service.WAKE_POLL_S = 0.05
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await client.update(power_state={"state": "Off"})
        client.refuse = True
        import time
        t0 = time.monotonic()
        assert await service.before_join() is False                 # the join goes on without the TV
        assert time.monotonic() - t0 < 1.0 and service.woken == ["AA:BB:CC:DD:EE:FF"]
        assert service.status()["on"] is False
    finally:
        await service.stop()


async def test_an_unexpected_off_is_woken_when_keep_on(tmp_path):
    (tmp_path / "tv-client-key").write_text("key-9")
    service = service_for(tmp_path)
    service.REWAKE_AFTER_S = 0.05
    await service.start()
    try:
        await FakeTvClient.instances[-1].update(power_state={"state": "Off"})
        for _ in range(40):
            if service.woken:
                break
            await asyncio.sleep(0.02)
        assert service.woken == ["AA:BB:CC:DD:EE:FF"]
    finally:
        await service.stop()


# What follows is the service's own: it runs under the manager, is built from the config, and imports nothing it can do without.

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


async def test_a_client_that_fails_to_hang_up_does_not_keep_the_service_from_stopping(tmp_path):
    class Stubborn(FakeTvClient):
        async def disconnect(self):
            raise RuntimeError("the library choked")
    (tmp_path / "tv-client-key").write_text("key-9")
    service = TvService(TvConfig(enabled=True, host="10.0.0.41"), KeyStore(tmp_path / "tv-client-key"), Stubborn, service_for(tmp_path)._wake)
    await service.start()
    assert service.status()["reachable"] is True
    await service.stop()
    assert service.status()["reachable"] is False and service._client is None


@pytest.mark.parametrize("how", ["stop", "forget"])
async def test_a_hang_up_that_never_ends_is_cut_off_at_its_bound_and_the_service_goes_on(tmp_path, monkeypatch, how):
    """The library waits for aiohttp's close timeout when it hangs up on a TV that is gone, and the agent's stop did that for the wake
    that was waiting, the loop and the client in turn (about half a minute behind a dead TV): each hang-up is given DISCONNECT_WAIT_S."""
    class Deaf(FakeTvClient):
        async def disconnect(self):
            self.calls.append(("disconnect",))
            await asyncio.Event().wait()
    monkeypatch.setattr(TvService, "DISCONNECT_WAIT_S", 0.1)
    service = service_for(tmp_path, Deaf, key="key-9")
    await service.start()
    assert service.status()["reachable"] is True
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.wait_for(getattr(service, how)(), 3.0)
    assert loop.time() - t0 < 1.0
    assert ("disconnect",) in FakeTvClient.instances[-1].calls and service._client is None and service._letting_go == 0
    assert service.status()["reachable"] is False
    await service.stop()


def test_the_waits_are_the_ones_the_spec_and_the_review_settled():
    assert TvService.WAKE_TIMEOUT_S == 15.0               # a wake, and so a join, waits this long for the TV to answer
    assert TvService.REWAKE_AFTER_S == 5.0                # a TV that went off by itself is woken after this
    assert TvService.WAKE_POLL_S == 1.0                   # and looked at this often meanwhile
    assert TvService.WAKE_RESEND_S == 3.0                 # with the magic packet sent again this often while it does not answer
    assert TvService.TURN_ON_GRACE_S == 0.5               # a TV in standby has this long to say that it was told to turn on
    assert TvService.MANUAL_TURN_ON_GRACE_S == 3.0        # and, when a person at the table asked for it, this long
    assert TvService.START_WAIT_S == 0.5                  # starting waits this long for the first look at the TV
    assert TvService.ASK_TIMEOUT_S == 3.0                 # a command the service gives the TV (a control from the table, turn on, show the Pi)
    assert TvService.DISCONNECT_WAIT_S == 2.0             # hanging up on a TV waits no longer than this for its goodbye
    assert TvService.PROMPT_AFTER_S == 5.0                # a try that has waited for the TV this long is waiting at its pairing prompt
    assert TvService.REREAD_TIMEOUT_S == 2.0              # and each read-back after a control has this long
    assert TvService.PAIR_WAIT_S == 60.0                  # a pairing waits this long for the person at the TV to accept the prompt
    # a person's tap keeps its own command and grace beyond the wait: the longest a Turn on from the table can take, which the
    # constants' comment in the service says, and the control's client waits longer than
    assert TvService.WAKE_TIMEOUT_S + TvService.ASK_TIMEOUT_S + TvService.MANUAL_TURN_ON_GRACE_S + TvService.REREAD_TIMEOUT_S == 23.0


def test_from_config_keeps_the_key_in_the_data_dir(tmp_path):
    config = Config(data_dir=str(tmp_path / "data"))
    config.tv = TvConfig(enabled=True, host="10.0.0.41")
    service = TvService.from_config(config)
    assert service.name == "tv" and service.status()["configured"] is True and service.status()["paired"] is False
    (tmp_path / "data" / "tv-client-key").write_text("key-9")
    assert service.status()["paired"] is True
    assert service._wake is wake                          # the real Wake-on-LAN, which imports its library only when it sends


def test_importing_the_service_leaves_the_tv_libraries_alone():
    """A room without a TV runs without aiowebostv, wakeonlan and evdev: nothing here imports them up front. Checked in
    a fresh interpreter, because this one has the test run's imports in it."""
    import croom
    src = Path(croom.__file__).resolve().parent.parent
    code = (f"import sys; sys.path.insert(0, {str(src)!r}); "
            "import croom.tv.client, croom.tv.errors, croom.tv.keystore, croom.tv.service, croom.tv.wake; "
            "print(sorted(m for m in ('aiowebostv', 'wakeonlan', 'evdev') if m in sys.modules))")
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
