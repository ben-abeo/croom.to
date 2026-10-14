"""
The SDK page and its crystalMeet bridge, driven in the venv's Chromium against
the stub ZoomMtg with Zoom's CDN blocked (spec 2026-09-25 Zoom, section 4.5).
"""

import asyncio
import json

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.zoom_sdk_site import ZoomSdkSite  # noqa: E402
from tests.unit.meeting.zoom_stub import STUB_JS, block_sdk_cdn  # noqa: E402

JOIN = {"meetingNumber": "99612060433", "passWord": "abc123", "userName": "Room 1", "signature": "sig.nature.x",
        "zak": "ZAK-1", "micOn": True, "cameraOn": True, "sdkVersion": "6.5.0"}


class PageRun:
    """A started site plus a browser page with the stub and an event recorder."""

    def __init__(self, params=None, stub=STUB_JS):
        self.params = dict(JOIN, **(params or {}))
        self.stub = stub
        self.events = []

    async def __aenter__(self):
        self.site = ZoomSdkSite()
        await self.site.start()
        self._pw = await playwright.async_playwright().start()
        self.browser = await self._pw.chromium.launch()
        self.context = await self.browser.new_context()
        await block_sdk_cdn(self.context)
        if self.stub:
            await self.context.add_init_script(self.stub)
        self.page = await self.context.new_page()
        await self.page.expose_function("crystalMeetEvent", lambda state, detail: self.events.append((state, detail)))
        token = self.site.register_join(self.params)
        await self.page.goto(self.site.url() + "#" + token, wait_until="load")
        return self

    async def __aexit__(self, *exc):
        await self.browser.close()
        await self._pw.stop()
        await self.site.stop()

    async def wait_for(self, state, timeout=5.0):
        for _ in range(int(timeout * 20)):
            if any(s == state for s, _ in self.events):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"state {state!r} never reported; events: {self.events}")

    async def calls(self, name):
        return [c for c in await self.page.evaluate("window.__zoomCalls") if c[0] == name]


async def test_joins_with_the_one_time_parameters_and_reports_connected():
    async with PageRun() as run:
        await run.wait_for("connected")
        [init] = await run.calls("init")
        assert init[1]["leaveUrl"] == "/left" and init[1]["disablePreview"] is True and init[1]["showMeetingHeader"] is False
        [join] = await run.calls("join")
        assert join[1] == {"signature": "sig.nature.x", "meetingNumber": "99612060433", "passWord": "abc123",
                           "userName": "Room 1", "userEmail": "", "zak": "ZAK-1"}
        assert run.events[0][0] == "joining" and run.events[-1][0] == "connected"
        assert await run.page.evaluate("window.crystalMeet.state") == "connected"
        assert await run.page.evaluate("window.crystalMeet.userId") == 16777216
        [lib] = await run.calls("setZoomJSLib")
        assert lib[1:] == ["https://source.zoom.us/6.5.0/lib", "/av"]


async def test_join_without_zak_omits_it():
    async with PageRun({"zak": None}) as run:
        await run.wait_for("connected")
        [join] = await run.calls("join")
        assert "zak" not in join[1]


async def test_join_error_is_reported_in_words():
    async with PageRun({"meetingNumber": "999"}) as run:
        await run.wait_for("error")
        state, detail = [e for e in run.events if e[0] == "error"][0]
        assert detail == "This meeting ID is not valid (code 3712)"


async def test_waiting_room_is_reported_before_connected():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.waiting = true;") as run:
        await run.wait_for("connected")
        states = [s for s, _ in run.events]
        assert "waiting" in states and states.index("waiting") < states.index("connected")


async def test_mute_and_leave_go_through_the_sdk():
    async with PageRun() as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.mute(true)") is True
        assert await run.page.evaluate("window.crystalMeet.mute(false)") is False
        assert [c[1] for c in await run.calls("mute")] == [{"userId": 16777216, "mute": True}, {"userId": 16777216, "mute": False}]
        assert await run.page.evaluate("window.crystalMeet.leave()") is True
        assert [c[1] for c in await run.calls("leaveMeeting")] == [{"confirm": False}]
        await run.wait_for("left")


async def test_mic_off_at_join_mutes_after_connecting():
    async with PageRun({"micOn": False}) as run:
        await run.wait_for("connected")
        for _ in range(40):
            if await run.calls("mute"):
                break
            await asyncio.sleep(0.05)
        assert [c[1] for c in await run.calls("mute")] == [{"userId": 16777216, "mute": True}]


async def test_missing_sdk_is_reported_with_the_failing_urls():
    async with PageRun(stub="") as run:
        await run.wait_for("error")
        detail = [d for s, d in run.events if s == "error"][0]
        assert "did not load from source.zoom.us" in detail
        assert "zoom-meeting-6.5.0.min.js" in detail and "still published" in detail


async def test_connected_is_reported_once_the_current_user_is_known():
    async with PageRun() as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.userId") == 16777216
        assert [s for s, _ in run.events].count("connected") == 1


async def test_join_success_without_status_events_connects_after_the_grace_period():
    async with PageRun({"connectGraceMs": 300}, stub=STUB_JS + "window.ZoomMtg._behaviour.silentConnect = true;") as run:
        await run.wait_for("connected")


async def test_status_events_may_use_the_status_field_name():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.statusField = 'status';") as run:
        await run.wait_for("connected")


async def test_host_ending_the_meeting_is_reported_as_left():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.endAfterMs = 200;") as run:
        await run.wait_for("connected")
        await run.wait_for("left")


async def test_used_join_token_is_reported():
    async with PageRun() as run:
        await run.wait_for("connected")
        token = run.site.register_join(JOIN)
        await run.page.goto("about:blank")  # a fragment-only change would not reload the page
        await run.page.goto(run.site.url() + "#" + token, wait_until="load")
        await run.wait_for("connected")
        run.events.clear()
        await run.page.goto("about:blank")
        await run.page.goto(run.site.url() + "#" + token, wait_until="load")  # the same token again
        await run.wait_for("error")
        assert "join parameters expired" in [d for s, d in run.events if s == "error"][0]


CLIENT_LABELS = STUB_JS + "window.ZoomMtg._behaviour.labelStyle = 'client';"   # the way Zoom's web client names its buttons


async def test_the_bridge_reads_mute_and_video_from_zoom():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.mutedOnEntry = true; window.ZoomMtg._behaviour.videoOffOnEntry = true;") as run:
        await run.wait_for("connected")
        state = await run.page.evaluate("window.crystalMeet.readState()")
        assert state["muted"] is True and state["videoOn"] is False
        assert state["isHost"] is False and state["raisedHand"] is False
        assert "video" in state["keys"]                         # the raw field names, for the diagnostics


async def test_the_video_button_is_found_by_what_it_is_not_by_one_label():
    """Zoom's web client says "start my video" in the accessibility name and "Start Video" on the button; the old
    exact match found neither, so the camera could never be turned on (the fault seen in the test meeting)."""
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": True, "label": "stop my video"}
        assert await run.page.evaluate("window.crystalMeet.setVideo(false)") is False
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": False, "label": "start my video"}
        assert await run.page.evaluate("window.crystalMeet.setVideo(false)") is False   # already off: no press
        assert await run.page.evaluate("window.__videoClicks") == 1
        assert await run.page.evaluate("window.crystalMeet.setVideo(true)") is True
        assert await run.page.evaluate("window.__videoClicks") == 2


async def test_without_a_toolbar_the_video_is_unknown_and_a_press_says_so():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": None, "label": None}
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.setVideo(true)")
        assert "camera button" in str(failure.value)


async def test_a_change_the_host_makes_reaches_the_device_as_a_state_event():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._setMuted(true)")
        await run.wait_for("state")
        detail = next(d for s, d in run.events if s == "state")
        assert '"muted": true' in detail.replace('":', '": ')
        assert sum(1 for s, _ in run.events if s == "state") == 1       # only the change is reported, not every event
        await run.page.evaluate("window.ZoomMtg._fire('onUserUpdate', {userId: 1})")   # someone else's change
        await asyncio.sleep(0.3)
        assert sum(1 for s, _ in run.events if s == "state") == 1


async def test_read_meeting_reports_me_the_room_and_the_clients_buttons():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';") as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["me"] == {"userId": 16777216, "muted": False, "videoOn": True, "isHost": True, "isCoHost": False, "raisedHand": False}
        assert m["role"] == "host" and m["count"] == 3 and m["recording"] is False
        assert [p["name"] for p in m["participants"]] == ["Room 1", "Ann Lee", "Raj Patel"]
        assert m["participants"][2] == {"id": 16777218, "name": "Raj Patel", "muted": True, "video": False, "hand": True, "host": False, "waiting": False, "me": False}
        assert m["participants"][0]["me"] is True
        assert m["buttons"] == {"camera": {"on": True}, "raise_hand": {"on": False}, "reactions": {}, "captions": {"on": False},
                                "layout": {"value": "speaker"}, "hide_self": {"on": False}}


async def test_without_a_toolbar_no_buttons_are_reported_and_the_rest_still_reads():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"] == {} and m["count"] == 3 and m["me"]["videoOn"] is True   # video from the user's record


async def test_a_change_to_another_participant_reaches_the_device_with_the_whole_model():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._setAttendee(16777218, {bRaiseHand: false})")
        await run.wait_for("state")
        detail = json.loads(next(d for s, d in run.events if s == "state"))
        assert detail["participants"][2]["hand"] is False and detail["me"]["muted"] is False


# ---- Zoom as its real SDK is: its field names, its footer, and the reducer it calls listeners from ----


async def states(run, count, timeout=3.0):
    """The "state" events' details, decoded, once there are at least `count` of them."""
    for _ in range(int(timeout * 20)):
        found = [json.loads(d) for s, d in run.events if s == "state"]
        if len(found) >= count:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"{count} state events never reported; events: {run.events}")


async def test_the_state_event_carries_the_current_users_fields_at_the_top_level_too():
    """The provider reads muted and videoOn from the top of the event until it reads `me`: both places say the same."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._setMuted(true)")
        [detail] = await states(run, 1)
        assert detail["muted"] is True and detail["me"]["muted"] is True
        assert detail["videoOn"] is True and detail["isHost"] is False and detail["isCoHost"] is False and detail["raisedHand"] is False
        assert detail["participants"][0]["muted"] is True and detail["count"] == 3 and "keys" in detail


async def test_zoom_firing_updates_inside_its_reducer_is_read_after_it_returns_and_reported_once():
    """Zoom calls onUserUpdate from inside its own state reducer, where getCurrentUser throws (the stub does the same):
    the bridge reads a moment later, so a host-side mute still reaches the device, exactly once, with no error."""
    async with PageRun() as run:
        await run.wait_for("connected")
        errors = []
        run.page.on("pageerror", lambda error: errors.append(str(error)))
        await run.page.evaluate("window.ZoomMtg._setMuted(true)")
        await run.wait_for("state", timeout=3)                        # from the listener's read, not the five-second one
        await asyncio.sleep(0.3)
        assert sum(1 for s, _ in run.events if s == "state") == 1
        assert errors == []


async def test_a_burst_of_updates_is_read_once():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            window.__reads = 0;
            const real = window.ZoomMtg.getCurrentUser;
            window.ZoomMtg.getCurrentUser = function (opts) { window.__reads += 1; return real.call(this, opts); };
            window.ZoomMtg._setMuted(true);
            for (let i = 0; i < 4; i++) window.ZoomMtg._fire("onUserUpdate", { userId: 16777217 + i });
        }""")
        await run.wait_for("state", timeout=3)
        await asyncio.sleep(0.3)
        assert await run.page.evaluate("window.__reads") == 1
        assert sum(1 for s, _ in run.events if s == "state") == 1


async def test_a_read_that_fails_is_dropped_quietly_and_the_next_change_still_arrives():
    async with PageRun() as run:
        await run.wait_for("connected")
        errors = []
        run.page.on("pageerror", lambda error: errors.append(str(error)))
        await run.page.evaluate("""() => {
            window.__real = window.ZoomMtg.getCurrentUser;
            window.ZoomMtg.getCurrentUser = () => { throw new Error("Minified Redux error #3"); };
            window.ZoomMtg._fire("onUserUpdate", { userId: 1 });
        }""")
        await asyncio.sleep(0.3)
        assert errors == [] and not any(s == "state" for s, _ in run.events)
        await run.page.evaluate("window.ZoomMtg.getCurrentUser = window.__real; window.ZoomMtg._setMuted(true)")
        await run.wait_for("state", timeout=3)


async def test_the_five_second_read_is_installed_once_even_after_a_reconnect():
    counting = STUB_JS + """
    window.__polls = [];
    const realSetInterval = window.setInterval;
    window.setInterval = (fn, ms, ...rest) => { window.__polls.push(ms); return realSetInterval(fn, ms, ...rest); };
    """
    async with PageRun(stub=counting) as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.__polls.filter((ms) => ms === 5000).length") == 1
        await run.page.evaluate("window.ZoomMtg._status(4)")        # Zoom reconnecting ...
        await run.page.evaluate("window.ZoomMtg._status(2)")        # ... and connected again
        for _ in range(100):
            if [s for s, _ in run.events].count("connected") == 2:
                break
            await asyncio.sleep(0.05)
        assert [s for s, _ in run.events].count("connected") == 2
        assert await run.page.evaluate("window.__polls.filter((ms) => ms === 5000).length") == 1


async def test_recording_starting_reaches_the_device():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._setRecording(true)")
        [detail] = await states(run, 1)
        assert detail["recording"] is True


async def test_people_joining_and_leaving_reach_the_device_with_the_new_count():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            window.ZoomMtg._attendees.push({ userId: 16777219, userName: "Guest", muted: true, bVideoOn: false, bRaiseHand: false, isHost: false });
            window.ZoomMtg._fire("onUserJoin", { userId: 16777219 });
        }""")
        [joined] = await states(run, 1)
        assert joined["count"] == 4 and joined["participants"][3]["name"] == "Guest"
        await run.page.evaluate("""() => {
            window.ZoomMtg._attendees = window.ZoomMtg._attendees.filter((a) => a.userId !== 16777219);
            window.ZoomMtg._fire("onUserLeave", { userId: 16777219 });
        }""")
        _, left = await states(run, 2)
        assert left["count"] == 3


async def test_a_co_host_and_the_waiting_room_are_read_by_zooms_field_names():
    """isCoHost is "" when not (truthy-tested), the camera is `video`, the hand `raiseHand`, the waiting room `isHold`."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'co-host';") as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._setAttendee(16777218, {waiting: true})")        # said as isHold
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["me"]["isCoHost"] is True and m["me"]["isHost"] is False and m["role"] == "co-host"
        assert m["participants"][1]["video"] is True and m["participants"][2]["hand"] is True
        assert m["participants"][2]["waiting"] is True and m["participants"][1]["waiting"] is False
    async with PageRun() as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["me"]["isCoHost"] is False and m["role"] == "participant"


async def test_an_older_builds_field_names_are_still_read():
    """bVideoOn, bRaiseHand, bHold and the like stay as fallbacks behind Zoom's own names."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            window.ZoomMtg.getCurrentUser = (o) => o.success({ result: { currentUser:
                { userId: 16777216, bMuted: true, bVideoOn: false, bRaiseHand: true, isHost: true, isCoHost: "" } } });
            window.ZoomMtg.getAttendeeslist = (o) => o.success({ result: { attendeesList:
                [{ userId: 5, userName: "Old", bVideoOn: true, bRaiseHand: true, bHold: true }] } });
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["me"] == {"userId": 16777216, "muted": True, "videoOn": False, "isHost": True, "isCoHost": False, "raisedHand": True}
        assert m["participants"] == [{"id": 5, "name": "Old", "muted": False, "video": True, "hand": True, "host": False, "waiting": True, "me": False}]


async def test_a_record_without_mute_or_video_keeps_the_last_known_mute_and_leaves_the_camera_unknown():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.mute(true)") is True
        await run.page.evaluate("() => { window.ZoomMtg.getCurrentUser = (o) => o.success({ result: { currentUser: { userId: 16777216 } } }); }")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["me"]["muted"] is True and m["me"]["videoOn"] is None     # not read as unmuted and off
        state = await run.page.evaluate("window.crystalMeet.readState()")
        assert state["muted"] is True and state["videoOn"] is None and state["keys"] == ["userId"]


async def test_a_video_button_that_says_only_video_or_is_disabled_is_there_but_not_usable():
    """Zoom's footer button is named "start my video" / "stop my video" only while the camera is usable; otherwise its name
    is just "Video", and while video starts it is aria-disabled and ignores clicks."""
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("document.querySelector('#wc-footer #foot-bar button.send-video-container__btn').id") == "stub-video"
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": True, "label": "stop my video"}
        await run.page.evaluate("document.getElementById('stub-video').setAttribute('aria-label', 'Video')")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": None, "usable": False, "label": "Video"}
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.setVideo(true)")
        assert "camera button" in str(failure.value)
        assert await run.page.evaluate("window.__videoClicks") == 0                           # nothing was pressed
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert "camera" not in m["buttons"] and m["me"]["videoOn"] is True                    # the user's record still says
        await run.page.evaluate("""() => {
            const b = document.getElementById('stub-video');
            b.setAttribute('aria-label', 'start my video'); b.setAttribute('aria-disabled', 'true');
        }""")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": False, "usable": False, "label": "start my video"}
        with pytest.raises(Exception) as busy:
            await run.page.evaluate("window.crystalMeet.setVideo(true)")                         # a press the client would ignore
        assert "camera button" in str(busy.value) and await run.page.evaluate("window.__videoClicks") == 0
        assert await run.page.evaluate("window.crystalMeet.setVideo(false)") is False            # already what was asked: no press


async def test_without_the_real_buttons_class_a_footer_button_named_start_or_stop_video_is_the_one():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const outside = document.createElement("button"); outside.setAttribute("aria-label", "Stop Video"); document.body.append(outside);
        }""")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": None, "label": None}     # not in the footer: not the one
        await run.page.evaluate("""() => {
            const footer = document.createElement("div"); footer.id = "wc-footer";
            const inside = document.createElement("button"); inside.setAttribute("aria-label", "start my video");
            footer.append(inside); document.body.append(footer);
        }""")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": False, "label": "start my video"}


async def test_the_footer_is_searched_before_the_rest_of_the_page():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const before = document.createElement("button"); before.setAttribute("aria-label", "Lower Hand"); document.body.prepend(before);
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"]["raise_hand"] == {"on": False}          # the footer's own "Raise Hand", not the one before it on the page


async def test_without_the_footer_the_whole_page_is_searched_but_never_a_menu():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const menu = document.createElement("div"); menu.setAttribute("role", "menu");
            const item = document.createElement("button"); item.setAttribute("aria-label", "Hide Captions"); menu.append(item);
            const hand = document.createElement("button"); hand.setAttribute("aria-label", "Lower Hand");
            document.body.append(menu, hand);
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"] == {"raise_hand": {"on": True}}         # a menu's item is no button of the client's
