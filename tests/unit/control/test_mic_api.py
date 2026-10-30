"""The microphone's level on the room page's API (spec 2026-10-29, Mic level): the status's mic block, GET and POST /api/audio/mic,
outside the Settings PIN."""
from croom.devices.mic import RoomMic
from tests.unit.control.test_service import StubDevices, StubMic, client_factory, make_service  # noqa: F401  (client_factory is a fixture)


async def test_the_status_carries_the_mic_block_like_the_speakers(client_factory):  # noqa: F811
    client = await client_factory(make_service(devices=StubDevices()))
    data = await (await client.get("/api/status")).json()
    assert data["mic"] == {"available": True, "device": "Logitech MeetUp Speakerphone Analog Stereo", "level": 100, "device_level": 100,
                           "reason": None}


async def test_get_and_the_two_ways_to_change_it_without_a_pin(client_factory):  # noqa: F811
    devices = StubDevices()
    client = await client_factory(make_service(devices=devices))
    assert (await (await client.get("/api/audio/mic")).json())["level"] == 100
    assert (await (await client.post("/api/audio/mic", json={"level": 130})).json())["level"] == 130
    assert (await (await client.post("/api/audio/mic", json={"step": 5})).json())["level"] == 135
    assert (await (await client.post("/api/audio/mic", json={"level": 400})).json())["level"] == 150
    assert devices.mic.calls == [("level", 130), ("level", 135), ("level", 150)]


async def test_what_it_refuses(client_factory):  # noqa: F811
    devices = StubDevices()
    client = await client_factory(make_service(devices=devices))
    for body in ({}, {"level": 100, "step": 5}, {"level": "loud"}, {"step": True}, {"muted": True}):
        response = await client.post("/api/audio/mic", json=body)
        assert response.status == 400 and "error" in await response.json(), body
    assert (await client.post("/api/audio/mic", data="level=5")).status == 415
    assert devices.mic.calls == []


async def test_no_microphone_answers_409_with_the_reason(client_factory):  # noqa: F811
    client = await client_factory(make_service(devices=StubDevices(mic=StubMic(available=False))))
    response = await client.post("/api/audio/mic", json={"step": 5})
    assert response.status == 409 and (await response.json())["error"] == "no audio source"
    assert (await (await client.get("/api/status")).json())["mic"]["available"] is False


async def test_without_a_devices_service_the_block_has_the_real_keys_and_the_route_says_so(client_factory):  # noqa: F811
    client = await client_factory(make_service())
    data = await (await client.get("/api/status")).json()
    assert set(data["mic"]) == set(RoomMic().state())
    assert data["mic"]["available"] is False and data["mic"]["reason"] == "no devices service"
    assert (await (await client.get("/api/audio/mic")).json()) == data["mic"]
    response = await client.post("/api/audio/mic", json={"level": 10})
    assert response.status == 409 and (await response.json())["error"] == "no devices service"


async def test_the_status_reads_the_microphone_on_each_poll(client_factory):  # noqa: F811
    devices = StubDevices()
    client = await client_factory(make_service(devices=devices))
    await client.get("/api/status")
    await client.get("/api/status")
    assert devices.mic.refreshes == 2
