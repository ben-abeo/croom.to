"""The room page's Laptop route (spec 2026-10-27): both ways, what it refuses, the status's laptop block, and Join refused while the
laptop has the MeetUp."""
from croom.calendar.bookings import SlotTaken
from croom.laptop.service import CAMERA_AWAY, LaptopError
from tests.unit.control.test_service import StubMeeting, client_factory, make_service  # noqa: F401  (client_factory is a fixture)


class StubLaptop:
    def __init__(self):
        self.calls, self.away, self.fail = [], False, None

    @property
    def camera_away(self):
        return self.away

    def state(self):
        return {"enabled": True, "available": True, "holder": "laptop" if self.away else "room", "until": None,
                "made_booking": False, "switching": False, "problem": None}

    async def to_laptop(self, minutes=None):
        self.calls.append(("laptop", minutes))
        if self.fail is not None:
            raise self.fail
        self.away = True
        return self.state()

    async def to_room(self, by_hand=True):
        self.calls.append(("room", by_hand))
        self.away = False
        return self.state()


async def test_laptop_and_back(client_factory):  # noqa: F811
    laptop = StubLaptop()
    client = await client_factory(make_service(meeting=StubMeeting(), laptop=laptop))
    response = await client.post("/api/laptop", json={"to": "laptop", "minutes": 45})
    assert response.status == 200 and (await response.json())["laptop"]["holder"] == "laptop"
    response = await client.post("/api/laptop", json={"to": "room"})
    assert response.status == 200 and (await response.json())["laptop"]["holder"] == "room"
    assert laptop.calls == [("laptop", 45), ("room", True)]


async def test_laptop_with_no_minutes_is_asked_as_such(client_factory):  # noqa: F811
    laptop = StubLaptop()
    client = await client_factory(make_service(laptop=laptop))
    assert (await client.post("/api/laptop", json={"to": "laptop"})).status == 200
    assert laptop.calls == [("laptop", None)]


async def test_what_the_route_refuses(client_factory):  # noqa: F811
    laptop = StubLaptop()
    client = await client_factory(make_service(laptop=laptop))
    assert (await client.post("/api/laptop", json={"to": "somewhere"})).status == 400
    assert (await client.post("/api/laptop", json={"to": "laptop", "minutes": "45"})).status == 400
    assert (await client.post("/api/laptop", json={"to": "laptop", "minutes": True})).status == 400
    assert (await client.post("/api/laptop", data="to=laptop")).status == 415
    laptop.fail = LaptopError("The switch did not answer: press the button on the switch box")
    response = await client.post("/api/laptop", json={"to": "laptop", "minutes": 30})
    assert response.status == 409 and "switch box" in (await response.json())["error"]
    laptop.fail = SlotTaken("That time is already booked")
    assert (await client.post("/api/laptop", json={"to": "laptop", "minutes": 30})).status == 409
    assert laptop.calls == [("laptop", 30), ("laptop", 30)]


async def test_a_room_without_the_switch_has_no_laptop_route_and_says_so_in_the_status(client_factory):  # noqa: F811
    client = await client_factory(make_service())
    assert (await client.post("/api/laptop", json={"to": "laptop"})).status == 503
    assert (await (await client.get("/api/status")).json())["laptop"] == {"enabled": False}


async def test_the_status_carries_laptop_mode(client_factory):  # noqa: F811
    laptop = StubLaptop()
    laptop.away = True
    client = await client_factory(make_service(laptop=laptop))
    assert (await (await client.get("/api/status")).json())["laptop"]["holder"] == "laptop"


async def test_join_is_refused_while_the_laptop_has_the_meetup(client_factory):  # noqa: F811
    laptop, meeting = StubLaptop(), StubMeeting()
    laptop.away = True
    client = await client_factory(make_service(meeting=meeting, laptop=laptop))
    response = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
    assert response.status == 409 and (await response.json())["error"] == CAMERA_AWAY
    assert meeting.joins == []
