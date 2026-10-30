"""Settings › Meetings › Sign in to Google (spec 2026-10-29, sections 3 and 8): the routes behind the PIN, the refusals in words (a meeting,
the laptop's camera, no Google profile folder), a join refused while signing in, Done and Cancel, and the touch Pi's typing reaching the
room Pi's keyboard. The meeting service is the stub, and once the real one on a fake display and a fake browser."""

import asyncio
from types import SimpleNamespace

import pytest

from croom.core.config import Config
from croom.meeting.providers.base import MeetingState
from croom.meeting.service import MeetingService
from croom.meeting.signin_session import NO_SESSION, SET_UP_FIRST, SignInError
from croom.settings.pin import PinStore
from croom.tv.pointer import keystrokes
from tests.unit.control.test_service import StubKeyboard, StubMeeting, StubMouse, client_factory, make_service, wait_until  # noqa: F401
from tests.unit.meeting.test_signin_session import Rig, write_cookies, write_preferences

PIN = "1234"
STATE = "/api/settings/google/sign-in/state"
BEGIN = "/api/settings/google/sign-in"
DONE = "/api/settings/google/sign-in/done"
CANCEL = "/api/settings/google/sign-in/cancel"


@pytest.fixture
def pins(tmp_path):
    store = PinStore(tmp_path / "settings-pin.json")
    store.set_pin(PIN)
    return store


@pytest.fixture
def unlocked(pins):
    return {"X-Croom-Unlock": pins.unlock(PIN)}


@pytest.fixture
def meeting():
    return StubMeeting()


@pytest.fixture
def keyboard():
    return StubKeyboard()


@pytest.fixture
async def client(client_factory, pins, meeting, keyboard):  # noqa: F811
    return await client_factory(make_service(pin_store=pins, meeting=meeting, keyboard=keyboard, mouse=StubMouse()))


async def test_every_sign_in_route_asks_for_the_pin_first(client, meeting):
    for method, path in (("get", STATE), ("post", BEGIN), ("post", DONE), ("post", CANCEL)):
        response = await getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert response.status == 401 and (await response.json())["error"] == "Enter the PIN", path
    assert meeting._sign_ins() == []


async def test_the_posts_are_json(client, unlocked, meeting):
    for path in (BEGIN, DONE, CANCEL):
        response = await client.post(path, data="x", headers=unlocked)
        assert response.status == 415, path
    assert meeting._sign_ins() == []


async def test_a_room_without_a_meeting_service_says_so(client_factory, pins, unlocked):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins))
    assert (await client.get(STATE, headers=unlocked)).status == 503
    response = await client.post(BEGIN, json={}, headers=unlocked)
    assert response.status == 503 and (await response.json())["error"] == "Meeting service not available"


async def test_the_state_says_whether_the_room_is_signed_in_and_offers_the_rooms_folder(client, unlocked, meeting):
    body = await (await client.get(STATE, headers=unlocked)).json()
    assert body == {"configured": True, "profile_dir": "/var/lib/croom/meet-profile", "active": False, "last": None, "signed_in": False,
                    "address": None, "default_profile_dir": "/var/lib/croom/meet-profile"}
    meeting.google = {"signed_in": True, "address": "conference-a@crystalpm.com"}
    body = await (await client.get(STATE, headers=unlocked)).json()
    assert body["signed_in"] is True and body["address"] == "conference-a@crystalpm.com"


async def test_sign_in_opens_on_the_tv_makes_the_keyboard_and_done_says_who_the_room_is_signed_in_as(client, unlocked, meeting, keyboard):
    response = await client.post(BEGIN, json={}, headers=unlocked)
    assert response.status == 200
    body = await response.json()
    assert body["active"] is True and body["idle_left_s"] == 900
    assert keyboard.opened == 1                                     # made as the sign-in begins: the first key is not lost
    assert (await (await client.get(STATE, headers=unlocked)).json())["active"] is True
    response = await client.post(DONE, json={}, headers=unlocked)
    assert response.status == 200
    assert await response.json() == {"signed_in": True, "address": "conference-a@crystalpm.com",
                                     "words": "Signed in as conference-a@crystalpm.com"}
    assert meeting._sign_ins() == ["begin", "done"]
    body = await (await client.get(STATE, headers=unlocked)).json()
    assert body["active"] is False and body["signed_in"] is True and body["last"]["words"] == "Signed in as conference-a@crystalpm.com"


async def test_done_when_google_kept_no_session_says_so(client, unlocked, meeting):
    meeting.signs_in_as = None
    await client.post(BEGIN, json={}, headers=unlocked)
    body = await (await client.post(DONE, json={}, headers=unlocked)).json()
    assert body == {"signed_in": False, "address": None, "words": NO_SESSION}


async def test_cancel_changes_nothing_and_says_so(client, unlocked, meeting):
    await client.post(BEGIN, json={}, headers=unlocked)
    response = await client.post(CANCEL, json={}, headers=unlocked)
    assert response.status == 200
    assert (await response.json())["words"] == "Sign-in cancelled: nothing changed"
    assert meeting._sign_ins() == ["begin", "cancel"] and meeting.google["signed_in"] is False


@pytest.mark.parametrize("path", [DONE, CANCEL])
async def test_ending_a_sign_in_that_has_ended_is_refused_in_words(client, unlocked, path):
    response = await client.post(path, json={}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "The sign-in has ended already"


@pytest.mark.parametrize("state", [MeetingState.JOINING, MeetingState.IN_LOBBY, MeetingState.CONNECTED, MeetingState.LEAVING])
async def test_sign_in_is_refused_in_a_meeting(client, unlocked, meeting, state):
    meeting.state = state
    response = await client.post(BEGIN, json={}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "Leave the meeting first"
    assert meeting._sign_ins() == []


async def test_sign_in_is_refused_while_the_laptop_has_the_camera(client_factory, pins, unlocked, meeting):  # noqa: F811
    laptop = SimpleNamespace(join_refusal=lambda: "The camera is on the laptop: press Back to room first")
    client = await client_factory(make_service(pin_store=pins, meeting=meeting, laptop=laptop))
    response = await client.post(BEGIN, json={}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "The camera is on the laptop: press Back to room first"
    assert meeting._sign_ins() == []


async def test_a_room_with_no_google_profile_folder_is_told_to_set_up_google_meet_first(client, unlocked, meeting):
    meeting.google_profile = None
    body = await (await client.get(STATE, headers=unlocked)).json()
    assert body["configured"] is False and body["default_profile_dir"] == "/var/lib/croom/meet-profile"
    response = await client.post(BEGIN, json={}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == SET_UP_FIRST


async def test_a_browser_that_could_not_be_opened_is_said_in_words(client, unlocked, meeting):
    meeting.sign_in_refusal = SignInError("Google's sign-in could not be opened on the TV: no Chromium", 500)
    response = await client.post(BEGIN, json={}, headers=unlocked)
    assert response.status == 500 and (await response.json())["error"] == "Google's sign-in could not be opened on the TV: no Chromium"


async def test_a_join_is_refused_while_signing_in(client, unlocked, meeting):
    await client.post(BEGIN, json={}, headers=unlocked)
    response = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
    assert response.status == 409 and (await response.json())["error"] == "Signing in to Google: finish or cancel it first"
    assert meeting.joins == []
    await client.post(CANCEL, json={}, headers=unlocked)
    response = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
    assert response.status == 202


async def test_typing_on_the_touch_pi_reaches_the_room_pis_keyboard_and_keeps_the_sign_in_open(client, meeting, keyboard):
    async with client.ws_connect("/ws/pointer") as ws:
        await ws.send_json({"target": "pi", "type": "keys", "text": "Room@crystalpm.com"})
        await ws.send_json({"target": "pi", "type": "keys", "key": "Enter"})
        await wait_until(lambda: keyboard.typed() == "Room@crystalpm.com<Enter>")
        await wait_until(lambda: meeting.pointer_uses == 2)          # each message the room Pi took: the sign-in's 15 minutes start again
        await ws.send_json({"target": "pi", "type": "keys", "text": "ü"})
        assert (await asyncio.wait_for(ws.receive_json(), 2))["error"].startswith("The TV keyboard types the letters")
    assert keyboard.strokes == keystrokes({"text": "Room@crystalpm.com"}) + keystrokes({"key": "Enter"})


async def test_the_whole_way_with_the_real_meeting_service_on_a_fake_tv(client_factory, pins, unlocked, tmp_path):  # noqa: F811
    """The real MeetingService and SignInSession, on a fake display and browser: Sign in, a finger and keys on the touch Pi (the 15
    minutes start again), Done; the TV's browser comes back. A join meanwhile is refused."""
    rig = Rig(tmp_path)
    rig.google_signs_in = lambda: (write_cookies(rig.profile, ["SID"]), write_preferences(rig.profile, [{"email": "room@crystalpm.com"}]))
    config = Config()
    config.meeting.google_profile_dir = str(rig.profile)
    service = MeetingService(config, display=rig.display, sign_in=rig.session)
    keyboard = StubKeyboard()
    client = await client_factory(make_service(pin_store=pins, meeting=service, keyboard=keyboard, mouse=StubMouse()))
    body = await (await client.post(BEGIN, json={}, headers=unlocked)).json()
    assert body["active"] is True and rig.events == ["display stopped", "browser opened"]
    rig.clock.advance(14 * 60)
    async with client.ws_connect("/ws/pointer") as ws:
        await ws.send_json({"target": "pi", "type": "move", "dx": 3, "dy": 4})
        await ws.send_json({"target": "pi", "type": "keys", "text": "pw"})
        await wait_until(lambda: keyboard.typed() == "pw")
    assert (await (await client.get(STATE, headers=unlocked)).json())["idle_left_s"] == 15 * 60
    with pytest.raises(RuntimeError, match="Signing in to Google: finish or cancel it first"):
        await service.join_meeting("https://meet.google.com/abc-defg-hij")
    body = await (await client.post(DONE, json={}, headers=unlocked)).json()
    assert body["words"] == "Signed in as room@crystalpm.com"
    assert rig.events == ["display stopped", "browser opened", "browser closed", "display started"]
    state = await (await client.get(STATE, headers=unlocked)).json()
    assert state["signed_in"] is True and state["address"] == "room@crystalpm.com" and state["active"] is False
