"""The Settings pages' config routes (spec 2026-10-29, sections 3 and 4): a page's values and fields from config.yaml, a save through
the root helper (a fake here) with its refusals by setting, the live settings applied at once, and Restart now with its status."""

from types import SimpleNamespace

import pytest
import yaml

from croom.calendar.bookings import RoomBookings
from croom.control.service import ControlService
from croom.core.config import Config
from croom.devices.camera import RoomCamera
from croom.meeting.providers.base import MeetingState
from croom.settings.pin import PinStore
from tests.unit.control.test_service import StubMeeting, client_factory, make_service  # noqa: F401  (client_factory is a fixture)
from tests.unit.devices.fake_v4l2 import FakeV4l2
from tests.unit.settings.test_schema import HAND_WRITTEN, FakeAdmin

PIN = "1234"


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(HAND_WRITTEN)
    return path


@pytest.fixture
def admin(config_file):
    return FakeAdmin(config_file)


@pytest.fixture
def pins(tmp_path):
    store = PinStore(tmp_path / "settings-pin.json")
    store.set_pin(PIN)
    return store


@pytest.fixture
def room(tmp_path):
    """The room's bookings and camera, which the live settings reach."""
    bookings = RoomBookings(tmp_path / "bookings.json", tmp_path / "calendar-cache.json", "Lab")
    camera = RoomCamera(v4l2=FakeV4l2())
    return SimpleNamespace(calendar=SimpleNamespace(bookings=bookings, events=[]), devices=SimpleNamespace(camera=camera, volume=None),
                           bookings=bookings, camera=camera, meeting=StubMeeting())


@pytest.fixture
async def client(client_factory, config_file, admin, pins, room):  # noqa: F811
    service = make_service(pin_store=pins, admin=admin, config_file=str(config_file), calendar=room.calendar, devices=room.devices,
                           meeting=room.meeting)
    return await client_factory(service)


@pytest.fixture
def unlocked(pins):
    return {"X-Croom-Unlock": pins.unlock(PIN)}


async def test_every_route_asks_for_the_pin_first(client, admin):
    for method, path in (("get", "/api/settings/config/room"), ("post", "/api/settings/config/room"), ("get", "/api/settings/restart"),
                         ("post", "/api/settings/restart")):
        response = await getattr(client, method)(path, json={"values": {"room.name": "X"}})
        assert response.status == 401 and (await response.json())["error"] == "Enter the PIN", path
    assert admin.calls == []


async def test_a_page_answers_its_values_and_fields(client, unlocked):
    response = await client.get("/api/settings/config/room", headers=unlocked)
    assert response.status == 200
    body = await response.json()
    assert body["values"] == {"room.name": "Conference A", "room.location": "", "room.company": "Crystal Practice Management",
                              "room.timezone": "America/Chicago"}
    assert [f["path"] for f in body["fields"]] == list(body["values"])
    zone = body["fields"][3]
    assert zone["kind"] == "choice" and {"value": "Europe/London", "label": "Europe/London"} in zone["choices"]
    assert body["pending_restart"] is False


async def test_an_unknown_page_is_404(client, unlocked):
    assert (await client.get("/api/settings/config/network", headers=unlocked)).status == 404
    assert (await client.post("/api/settings/config/nope", json={"values": {}}, headers=unlocked)).status == 404


async def test_saving_the_room_name_writes_the_config_and_waits_for_a_restart(client, unlocked, config_file, admin):
    response = await client.post("/api/settings/config/room", json={"values": {"room.name": "Boardroom"}}, headers=unlocked)
    assert response.status == 200
    assert await response.json() == {"saved": ["room.name"], "restart": True, "pending_restart": True}
    assert [job for job, _ in admin.calls] == ["write_config"]
    data = yaml.safe_load(config_file.read_text())
    assert data["room"]["name"] == "Boardroom" and data["tv"]["host"] == "10.0.0.40"
    again = await (await client.get("/api/settings/config/room", headers=unlocked)).json()
    assert again["values"]["room.name"] == "Boardroom" and again["pending_restart"] is True


async def test_a_refused_value_is_400_with_words_by_setting_and_nothing_is_written(client, unlocked, config_file, admin):
    response = await client.post("/api/settings/config/room", json={"values": {"room.name": " ", "room.timezone": "Mars/Base"}},
                                 headers=unlocked)
    assert response.status == 400
    body = await response.json()
    assert body["errors"] == {"room.name": "Name can't be blank", "room.timezone": "Time zone: choose one of the list"}
    assert body["error"]
    assert admin.calls == [] and config_file.read_text() == HAND_WRITTEN


async def test_a_save_must_be_json_with_values(client, unlocked, admin):
    assert (await client.post("/api/settings/config/room", data="room.name=X", headers=unlocked)).status == 415
    response = await client.post("/api/settings/config/room", json={"values": ["room.name"]}, headers=unlocked)
    assert response.status == 400 and "mapping" in (await response.json())["error"]
    assert admin.calls == []


async def test_booking_hours_apply_at_once(client, unlocked, room):
    response = await client.post("/api/settings/config/calendar",
                                 json={"values": {"calendar.booking_start": "07:00", "calendar.booking_end": "20:00"}}, headers=unlocked)
    assert await response.json() == {"saved": ["calendar.booking_start", "calendar.booking_end"], "restart": False, "pending_restart": False}
    assert (room.bookings.start_hour.isoformat(), room.bookings.end_hour.isoformat()) == ("07:00:00", "20:00:00")


async def test_camera_preset_names_apply_at_once(client, unlocked, room):
    response = await client.post("/api/settings/config/devices", json={"values": {"control.camera_presets": ["Door", "Table", "Board"]}},
                                 headers=unlocked)
    assert (await response.json())["restart"] is False
    assert [p["name"] for p in room.camera.state()["presets"]] == ["Door", "Table", "Board"]


async def test_the_helpers_refusal_is_said(client, unlocked, admin):
    admin.refuse = "The room is restarting: try again in a moment"
    response = await client.post("/api/settings/config/room", json={"values": {"room.name": "B"}}, headers=unlocked)
    assert response.status == 409 and await response.json() == {"error": "The room is restarting: try again in a moment"}


async def test_without_the_helper_the_pages_are_not_available_on_this_device(client_factory, tmp_path, pins, unlocked):  # noqa: F811
    for admin in (None, FakeAdmin()):
        if admin is not None:
            admin.gone = True
        client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(tmp_path / "config.yaml")))
        for method, path in (("get", "/api/settings/config/room"), ("post", "/api/settings/config/room"),
                             ("get", "/api/settings/restart"), ("post", "/api/settings/restart")):
            response = await getattr(client, method)(path, json={"values": {"room.name": "X"}}, headers=unlocked)
            assert response.status == 503 and await response.json() == {"error": "Not available on this device"}, path


async def test_restart_now_asks_the_helper(client, unlocked, admin):
    response = await client.post("/api/settings/restart", json={}, headers=unlocked)
    assert response.status == 200 and await response.json() == {"restarting": True}
    assert admin.calls == [("restart", {})]


@pytest.mark.parametrize("state", [MeetingState.JOINING, MeetingState.CONNECTED])
async def test_restart_is_refused_in_a_meeting(client, unlocked, admin, room, state):
    room.meeting.set_state(state)
    response = await client.post("/api/settings/restart", json={}, headers=unlocked)
    assert response.status == 409 and await response.json() == {"error": "Leave the meeting first"}
    assert admin.calls == []


async def test_restart_status_is_the_helpers_record(client, unlocked, admin):
    async def call(job, **args):
        admin.calls.append((job, args))
        return {"ok": True, "state": "undone", "undone": True, "at": "2026-10-30T10:00:00+00:00", "why": "The room did not answer"}

    admin.call = call
    response = await client.get("/api/settings/restart", headers=unlocked)
    assert await response.json() == {"state": "undone", "undone": True, "at": "2026-10-30T10:00:00+00:00", "why": "The room did not answer"}


def test_from_config_makes_the_real_helper_client_and_reads_etc_croom(tmp_path):
    config = Config.from_dict({"data_dir": str(tmp_path)})
    service = ControlService.from_config(config)
    assert type(service._admin).__name__ == "AdminClient" and service._admin._path == "/run/croom-admin.sock"
    assert str(service._editor.path) == "/etc/croom/config.yaml"
    fake = FakeAdmin()
    assert ControlService.from_config(config, admin=fake)._admin is fake
