import subprocess
import sys
import types
from pathlib import Path

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


def test_importing_the_service_leaves_the_tv_libraries_alone():
    """A room without a TV runs without aiowebostv, wakeonlan and evdev: nothing here imports them up front. Checked in
    a fresh interpreter, because this one has the test run's imports in it."""
    import croom
    src = Path(croom.__file__).resolve().parent.parent
    code = (f"import sys; sys.path.insert(0, {str(src)!r}); "
            "import croom.tv.client, croom.tv.errors, croom.tv.keystore, croom.tv.service; "
            "print(sorted(m for m in ('aiowebostv', 'wakeonlan', 'evdev') if m in sys.modules))")
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
