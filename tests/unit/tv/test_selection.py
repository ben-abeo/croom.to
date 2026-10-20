"""A selection on the table becomes the room's TV, including after a restart."""
import asyncio
import json
from io import StringIO

import pytest

from croom.core.config import Config
from croom.tv.errors import TvError
from croom.tv.service import TvService
from tests.unit.tv.fake_client import FakeTvClient, PromptingTvClient
from tests.unit.tv.helpers import until


LG = {"host": "10.0.0.41", "brand": "lg", "mac": "AA:BB:CC:DD:EE:02", "name": "Room TV"}
SAMSUNG = {"host": "10.0.0.42", "brand": "samsung", "mac": "AA:BB:CC:DD:EE:04", "name": "Samsung"}


@pytest.fixture
def room(tmp_path, monkeypatch):
    from croom.tv import client

    monkeypatch.setattr(client, "real_client_factory", lambda host, key, brand="lg": FakeTvClient(host, key))
    return Config.from_dict({"data_dir": str(tmp_path)})


async def test_selecting_an_unconfigured_tv_saves_and_pairs_without_restart(room, tmp_path):
    tv = TvService.from_config(room)
    await tv.start()
    try:
        await tv.select(LG)
        assert tv.status()["configured"] and tv.status()["paired"] and tv.status()["reachable"]
        assert tv.status()["host"] == LG["host"]
        assert tv.status()["mac"] == LG["mac"]
        assert FakeTvClient.instances[-1].calls[0] == ("connect", None)
        saved = tmp_path / "tv-settings.json"
        assert json.loads(saved.read_text())["tv"]["host"] == LG["host"]
        assert saved.stat().st_mode & 0o777 == 0o600
        fresh = Config.from_dict({"data_dir": str(tmp_path)})
        restarted = TvService.from_config(fresh)
        assert restarted.status()["host"] == LG["host"]
        assert restarted.status()["paired"]
        assert fresh.tv.enabled
    finally:
        await tv.stop()


async def test_switching_brand_never_sends_the_previous_tvs_key(room):
    tv = TvService.from_config(room)
    await tv.start()
    try:
        await tv.select(LG)
        previous = FakeTvClient.instances[-1]
        old_key_path = tv.key_path
        await tv.select(SAMSUNG)
        assert not previous.connected
        assert tv.key_path != old_key_path
        assert FakeTvClient.instances[-1].calls[0] == ("connect", None)
        assert tv.status()["brand"] == "samsung"
        assert tv.status()["capabilities"]["pointer"] is False
    finally:
        await tv.stop()


async def test_settings_can_be_changed_without_repairing_and_survive_restart(room, tmp_path):
    tv = TvService.from_config(room)
    try:
        await tv.select({**LG, "mac": ""})
        client = FakeTvClient.instances[-1]
        await tv.update_settings({"mac": "aa-bb-cc-dd-ee-06", "pi_input": "HDMI_3"})
        assert tv.status()["mac"] == "AA:BB:CC:DD:EE:06"
        assert tv.status()["pi_input"] == "HDMI_3"
        assert FakeTvClient.instances[-1] is client and client.connected
        restarted = TvService.from_config(Config.from_dict({"data_dir": str(tmp_path)}))
        assert restarted.status()["mac"] == "AA:BB:CC:DD:EE:06"
        assert restarted.status()["pi_input"] == "HDMI_3"
    finally:
        await tv.stop()


async def test_a_save_failure_leaves_the_current_tv_connected(room, monkeypatch):
    tv = TvService.from_config(room)
    try:
        await tv.select(LG)
        client = FakeTvClient.instances[-1]

        def denied(*args):
            raise OSError("disk full")

        monkeypatch.setattr(tv._settings_store, "save", denied)
        with pytest.raises(TvError, match="save"):
            await tv.select(SAMSUNG)
        assert tv.status()["host"] == LG["host"]
        assert client.connected
    finally:
        await tv.stop()


@pytest.mark.parametrize("patch", [
    {"host": "http://10.0.0.41"}, {"host": "127.0.0.1"}, {"host": "::1"},
    {"brand": "other"}, {"mac": "not-a-mac"}, {"mac": "FF:FF:FF:FF:FF:FF"},
    {"pi_input": "../../input"},
])
async def test_invalid_selection_is_rejected_before_changing_the_room(room, patch):
    tv = TvService.from_config(room)
    with pytest.raises(TvError):
        await tv.select({**LG, **patch})
    assert not tv.status()["configured"]
    assert not FakeTvClient.instances


async def test_selecting_the_connected_tv_again_does_not_prompt_again(room):
    tv = TvService.from_config(room)
    try:
        await tv.select(LG)
        client = FakeTvClient.instances[-1]
        await tv.select(LG)
        assert FakeTvClient.instances[-1] is client
        assert len(FakeTvClient.instances) == 1
    finally:
        await tv.stop()


async def test_second_selection_is_rejected_while_the_first_waits_for_pairing(room, monkeypatch):
    from croom.tv import client

    monkeypatch.setattr(client, "real_client_factory", lambda host, key, brand="lg": PromptingTvClient(host, key))
    tv = TvService.from_config(room)
    pending = asyncio.create_task(tv.select(LG))
    try:
        await until(lambda: tv._pairing)
        with pytest.raises(TvError, match="already"):
            await tv.select(SAMSUNG)
        await tv.forget()
        with pytest.raises(TvError):
            await pending
        assert not tv.status()["paired"]
        assert tv.status()["host"] == LG["host"]
    finally:
        pending.cancel()
        await tv.stop()


def test_samsung_brand_round_trips_through_room_config():
    config = Config.from_dict({"tv": {"brand": "samsung", "enabled": True, "host": "10.0.0.42"}})
    assert config.tv.brand == "samsung"
    assert config.to_dict()["tv"]["brand"] == "samsung"


async def test_check_tv_uses_the_saved_selection_when_yaml_has_no_tv(room, tmp_path):
    from croom.tv.check import check_tv

    tv = TvService.from_config(room)
    try:
        await tv.select(LG)
    finally:
        await tv.stop()
    out = StringIO()
    fresh = Config.from_dict({"data_dir": str(tmp_path)})
    async def no_discovery():
        pytest.fail("A saved TV should be checked instead of starting discovery")
    assert await check_tv(fresh, out=out, discover=no_discovery) == 0
    assert LG["host"] in out.getvalue() and "Ready:" in out.getvalue()


async def test_manual_mac_can_use_the_unseparated_format_from_tv_settings(room):
    tv = TvService.from_config(room)
    try:
        await tv.select({**LG, "mac": "AABBCCDDEE02"})
        assert tv.status()["mac"] == "AA:BB:CC:DD:EE:02"
    finally:
        await tv.stop()


async def test_reselecting_a_saved_tv_bounds_a_connection_that_never_answers(room, monkeypatch):
    from croom.tv import client

    tv = TvService.from_config(room)
    await tv.select(LG)
    await tv.stop()

    class Silent(FakeTvClient):
        async def connect(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(client, "real_client_factory", lambda host, key, brand="lg": Silent(host, key))
    tv = TvService.from_config(room)
    tv.ASK_TIMEOUT_S = 0.02
    try:
        with pytest.raises(TvError, match="answer"):
            await asyncio.wait_for(tv.select(LG), 1)
        assert tv.status()["host"] == LG["host"]
        assert not tv._selecting
    finally:
        await tv.stop()


async def test_rediscovery_without_a_mac_preserves_the_same_tvs_known_mac(room):
    tv = TvService.from_config(room)
    try:
        await tv.select(LG)
        await tv.select({**LG, "mac": ""})
        assert tv.status()["mac"] == LG["mac"]
    finally:
        await tv.stop()


async def test_declined_pairing_keeps_the_selected_tv_ready_to_retry(room, tmp_path, monkeypatch):
    from croom.tv import client

    def refused(host, key, brand="lg"):
        result = FakeTvClient(host, key)
        result.decline = True
        return result
    monkeypatch.setattr(client, "real_client_factory", refused)
    tv = TvService.from_config(room)
    try:
        with pytest.raises(TvError):
            await tv.select(SAMSUNG)
        assert tv.status()["configured"] and not tv.status()["paired"]
        assert tv.status()["host"] == SAMSUNG["host"]
        restarted = TvService.from_config(Config.from_dict({"data_dir": str(tmp_path)}))
        assert restarted.status()["brand"] == "samsung"
    finally:
        await tv.stop()


def test_a_manually_configured_samsung_does_not_adopt_a_legacy_lg_key(tmp_path):
    (tmp_path / "tv-client-key").write_text("lg-only-key")
    config = Config.from_dict({"data_dir": str(tmp_path), "tv": {
        "enabled": True, "brand": "samsung", "host": SAMSUNG["host"], "mac": SAMSUNG["mac"],
    }})
    tv = TvService.from_config(config)
    assert not tv.status()["paired"]
    assert tv.key_path.name != "tv-client-key"


async def test_cancel_while_previous_tv_disconnects_never_starts_new_pairing(room):
    tv = TvService.from_config(room)
    release = asyncio.Event()
    disconnecting = asyncio.Event()
    pending = None
    try:
        await tv.select(LG)
        previous = FakeTvClient.instances[-1]
        original_disconnect = previous.disconnect

        async def delayed_disconnect():
            disconnecting.set()
            await release.wait()
            await original_disconnect()

        previous.disconnect = delayed_disconnect
        before = len(FakeTvClient.instances)
        pending = asyncio.create_task(tv.select(SAMSUNG))
        await disconnecting.wait()
        await tv.forget()
        release.set()
        with pytest.raises(TvError, match="stopped|cancel"):
            await pending
        assert len(FakeTvClient.instances) == before
        assert not tv.status()["paired"]
    finally:
        release.set()
        if pending and not pending.done():
            pending.cancel()
        await tv.stop()


async def test_cancel_during_saved_token_reconnect_does_not_start_pairing(room, monkeypatch):
    from croom.tv import client

    original = TvService.from_config(room)
    await original.select(SAMSUNG)
    await original.stop()
    reconnecting = asyncio.Event()
    disconnected = asyncio.Event()

    class Reconnecting(FakeTvClient):
        async def connect(self):
            if self.client_key:
                reconnecting.set()
                await disconnected.wait()
                return False
            return await super().connect()

        async def disconnect(self):
            disconnected.set()
            await super().disconnect()

    monkeypatch.setattr(client, "real_client_factory",
                        lambda host, key, brand="lg": Reconnecting(host, key))
    tv = TvService.from_config(room)
    before = len(FakeTvClient.instances)
    try:
        selecting = asyncio.create_task(tv.select(SAMSUNG))
        await reconnecting.wait()
        await tv.forget()
        with pytest.raises(TvError, match="cancel"):
            await selecting
        assert len(FakeTvClient.instances) == before + 1
        assert not tv.status()["paired"]
    finally:
        await tv.stop()
