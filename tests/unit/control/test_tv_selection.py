"""The TV picker accepts discovered devices, persists them and starts pairing."""
import pytest

from croom.core.config import Config
from croom.tv.service import TvService
from tests.unit.control.test_service import client_factory, make_service, StubSearch
from tests.unit.tv.fake_client import FakeTvClient


TV = {"host": "10.0.0.41", "brand": "lg", "mac": "AA:BB:CC:DD:EE:02", "name": "Room TV"}


@pytest.fixture
def tv(tmp_path, monkeypatch):
    from croom.tv import client

    monkeypatch.setattr(client, "real_client_factory", lambda host, key, brand="lg": FakeTvClient(host, key))
    return TvService.from_config(Config.from_dict({"data_dir": str(tmp_path)}))


async def test_discover_then_select_saves_and_pairs_the_discovered_device(client_factory, tv):
    client = await client_factory(make_service(tv=tv, discover=StubSearch([TV])))
    await client.get("/api/tv/discover")
    response = await client.post("/api/tv/select", json={"host": TV["host"], "brand": "samsung"})
    try:
        assert response.status == 200
        status = (await response.json())["tv"]
        assert status["host"] == TV["host"] and status["paired"]
        assert status["brand"] == "lg"  # brand comes from discovery, never the browser
    finally:
        await tv.stop()


async def test_select_cannot_connect_to_an_address_not_in_discovery(client_factory, tv):
    client = await client_factory(make_service(tv=tv, discover=StubSearch([TV])))
    await client.get("/api/tv/discover")
    response = await client.post("/api/tv/select", json={"host": "10.0.0.99"})
    assert response.status == 400
    assert not tv.status()["configured"]


async def test_selection_requires_json_and_a_recent_search(client_factory, tv):
    client = await client_factory(make_service(tv=tv, discover=StubSearch([TV])))
    response = await client.post("/api/tv/select", data='{"host":"10.0.0.41"}', headers={"Content-Type": "text/plain"})
    assert response.status == 415
    response = await client.post("/api/tv/select", json={"host": TV["host"]})
    assert response.status == 400
    await client.get("/api/tv/discover")
    response = await client.post("/api/tv/select", json={"host": TV["host"], "mac": "invalid"})
    assert response.status == 409
    assert not tv.status()["configured"]


async def test_selected_tv_settings_can_be_updated_without_changing_host(client_factory, tv):
    client = await client_factory(make_service(tv=tv, discover=StubSearch([TV])))
    await client.get("/api/tv/discover")
    await client.post("/api/tv/select", json={"host": TV["host"]})
    try:
        response = await client.post("/api/tv/settings", json={"mac": "aa:bb:cc:dd:ee:04", "pi_input": "HDMI_2"})
        assert response.status == 200
        assert (await response.json())["tv"]["pi_input"] == "HDMI_2"
        response = await client.post("/api/tv/settings", json={"host": "10.0.0.99"})
        assert response.status == 409
        assert tv.status()["host"] == TV["host"]
    finally:
        await tv.stop()


async def test_expired_discovery_requires_a_new_search_before_connecting(client_factory, tv):
    service = make_service(tv=tv, discover=StubSearch([TV]))
    client = await client_factory(service)
    await client.get("/api/tv/discover")
    service._discovered_at -= 301
    response = await client.post("/api/tv/select", json={"host": TV["host"]})
    assert response.status == 400
    assert not tv.status()["configured"]
