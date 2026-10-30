"""The Settings PIN's routes (spec 2026-10-29, section 2): choosing it once, unlocking and locking, the token every protected route
asks for (header X-Croom-Unlock), the lock-out, and changing the PIN."""

import pytest

from croom.control.service import ControlService
from croom.settings.pin import PinStore
from tests.unit.control.test_service import client_factory, make_service  # noqa: F401  (client_factory is a fixture)
from tests.unit.settings.test_pin import Clock


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def pins(tmp_path, clock):
    return PinStore(tmp_path / "settings-pin.json", clock=clock)


def unlocked(token):
    return {"X-Croom-Unlock": token}


async def chosen(client, pin="1234"):
    """A PIN chosen through the route: its token."""
    response = await client.post("/api/settings/pin/set", json={"pin": pin})
    assert response.status == 200, await response.text()
    return (await response.json())["token"]


async def test_a_room_with_no_pin_says_so(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    response = await client.get("/api/settings/pin")
    assert response.status == 200
    assert await response.json() == {"set": False, "locked_minutes": 0, "unlocked": False}


async def test_a_pin_is_chosen_once_and_answers_a_token(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    response = await client.post("/api/settings/pin/set", json={"pin": "1234"})
    body = await response.json()
    assert response.status == 200 and body["expires_in"] == 300 and pins.check(body["token"])
    assert (await (await client.get("/api/settings/pin")).json())["set"] is True
    again = await client.post("/api/settings/pin/set", json={"pin": "9999"})
    assert again.status == 409 and "already" in (await again.json())["error"]
    assert pins.check(pins.unlock("1234"))                                        # the first one stands


async def test_choosing_a_pin_that_is_not_four_to_eight_digits_is_refused_in_words(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    for pin in ("12", "123456789", "12ab", 1234):
        response = await client.post("/api/settings/pin/set", json={"pin": pin})
        assert response.status == 400 and await response.json() == {"error": "The PIN is 4 to 8 digits"}, pin
    assert not pins.is_set()


async def test_every_post_is_json(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    for path in ("/api/settings/pin/set", "/api/settings/unlock", "/api/settings/lock"):
        response = await client.post(path, data="pin=1234")
        assert response.status == 415, path
    assert not pins.is_set()
    response = await client.post("/api/settings/unlock", data="[1, 2]", headers={"Content-Type": "application/json"})
    assert response.status == 400


async def test_unlock_answers_a_token_and_lock_ends_it(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    await chosen(client)
    response = await client.post("/api/settings/unlock", json={"pin": "1234"})
    body = await response.json()
    assert response.status == 200 and set(body) == {"token", "expires_in"} and body["expires_in"] == 300
    token = body["token"]
    status = await (await client.get("/api/settings/pin", headers=unlocked(token))).json()
    assert status == {"set": True, "locked_minutes": 0, "unlocked": True}
    response = await client.post("/api/settings/lock", json={}, headers=unlocked(token))
    assert response.status == 200 and await response.json() == {"locked": True}
    assert not pins.check(token)
    status = await (await client.get("/api/settings/pin", headers=unlocked(token))).json()
    assert status["unlocked"] is False


async def test_lock_without_a_token_has_nothing_to_end(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    response = await client.post("/api/settings/lock", json={})
    assert response.status == 200


async def test_unlock_before_a_pin_is_chosen_says_to_choose_one(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    response = await client.post("/api/settings/unlock", json={"pin": "1234"})
    assert response.status == 409 and await response.json() == {"error": "Choose a PIN first"}


async def test_a_wrong_pin_is_refused_with_the_tries_left_and_five_lock_it_for_ten_minutes(client_factory, pins, clock):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    await chosen(client)
    response = await client.post("/api/settings/unlock", json={"pin": "0000"})
    assert response.status == 403 and await response.json() == {"error": "Wrong PIN: 4 tries left"}
    for _ in range(3):
        await client.post("/api/settings/unlock", json={"pin": "0000"})
    response = await client.post("/api/settings/unlock", json={"pin": "0000"})
    assert response.status == 423
    assert await response.json() == {"error": "Too many wrong PINs: try again in 10 minutes", "locked": True, "minutes": 10}
    response = await client.post("/api/settings/unlock", json={"pin": "1234"})
    assert response.status == 423                                                  # the right PIN too, while it lasts
    assert (await (await client.get("/api/settings/pin")).json())["locked_minutes"] == 10
    clock.advance(10 * 60)
    assert (await client.post("/api/settings/unlock", json={"pin": "1234"})).status == 200


async def test_a_protected_route_answers_401_without_a_good_token(client_factory, pins, clock):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    token = await chosen(client)
    body = {"old": "1234", "new": "5678"}
    for headers in ({}, unlocked(""), unlocked("made-up")):
        response = await client.post("/api/settings/pin/change", json=body, headers=headers)
        assert response.status == 401 and await response.json() == {"error": "Enter the PIN", "locked": False}, headers
    clock.advance(5 * 60)                                                          # five minutes with no request: gone
    response = await client.post("/api/settings/pin/change", json=body, headers=unlocked(token))
    assert response.status == 401
    assert pins.check(pins.unlock("1234"))                                         # nothing was changed


async def test_a_protected_route_answers_423_with_the_minutes_left_while_locked_out(client_factory, pins, clock):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    await chosen(client)
    for _ in range(5):
        await client.post("/api/settings/unlock", json={"pin": "0000"})
    clock.advance(4 * 60 + 30)
    response = await client.post("/api/settings/pin/change", json={"old": "1234", "new": "5678"})
    assert response.status == 423
    assert await response.json() == {"error": "Too many wrong PINs: try again in 6 minutes", "locked": True, "minutes": 6}


async def test_each_protected_request_keeps_the_token_five_more_minutes(client_factory, pins, clock):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    token = await chosen(client)
    for _ in range(3):
        clock.advance(4 * 60)
        status = await (await client.get("/api/settings/pin", headers=unlocked(token))).json()
        assert status["unlocked"] is True


async def test_change_the_pin(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    token = await chosen(client)
    response = await client.post("/api/settings/pin/change", json={"old": "1234", "new": "567890"}, headers=unlocked(token))
    assert response.status == 200 and await response.json() == {"changed": True}
    assert (await client.post("/api/settings/unlock", json={"pin": "1234"})).status == 403
    assert (await client.post("/api/settings/unlock", json={"pin": "567890"})).status == 200


async def test_change_refuses_a_wrong_old_pin_and_a_bad_new_one_in_words(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    token = await chosen(client)
    response = await client.post("/api/settings/pin/change", json={"old": "9999", "new": "5678"}, headers=unlocked(token))
    assert response.status == 403 and await response.json() == {"error": "Wrong PIN: 4 tries left"}
    response = await client.post("/api/settings/pin/change", json={"old": "1234", "new": "5"}, headers=unlocked(token))
    assert response.status == 400 and await response.json() == {"error": "The PIN is 4 to 8 digits"}
    response = await client.post("/api/settings/pin/change", data="old=1234", headers=unlocked(token))
    assert response.status == 415
    assert pins.check(pins.unlock("1234"))


async def test_the_service_keeps_its_pin_beside_its_settings_file_by_default(tmp_path):
    service = ControlService(config={"settings_file": str(tmp_path / "control-settings.json")})
    assert service.pin_store.path == tmp_path / "settings-pin.json"
    service = ControlService(config={"settings_file": str(tmp_path / "a.json"), "pin_file": str(tmp_path / "pins" / "p.json")})
    assert service.pin_store.path == tmp_path / "pins" / "p.json"


def test_from_config_puts_the_pin_in_the_data_directory(tmp_path):
    from croom.core.config import Config
    config = Config()
    config.data_dir = str(tmp_path)
    service = ControlService.from_config(config)
    assert service.pin_store.path == tmp_path / "settings-pin.json"


async def test_changing_the_pin_ends_another_screens_token_and_keeps_this_ones(client_factory, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    mine = await chosen(client)
    theirs = (await (await client.post("/api/settings/unlock", json={"pin": "1234"})).json())["token"]
    response = await client.post("/api/settings/pin/change", json={"old": "1234", "new": "5678"}, headers=unlocked(mine))
    assert response.status == 200
    refused = await client.post("/api/settings/pin/change", json={"old": "5678", "new": "1111"}, headers=unlocked(theirs))
    assert refused.status == 401
    assert (await (await client.get("/api/settings/pin", headers=unlocked(mine))).json())["unlocked"] is True


async def test_a_pin_chosen_after_a_reset_during_a_lock_out_answers_a_token(client_factory, pins, tmp_path):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    old = await chosen(client)
    for _ in range(5):
        await client.post("/api/settings/unlock", json={"pin": "0000"})
    (tmp_path / "settings-pin.json").unlink()
    response = await client.post("/api/settings/pin/set", json={"pin": "8888"})
    assert response.status == 200 and pins.check((await response.json())["token"])
    assert (await client.post("/api/settings/pin/change", json={"old": "8888", "new": "1111"}, headers=unlocked(old))).status == 401
