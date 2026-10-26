"""
The SDK page and its crystalMeet bridge, driven in the venv's Chromium against
the stub ZoomMtg with Zoom's CDN blocked (spec 2026-09-25 Zoom, section 4.5).
"""

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.zoom_sdk_site import SDK_VERSION, ZoomSdkSite  # noqa: E402
from tests.unit.meeting.zoom_stub import STUB_JS, block_sdk_cdn  # noqa: E402

JOIN = {"meetingNumber": "99612060433", "passWord": "abc123", "userName": "Room 1", "signature": "sig.nature.x",
        "zak": "ZAK-1", "micOn": True, "cameraOn": True, "sdkVersion": "6.5.0"}


class PageRun:
    """A started site plus a browser page with the stub and an event recorder."""

    def __init__(self, params=None, stub=STUB_JS):
        self.params = dict(JOIN, **(params or {}))
        self.stub = stub
        self.events = []
        self.errors = []   # uncaught errors and unhandled rejections in the page

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
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
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
        await later(run, 1000)                                                       # Zoom drops a mute made again within a second
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
SDK_LABELS = STUB_JS + "window.ZoomMtg._behaviour.labelStyle = 'sdk';"          # the way other builds do: "Start Video"


def client(**flags):
    """The stub with Zoom's web-client labels and the given behaviour flags."""
    return CLIENT_LABELS + "".join(f"window.ZoomMtg._behaviour.{name} = {json.dumps(value)};" for name, value in flags.items())


async def test_the_bridge_reads_mute_and_video_from_zoom():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.mutedOnEntry = true; window.ZoomMtg._behaviour.videoOffOnEntry = true;") as run:
        await run.wait_for("connected")
        state = await run.page.evaluate("window.crystalMeet.readState()")
        assert state["muted"] is True and state["videoOn"] is False
        assert state["isHost"] is False and state["isCoHost"] is False and state["raisedHand"] is False
        assert "video" in state["keys"]                         # the SDK's own field names, for the diagnostics


async def test_the_video_button_is_found_by_what_it_is_not_by_one_label():
    """Zoom's web client says "start my video" or "stop my video" in the accessibility name and just "Video" on the
    button; the old exact match ("Start Video") found neither, so the camera could never be turned on (the fault seen in
    the test meeting)."""
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": True, "usable": True, "label": "stop my video", "disabled": False}
        assert await run.page.evaluate("window.crystalMeet.setVideo(false)") is False
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": False, "usable": True, "label": "start my video", "disabled": False}
        assert await run.page.evaluate("window.crystalMeet.setVideo(false)") is False   # already off: no press
        assert await run.page.evaluate("window.__videoClicks") == 1
        assert await run.page.evaluate("window.crystalMeet.setVideo(true)") is True
        assert await run.page.evaluate("window.__videoClicks") == 2


async def test_without_a_toolbar_the_video_is_unknown_and_a_press_says_so():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": None, "usable": False, "label": None, "disabled": False}
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.setVideo(true)")
        assert "Zoom's camera button is not on the screen yet; try again in a moment" in str(failure.value)


async def test_a_camera_press_waits_while_zooms_button_is_disabled():
    """Zoom keeps its video button aria-disabled while its video encoder starts (two or three seconds after connecting),
    and its onClick returns at once meanwhile: a click then did nothing, and the room joined dark. A press now waits for
    the button, presses it once, and settles on what Zoom has when the switch is done."""
    async with PageRun(stub=client(videoOffOnEntry=True, videoStartsAfterMs=800)) as run:
        await run.wait_for("connected")
        assert (await run.page.evaluate("window.crystalMeet.video()"))["disabled"] is True
        assert await run.page.evaluate("window.crystalMeet.setVideo(true)") is True
        assert await run.page.evaluate("window.__videoClicks") == 1             # one press, made once the button took it
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": True, "usable": True, "label": "stop my video", "disabled": False}


async def test_a_camera_press_zoom_never_takes_is_not_reported_as_done():
    """A button that stays disabled, or a press that changes nothing, is answered in words: the old state is never
    reported as the new one."""
    async with PageRun(stub=client(videoOffOnEntry=True, videoStartsAfterMs=60000)) as run:
        await run.wait_for("connected")
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.setVideo(true, {readyMs: 300, switchMs: 300})")
        assert "Zoom's camera button is still starting; try again in a moment" in str(failure.value)
        assert await run.page.evaluate("window.__videoClicks") == 0             # never pressed while disabled
    async with PageRun(stub=client(videoOffOnEntry=True, videoStuckOff=True)) as run:
        await run.wait_for("connected")
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.setVideo(true, {readyMs: 300, switchMs: 300})")
        assert "Zoom did not switch the camera" in str(failure.value)
        assert await run.page.evaluate("window.__videoClicks") == 1


async def test_without_a_usable_camera_the_video_is_off_and_a_press_says_why():
    """No camera, the camera blocked, or video disabled by the host: Zoom names its button just "Video" (aria-label and
    text). The room's video is off, as Zoom's record says, and a press is not made but answered in words."""
    async with PageRun(stub=client(videoUnusable=True)) as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": None, "usable": False, "label": "Video", "disabled": False}
        assert (await run.page.evaluate("window.crystalMeet.readState()"))["videoOn"] is False
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.setVideo(true)")
        assert "Zoom cannot use the camera right now" in str(failure.value)
        assert await run.page.evaluate("window.__videoClicks") == 0


DECOYS = r"""(() => {
  window.__decoyClicks = [];
  const decoy = (where, label, text) => {
    const b = document.createElement("button");
    if (label) b.setAttribute("aria-label", label);
    b.textContent = text;
    b.addEventListener("click", () => window.__decoyClicks.push(label || text));
    where.appendChild(b);
  };
  // Other parts of the client, earlier in the page than the footer: the More menu, a host's request, participant menus,
  // Settings; and the footer's own words on a button outside it.
  const elsewhere = document.createElement("div");
  for (const text of ["Stop Incoming Video", "Start My Video", "Stop Video", "Ask For Start Video", "Stop Room Video",
                      "Stop my video when joining"]) decoy(elsewhere, "", text);
  decoy(elsewhere, "stop my video", "Video");
  document.body.prepend(elsewhere);
  decoy(document.getElementById("wc-footer"), "Stop Incoming Video", "Stop Incoming Video");   // a menu opened in the footer
})()"""


async def test_the_video_button_is_the_footers_own_and_no_other_that_speaks_of_video():
    """Other buttons of Zoom's speak of starting or stopping video, and the first of them in the page was the one
    pressed: Stop Incoming Video, in the More menu, blanks every remote video on the TV. Only the footer's own video
    button is read or pressed."""
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        await run.page.evaluate(DECOYS)
        assert (await run.page.evaluate("window.crystalMeet.video()"))["label"] == "stop my video"
        assert await run.page.evaluate("window.crystalMeet.setVideo(false)") is False
        assert await run.page.evaluate("window.__videoClicks") == 1
        assert await run.page.evaluate("window.__decoyClicks") == []
        # A build whose button lost Zoom's class is still found in the footer by its exact name, and nowhere else.
        await run.page.evaluate("document.getElementById('stub-video').className = ''")
        assert (await run.page.evaluate("window.crystalMeet.video()"))["label"] == "start my video"
        await run.page.evaluate("document.getElementById('wc-footer').remove()")
        assert (await run.page.evaluate("window.crystalMeet.video()"))["label"] is None
        with pytest.raises(Exception):
            await run.page.evaluate("window.crystalMeet.setVideo(true)")
        assert await run.page.evaluate("window.__decoyClicks") == []


async def test_diagnostics_list_the_footers_buttons_and_the_view_widgets_but_no_others():
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        await run.page.evaluate(DECOYS)
        await run.page.evaluate("""() => { const bar = document.getElementById('foot-bar');
            for (let i = 0; i < 50; i++) { const b = document.createElement('button');
                b.setAttribute('aria-label', 'More ' + i); b.textContent = 'More ' + i; bar.appendChild(b); } }""")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert data["buttons"][0] == {"label": "stop my video", "text": "Video"}
        assert data["buttons"][1] == {"label": "Reactions"}              # the text is left out when it says the same
        assert {"label": "More 0"} in data["buttons"] and len(data["buttons"]) == 40
        named = " ".join(b["label"] + " " + b.get("text", "") for b in data["buttons"])
        assert "Start My Video" not in named and "Ask For Start Video" not in named
        assert "video" in data["user_keys"] and data["state"]["videoOn"] is True


async def test_a_change_the_host_makes_reaches_the_device_as_a_state_event():
    """Zoom calls onUserUpdate from inside its reducer, where reading the room's record throws: the bridge reads it a
    moment later, so a host's mute reaches the device at once, not with the next five-second look."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._setMuted(true)")
        await run.wait_for("state", timeout=1.0)
        details = [json.loads(d) for s, d in run.events if s == "state"]
        assert len(details) == 1 and details[0]["muted"] is True        # only the change is reported, not every event
        await run.page.evaluate("window.ZoomMtg._fire('onUserUpdate', {userId: 1})")   # someone else's change
        await asyncio.sleep(0.3)
        assert sum(1 for s, _ in run.events if s == "state") == 1
        assert run.errors == []                                          # no read thrown inside Zoom's reducer


async def test_one_state_poll_however_often_zoom_reconnects():
    count = ("window.__polls = 0; const setInterval0 = window.setInterval;"
             "window.setInterval = function (f, ms, ...rest) { if (ms === 5000) window.__polls += 1;"
             " return setInterval0.call(window, f, ms, ...rest); };")
    async with PageRun(stub=count + STUB_JS) as run:
        await run.wait_for("connected")
        for _ in range(3):
            await run.page.evaluate("window.ZoomMtg._status(4)")         # reconnecting ...
            await run.page.evaluate("window.ZoomMtg._status(2)")         # ... and connected again
        await asyncio.sleep(0.2)
        assert [s for s, _ in run.events].count("connected") == 4
        assert await run.page.evaluate("window.__polls") == 1


NO_USER_YET = STUB_JS + r"""
(() => {   // before Zoom's attendee list has the room, its record is a few fields and no user (the SDK's PI(undefined))
  const full = window.ZoomMtg.getCurrentUser;
  window.__listed = false;
  window.ZoomMtg.getCurrentUser = function (opts) {
    if (window.__listed) return full.call(this, opts);
    opts.success && opts.success({ result: { currentUser: { isOriginhost: false, isAlternativeHost: false, raiseHand: false, isHold: false } } });
  };
})();
"""


async def test_the_room_is_known_from_zooms_record_once_zoom_lists_it():
    """Until Zoom lists the room its record names no user: mute is unknown (never a definite "live"), a mute says why it
    cannot be done yet, and the next read takes the room's id and name."""
    async with PageRun(stub=NO_USER_YET) as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.userId") is None
        state = await run.page.evaluate("window.crystalMeet.readState()")
        assert state["muted"] is None and state["videoOn"] is True      # the button still says what the camera does
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.mute(true)")
        assert "Zoom has not listed the room in the meeting yet; try again in a moment" in str(failure.value)
        assert await run.calls("mute") == []
        await run.page.evaluate("window.__listed = true")
        assert (await run.page.evaluate("window.crystalMeet.readState()"))["muted"] is False
        assert await run.page.evaluate("[window.crystalMeet.userId, window.crystalMeet.userName]") == [16777216, "Room 1"]
        assert await run.page.evaluate("window.crystalMeet.mute(true)") is True


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


async def settled(run):
    """Connected, and past the read that Zoom's recording effect queues as the meeting UI mounts (50 ms after it): a test
    that changes something right after connecting would otherwise have the change picked up by that read, not its own."""
    await run.wait_for("connected")
    await asyncio.sleep(0.3)


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
        await settled(run)                       # no read in progress: it would take a synchronous listener's call for its own
        errors = []
        run.page.on("pageerror", lambda error: errors.append(str(error)))
        await run.page.evaluate("window.ZoomMtg._setMuted(true)")
        await run.wait_for("state", timeout=3)                        # from the listener's read, not the five-second one
        await asyncio.sleep(0.3)
        assert sum(1 for s, _ in run.events if s == "state") == 1
        assert errors == []


async def test_a_burst_of_updates_is_read_once():
    async with PageRun() as run:
        await settled(run)
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
    async with PageRun() as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.__pollFns.length") == 1
        await run.page.evaluate("window.ZoomMtg._status(4)")        # Zoom reconnecting ...
        await run.page.evaluate("window.ZoomMtg._status(2)")        # ... and connected again
        for _ in range(100):
            if [s for s, _ in run.events].count("connected") == 2:
                break
            await asyncio.sleep(0.05)
        assert [s for s, _ in run.events].count("connected") == 2
        assert await run.page.evaluate("window.__pollFns.length") == 1


async def test_recording_starting_reaches_the_device():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._setRecording(true)")
        [detail] = await states(run, 1)
        assert detail["recording"] is True


async def test_people_joining_and_leaving_reach_the_device_with_the_new_count():
    async with PageRun() as run:
        await settled(run)
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


async def test_an_attendee_whose_camera_zoom_does_not_speak_of_is_unknown_and_not_off():
    """Only a camera Zoom says is off is off: a record with no field for it says nothing, and the table must not mark that person "Video off"."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            window.ZoomMtg.getAttendeeslist = (o) => o.success({ result: { attendeesList: [
                { userId: 5, userName: "Says on", video: true }, { userId: 6, userName: "Says off", video: false }, { userId: 7, userName: "Says nothing" },
                { userId: 8, userName: "Older name", bVideoOn: false }, { userId: 9, userName: "Blank", video: "" }] } });
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert [p["video"] for p in m["participants"]] == [True, False, None, False, None]


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
    is just "Video", and while video starts it is aria-disabled and ignores clicks. The model offers the camera as unusable
    either way."""
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("document.querySelector('#wc-footer #foot-bar button.send-video-container__btn').id") == "stub-video"
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": True, "usable": True, "label": "stop my video", "disabled": False}
        await run.page.evaluate("document.getElementById('stub-video').setAttribute('aria-label', 'Video')")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": None, "usable": False, "label": "Video", "disabled": False}
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.setVideo(true)")
        assert "Zoom cannot use the camera right now" in str(failure.value)
        assert await run.page.evaluate("window.__videoClicks") == 0                           # nothing was pressed
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"]["camera"] == {"on": True, "usable": False} and m["me"]["videoOn"] is True   # offered as unavailable
        await run.page.evaluate("""() => {
            const b = document.getElementById('stub-video');
            b.setAttribute('aria-label', 'start my video'); b.setAttribute('aria-disabled', 'true');
        }""")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": False, "usable": True, "label": "start my video", "disabled": True}
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"]["camera"] == {"on": False, "usable": False}
        with pytest.raises(Exception) as busy:                                                    # a press the client would ignore
            await run.page.evaluate("window.crystalMeet.setVideo(true, {readyMs: 300, switchMs: 300})")
        assert "camera button" in str(busy.value) and await run.page.evaluate("window.__videoClicks") == 0
        assert await run.page.evaluate("window.crystalMeet.setVideo(false)") is False            # already what was asked: no press


async def test_the_real_buttons_class_comes_first_and_the_fallback_reads_the_name_not_the_text():
    """Without Zoom's class, a button in the footer is the video button only by its exact name (aria-label): the words on a
    button are not its name (spec 2026-09-25, amended 2026-10-13)."""
    nothing = {"on": None, "usable": False, "label": None, "disabled": False}
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const outside = document.createElement("button"); outside.setAttribute("aria-label", "Stop Video"); document.body.append(outside);
        }""")
        assert await run.page.evaluate("window.crystalMeet.video()") == nothing                         # not in the footer: not the one
        await run.page.evaluate("""() => {
            const footer = document.createElement("div"); footer.id = "wc-footer";
            const inside = document.createElement("button"); inside.id = "named"; inside.setAttribute("aria-label", "start my video");
            footer.append(inside); document.body.append(footer);
        }""")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": False, "usable": True, "label": "start my video", "disabled": False}   # by its name ...
        await run.page.evaluate("""() => {
            const named = document.getElementById("named");
            named.removeAttribute("aria-label"); named.textContent = "Stop Video";
        }""")
        assert await run.page.evaluate("window.crystalMeet.video()") == nothing                         # ... not by its text
        await run.page.evaluate("document.getElementById('named').setAttribute('aria-label', 'Stop Video')")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": True, "usable": True, "label": "Stop Video", "disabled": False}       # another build's name
        await run.page.evaluate("""() => {
            const bar = document.createElement("div"); bar.id = "foot-bar";
            const real = document.createElement("button"); real.className = "send-video-container__btn"; real.setAttribute("aria-label", "start my video");
            bar.append(real); document.getElementById("wc-footer").append(bar);
        }""")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": False, "usable": True, "label": "start my video", "disabled": False}   # the class comes first


async def test_a_hand_button_outside_the_footer_is_not_the_one():
    """The hand is a row of the Reactions picker, or a footer button of its own in an uncommon build; the client's other panels
    have hand buttons too, and one outside the footer is neither read nor pressed."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            window.__decoyClicks = [];
            const before = document.createElement("button"); before.setAttribute("aria-label", "Lower Hand");
            before.addEventListener("click", () => window.__decoyClicks.push("Lower Hand")); document.body.prepend(before);
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"]["raise_hand"] == {"on": False}          # the room's record, not the words of a button that is not the footer's
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["me"]["raisedHand"] is True and await run.page.evaluate("window.__decoyClicks") == []


async def test_without_the_footer_the_whole_page_is_searched_but_never_a_menu():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const menu = document.createElement("div"); menu.setAttribute("role", "menu");
            const item = document.createElement("button"); item.setAttribute("aria-label", "Hide Captions"); menu.append(item);
            const captions = document.createElement("button"); captions.setAttribute("aria-label", "Show Captions");
            const hand = document.createElement("button"); hand.setAttribute("aria-label", "Lower Hand");
            document.body.append(menu, captions, hand);
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"] == {"captions": {"on": False}}          # a menu's item is no button of the client's, and the hand is the footer's


# ---- what Zoom 6.5.0 really sends and does (fix round 1) ----


async def test_recording_is_read_from_the_words_zoom_sends_not_from_their_being_there():
    """Zoom's only emitter sends {recording: "stop" | "recording" | "pause", hasLocalRecord} and fires it once as the
    meeting UI mounts, with "stop": a meeting nobody records must not read as recording from that first event."""
    async with PageRun() as run:
        await run.wait_for("connected")
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["recording"] is False      # after the mount event
        seen = []
        for words in ["stop", "recording", "pause", "stop"]:
            await run.page.evaluate("(words) => window.ZoomMtg._fire('onRecordingChange', { recording: words, hasLocalRecord: false })", words)
            seen.append((await run.page.evaluate("window.crystalMeet.readMeeting()"))["recording"])
        assert seen == [False, True, False, False]
        for flag in (True, False):                                                       # a boolean, as an older build may send
            await run.page.evaluate("(flag) => window.ZoomMtg._fire('onRecordingChange', { recording: flag })", flag)
            assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["recording"] is flag


async def test_a_roster_that_never_answers_does_not_hang_the_read():
    """Zoom's getAttendeeslist calls neither callback while its roster is empty (it fills a moment after "connected"):
    readState and readMeeting must still answer, with the roster as last known (at first, empty)."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.silentAttendees = true;") as run:
        await run.wait_for("connected")
        started = time.monotonic()
        state = await asyncio.wait_for(run.page.evaluate("window.crystalMeet.readState()"), timeout=2.5)
        assert time.monotonic() - started < 2.5 and state["muted"] is False and state["userId"] == 16777216
        m = await asyncio.wait_for(run.page.evaluate("window.crystalMeet.readMeeting()"), timeout=2.5)
        assert m["participants"] == [] and m["count"] == 0 and m["me"]["userId"] == 16777216


async def test_a_roster_that_goes_quiet_keeps_the_last_list():
    async with PageRun() as run:
        await run.wait_for("connected")
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["count"] == 3
        await run.page.evaluate("window.ZoomMtg._behaviour.silentAttendees = true")
        m = await asyncio.wait_for(run.page.evaluate("window.crystalMeet.readMeeting()"), timeout=2.5)
        assert m["count"] == 3 and [p["name"] for p in m["participants"]] == ["Room 1", "Ann Lee", "Raj Patel"]


async def test_a_current_user_that_never_answers_does_not_hang_the_read_either():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("() => { window.ZoomMtg.getCurrentUser = () => {}; }")              # neither callback
        state = await asyncio.wait_for(run.page.evaluate("window.crystalMeet.readState()"), timeout=2.5)
        assert state["muted"] is False and state["userId"] == 16777216 and "video" in state["keys"]     # as last known


async def test_a_roster_that_arrives_within_the_wait_ends_the_wait_with_the_roster():
    """A silent getAttendeeslist registers nothing, so the read asks again every 250 ms: it is done when Zoom answers, not
    after the full two seconds (and a join reads twice)."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.rosterAfterMs = 600;") as run:
        await run.wait_for("connected")
        started = time.monotonic()
        m = await asyncio.wait_for(run.page.evaluate("window.crystalMeet.readMeeting()"), timeout=3.0)
        assert time.monotonic() - started < 1.5
        assert m["count"] == 3 and m["me"]["userId"] == 16777216          # the roster, and the user's record built from it


async def test_reads_run_one_at_a_time_and_a_request_during_a_read_is_answered_by_one_more():
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("""() => {
            window.__active = 0; window.__maxActive = 0; window.__reads = 0;
            const real = window.crystalMeet.readMeeting;
            window.crystalMeet.readMeeting = async function () {              // a slow read, whose answer is as of its start
                window.__active += 1; window.__maxActive = Math.max(window.__maxActive, window.__active); window.__reads += 1;
                try { const seen = await real.call(this); await new Promise((r) => setTimeout(r, 400)); return seen; }
                finally { window.__active -= 1; }
            };
            window.ZoomMtg._fire("onUserUpdate", { userId: 1 });              // starts a read in 50 ms
        }""")
        await asyncio.sleep(0.2)                                              # that read is in progress
        await run.page.evaluate("window.ZoomMtg._setMuted(true)")             # a change it has not seen
        [detail] = await states(run, 1, timeout=3.0)
        assert detail["muted"] is True
        assert await run.page.evaluate("[window.__maxActive, window.__reads]") == [1, 2]


async def test_the_current_user_is_filled_in_from_a_later_record_when_the_first_was_empty():
    """getCurrentUser may answer {} while Zoom is still starting: neither the id nor the name is known until a record has
    them, and the join's mic-off default, which needs the id, goes out then, once."""
    emptyAtFirst = STUB_JS + """
    (function () {
      const real = window.ZoomMtg.getCurrentUser;
      window.__emptyUser = true;
      window.ZoomMtg.getCurrentUser = function (opts) {
        if (window.__emptyUser) opts.success({ result: { currentUser: {} } }); else real.call(this, opts);
      };
    })();
    """
    async with PageRun({"micOn": False}, stub=emptyAtFirst) as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("[window.crystalMeet.userId === null, window.crystalMeet.userName === null]") == [True, True]
        assert await run.calls("mute") == []                           # no mute is sent for a user nobody knows the id of ...
        await run.page.evaluate("window.__emptyUser = false")
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["me"]["userId"] == 16777216
        assert await run.page.evaluate("[window.crystalMeet.userId, window.crystalMeet.userName]") == [16777216, "Room 1"]
        assert [c[1] for c in await run.calls("mute")] == [{"userId": 16777216, "mute": True}]      # ... until there is one: then it goes out
        await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert len(await run.calls("mute")) == 1                                                    # and only once


BLANK_ID = STUB_JS + """
(function () {   // a record whose userId is window.__blankId ("" or null) until the test sets it to undefined
  const real = window.ZoomMtg.getCurrentUser;
  window.__blankId = "";
  window.ZoomMtg.getCurrentUser = function (opts) {
    if (window.__blankId === undefined) { real.call(this, opts); return; }
    opts.success({ result: { currentUser: { userId: window.__blankId, userName: "", isHold: false } } });
  };
})();
"""


async def test_a_record_whose_id_is_empty_names_nobody():
    """An id of null or "" is no id: the page takes neither it nor the empty name, a mute from the table says why it cannot
    be done yet, and the join's mic-off default waits for a real id."""
    async with PageRun({"micOn": False}, stub=BLANK_ID) as run:
        await run.wait_for("connected")
        for blank in ("", None):
            await run.page.evaluate("(id) => { window.__blankId = id; }", blank)
            await run.page.evaluate("window.crystalMeet.readMeeting()")
            assert await run.page.evaluate("[window.crystalMeet.userId, window.crystalMeet.userName]") == [None, None]
            with pytest.raises(Exception, match="Zoom has not listed the room in the meeting yet"):
                await run.page.evaluate("window.crystalMeet.mute(true)")
        assert await run.calls("mute") == []
        await run.page.evaluate("() => { window.__blankId = undefined; }")
        await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert [c[1] for c in await run.calls("mute")] == [{"userId": 16777216, "mute": True}]   # a real id: the default goes out


async def test_a_roster_record_with_no_id_is_not_the_room():
    """An assistant-shaped current user has no id (userId null, a name like "You are a assistant", nothing else known): the roster's
    records with a null or empty id are not the room, whatever else they have in common with it; only a record with an id can be it."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            window.ZoomMtg.getCurrentUser = (o) => o.success({result: {currentUser: {userId: null, userName: "You are a assistant", muted: null, audio: null, isHost: null, isGuest: null, isHold: false}}});
            window.ZoomMtg.getAttendeeslist = (o) => o.success({result: {attendeesList: [{userId: null, userName: "A"}, {userId: "", userName: "B"}, {userId: 7, userName: "C"}]}});
            window.crystalMeet.userId = null;
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert [p["me"] for p in m["participants"]] == [False, False, False]


async def test_a_reconnect_does_not_mute_the_room_again():
    """The join's mic-off default is armed once per join: when Zoom reconnects, an unmute the table made since stands."""
    async with PageRun({"micOn": False}) as run:
        await settled(run)
        assert [c[1]["mute"] for c in await run.calls("mute")] == [True]                          # the join's default
        await later(run, 1000)                                                                    # Zoom drops a mute made again within a second
        assert await run.page.evaluate("window.crystalMeet.mute(false)") is False                 # the table unmutes
        await run.page.evaluate("window.ZoomMtg._status(4)")                                      # Zoom reconnecting ...
        await run.page.evaluate("window.ZoomMtg._status(2)")                                      # ... and connected again
        await asyncio.sleep(0.3)
        assert [s for s, _ in run.events].count("connected") == 2
        assert [c[1]["mute"] for c in await run.calls("mute")] == [True, False]
        assert await run.page.evaluate("window.ZoomMtg._muted") is False


async def test_the_five_second_read_reports_a_change_nothing_else_announced():
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("window.ZoomMtg._muted = true")                          # no event from Zoom for it
        await run.page.evaluate("window.__pollFns[0]()")                                 # the five-second tick
        [detail] = await states(run, 1, timeout=2.0)
        assert detail["muted"] is True


async def test_the_five_second_read_swallows_a_failed_read():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("() => { window.ZoomMtg.getCurrentUser = () => { throw new Error('Minified Redux error #3'); }; window.__pollFns[0](); }")
        await asyncio.sleep(0.3)
        assert await run.page.evaluate("window.__rejections") == []


async def test_a_failed_first_read_still_starts_the_five_second_read():
    failsOnce = STUB_JS + """
    (function () {
      const real = window.ZoomMtg.getAttendeeslist; let first = true;
      window.ZoomMtg.getAttendeeslist = function (opts) {
        if (first) { first = false; throw new Error("Minified Redux error #3"); }
        return real.call(this, opts);
      };
    })();
    """
    async with PageRun(stub=failsOnce) as run:
        await run.wait_for("connected")
        await asyncio.sleep(0.3)
        assert await run.page.evaluate("window.__pollFns.length") == 1
        assert await run.page.evaluate("window.__rejections") == []                      # and the failure was not left unhandled


async def test_a_change_to_the_clients_buttons_alone_reaches_the_device():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.click("#stub-captions")                                           # the button says "Hide Captions" now
        await run.page.evaluate("window.ZoomMtg._fire('onUserUpdate', { userId: 1 })")   # and Zoom says only that something changed
        [detail] = await states(run, 1)
        assert detail["buttons"]["captions"] == {"on": True}


async def test_the_view_button_gives_the_view_and_self_view_and_a_self_view_button_is_not_the_view_button():
    async with PageRun() as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"]["layout"] == {"value": "speaker"} and m["buttons"]["hide_self"] == {"on": False}     # both from the one View button
        await run.page.evaluate("""() => {
            document.getElementById('stub-view').remove();
            const bar = document.getElementById("foot-bar"), button = document.createElement("button");
            button.setAttribute("aria-label", "Hide Self View"); bar.append(button);                                // "Hide Self View" is no view switcher
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert "layout" not in m["buttons"] and "hide_self" not in m["buttons"]


async def test_a_video_button_outside_the_footer_bar_is_not_the_one():
    """The real client has a second .send-video-container__btn, in .minimize-widget__footer, outside #foot-bar: the footer
    bar's own comes first (the widget's is the one only while the meeting is minimized and the footer bar has none)."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const mini = document.createElement("div"); mini.className = "minimize-widget__footer";
            const decoy = document.createElement("button"); decoy.className = "send-video-container__btn"; decoy.setAttribute("aria-label", "start my video");
            mini.append(decoy); document.body.prepend(mini);
        }""")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": True, "usable": True, "label": "stop my video", "disabled": False}


async def test_zooms_own_field_names_win_over_the_older_ones_in_the_same_record():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            window.ZoomMtg.getCurrentUser = (o) => o.success({ result: { currentUser:
                { userId: 16777216, video: true, bVideoOn: false, raiseHand: true, bRaiseHand: false } } });
            window.ZoomMtg.getAttendeeslist = (o) => o.success({ result: { attendeesList: [
                { userId: 5, userName: "A", video: true, bVideoOn: false, raiseHand: true, bRaiseHand: false, isHold: false, waiting: true },
                { userId: 6, userName: "B", video: false, bVideoOn: true, raiseHand: false, bRaiseHand: true, isHold: true, waiting: false }] } });
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["me"]["videoOn"] is True and m["me"]["raisedHand"] is True
        a, b = m["participants"]
        assert (a["video"], a["hand"], a["waiting"]) == (True, True, False)
        assert (b["video"], b["hand"], b["waiting"]) == (False, False, True)


async def test_a_build_that_names_the_video_button_start_video_is_read_too():
    async with PageRun(stub=SDK_LABELS) as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": True, "usable": True, "label": "Stop Video", "disabled": False}
        assert await run.page.evaluate("window.crystalMeet.setVideo(false)") is False
        assert await run.page.evaluate("window.crystalMeet.video()") == {"on": False, "usable": True, "label": "Start Video", "disabled": False}


async def test_another_persons_waiting_room_event_does_not_make_the_room_wait():
    """Zoom also sends onUserIsInWaitingRoom to a host for other people entering or leaving its waiting room (self: false),
    and with isInWaitingRoom false when this room is let in: only this room's own wait is a state of the page."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.neverConnect = true;") as run:
        await run.wait_for("joining")
        await run.page.evaluate("window.ZoomMtg._fire('onUserIsInWaitingRoom', { userId: 99, self: false, isInWaitingRoom: true })")
        await run.page.evaluate("window.ZoomMtg._fire('onUserIsInWaitingRoom', { userId: 99, self: false, isInWaitingRoom: false })")
        await run.page.evaluate("window.ZoomMtg._fire('onUserIsInWaitingRoom', { userId: 16777216, self: true, isInWaitingRoom: false })")
        assert await run.page.evaluate("window.crystalMeet.state") == "joining"
        await run.page.evaluate("window.ZoomMtg._fire('onUserIsInWaitingRoom', { userId: 16777216, self: true, isInWaitingRoom: true })")
        assert await run.page.evaluate("window.crystalMeet.state") == "waiting"


# ---- fix round 2: the order of a read, the id that comes late, and the edges of one-at-a-time ----


async def test_the_mic_off_default_is_applied_when_the_room_is_first_listed():
    """Zoom builds the current user's record from its roster: until the roster is there the record has no id, and there is
    nobody to mute. The join's mic-off default waits for the id, then goes out once, and the table is told it happened."""
    async with PageRun({"micOn": False}, stub=STUB_JS + "window.ZoomMtg._behaviour.rosterAfterMs = 2500;") as run:
        await run.wait_for("connected")
        with pytest.raises(Exception, match="Zoom has not listed the room in the meeting yet; try again in a moment"):
            await run.page.evaluate("window.crystalMeet.mute(true)")
        assert await run.calls("mute") == []
        for _ in range(100):                                  # the roster is there 2.5 s after the join: the next read finds the id
            if await run.calls("mute"):
                break
            await asyncio.sleep(0.05)
        assert [c[1] for c in await run.calls("mute")] == [{"userId": 16777216, "mute": True}]
        for _ in range(40):                                   # that read had the record from before the mute: it is read again
            if any(json.loads(d)["muted"] for s, d in run.events if s == "state"):
                break
            await asyncio.sleep(0.05)
        assert any(json.loads(d)["muted"] is True for s, d in run.events if s == "state")


async def test_mute_says_it_is_not_in_a_meeting_before_the_meeting_is_joined():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.neverConnect = true;") as run:
        await run.wait_for("joining")
        with pytest.raises(Exception, match="not in a meeting"):
            await run.page.evaluate("window.crystalMeet.mute(true)")


async def test_a_read_takes_the_current_user_after_the_roster_wait_not_before_it():
    """A read that waits for a silent roster must not carry a current user from before the wait: a mute made during it would
    be reported as it was, the table showing unmuted for a room Zoom has muted."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.silentAttendees = true;") as run:
        await run.wait_for("connected")
        read = asyncio.ensure_future(run.page.evaluate("window.crystalMeet.readMeeting()"))
        await asyncio.sleep(0.5)
        await run.page.evaluate("window.ZoomMtg._muted = true")               # during the wait
        m = await asyncio.wait_for(read, timeout=3.5)
        assert m["me"]["muted"] is True


FAILS_ONCE = STUB_JS + """
(function () {
  const real = document.querySelector.bind(document);       // the read looks at the client's buttons once its answers are in
  document.querySelector = function (selector) {
    if (window.__break) { window.__break = false; throw new Error("the page broke"); }
    return real(selector);
  };
})();
"""


async def test_a_request_queued_during_a_read_that_fails_is_read_again():
    """A read that throws after waiting must not take down with it the request that came while it ran."""
    async with PageRun(stub=FAILS_ONCE) as run:
        await settled(run)
        await run.page.evaluate("() => { window.ZoomMtg._behaviour.silentAttendees = true; window.ZoomMtg._setMuted(true); }")   # a read starts and waits
        await asyncio.sleep(0.3)
        await run.page.evaluate("window.ZoomMtg._fire('onUserUpdate', { userId: 1 })")                    # a request while it runs
        await asyncio.sleep(0.1)
        await run.page.evaluate("() => { window.ZoomMtg._behaviour.silentAttendees = false; window.__break = true; }")   # it fails once the roster is in
        [detail] = await states(run, 1, timeout=2.0)
        assert detail["muted"] is True
        assert await run.page.evaluate("window.__rejections") == []


async def test_no_state_event_follows_left():
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("() => { window.ZoomMtg._behaviour.silentAttendees = true; window.ZoomMtg._setMuted(true); }")   # a read starts and waits
        await asyncio.sleep(0.4)
        await run.page.evaluate("window.crystalMeet.leave()")
        await asyncio.sleep(2.1)                                              # past the end of that read
        order = [s for s, _ in run.events]
        assert "left" in order and "state" not in order[order.index("left"):]


async def test_the_rooms_own_waiting_room_event_arriving_after_it_connected_is_ignored():
    """Zoom delivers the room's own waiting-room event up to a second and a half late: once the room is in, it is not
    waiting, and the page goes on reporting what changes."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._fire('onUserIsInWaitingRoom', { userId: 16777216, self: true, isInWaitingRoom: true })")
        assert await run.page.evaluate("window.crystalMeet.state") == "connected"
        await run.page.evaluate("window.ZoomMtg._setMuted(true)")
        [detail] = await states(run, 1)
        assert detail["muted"] is True


async def test_an_older_waiting_room_event_without_self_means_this_room():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.neverConnect = true;") as run:
        await run.wait_for("joining")
        await run.page.evaluate("window.ZoomMtg._fire('onUserIsInWaitingRoom', { isInWaitingRoom: true })")
        assert await run.page.evaluate("window.crystalMeet.state") == "waiting"


async def test_a_roster_error_keeps_the_last_list():
    """A transient error must not be reported as every participant having gone."""
    async with PageRun() as run:
        await run.wait_for("connected")
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["count"] == 3
        await run.page.evaluate("() => { window.ZoomMtg.getAttendeeslist = (o) => o.error({ errorCode: 1, errorMessage: 'not now' }); }")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["count"] == 3


async def test_a_roster_answer_that_is_not_a_list_is_read_as_the_last_list_was():
    async with PageRun() as run:
        await run.wait_for("connected")
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["count"] == 3
        await run.page.evaluate("() => { window.ZoomMtg.getAttendeeslist = (o) => o.success({ result: { attendeesList: { notAList: true } } }); }")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["count"] == 3
        await run.page.evaluate("() => { window.ZoomMtg.getAttendeeslist = (o) => o.success({ result: {} }); }")
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["count"] == 3          # nor does an answer with no list


async def test_a_read_that_fails_is_passed_on_to_whoever_waits_for_it():
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("() => { window.__real = window.ZoomMtg.getCurrentUser; window.ZoomMtg.getCurrentUser = () => { throw new Error('the roster broke'); }; }")
        with pytest.raises(Exception, match="the roster broke"):
            await run.page.evaluate("window.crystalMeet._sync()")
        await run.page.evaluate("() => { window.ZoomMtg.getCurrentUser = window.__real; }")
        await run.page.evaluate("window.crystalMeet._sync()")                 # and when it works, it just finishes


async def test_a_failed_read_followed_by_a_good_one_is_not_passed_on():
    """Only the failure of the last read is passed on: a read that fails while a request waits for it is followed by one
    more read, and when that one works, nobody waiting is told of a failure."""
    async with PageRun(stub=FAILS_ONCE) as run:
        await settled(run)
        await run.page.evaluate("() => { window.ZoomMtg._behaviour.silentAttendees = true; }")
        first = asyncio.ensure_future(run.page.evaluate("window.crystalMeet._sync()"))         # a read, waiting for the roster
        await asyncio.sleep(0.3)
        second = asyncio.ensure_future(run.page.evaluate("window.crystalMeet._sync()"))        # a request while it runs
        await asyncio.sleep(0.1)
        await run.page.evaluate("() => { window.ZoomMtg._behaviour.silentAttendees = false; window.__break = true; }")   # the first read fails ...
        await asyncio.wait_for(first, timeout=3)                                               # ... the one after it works
        await asyncio.wait_for(second, timeout=3)


# ---- Controls: the client's own buttons and popovers, pressed from the table (spec 2026-10-13, section 4.3) ----


CLOSED = {"picker": False, "view": False}


async def tidy(run):
    """No popover of the client's is left open, and none was taken out of the page by anyone but its owner."""
    assert await run.page.evaluate("window.ZoomMtg._open") == CLOSED
    assert await run.page.evaluate("document.getElementById('reactionPicker') || document.querySelector('.full-screen-viev-dropdown .dropdown-menu')") is None
    assert await run.page.evaluate("window.__reactErrors") == []


async def presses(run):
    return [p[0] for p in await run.page.evaluate("window.__presses")]


async def test_button_controls_press_the_clients_buttons_and_settle_on_the_new_state():
    async with PageRun() as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["buttons"]["raise_hand"] == {"on": True} and m["me"]["raisedHand"] is True    # the table's button and the room's record agree
        m = await run.page.evaluate("window.crystalMeet.control('captions')")
        assert m["buttons"]["captions"] == {"on": True}
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
        assert m["buttons"]["hide_self"] == {"on": True}
        m = await run.page.evaluate("window.crystalMeet.control('camera')")
        assert m["buttons"]["camera"] == {"on": False} and m["me"]["videoOn"] is False
        assert await presses(run) == ["stub-reactions", "stub-hand", "stub-captions", "stub-view", "stub-self"]   # the hand and self view in their popovers
        await tidy(run)


async def test_menus_open_pick_and_close_within_one_call():
    async with PageRun() as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        assert m["buttons"]["layout"] == {"value": "gallery"} and await run.page.evaluate("window.ZoomMtg._layout") == "gallery"
        assert await run.page.evaluate("document.querySelector('.full-screen-viev-dropdown .dropdown-menu')") is None
        await run.page.evaluate("window.crystalMeet.control('reactions', '👏')")
        assert ["reaction", "clap"] in await run.page.evaluate("window.__zoomCalls")
        assert await run.page.evaluate("document.getElementById('reactionPicker')") is None
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('reactions', '🦄')")
        assert "Zoom offers no 🦄 here" in str(failure.value)
        assert await run.page.evaluate("document.getElementById('reactionPicker')") is None   # closed again after the miss
        assert await presses(run) == ["stub-view", "stub-view-item", "stub-reactions", "stub-reaction", "stub-reactions", "stub-reactions"]   # a miss closes it by its opener
        await tidy(run)


async def test_a_missing_button_says_so():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert "not on the screen yet" in str(failure.value)


async def test_every_control_names_the_button_that_is_not_on_the_screen():
    """Without the footer each control says which of Zoom's buttons it could not find, in the camera's own wording, and
    presses nothing."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        for name, button in [("raise_hand", "raise hand"), ("captions", "captions"), ("hide_self", "self view"), ("reactions", "reactions"),
                             ("layout", "layout"), ("camera", "camera")]:
            with pytest.raises(Exception) as failure:
                await run.page.evaluate(f"window.crystalMeet.control('{name}', 'gallery')")
            assert f"Zoom's {button} button is not on the screen yet; try again in a moment" in str(failure.value)
        assert await run.page.evaluate("window.__presses") == []


async def test_an_unknown_control_is_refused_in_words():
    async with PageRun() as run:
        await run.wait_for("connected")
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('dance')")
        assert "Unknown control dance" in str(failure.value)
        assert await run.page.evaluate("window.__presses") == []


async def test_a_control_before_the_meeting_is_joined_says_so_and_presses_nothing():
    """The footer can be on the screen before Zoom has said the room is in: the page is safe on its own, whatever the
    provider checks."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.silentConnect = true;", params={"connectGraceMs": 4000}) as run:
        await asyncio.sleep(0.4)
        assert await run.page.evaluate("window.crystalMeet.state") == "joining" and await run.page.evaluate("!!document.getElementById('stub-captions')") is True
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('captions')")
        assert "not in a meeting" in str(failure.value)
        assert await run.page.evaluate("window.__presses") == []


async def test_a_button_that_shows_its_change_late_is_waited_for():
    """Zoom's footer follows its state a moment after the press: the answer is the meeting once the button says so, not the
    meeting as it was read at the press."""
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("""() => {
            const captions = document.getElementById("stub-captions");
            captions.addEventListener("click", () => {          // after the stub's own listener: the client has acted, the button has not said so
                const says = captions.getAttribute("aria-label");
                captions.setAttribute("aria-label", "Show Captions");
                setTimeout(() => captions.setAttribute("aria-label", says), 400);
            });
        }""")
        m = await run.page.evaluate("window.crystalMeet.control('captions')")
        assert m["buttons"]["captions"] == {"on": True}


async def test_a_button_that_takes_no_press_is_a_failure_in_words():
    """An unchanged state is never a success, as it is not for the camera: the call ends within about two seconds saying what
    did not happen."""
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("() => { const captions = document.getElementById('stub-captions'); captions.replaceWith(captions.cloneNode(true)); }")   # no listener
        with pytest.raises(Exception) as failure:
            await asyncio.wait_for(run.page.evaluate("window.crystalMeet.control('captions')"), timeout=8)
        assert "Zoom did not switch captions" in str(failure.value)


async def test_a_hand_zoom_does_not_raise_or_lower_is_a_failure_in_words_and_the_record_is_the_judge():
    async with PageRun(stub=client(handIgnored=True)) as run:
        await settled(run)
        with pytest.raises(Exception) as failure:
            await asyncio.wait_for(run.page.evaluate("window.crystalMeet.control('raise_hand')"), timeout=8)
        assert "Zoom did not raise the hand" in str(failure.value)
        await tidy(run)                                                                      # the picker it closed was not left open either
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["me"]["raisedHand"] is False
        await run.page.evaluate("window.ZoomMtg._behaviour.handIgnored = false")
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["me"]["raisedHand"] is True
        await run.page.evaluate("window.ZoomMtg._behaviour.handIgnored = true")             # and now it will not come down
        with pytest.raises(Exception) as failure:
            await asyncio.wait_for(run.page.evaluate("window.crystalMeet.control('raise_hand')"), timeout=8)
        assert "Zoom did not lower the hand" in str(failure.value)
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["buttons"]["raise_hand"] == {"on": True}


async def test_a_self_view_item_that_does_nothing_is_a_failure_in_words_and_is_not_kept_as_done():
    async with PageRun(stub=client(selfViewIgnored=True)) as run:
        await settled(run)
        with pytest.raises(Exception) as failure:
            await asyncio.wait_for(run.page.evaluate("window.crystalMeet.control('hide_self')"), timeout=8)
        assert "Zoom did not hide self view" in str(failure.value)
        await tidy(run)                                                                      # the dropdown it left open was closed by its opener
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["buttons"]["hide_self"] == {"on": False}
        await run.page.evaluate("window.ZoomMtg._behaviour.selfViewIgnored = false")
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
        assert m["buttons"]["hide_self"] == {"on": True}
        await run.page.evaluate("window.ZoomMtg._behaviour.selfViewIgnored = true")
        with pytest.raises(Exception) as failure:
            await asyncio.wait_for(run.page.evaluate("window.crystalMeet.control('hide_self')"), timeout=8)
        assert "Zoom did not show self view" in str(failure.value)
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["buttons"]["hide_self"] == {"on": True}
        await tidy(run)


async def test_a_disabled_button_is_not_pressed_and_says_it_is_not_ready():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            for (const id of ["stub-reactions", "stub-captions"]) document.getElementById(id).setAttribute("aria-disabled", "true");
            document.getElementById("stub-view").disabled = true;
        }""")
        for call, button in [("control('reactions', 'clap')", "reactions"), ("control('raise_hand')", "raise hand"), ("control('captions')", "captions"),
                             ("control('layout', 'gallery')", "layout"), ("control('hide_self')", "self view")]:
            with pytest.raises(Exception) as failure:
                await run.page.evaluate("window.crystalMeet." + call)
            assert f"Zoom's {button} button is not ready yet; try again in a moment" in str(failure.value)
        assert await run.page.evaluate("window.__presses") == []


async def test_a_disabled_item_of_a_popover_is_not_pressed_either():
    """The host can switch these off; the client then draws the items disabled. Reactions and views have nothing else that
    says a press was ignored, so the press is not made."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const Z = window.ZoomMtg, picker = Z._drawPicker, view = Z._drawView;
            Z._drawPicker = function () { const node = picker.call(this); for (const b of node.querySelectorAll("button")) b.setAttribute("aria-disabled", "true"); return node; };
            Z._drawView = function () { const node = view.call(this); for (const a of node.querySelectorAll("a")) a.setAttribute("aria-disabled", "true"); return node; };
        }""")
        for call, button in [("control('reactions', 'clap')", "reactions"), ("control('raise_hand')", "raise hand"),
                             ("control('layout', 'gallery')", "layout"), ("control('hide_self')", "self view")]:
            with pytest.raises(Exception) as failure:
                await run.page.evaluate("window.crystalMeet." + call)
            assert f"Zoom's {button} button is not ready yet; try again in a moment" in str(failure.value)
        assert not {"stub-reaction", "stub-hand", "stub-view-item", "stub-self"} & set(await presses(run))
        await tidy(run)


async def test_the_camera_control_waits_for_zooms_button_as_a_camera_press_does():
    """The camera goes through setVideo: Zoom swallows a press while its video button is aria-disabled, so a control made
    right after connecting waits for the button, presses it once, and answers when the camera has switched."""
    async with PageRun(stub=client(videoOffOnEntry=True, videoStartsAfterMs=800)) as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('camera')")
        assert m["buttons"]["camera"] == {"on": True} and m["me"]["videoOn"] is True
        assert await run.page.evaluate("window.__videoClicks") == 1


async def test_the_answer_and_the_state_event_carry_the_same_meeting():
    """The call answers with the meeting, and the same change goes out as a "state" event, so the device does not wait for the
    next five-second read to hear of it."""
    async with PageRun() as run:
        await settled(run)
        m = await run.page.evaluate("window.crystalMeet.control('captions')")
        found = await states(run, 1, timeout=1.0)
        assert m["buttons"]["captions"] == {"on": True}
        assert found[0]["buttons"] == m["buttons"] and found[0]["me"] == m["me"] and found[0]["participants"] == m["participants"]


async def test_a_normal_press_returns_well_under_a_second():
    """What the press changed is looked at as soon as the press is made: a press that works returns at once, not after the
    wait that is only the bound for one that does not (a popover is looked at the same way). Timed in the page, in
    milliseconds: the median of several controls shows no wait at all, and none comes near a second."""
    async with PageRun() as run:
        await settled(run)
        took = await run.page.evaluate("""async () => {
            const times = [];
            for (const [name, value] of [["captions"], ["layout", "gallery"], ["reactions", "clap"], ["raise_hand"], ["hide_self"], ["captions"], ["raise_hand"]]) {
                const t = performance.now(); await window.crystalMeet.control(name, value); times.push(Math.round(performance.now() - t));
            }
            return times;
        }""")
        assert sorted(took)[len(took) // 2] < 50 and max(took) < 500, took


async def test_a_popover_that_opens_a_moment_after_the_press_is_waited_for():
    async with PageRun(stub=client(popoverOpensAfterMs=300)) as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        assert m["buttons"]["layout"] == {"value": "gallery"} and await run.page.evaluate("window.ZoomMtg._layout") == "gallery"
        await run.page.evaluate("window.crystalMeet.control('reactions', 'clap')")
        assert ["reaction", "clap"] in await run.page.evaluate("window.__zoomCalls")
        await tidy(run)


async def test_a_popover_that_is_already_open_is_used_as_it_is():
    """A person (or a call whose close ran out of time) left it open: its items are on the screen, so the page uses them and
    does not press the opener, which would close it and say it did not open."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("document.getElementById('stub-reactions').click()")          # open, by somebody else
        await run.page.evaluate("window.crystalMeet.control('reactions', 'clap')")
        assert ["reaction", "clap"] in await run.page.evaluate("window.__zoomCalls")
        assert await presses(run) == ["stub-reactions", "stub-reaction"]                       # the one press of the opener is the test's own
        await run.page.evaluate("document.getElementById('stub-view').click()")
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
        assert m["buttons"]["hide_self"] == {"on": True} and (await presses(run))[2:] == ["stub-view", "stub-self"]
        await tidy(run)
    async with PageRun(stub=client(popoverStays=True)) as run:                                 # and one Zoom leaves open after the pick is closed as ever
        await run.wait_for("connected")
        await run.page.evaluate("document.getElementById('stub-reactions').click()")
        await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert await presses(run) == ["stub-reactions", "stub-hand", "stub-reactions"]
        await tidy(run)


async def test_a_popover_that_never_opens_says_so_and_is_not_taken_for_a_missing_item():
    async with PageRun(stub=client(openerIgnored=True)) as run:
        await run.wait_for("connected")
        for call, button in [("control('reactions', 'clap')", "reactions"), ("control('layout', 'gallery')", "view")]:
            with pytest.raises(Exception) as failure:
                await run.page.evaluate("window.crystalMeet." + call)
            assert f"Zoom's {button} menu did not open; try again in a moment" in str(failure.value) and "offers no" not in str(failure.value)


async def test_a_popover_zoom_leaves_open_after_the_pick_is_closed_by_its_opener():
    """Zoom's popovers toggle on their opener: one it has left open is closed by pressing the opener again, as a person would,
    and the page takes nothing out of the client's page."""
    async with PageRun(stub=client(popoverStays=True)) as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        assert m["buttons"]["layout"] == {"value": "gallery"} and await run.page.evaluate("window.ZoomMtg._layout") == "gallery"
        assert await presses(run) == ["stub-view", "stub-view-item", "stub-view"]
        await run.page.evaluate("window.crystalMeet.control('reactions', 'heart')")
        assert (await presses(run))[3:] == ["stub-reactions", "stub-reaction", "stub-reactions"]
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")                # the item says "Show Self View" in place: taken
        assert m["buttons"]["hide_self"] == {"on": True} and (await presses(run))[6:] == ["stub-view", "stub-self", "stub-view"]
        await tidy(run)


async def test_an_opener_the_client_draws_again_is_not_an_item_and_is_the_one_pressed_to_close():
    """If the footer draws its button again when it is pressed, the new button is not something the press brought up (it would
    keep the popover from ever looking closed, and a press of it, a toggle, would open the popover Zoom had just closed), and
    the popover Zoom left open is closed with the new button, not with the one that is gone."""
    async with PageRun(stub=client(openersRedrawn=True)) as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        assert m["buttons"]["layout"] == {"value": "gallery"} and await presses(run) == ["stub-view", "stub-view-item"]
        await run.page.evaluate("window.crystalMeet.control('reactions', 'clap')")
        assert (await presses(run))[2:] == ["stub-reactions", "stub-reaction"]
        await tidy(run)
    async with PageRun(stub=client(openersRedrawn=True, popoverStays=True)) as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        await run.page.evaluate("window.crystalMeet.control('reactions', 'clap')")
        assert await presses(run) == ["stub-view", "stub-view-item", "stub-view", "stub-reactions", "stub-reaction", "stub-reactions"]
        await tidy(run)


async def test_a_popover_whose_opener_has_gone_is_left_and_the_call_still_answers():
    """If the client's button is no longer there to close the popover with, the page can do no more (it takes nothing of the
    client's out of the page): the pick it made stands and the call answers."""
    async with PageRun(stub=client(popoverStays=True)) as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const Z = window.ZoomMtg, picked = Z._picked;
            Z._picked = function (key) { picked.call(this, key); this._pop[key].opener.remove(); };       // the button is gone after the pick
        }""")
        m = await run.page.evaluate("window.crystalMeet.control('reactions', 'clap')")
        assert ["reaction", "clap"] in await run.page.evaluate("window.__zoomCalls") and "reactions" not in m["buttons"]
        assert await presses(run) == ["stub-reactions", "stub-reaction"] and await run.page.evaluate("window.ZoomMtg._open.picker") is True


async def test_a_popover_zoom_closes_a_moment_after_the_pick_is_left_to_close_itself():
    """The popover is Zoom's own: the page gives it its moment to close and does not press its opener to close what is
    already going (which, as a toggle, would open it again)."""
    async with PageRun(stub=client(popoverClosesAfterMs=300)) as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        assert m["buttons"]["layout"] == {"value": "gallery"} and await run.page.evaluate("window.__closedByZoom") is True
        assert await presses(run) == ["stub-view", "stub-view-item"]
        await tidy(run)


async def test_an_item_is_found_by_the_name_zoom_gives_it_whatever_the_case_and_by_the_specs_emoji():
    async with PageRun() as run:
        await run.wait_for("connected")
        # keys, names in any case, the spec's six emoji (the heart with its variation selector, as the spec writes it, and without)
        picks = [("thumbsup", "thumbs_up"), ("Thumbs Up", "thumbs_up"), ("THUMBS-UP", "thumbs_up"), ("👍", "thumbs_up"), ("👏", "clap"),
                 ("❤️", "heart"), ("❤", "heart"), ("😂", "joy"), ("😮", "open_mouth"), ("🎉", "tada"), ("open_mouth", "open_mouth"),
                 ("ClAp", "clap"), (" heart ", "heart")]
        for asked, _ in picks:
            await run.page.evaluate("([name, value]) => window.crystalMeet.control(name, value)", ["reactions", asked])
        assert [c[1] for c in await run.calls("reaction")] == [key for _, key in picks]
        assert (await presses(run)).count("stub-reactions") == len(picks)                    # the opener once a call
        for asked, shown in [("gallery", "gallery"), ("Speaker View", "speaker"), ("GALLERY", "gallery"), ("speaker", "speaker"), ("Gallery View", "gallery")]:
            m = await run.page.evaluate("([name, value]) => window.crystalMeet.control(name, value)", ["layout", asked])
            assert m["buttons"]["layout"] == {"value": shown} and await run.page.evaluate("window.ZoomMtg._layout") == shown
        await tidy(run)


async def test_whitespace_around_and_inside_an_items_name_does_not_matter():
    async with PageRun(stub=client(paddedLabels=True)) as run:
        await run.wait_for("connected")
        await run.page.evaluate("document.getElementById('stub-reactions').click()")
        said = await run.page.evaluate("document.querySelector('#reactionPicker button').getAttribute('aria-label')")
        await run.page.evaluate("document.getElementById('stub-reactions').click()")
        assert said != said.strip() and said.strip() == "Clap"                                # the premise: the client does pad its labels
        await run.page.evaluate("window.crystalMeet.control('reactions', 'thumbs up')")
        assert [c[1] for c in await run.calls("reaction")] == ["thumbs_up"]
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        assert m["buttons"]["layout"] == {"value": "gallery"}
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["me"]["raisedHand"] is True
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
        assert m["buttons"]["hide_self"] == {"on": True}
        await tidy(run)


async def test_a_reaction_changes_nothing_about_the_view_and_a_view_pick_sends_no_reaction():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        m = await run.page.evaluate("window.crystalMeet.control('reactions', 'tada')")
        assert m["buttons"]["layout"] == {"value": "gallery"} and m["buttons"]["hide_self"] == {"on": False} and m["buttons"]["reactions"] == {}
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'speaker')")
        assert m["buttons"]["layout"] == {"value": "speaker"}
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["buttons"]["layout"] == {"value": "speaker"} and m["me"]["raisedHand"] is True
        assert [c[1] for c in await run.calls("reaction")] == ["tada"]                       # only the reaction pick sent one: not the views, not the hand


async def test_a_menu_control_without_a_value_is_refused_before_anything_is_pressed():
    async with PageRun() as run:
        await run.wait_for("connected")
        for call, words in [("control('reactions')", "Say which reaction"), ("control('reactions', null)", "Say which reaction"),
                            ("control('reactions', '')", "Say which reaction"), ("control('reactions', '   ')", "Say which reaction"),
                            ("control('layout')", "Say which view"), ("control('layout', '')", "Say which view")]:
            with pytest.raises(Exception) as failure:
                await run.page.evaluate("window.crystalMeet." + call)
            assert words in str(failure.value)
        assert await run.page.evaluate("window.__presses") == []                              # no popover was opened to be shut again
        await tidy(run)


async def test_the_hand_goes_through_the_reactions_picker_and_comes_down_the_same_way():
    async with PageRun() as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["me"]["raisedHand"] is True and m["buttons"]["raise_hand"] == {"on": True}
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["me"]["raisedHand"] is False and m["buttons"]["raise_hand"] == {"on": False}
        assert await presses(run) == ["stub-reactions", "stub-hand", "stub-reactions", "stub-hand"]
        assert await run.calls("reaction") == []                                              # a hand is no reaction
        await tidy(run)


async def test_self_view_goes_through_the_view_dropdown_which_is_read_while_it_is_open():
    async with PageRun() as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
        assert m["buttons"]["hide_self"] == {"on": True} and await run.page.evaluate("window.ZoomMtg._selfHidden") is True
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
        assert m["buttons"]["hide_self"] == {"on": False} and await run.page.evaluate("window.ZoomMtg._selfHidden") is False
        assert await presses(run) == ["stub-view", "stub-self", "stub-view", "stub-self"]
        # What the open menu shows wins over what was last picked: the view and self view are changed behind the bridge's back.
        await run.page.evaluate("() => { window.ZoomMtg._layout = 'gallery'; window.ZoomMtg._selfHidden = true; }")
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
        assert m["buttons"]["layout"] == {"value": "gallery"}                                 # the checked item, read while the menu was open
        assert m["buttons"]["hide_self"] == {"on": False} and await run.page.evaluate("window.ZoomMtg._selfHidden") is False   # "Show Self View", pressed
        await run.page.evaluate("window.ZoomMtg._selfHidden = true")                         # and a view pick opens the same menu, which says so too
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'speaker')")
        assert m["buttons"]["hide_self"] == {"on": True} and m["buttons"]["layout"] == {"value": "speaker"}
        await tidy(run)


async def test_a_hand_button_in_the_footer_is_pressed_when_zoom_draws_one():
    """In an uncommon build the hand is a footer button of its own, in place of the Reactions slot."""
    async with PageRun(stub=client(handInFooter=True)) as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            window.__decoyClicks = [];
            const before = document.createElement("button"); before.setAttribute("aria-label", "Lower Hand");
            before.addEventListener("click", () => window.__decoyClicks.push("Lower Hand")); document.body.prepend(before);
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"]["raise_hand"] == {"on": False} and "reactions" not in m["buttons"]
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["buttons"]["raise_hand"] == {"on": True} and m["me"]["raisedHand"] is True
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["buttons"]["raise_hand"] == {"on": False} and m["me"]["raisedHand"] is False
        assert await presses(run) == ["stub-hand", "stub-hand"] and await run.page.evaluate("window.__decoyClicks") == []
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('reactions', 'clap')")
        assert "Zoom's reactions button is not on the screen yet" in str(failure.value)
        await run.page.evaluate("document.getElementById('stub-hand').setAttribute('aria-disabled', 'true')")
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert "Zoom's raise hand button is not ready yet; try again in a moment" in str(failure.value)
        assert await presses(run) == ["stub-hand", "stub-hand"]                                 # and a disabled one is not pressed


async def test_the_buttons_offered_follow_what_the_client_draws():
    async with PageRun() as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert set(m["buttons"]) == {"camera", "raise_hand", "reactions", "captions", "layout", "hide_self"}
        await run.page.evaluate("window.ZoomMtg._toggleHand()")                              # no button says whether the hand is up: the record does
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"]["raise_hand"] == {"on": True} and m["me"]["raisedHand"] is True
        # With the picker open for a person, one of its rows is no button of the footer's: without the Reactions button
        # there is no hand to offer, however the client's other panels word theirs.
        await run.page.evaluate("document.getElementById('stub-reactions').click()")
        await run.page.evaluate("""() => {
            document.getElementById('stub-reactions').remove();
            const stray = document.createElement('button'); stray.setAttribute('aria-label', 'Lower Hand'); document.body.prepend(stray);
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert "raise_hand" not in m["buttons"] and "reactions" not in m["buttons"] and "layout" in m["buttons"]
        await run.page.evaluate("document.getElementById('stub-view').remove()")             # and without the View button, neither view nor self view
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert "layout" not in m["buttons"] and "hide_self" not in m["buttons"] and "captions" in m["buttons"] and "camera" in m["buttons"]


async def test_a_popover_without_the_hand_or_the_self_view_item_says_so():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {            // the client draws its popovers without them
            const Z = window.ZoomMtg, picker = Z._drawPicker, view = Z._drawView;
            Z._drawPicker = function () { const node = picker.call(this); node.querySelector(".reaction-simple-picker__block--raise-hand").remove(); return node; };
            Z._drawView = function () { const node = view.call(this); this._viewItems.self.remove(); return node; };
        }""")
        for call, words in [("control('raise_hand')", "Zoom offers no raise hand here"), ("control('hide_self')", "Zoom offers no self view here")]:
            with pytest.raises(Exception) as failure:
                await run.page.evaluate("window.crystalMeet." + call)
            assert words in str(failure.value)
        await tidy(run)


async def test_a_double_tap_is_one_press():
    async with PageRun() as run:
        await settled(run)
        results = await run.page.evaluate("""Promise.allSettled([window.crystalMeet.control('captions'), window.crystalMeet.control('captions')])
                                             .then((r) => r.map((x) => x.status))""")
        assert results == ["fulfilled", "fulfilled"] and await presses(run) == ["stub-captions"]
        assert await run.page.evaluate("window.ZoomMtg._captions") is True
        assert await run.page.evaluate("(() => { const a = window.crystalMeet.control('raise_hand'), b = window.crystalMeet.control('raise_hand'); return a === b; })()") is True


async def test_different_controls_run_one_after_the_other_and_a_failure_blocks_none_of_them():
    async with PageRun() as run:
        await settled(run)
        results = await run.page.evaluate("""Promise.allSettled([
            window.crystalMeet.control('layout', 'gallery'), window.crystalMeet.control('reactions', 'clap'),
            window.crystalMeet.control('reactions', 'heart'), window.crystalMeet.control('raise_hand')]).then((r) => r.map((x) => x.status))""")
        assert results == ["fulfilled"] * 4
        assert await run.page.evaluate("window.ZoomMtg._layout") == "gallery" and [c[1] for c in await run.calls("reaction")] == ["clap", "heart"]
        assert await run.page.evaluate("window.ZoomMtg._hand") is True and await run.page.evaluate("window.__maxOpen") == 1     # never two popovers at once
        assert await presses(run) == ["stub-view", "stub-view-item", "stub-reactions", "stub-reaction", "stub-reactions", "stub-reaction",
                                      "stub-reactions", "stub-hand"]                          # in the order they were asked
        results = await run.page.evaluate("""Promise.allSettled([window.crystalMeet.control('layout', 'nonsense'), window.crystalMeet.control('captions')])
                                              .then((r) => r.map((x) => x.status))""")
        assert results == ["rejected", "fulfilled"] and await run.page.evaluate("window.ZoomMtg._captions") is True
        await tidy(run)


async def test_a_menu_the_client_owns_is_left_alone_and_nothing_is_found_by_searching_the_page():
    """The client has role="menu" nodes of its own (its Leave options), and the names the controls look for appear on the page
    outside the popover too: the page presses only what its own press brought up, and takes nothing out of the client's."""
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("""() => {
            window.__decoyClicks = [];
            const decoy = (where, tag, text) => {
                const e = document.createElement(tag); if (tag === "a") e.setAttribute("role", "button"); e.setAttribute("aria-label", text); e.textContent = text;
                e.addEventListener("click", () => window.__decoyClicks.push(text)); where.append(e);
            };
            for (const text of ["Clap", "Gallery View", "Hide Self View", "Raise Hand", "Show Captions"]) decoy(document.querySelector("[role=menu]"), "button", text);
            for (const text of ["Clap", "Gallery View", "Hide Self View", "Raise Hand"]) { decoy(document.body, "a", text); document.body.prepend(document.body.lastChild); }
        }""")
        await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        await run.page.evaluate("window.crystalMeet.control('reactions', 'clap')")
        await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        await run.page.evaluate("window.crystalMeet.control('hide_self')")
        await run.page.evaluate("window.crystalMeet.control('captions')")
        with pytest.raises(Exception, match="Zoom offers no nothing here"):
            await run.page.evaluate("window.crystalMeet.control('reactions', 'nothing')")
        assert await run.page.evaluate("window.__decoyClicks") == []
        assert await run.page.evaluate("document.querySelectorAll('[role=menu]').length") == 1
        await tidy(run)
        await run.page.evaluate("window.ZoomMtg._unmountBystander()")                          # the owner's unmount: it throws if anyone else took the box out
        assert await run.page.evaluate("window.__unmounted") == "ok" and await run.page.evaluate("window.__reactErrors") == []


async def test_a_read_that_fails_after_the_press_does_not_fail_a_control_that_worked():
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("""() => {
            const real = window.ZoomMtg.getCurrentUser;
            window.ZoomMtg.getCurrentUser = function (o) { if (window.__failNext) { window.__failNext = false; throw new Error("Minified Redux error #3"); } return real.call(this, o); };
            window.__failNext = true;
        }""")
        m = await run.page.evaluate("window.crystalMeet.control('captions')")
        assert m["buttons"]["captions"] == {"on": True} and await run.page.evaluate("window.__failNext") is False      # the read did fail, and the press stands


async def test_a_read_that_fails_while_the_hand_is_waited_for_is_tried_again():
    """What the hand's wait asks Zoom may fail for a moment: that is not an answer, and not an end to the wait."""
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("""() => {
            const Z = window.ZoomMtg, real = Z.getCurrentUser, toggle = Z._toggleHand;
            Z.getCurrentUser = function (o) { if (Date.now() < (window.__failUntil || 0)) throw new Error("Minified Redux error #3"); return real.call(this, o); };
            Z._toggleHand = function () { window.__failUntil = Date.now() + 400; return toggle.call(this); };
        }""")
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["me"]["raisedHand"] is True and m["buttons"]["raise_hand"] == {"on": True}


async def test_only_a_reaction_is_picked_as_a_reaction_and_only_a_view_as_a_view():
    async with PageRun() as run:
        await run.wait_for("connected")
        for call, words in [("control('reactions', 'Raise Hand')", "Zoom offers no Raise Hand here"),
                            ("control('reactions', 'be right back')", "Zoom offers no be right back here"),
                            ("control('reactions', 'more')", "Zoom offers no more here"),
                            ("control('layout', 'Hide Self View')", "Zoom offers no Hide Self View here")]:
            with pytest.raises(Exception) as failure:
                await run.page.evaluate("window.crystalMeet." + call)
            assert words in str(failure.value)
        assert await run.calls("reaction") == [] and await run.page.evaluate("window.ZoomMtg._hand") is False
        assert await run.page.evaluate("window.ZoomMtg._selfHidden") is False
        await run.page.evaluate("window.ZoomMtg._hand = true")                               # the hand row now says "Lower Hand"
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('reactions', 'Lower Hand')")
        assert "Zoom offers no Lower Hand here" in str(failure.value)
        assert not {"stub-hand", "stub-brb", "stub-more", "stub-self"} & set(await presses(run))      # none of Zoom's other items was pressed
        await tidy(run)


async def test_an_item_is_found_by_whichever_of_its_label_tooltip_or_text_names_it():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {            // the client's reactions named one way at a time
            const Z = window.ZoomMtg, real = Z._drawPicker;
            Z._drawPicker = function () {
                const node = real.call(this);
                for (const b of node.querySelectorAll(".reaction-simple-picker__block--reaction[aria-label]")) {
                    const name = b.getAttribute("aria-label");
                    if (window.__only === "aria-label") b.removeAttribute("data-tooltip");
                    if (window.__only === "data-tooltip") b.removeAttribute("aria-label");
                    if (window.__only === "text") { b.removeAttribute("aria-label"); b.removeAttribute("data-tooltip"); b.append(name); }
                }
                return node;
            };
        }""")
        for only in ["aria-label", "data-tooltip", "text"]:
            await run.page.evaluate("(only) => { window.__only = only; }", only)
            await run.page.evaluate("window.crystalMeet.control('reactions', 'joy')")
        assert [c[1] for c in await run.calls("reaction")] == ["joy", "joy", "joy"]
        await tidy(run)


async def test_the_stub_notices_a_node_of_the_clients_taken_out_by_anyone_but_its_owner():
    """What the tests above rely on: the owner's removal throws, as React's unmount does, when something else took the node out."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("document.getElementById('stub-view').click()")
        await run.page.evaluate("document.querySelector('.full-screen-viev-dropdown .dropdown-menu').remove()")
        await run.page.evaluate("document.getElementById('stub-view').click()")               # its owner takes it out now
        await run.page.evaluate("document.querySelector('[role=menu]').remove()")
        await run.page.evaluate("window.ZoomMtg._unmountBystander()")
        errors = await run.page.evaluate("window.__reactErrors")
        assert len(errors) == 2 and all("not a child" in e for e in errors) and await run.page.evaluate("window.__unmounted") != "ok"


async def test_a_self_view_pick_that_leaves_with_its_menu_is_taken_though_the_item_never_says_the_opposite():
    """React unmounts the dropdown with the item that was picked in it, and does not draw the item again with the opposite
    words first: Zoom taking the press shows only as the item being gone, whichever way the press goes."""
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("document.addEventListener('click', (e) => { window.__picked = e.target.closest('a'); }, true)")
        for hides, said in [(True, "Hide Self View"), (False, "Show Self View")]:
            m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
            assert m["buttons"]["hide_self"] == {"on": hides} and await run.page.evaluate("window.ZoomMtg._selfHidden") is hides
            assert await run.page.evaluate("[window.__picked.isConnected, window.__picked.getAttribute('aria-label')]") == [False, said]
        await tidy(run)


async def test_another_dropdown_that_is_open_elsewhere_on_the_page_is_not_the_view_menu():
    """The client has other dropdowns of its own (div.dropdown-menu.show): the View menu is the one in the View widget's portal.
    One that is open already, with items named like the View's, is neither used for a pick, nor pressed, nor closed, and the
    View button still opens the real menu."""
    async with PageRun(stub=client(strayDropdown=True)) as run:
        await settled(run)
        assert await run.page.evaluate("document.querySelectorAll('.dropdown-menu.show').length") == 1      # the premise: open, and not the View menu
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        assert m["buttons"]["layout"] == {"value": "gallery"} and await run.page.evaluate("window.ZoomMtg._layout") == "gallery"
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
        assert m["buttons"]["hide_self"] == {"on": True} and await run.page.evaluate("window.ZoomMtg._selfHidden") is True
        assert await presses(run) == ["stub-view", "stub-view-item", "stub-view", "stub-self"]              # the View's own items, none of the stray one's
        assert await run.page.evaluate("document.querySelectorAll('.audio-options-dropdown .dropdown-menu.show a').length") == 3   # and it is open still
        await tidy(run)


async def test_a_hosts_view_menu_has_an_attendee_view_section_that_is_not_the_hosts_own_view():
    """A host's View menu carries a section more, "Attendee View": its items say "Speaker View" (labelled "<Attendee View>.Speaker
    View") and "Follow Host's View". A view is read from, and picked in, the items that begin with the view's name: the
    attendees' Speaker View is neither the host's view when it is checked nor the item a pick finds first. Where the section
    comes in the menu is not known, so it is tried both ways."""
    for first in (False, True):
        async with PageRun(stub=client(role="host", attendeeViewFirst=first)) as run:
            await settled(run)
            items = (await run.page.evaluate("window.crystalMeet.diagnostics()"))["popovers"]["view"]["items"]      # the premise: what the menu holds
            labels = [i["label"] for i in items]
            assert sorted(labels) == ["<Attendee View>.Speaker View", "Follow Host's View", "Gallery View", "Hide Self View", "Speaker View"]
            assert (labels.index("<Attendee View>.Speaker View") < labels.index("Gallery View")) is first
            assert [i["text"] for i in items if i["label"].startswith("<")] == ["Speaker View"]
            assert sorted(i["label"] for i in items if "full-screen-widget__pop-menu--checked" in i["class"]) == ["<Attendee View>.Speaker View", "Speaker View"]
            await run.page.evaluate("window.__presses.length = 0")
            await run.page.evaluate("window.ZoomMtg._layout = 'gallery'")           # the host's own view; the attendees' is Speaker, checked in its section
            m = await run.page.evaluate("window.crystalMeet.control('hide_self')")  # the menu is read while it is open
            assert m["buttons"]["layout"] == {"value": "gallery"}
            await run.page.evaluate("window.ZoomMtg._attendeeView = 'follow'")
            m = await run.page.evaluate("window.crystalMeet.control('layout', 'speaker')")
            assert m["buttons"]["layout"] == {"value": "speaker"} and await run.page.evaluate("window.ZoomMtg._layout") == "speaker"
            m = await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
            assert m["buttons"]["layout"] == {"value": "gallery"} and await run.page.evaluate("window.ZoomMtg._layout") == "gallery"
            assert await run.page.evaluate("window.ZoomMtg._attendeeView") == "follow"                                # the attendees' view was not touched
            assert await presses(run) == ["stub-view", "stub-self", "stub-view", "stub-view-item", "stub-view", "stub-view-item"]
            await tidy(run)


async def test_the_view_menu_can_be_read_without_picking_from_it():
    """The view and self view are only to be read from the View menu: until it has been opened the reading says the defaults (speaker,
    shown). A look opens it, reads it as a pick does, and closes it by its opener, and presses nothing in it: what the table starts from
    is what the client has."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        await run.page.evaluate("() => { window.ZoomMtg._layout = 'gallery'; window.ZoomMtg._selfHidden = true; }")      # as an earlier meeting left them
        before = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert before["buttons"]["layout"] == {"value": "speaker"} and before["buttons"]["hide_self"] == {"on": False}
        m, ms = await run.page.evaluate("async () => { const t = performance.now(); const m = await window.crystalMeet.control('layout.read'); return [m, performance.now() - t]; }")
        assert m["buttons"]["layout"] == {"value": "gallery"} and m["buttons"]["hide_self"] == {"on": True}
        assert ms < 500, ms                                                            # nothing was picked, so Zoom has no pick to close the menu after: its opener does
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["buttons"]["layout"] == {"value": "gallery"}      # kept, not only answered
        assert await presses(run) == ["stub-view", "stub-view"]                       # the opener, and the opener again to close it: no item
        assert await run.page.evaluate("[window.ZoomMtg._layout, window.ZoomMtg._selfHidden, window.ZoomMtg._attendeeView]") == ["gallery", True, "speaker"]
        await tidy(run)


async def test_looking_at_the_view_menu_without_the_view_button_says_so_and_presses_nothing():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('layout.read')")
        assert "Zoom's layout button is not on the screen yet; try again in a moment" in str(failure.value)
        assert await run.page.evaluate("window.__presses") == []


# ---- Diagnostics: the device inventory looks into the popovers too ----


async def test_diagnostics_list_the_view_widgets_button_beside_the_footers():
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert data["buttons"] == [{"label": "stop my video", "text": "Video"}, {"label": "Reactions"}, {"label": "Show Captions"},
                                   {"label": "View", "where": "view widget"}]         # not the Leave box's button, which is in neither place


async def test_diagnostics_list_what_the_two_popovers_hold_and_close_them_again():
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        data, took = await run.page.evaluate("""async () => {
            const t = performance.now(), data = await window.crystalMeet.diagnostics();
            return [data, performance.now() - t];
        }""")
        reactions, view = data["popovers"]["reactions"], data["popovers"]["view"]
        assert reactions["found"] is True and reactions["pressed"] is True and reactions["opener"] == {"label": "Reactions"} and "note" not in reactions
        assert [i["label"] or i["tooltip"] or i["text"] for i in reactions["items"]] == [
            "Clap", "Thumbs Up", "Joy", "Open Mouth", "Heart", "Tada", "More", "Raise Hand", "Be Right Back"]
        clap, hand = reactions["items"][0], reactions["items"][7]
        assert (clap["text"], clap["label"], clap["tooltip"], clap["disabled"]) == ("", "Clap", "Clap", False)       # an icon, named in attributes
        assert "reaction-simple-picker__block--reaction" in clap["class"]
        assert (hand["text"], hand["label"], hand["tooltip"]) == ("Raise Hand", "", "")                                # a row of text
        assert view["found"] is True and view["pressed"] is True and view["opener"] == {"label": "View"} and "note" not in view
        assert [i["label"] for i in view["items"]] == ["Speaker View", "Gallery View", "Hide Self View"]
        assert [i["text"] for i in view["items"]] == ["Speaker View", "Gallery View", "Hide Self View"]
        assert ["full-screen-widget__pop-menu--checked" in i["class"] for i in view["items"]] == [True, False, False]
        assert not any(i["disabled"] for i in reactions["items"] + view["items"])
        assert await presses(run) == ["stub-reactions", "stub-reactions", "stub-view", "stub-view"]               # each opened and closed by its opener
        await tidy(run)
        assert took < 500, took                                                       # nothing waited for a close that no pick would bring


async def test_diagnostics_find_the_view_widgets_button_by_where_it_is_and_by_what_it_says():
    """The inventory is for finding out what the client really says, so a button the bridge's own words would miss is listed
    for where Zoom draws it, and one that is not where it is drawn is listed for what it says."""
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        await run.page.evaluate("document.getElementById('stub-view').setAttribute('aria-label', 'Layout options')")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert data["buttons"][-1] == {"label": "Layout options", "where": "view widget"}
        await run.page.evaluate("""() => {
            const view = document.getElementById('stub-view');
            view.setAttribute('aria-label', 'View'); document.body.append(view);
            document.querySelector('.full-screen-icon').remove();
        }""")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert data["buttons"][-1] == {"label": "View", "where": "view widget"} and len(data["buttons"]) == 4


async def test_diagnostics_use_a_popover_that_is_already_open_and_do_not_list_its_items_as_buttons():
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        await run.page.evaluate("document.getElementById('stub-reactions').click()")          # open, by somebody else
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert [b["label"] for b in data["buttons"]] == ["stop my video", "Reactions", "Show Captions", "View"]
        assert data["popovers"]["reactions"]["pressed"] is False and len(data["popovers"]["reactions"]["items"]) == 9
        assert await presses(run) == ["stub-reactions", "stub-reactions", "stub-view", "stub-view"]     # the test's own opening, then the close; the view's own pair
        await tidy(run)
        # One the inventory cannot close (its opener is gone) is still open while the footer is listed: none of its items is a button of the footer's.
        await run.page.evaluate("document.getElementById('stub-reactions').click()")
        await run.page.evaluate("document.getElementById('stub-reactions').remove()")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert [b["label"] for b in data["buttons"]] == ["stop my video", "Show Captions", "View"]
        assert data["popovers"]["reactions"]["found"] is False and data["popovers"]["reactions"]["pressed"] is False


async def test_diagnostics_say_which_items_are_disabled():
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const Z = window.ZoomMtg, picker = Z._drawPicker;
            Z._drawPicker = function () {
                const node = picker.call(this), blocks = node.querySelectorAll("button");
                blocks[0].setAttribute("aria-disabled", "true"); blocks[2].disabled = true;
                return node;
            };
        }""")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert [i["disabled"] for i in data["popovers"]["reactions"]["items"]] == [True, False, True, False, False, False, False, False, False]


async def test_diagnostics_report_an_opener_that_is_missing_or_disabled_and_press_nothing():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            document.getElementById("stub-view").remove();
            document.getElementById("stub-reactions").setAttribute("aria-disabled", "true");
        }""")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert data["popovers"]["view"] == {"opener": None, "found": False, "pressed": False, "items": [], "note": "the button is not on the screen"}
        assert data["popovers"]["reactions"] == {"opener": {"label": "Reactions"}, "found": True, "pressed": False, "items": [],
                                                 "note": "the button is disabled"}
        assert await run.page.evaluate("window.__presses") == []


async def test_diagnostics_press_nothing_before_the_meeting_is_joined():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.silentConnect = true;", params={"connectGraceMs": 4000}) as run:
        await asyncio.sleep(0.4)
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        for name in ["reactions", "view"]:
            assert data["popovers"][name]["found"] is True and data["popovers"][name]["pressed"] is False
            assert data["popovers"][name]["items"] == [] and data["popovers"][name]["note"] == "not in a meeting"
        assert await run.page.evaluate("window.__presses") == []


async def test_diagnostics_say_when_a_popover_did_not_open():
    async with PageRun(stub=client(openerIgnored=True)) as run:
        await run.wait_for("connected")
        data = await asyncio.wait_for(run.page.evaluate("window.crystalMeet.diagnostics()"), timeout=12)
        assert data["popovers"]["reactions"]["pressed"] is True and data["popovers"]["reactions"]["items"] == []
        assert data["popovers"]["reactions"]["note"] == "Zoom's reactions menu did not open; try again in a moment"
        assert data["popovers"]["view"]["note"] == "Zoom's view menu did not open; try again in a moment"


async def test_diagnostics_run_among_the_controls_and_never_between_their_presses():
    async with PageRun(stub=client(popoverOpensAfterMs=300)) as run:
        await settled(run)
        results = await run.page.evaluate("""Promise.allSettled([window.crystalMeet.control('layout', 'gallery'), window.crystalMeet.diagnostics(),
                                              window.crystalMeet.control('reactions', 'clap')]).then((r) => r.map((x) => x.status))""")
        assert results == ["fulfilled"] * 3 and await run.page.evaluate("window.__maxOpen") == 1
        assert await presses(run) == ["stub-view", "stub-view-item", "stub-reactions", "stub-reactions", "stub-view", "stub-view",
                                      "stub-reactions", "stub-reaction"]                  # in the order they were asked
        await tidy(run)


# ---- Host calls, rename and the SDK's init flags (spec 2026-10-13, sections 4.2 and 4.4) ----


async def test_host_calls_go_to_the_sdk_and_a_participant_gets_zooms_refusal():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';") as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('host.mute_all')")
        assert all(p["muted"] for p in m["participants"] if not p["me"])
        m = await run.page.evaluate("window.crystalMeet.control('host.record')")
        assert m["recording"] is True
        m = await run.page.evaluate("window.crystalMeet.control('host.remove', 16777218)")
        assert [p["name"] for p in m["participants"]] == ["Room 1", "Ann Lee"]
        await run.page.evaluate("window.crystalMeet.control('host.hold', 16777217)")
        await later(run)                                                          # Zoom drops a putOnHold made again within a tenth of a second
        m = await run.page.evaluate("window.crystalMeet.control('host.admit', 16777217)")
        assert m["participants"][1]["waiting"] is False
        calls = [c[0] for c in await run.page.evaluate("window.__zoomCalls")]
        assert calls[-5:] == ["muteAll", "record", "expel", "putOnHold", "putOnHold"]
    async with PageRun() as run:                                                  # a participant
        await run.wait_for("connected")
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('host.lock')")
        assert "Current user is neither host nor cohost" in str(failure.value)


async def test_end_for_everyone_ends_the_meeting():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';") as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.crystalMeet.control('host.end')")
        await run.wait_for("left")


async def test_init_asks_for_chat_captions_and_recording():
    async with PageRun() as run:
        await run.wait_for("connected")
        [init] = await run.calls("init")
        assert init[1]["isSupportChat"] is True and init[1]["isSupportCC"] is True and init[1]["disableRecord"] is False


async def test_init_keeps_the_invite_function_hidden():
    async with PageRun() as run:
        await run.wait_for("connected")
        [init] = await run.calls("init")
        assert init[1]["disableInvite"] is True


HOST_CALLS = ("muteAll", "record", "lockMeeting", "putOnHold", "expel", "mute", "endMeeting", "rename")


def asked(calls):
    """What the page has asked of Zoom's host calls and of rename, in the order it asked: [name, arguments]."""
    return [c for c in calls if c[0] in HOST_CALLS]


async def control(run, name, value=None):
    return await run.page.evaluate("([name, value]) => window.crystalMeet.control(name, value)", [name, value])


async def later(run, ms=6000):
    """Time goes by for Zoom's windows (a call made again inside the one its method keeps is dropped), without the test waiting."""
    await run.page.evaluate("(ms) => window.ZoomMtg._later(ms)", ms)


async def test_each_host_call_sends_zoom_the_arguments_it_takes():
    """The first test sees which calls were made; each takes its own arguments, and a participant's id reaches Zoom as the number
    Zoom lists it by. Recording and locking are asked for as the opposite of what the meeting has."""
    async with PageRun(stub=client(role="host")) as run:
        await run.wait_for("connected")
        for name, value in [("host.mute_all", None), ("host.ask_unmute", None), ("host.record", None), ("host.record", None), ("host.lock", None),
                            ("host.lock", None), ("host.admit", 16777217), ("host.hold", 16777217), ("host.remove", 16777218),
                            ("host.mute_one", 16777217), ("host.end", None)]:
            await control(run, name, value)
            await later(run)                                                                  # Zoom drops a call made again within seconds
        assert asked(await run.page.evaluate("window.__zoomCalls")) == [
            ["muteAll", {"muteAll": True}], ["muteAll", {"muteAll": False}],
            ["record", {"record": True}], ["record", {"record": False}],
            ["lockMeeting", {"lockMeeting": True}], ["lockMeeting", {"lockMeeting": False}],
            ["putOnHold", {"userId": 16777217, "hold": False}], ["putOnHold", {"userId": 16777217, "hold": True}],
            ["expel", {"userId": 16777218}], ["mute", {"userId": 16777217, "mute": True}], ["endMeeting", {}]]


async def test_the_meeting_says_whether_it_is_locked_and_recording_follows_what_zoom_says():
    """Zoom's calls do not say whether the meeting is locked, so it is kept as the lock call last went, and the device hears of
    it as a "state" event; whether it is recording is what Zoom says (its event), also when somebody else starts it."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["locked"] is False and m["recording"] is False
        m = await control(run, "host.lock")
        assert m["locked"] is True and await run.page.evaluate("window.ZoomMtg._locked") is True
        assert (await states(run, 1, timeout=1.0))[-1]["locked"] is True
        await later(run)
        m = await control(run, "host.lock")
        assert m["locked"] is False and await run.page.evaluate("window.ZoomMtg._locked") is False
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["locked"] is False
        m = await control(run, "host.record")
        assert m["recording"] is True and m["locked"] is False
        await run.page.evaluate("window.ZoomMtg._setRecording(false)")             # stopped by somebody else: the next press starts it
        await later(run)
        m = await control(run, "host.record")
        assert m["recording"] is True
        await run.page.evaluate("window.ZoomMtg._setRecording(false)")
        await run.page.evaluate("window.ZoomMtg._setRecording(true)")               # started by somebody else: the next press stops it
        await later(run)
        m = await control(run, "host.record")
        assert m["recording"] is False
        assert [c[1] for c in await run.calls("record")] == [{"record": True}, {"record": True}, {"record": False}]


async def test_a_refused_host_call_changes_nothing_and_says_zooms_words():
    async with PageRun() as run:                                                   # a participant: Zoom refuses every one
        await settled(run)
        for name, value in [("host.lock", None), ("host.record", None), ("host.mute_all", None), ("host.ask_unmute", None), ("host.admit", 16777217),
                            ("host.hold", 16777217), ("host.remove", 16777218), ("host.mute_one", 16777217), ("host.end", None)]:
            with pytest.raises(Exception) as failure:
                await control(run, name, value)
            # the SDK's words and code 1; endMeeting is the host's alone, and refuses with a code and no words, which the call names
            assert ("Zoom refused endMeeting (code 3003)" if name == "host.end" else "Current user is neither host nor cohost (code 1)") in str(failure.value)
            await later(run)
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["locked"] is False and m["recording"] is False and m["count"] == 3                        # not kept as done
        assert [p["muted"] for p in m["participants"]] == [False, False, True] and not any(p["waiting"] for p in m["participants"])
        assert await run.page.evaluate("window.crystalMeet.state") == "connected"
        # a refusal does not hold the controls behind it
        results = await run.page.evaluate("""Promise.allSettled([window.crystalMeet.control('host.lock'), window.crystalMeet.control('captions')])
                                              .then((r) => r.map((x) => x.status))""")
        assert results == ["rejected", "fulfilled"] and await run.page.evaluate("window.ZoomMtg._captions") is True


async def test_rename_sends_zoom_the_rooms_id_and_the_name_zoom_has_for_it():
    async with PageRun() as run:                                                   # a participant may rename themselves
        await run.wait_for("connected")
        m = await control(run, "rename", "  Boardroom 2 ")
        assert [p["name"] for p in m["participants"] if p["me"]] == ["Boardroom 2"]                         # as Zoom says it now
        await later(run)
        m = await control(run, "rename", "Boardroom")
        assert [p["name"] for p in m["participants"] if p["me"]] == ["Boardroom"]
        assert [c[1] for c in await run.calls("rename")] == [
            {"userId": 16777216, "oldName": "Room 1", "newName": "Boardroom 2"},
            {"userId": 16777216, "oldName": "Boardroom 2", "newName": "Boardroom"}]


async def test_the_room_cannot_be_renamed_before_zoom_lists_it():
    async with PageRun(stub=NO_USER_YET) as run:
        await run.wait_for("connected")
        with pytest.raises(Exception) as failure:
            await control(run, "rename", "Boardroom")
        assert "Zoom has not listed the room in the meeting yet; try again in a moment" in str(failure.value)
        assert await run.calls("rename") == []
        await run.page.evaluate("window.__listed = true")
        await run.page.evaluate("window.crystalMeet.readState()")                  # the read that finds the room's id and name
        await control(run, "rename", "Boardroom")
        assert [c[1] for c in await run.calls("rename")] == [{"userId": 16777216, "oldName": "Room 1", "newName": "Boardroom"}]


async def test_a_host_call_without_a_participant_and_a_rename_without_a_name_are_refused_before_anything_is_sent():
    async with PageRun(stub=client(role="host")) as run:
        await run.wait_for("connected")
        for name in ["host.admit", "host.hold", "host.remove", "host.mute_one"]:
            for value in [None, "", "  ", "everyone", 0, -1, 1.5, "0x10", "1e3", " 5 ", "-5", 2 ** 53]:
                with pytest.raises(Exception, match="Say which participant"):
                    await control(run, name, value)
        for value in [None, "", "   "]:
            with pytest.raises(Exception, match="Say which name to use"):
                await control(run, "rename", value)
        assert asked(await run.page.evaluate("window.__zoomCalls")) == []
        said = await run.page.evaluate("window.crystalMeet.control('host.remove', 'everyone').then(() => 'done', (e) => e.message)")
        assert said == "Say which participant"                                                              # a rejected promise, as every control gives, not a throw
        await control(run, "host.remove", "16777218")                                                       # an id that came as text is the number
        assert asked(await run.page.evaluate("window.__zoomCalls")) == [["expel", {"userId": 16777218}]]


async def test_the_names_every_object_has_are_not_controls():
    async with PageRun(stub=client(role="host")) as run:
        await run.wait_for("connected")
        for name in ["constructor", "toString", "hasOwnProperty", "valueOf", "__proto__"]:
            with pytest.raises(Exception, match=f"Unknown control {name}"):
                await run.page.evaluate("(name) => window.crystalMeet.control(name)", name)
        assert asked(await run.page.evaluate("window.__zoomCalls")) == [] and await run.page.evaluate("window.__presses") == []


async def test_host_calls_and_rename_say_they_are_not_in_a_meeting_before_the_meeting_is_joined():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host'; window.ZoomMtg._behaviour.silentConnect = true;",
                       params={"connectGraceMs": 4000}) as run:
        await asyncio.sleep(0.4)
        assert await run.page.evaluate("window.crystalMeet.state") == "joining"
        for name, value in [("host.mute_all", None), ("host.remove", 16777218), ("host.end", None), ("rename", "Boardroom")]:
            with pytest.raises(Exception) as failure:
                await control(run, name, value)
            assert "not in a meeting" in str(failure.value)
        assert asked(await run.page.evaluate("window.__zoomCalls")) == []


async def test_host_calls_wait_their_turn_among_the_controls_and_a_double_tap_is_one_call():
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        await run.page.evaluate("""() => {            // Zoom answers these a moment after they are asked
            const Z = window.ZoomMtg;
            for (const call of ["muteAll", "lockMeeting"]) {
                const real = Z[call];
                Z[call] = function (o) { window.__presses.push([call + " asked", ""]); setTimeout(() => { window.__presses.push([call + " answered", ""]); real.call(this, o); }, 300); };
            }
        }""")
        results = await run.page.evaluate("""Promise.allSettled([window.crystalMeet.control('host.mute_all'), window.crystalMeet.control('captions'),
                                              window.crystalMeet.control('host.lock')]).then((r) => r.map((x) => x.status))""")
        assert results == ["fulfilled"] * 3
        assert await presses(run) == ["muteAll asked", "muteAll answered", "stub-captions", "lockMeeting asked", "lockMeeting answered"]
        assert await run.page.evaluate("""async () => {
            const a = window.crystalMeet.control('host.record'), b = window.crystalMeet.control('host.record');
            await Promise.all([a, b]);
            return a === b;
        }""") is True
        assert [c[1] for c in await run.calls("record")] == [{"record": True}]                              # one call, not a start and a stop


BOUNDS = """() => {
    const set = window.setTimeout, clear = window.clearTimeout;
    window.__bounds = new Map();           // the page's long timers, which are the bound on a call to Zoom: kept for the test to look at, and to fire
    let count = 0;
    window.setTimeout = function (fn, ms, ...rest) {
        if (ms < 3000) return set.call(window, fn, ms, ...rest);
        count += 1; window.__bounds.set("bound-" + count, { fn, ms }); return "bound-" + count;
    };
    window.clearTimeout = function (id) { if (!window.__bounds.delete(id)) clear.call(window, id); };
}"""


async def test_a_call_zoom_never_answers_ends_in_words_and_does_not_hold_the_controls_behind_it():
    """Controls run one at a time, so a call Zoom never answered would hold every later control, and the inventory, until the page
    was reloaded. It ends after twelve seconds in words (the SDK's own ten-second wait for its store, which says why, comes first
    where there is one), and what waited behind it goes on. The twelve seconds are not waited for: the page's timer for them is
    taken, its delay is looked at, and the test lets it go off."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        await run.page.evaluate(BOUNDS)
        await run.page.evaluate("() => { window.ZoomMtg.muteAll = (o) => { window.__zoomCalls.push(['muteAll', { muteAll: o.muteAll }]); }; }")   # Zoom never calls back

        async def asked_of_zoom(count):
            for _ in range(100):
                if len(await run.calls("muteAll")) >= count:
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(f"Zoom was not asked {count} times")

        async def twelve_seconds_go_by():
            assert await run.page.evaluate("[...window.__bounds.values()].map((b) => b.ms)") == [12000]       # one call waiting, for twelve seconds
            await run.page.evaluate("[...window.__bounds].forEach(([id, b]) => { window.__bounds.delete(id); b.fn(); })")      # it goes off, once

        stuck = asyncio.ensure_future(run.page.evaluate("window.crystalMeet.control('host.mute_all')"))
        behind = asyncio.ensure_future(run.page.evaluate("window.crystalMeet.control('captions')"))
        inventory = asyncio.ensure_future(run.page.evaluate("window.crystalMeet.diagnostics()"))
        await asked_of_zoom(1)
        await asyncio.sleep(0.3)
        assert not stuck.done() and not behind.done() and not inventory.done()                              # they wait for it
        await twelve_seconds_go_by()
        with pytest.raises(Exception) as failure:
            await asyncio.wait_for(stuck, timeout=5)
        assert "Zoom did not answer muteAll in time" in str(failure.value)
        m = await asyncio.wait_for(behind, timeout=5)
        assert m["buttons"]["captions"] == {"on": True}
        assert (await asyncio.wait_for(inventory, timeout=5))["popovers"]["reactions"]["found"] is True
        again = asyncio.ensure_future(run.page.evaluate("window.crystalMeet.control('host.mute_all')"))    # no longer in flight: a new call, which ends the same way
        await asked_of_zoom(2)
        await twelve_seconds_go_by()
        with pytest.raises(Exception, match="Zoom did not answer muteAll in time"):
            await asyncio.wait_for(again, timeout=5)


async def test_a_call_that_ends_leaves_no_timer_behind_whichever_way_it_ends():
    """The bound on a call is a timer set with it: it is cleared when Zoom answers, when it refuses and when it throws, and Zoom's
    own words, whatever they are, are the call's error."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        await run.page.evaluate(BOUNDS)
        await run.page.evaluate("""() => {
            window.ZoomMtg.lockMeeting = (o) => o.error({ errorCode: 3023, errorMessage: "Locking is off for this account" });
            window.ZoomMtg.expel = () => { throw new Error("Minified Redux error #3"); };
        }""")
        await control(run, "host.mute_all")
        assert await run.page.evaluate("window.__bounds.size") == 0
        with pytest.raises(Exception, match=r"Locking is off for this account \(code 3023\)"):
            await control(run, "host.lock")
        assert await run.page.evaluate("window.__bounds.size") == 0
        with pytest.raises(Exception, match="Minified Redux error #3"):
            await control(run, "host.remove", 16777218)
        assert await run.page.evaluate("window.__bounds.size") == 0


# ---- What Zoom 6.5.0's API does that the page lives with: a refusal followed by success, calls it drops, a recording it confirms later ----


ANSWERS_IN_ORDER = """() => {            // what Zoom calls back for a rename, in order
    const real = window.ZoomMtg.rename; window.__order = [];
    window.ZoomMtg.rename = function (o) {
        return real.call(this, Object.assign({}, o, { error: (e) => { window.__order.push("error"); o.error(e); }, success: () => { window.__order.push("success"); o.success(); } }));
    };
}"""


async def test_a_participants_own_rename_is_done_though_zoom_refuses_it_first():
    """Zoom's rename runs its host check first and goes on: for a participant it calls error ("Current user is neither host nor
    cohost", code 1) and then, with their own id, carries the rename out and calls success. The rename happened, so it is not a
    failure; a refusal that nothing follows is one, and is said at once. A host is not refused first."""
    async with PageRun(stub=client(role="host")) as run:
        await run.wait_for("connected")
        await run.page.evaluate(ANSWERS_IN_ORDER)
        await control(run, "rename", "Boardroom")
        assert await run.page.evaluate("window.__order") == ["success"]
    async with PageRun() as run:                                                   # a participant, the room's usual case
        await run.wait_for("connected")
        await run.page.evaluate(ANSWERS_IN_ORDER)
        m = await control(run, "rename", "Boardroom")
        assert await run.page.evaluate("window.__order") == ["error", "success"]                           # the premise: Zoom's order
        assert [p["name"] for p in m["participants"] if p["me"]] == ["Boardroom"]
        await run.page.evaluate("() => { window.ZoomMtg.rename = (o) => o.error({ errorCode: 1, errorMessage: 'Current user is neither host nor cohost' }); }")
        said, took = await run.page.evaluate("""async () => {
            const t = performance.now();
            const said = await window.crystalMeet.control('rename', 'Another').then(() => "done", (e) => e.message);
            return [said, performance.now() - t];
        }""")
        assert said == "Current user is neither host nor cohost (code 1)" and took < 1000, (said, took)


async def test_a_call_zoom_drops_is_refused_at_once_in_words_and_does_not_hold_the_controls_behind_it():
    """Zoom's API drops a call made again inside the window it keeps for its method (five seconds for muteAll, a second for mute):
    it answers false and calls nothing back. That is an ordinary pair of taps (Mute all, then Ask all to unmute), and it is said
    at once, not left to the bound that would hold every control behind it."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        await control(run, "host.mute_all")
        results, took = await run.page.evaluate("""(async () => {
            const t = performance.now();
            const r = await Promise.allSettled([window.crystalMeet.control('host.ask_unmute'), window.crystalMeet.control('captions')]);
            return [r.map((x) => x.status === "fulfilled" ? "fulfilled" : x.reason.message), performance.now() - t];
        })()""")
        assert results == ["Zoom ignored muteAll (it takes one a few seconds after the last); try again in a moment", "fulfilled"], results
        assert took < 2000, took                                                          # at once: not the twelve seconds of a call nobody answers
        assert len(await run.calls("muteAll")) == 2 and [d[1] for d in await run.page.evaluate("window.__dropped")] == [{"muteAll": False}]
        assert await run.page.evaluate("window.ZoomMtg._captions") is True                # the control behind it went on
        await later(run)
        await control(run, "host.ask_unmute")                                              # after the window the same call is taken
        assert len(await run.calls("muteAll")) == 3 and len(await run.page.evaluate("window.__dropped")) == 1
        # two Mute taps inside a second, for two people: the SDK keeps one window for mute, not one for each person
        results = await run.page.evaluate("""Promise.allSettled([window.crystalMeet.control('host.mute_one', 16777217), window.crystalMeet.control('host.mute_one', 16777218)])
                                              .then((r) => r.map((x) => x.status === "fulfilled" ? "fulfilled" : x.reason.message))""")
        assert results == ["fulfilled", "Zoom ignored mute (it takes one a few seconds after the last); try again in a moment"], results
        await later(run, 1000)
        await control(run, "host.mute_one", 16777218)


async def test_a_second_hold_waits_out_the_tenth_of_a_second_zoom_drops_it_inside():
    """Zoom drops a putOnHold made again inside 0.1 s and says nothing of it: its API answers undefined, as it does to a call it takes
    (it has callbacks), and calls nothing back, so a pair of taps (Hold, then Admit) would be a call nobody answers, holding every
    control behind it for the twelve seconds. The page remembers when it last asked and waits out the rest of the tenth of a second
    before it asks again. Here the stub's window runs on the real clock, the one the wait is measured on."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        await run.page.evaluate("() => { window.ZoomMtg._now = () => performance.now(); }")
        results, took = await asyncio.wait_for(run.page.evaluate("""(async () => {
            const t = performance.now();
            const r = await Promise.allSettled([window.crystalMeet.control('host.hold', 16777217), window.crystalMeet.control('host.admit', 16777217)]);
            return [r.map((x) => x.status === "fulfilled" ? "fulfilled" : x.reason.message), performance.now() - t];
        })()"""), timeout=8)
        assert results == ["fulfilled", "fulfilled"], results
        assert took < 3000, took                                                          # not the twelve seconds of a call nobody answers
        assert await run.page.evaluate("window.__dropped") == []
        assert [c[1] for c in await run.calls("putOnHold")] == [{"userId": 16777217, "hold": True}, {"userId": 16777217, "hold": False}]
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert [p["waiting"] for p in m["participants"] if p["id"] == 16777217] == [False]        # the second took effect: Ann was admitted again


async def test_the_stub_drops_what_the_sdk_drops():
    """What the tests above rely on: Zoom's API answers false, and calls nothing back, to a call with a key its method does not take
    and to one made again inside the window it keeps for its method, except putOnHold, whose drop is silent (it answers undefined,
    as it does to a call it takes, since it has callbacks); the windows run on the stub's clock. A key left out is no drop: the SDK's
    gate looks only at the keys given, and the call goes ahead with it undefined (lockMeeting({}) unlocks). A call it takes returns
    nothing and is answered after it has returned, as the SDK answers, except rename, record and endMeeting, which it answers inside
    the call."""
    async with PageRun(stub=client(role="host")) as run:
        await run.wait_for("connected")

        async def ask(name, args):
            """[what the call returned, what had been called back when it returned, and a moment after]"""
            return await run.page.evaluate("""async ([name, args]) => {
                const answered = [];
                const r = window.ZoomMtg[name](Object.assign({}, args, { success: () => answered.push("success"), error: () => answered.push("error") }));
                const inside = answered.slice();
                await null;
                return [String(r), inside, answered];
            }""", [name, args])

        def taken(name):
            return ["undefined", ["success"] if name in ("rename", "record", "endMeeting") else [], ["success"]]

        dropped = ["false", [], []]
        keys = {"muteAll": {"muteAll": True}, "record": {"record": True}, "lockMeeting": {"lockMeeting": True}, "putOnHold": {"userId": 16777217, "hold": True},
                "expel": {"userId": 16777218}, "mute": {"userId": 16777217, "mute": True}, "rename": {"userId": 16777216, "oldName": "Room 1", "newName": "Room 2"}}
        windows = {"muteAll": 5000, "record": 5000, "lockMeeting": 5000, "expel": 5000, "rename": 5000, "mute": 1000, "putOnHold": 100}
        for name, args in keys.items():
            assert await ask(name, args) == taken(name)
            await later(run, windows[name] - 1)
            assert await ask(name, args) == (["undefined", [], []] if name == "putOnHold" else dropped)      # a moment too soon: dropped, and nothing called back
            await later(run, 1)
            assert await ask(name, args) == taken(name)                                       # the window is over
        for name, args in [("lockMeeting", {"lockMeeting": True, "note": "x"}), ("endMeeting", {"end": True}), ("muteAll", {"mute": True}),
                           ("putOnHold", {"userId": 16777217, "hold": True, "all": True}), ("mute", {"userId": 16777217, "mute": True, "all": True}),
                           ("expel", {"userId": 16777218, "reason": "x"})]:
            await later(run)
            assert await ask(name, args) == dropped                                           # a key it does not take, whatever the method
        await later(run)
        assert await ask("lockMeeting", {"lockMeeting": True}) == taken("lockMeeting")
        assert await run.page.evaluate("window.ZoomMtg._locked") is True
        await later(run)
        assert await ask("lockMeeting", {}) == taken("lockMeeting")                           # a key left out goes ahead as undefined: it unlocks
        assert await run.page.evaluate("window.ZoomMtg._locked") is False
        for name, args in [("record", {}), ("putOnHold", {"userId": 16777217}), ("muteAll", {}), ("mute", {"userId": 16777217}), ("expel", {})]:
            await later(run)
            assert await ask(name, args) == taken(name)
        await later(run)
        assert await ask("mute", {"userId": 16777216, "mute": True}) == taken("mute")         # the room's own mute shares the window with another's
        assert await ask("mute", {"userId": 16777217, "mute": True}) == dropped
        assert await ask("mute", {"userId": 16777216, "mute": False}) == dropped
        await later(run, 1000)
        assert await ask("mute", {"userId": 16777216, "mute": False}) == taken("mute")
        assert await ask("endMeeting", {}) == taken("endMeeting")


async def test_a_refusal_with_no_words_is_named_by_the_call():
    """endMeeting is the host's alone: the SDK refuses a co-host with a code and no message, which would read as a blob of JSON."""
    async with PageRun(stub=client(role="co-host")) as run:
        await settled(run)
        with pytest.raises(Exception) as failure:
            await control(run, "host.end")
        assert "Zoom refused endMeeting (code 3003)" in str(failure.value) and "errorMessage" not in str(failure.value)
        await run.page.evaluate("() => { window.ZoomMtg.lockMeeting = (o) => o.error(); }")                  # no reason and no code at all
        with pytest.raises(Exception) as failure:
            await control(run, "host.lock")
        assert "Zoom refused lockMeeting" in str(failure.value) and "(code" not in str(failure.value)
        m = await control(run, "host.mute_all")                                                                # a co-host may do the rest
        assert all(p["muted"] for p in m["participants"] if not p["me"])


async def test_zooms_own_timeout_is_said_in_words_and_not_as_the_json_it_sends():
    """The SDK waits ten seconds for its store to confirm a call and then ends it with an error whose message is the JSON of
    {type: "OPERATION_TIMEOUT", errorCode: 1}: that reads "Zoom did not confirm <call> in time", whichever way the error comes
    (an Error, an errorMessage, or the object itself), and not as the blob. It is not the page's own bound for a call nobody answers."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        blob = "JSON.stringify({ type: 'OPERATION_TIMEOUT', errorCode: 1 })"
        for what, error in [("an Error", f"new Error({blob})"), ("an errorMessage", f"{{ errorCode: 1, errorMessage: {blob} }}"),
                            ("the object", "{ type: 'OPERATION_TIMEOUT', errorCode: 1 }")]:
            await run.page.evaluate(f"() => {{ window.ZoomMtg.lockMeeting = (o) => o.error({error}); }}")
            with pytest.raises(Exception) as failure:
                await control(run, "host.lock")
            assert "Zoom did not confirm lockMeeting in time" in str(failure.value) and "OPERATION_TIMEOUT" not in str(failure.value), what
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["locked"] is False                # a call Zoom did not confirm is not kept as done


async def test_recording_is_answered_once_zoom_has_confirmed_it_and_says_so_when_it_does_not():
    """Zoom's record answers at once and tells of the recording when its server has confirmed it (onRecordingChange): the answer
    waits for that, up to three seconds, and a recording nobody confirms is a failure in words, for starting and for stopping."""
    async with PageRun(stub=client(role="host", recordConfirmsAfterMs=800)) as run:
        await settled(run)
        recording, took = await run.page.evaluate("""async () => {
            const t = performance.now(), m = await window.crystalMeet.control('host.record');
            return [m.recording, performance.now() - t];
        }""")
        assert recording is True and 700 <= took < 2500, (recording, took)               # not the answer as it was at the call
    async with PageRun(stub=client(role="host", recordNeverConfirms=True)) as run:
        await settled(run)
        with pytest.raises(Exception, match="Zoom did not start recording"):
            await asyncio.wait_for(control(run, "host.record"), timeout=10)
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["recording"] is False
        await run.page.evaluate("window.ZoomMtg._setRecording(true)")                      # it is recording, though (somebody else started it)
        await later(run)
        with pytest.raises(Exception, match="Zoom did not stop recording"):
            await asyncio.wait_for(control(run, "host.record"), timeout=10)
        assert (await run.page.evaluate("window.crystalMeet.readMeeting()"))["recording"] is True


async def test_the_same_participant_spelled_two_ways_is_one_call():
    """Zoom drops the second call of a method made within seconds of the first, so a number and its text must not be two calls."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        same, results = await run.page.evaluate("""(async () => {
            const a = window.crystalMeet.control('host.remove', 16777218), b = window.crystalMeet.control('host.remove', '16777218');
            const r = await Promise.allSettled([a, b]);
            return [a === b, r.map((x) => x.status)];
        })()""")
        assert same is True and results == ["fulfilled", "fulfilled"]
        assert [c[1] for c in await run.calls("expel")] == [{"userId": 16777218}]


async def test_a_second_mute_inside_the_second_zoom_keeps_for_it_is_refused_at_once_in_words_and_the_first_stands():
    """Two Mute taps within a second: Zoom drops the second (its API answers false and calls nothing back), and the page waited for
    an answer that never came. It is refused at once, in words, and what the first did stands."""
    async with PageRun() as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.mute(true)") is True
        said, took = await asyncio.wait_for(run.page.evaluate("""async () => {
            const t = performance.now();
            const said = await window.crystalMeet.mute(false).then(() => "done", (e) => e.message);
            return [said, performance.now() - t];
        }"""), timeout=5)
        assert said == "Zoom ignored mute (it takes one a few seconds after the last); try again in a moment" and took < 1000, (said, took)
        assert await run.page.evaluate("window.ZoomMtg._muted") is True                                    # the first stands ...
        assert (await run.page.evaluate("window.crystalMeet.readState()"))["muted"] is True               # ... and so does what the page says of it
        assert [c[1] for c in await run.calls("mute")] == [{"userId": 16777216, "mute": True}, {"userId": 16777216, "mute": False}]   # both were asked
        assert [d[1] for d in await run.page.evaluate("window.__dropped")] == [{"userId": 16777216, "mute": False}]
        await later(run, 1000)
        assert await run.page.evaluate("window.crystalMeet.mute(false)") is False                         # a second later the tap is taken


async def test_the_mute_at_join_is_taken_and_a_tap_right_after_it_is_the_one_zoom_ignores():
    """The join's mic-off default is the first mute there is, so Zoom takes it, and nothing of the join asks twice: a reconnect does
    not mute the room again. An Unmute tapped inside the second after it is a second mute, and is refused at once."""
    async with PageRun({"micOn": False}) as run:
        await settled(run)
        assert [c[1]["mute"] for c in await run.calls("mute")] == [True] and await run.page.evaluate("window.__dropped") == []   # the default, taken
        with pytest.raises(Exception, match="Zoom ignored mute"):
            await asyncio.wait_for(run.page.evaluate("window.crystalMeet.mute(false)"), timeout=5)
        await run.page.evaluate("window.ZoomMtg._status(4)")                                              # Zoom reconnecting ...
        await run.page.evaluate("window.ZoomMtg._status(2)")                                              # ... and connected again
        await asyncio.sleep(0.3)
        assert [c[1]["mute"] for c in await run.calls("mute")] == [True, False]                           # the reconnect asked for nothing: only the tap
        assert await run.page.evaluate("window.ZoomMtg._muted") is True                                   # and the default stands
        await later(run, 1000)
        assert await run.page.evaluate("window.crystalMeet.mute(false)") is False                         # a second later the tap is taken


async def test_the_rooms_mute_and_a_hosts_mute_of_somebody_share_the_one_window():
    """Zoom keeps one window for mute, whoever is muted: a Mute tapped for a person right after the room's own is the one ignored."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        assert await run.page.evaluate("window.crystalMeet.mute(true)") is True
        with pytest.raises(Exception, match="Zoom ignored mute"):
            await asyncio.wait_for(control(run, "host.mute_one", 16777217), timeout=5)
        await later(run, 1000)
        await control(run, "host.mute_one", 16777217)
        with pytest.raises(Exception, match="Zoom ignored mute"):
            await asyncio.wait_for(run.page.evaluate("window.crystalMeet.mute(false)"), timeout=5)


# ---- Dialogs and chat (spec 2026-10-13, sections 4.2 and 5): everything the client draws over the meeting is reported, and a dialog is
# answered on request by pressing its own button; the page never presses one on its own, and never takes a node of the client's out ----


async def reported(run, state, count=1, timeout=3.0):
    """The decoded details of the page's events of one kind, once there are at least `count` of them."""
    for _ in range(int(timeout * 20)):
        found = [json.loads(d) for s, d in run.events if s == state]
        if len(found) >= count:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"{count} {state!r} events never reported; events: {run.events}")


async def show(run, title, text, buttons):
    """Zoom draws a modal; answers the id of its node (the page's own id for it comes with its report)."""
    return await run.page.evaluate("([title, text, buttons]) => window.ZoomMtg._showDialog(title, text, buttons)", [title, text, buttons])


async def open_dialogs(run):
    return await run.page.evaluate("window.crystalMeet.dialogs()")


async def test_a_dialog_is_reported_with_its_words_and_buttons_and_answered_by_label():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._showDialog('Recording consent', 'This meeting is being recorded.', ['Got it', 'Leave meeting'])")
        await run.wait_for("dialog")
        d = json.loads(next(x for s, x in run.events if s == "dialog"))
        assert d["title"] == "Recording consent" and d["buttons"] == ["Got it", "Leave meeting"] and "being recorded" in d["text"]
        assert not any(c[0] == "dialog" for c in await run.page.evaluate("window.__zoomCalls"))   # the page presses nothing on its own
        assert await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, 'got it')") is True
        assert ["dialog", "Recording consent", "Got it"] in await run.page.evaluate("window.__zoomCalls")
        await run.wait_for("dialog-closed")
        assert await run.page.evaluate("window.__reactErrors") == []                                 # the client's node left as its owner took it


async def test_a_dialog_that_goes_away_on_its_own_is_reported_closed_and_cannot_be_answered():
    async with PageRun() as run:
        await run.wait_for("connected")
        sid = await run.page.evaluate("window.ZoomMtg._showDialog('Unmute?', 'The host would like you to unmute.', ['Unmute', 'Stay muted'])")
        await run.wait_for("dialog")
        d = json.loads(next(x for s, x in run.events if s == "dialog"))
        await run.page.evaluate(f"window.ZoomMtg._closeDialog('{sid}')")
        await run.wait_for("dialog-closed")
        assert json.loads(next(x for s, x in run.events if s == "dialog-closed")) == {"id": d["id"]}
        with pytest.raises(Exception, match="no longer on the screen"):
            await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, 'Unmute')")
        assert await run.calls("dialog") == [] and await run.page.evaluate("window.__reactErrors") == []


async def test_chat_messages_reach_the_device():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._chat('Ann Lee', 'Can you hear us?')")
        await run.wait_for("chat")
        c = json.loads(next(x for s, x in run.events if s == "chat"))
        assert c["from"] == "Ann Lee" and c["text"] == "Can you hear us?" and c["at"]


async def test_a_dialog_is_reported_once_with_the_words_of_the_dialog_alone():
    """Its title, its own words (not the buttons', nor the footer's checkbox and link, nor a style or a script), the words of separate
    parts kept apart, and its buttons as the client draws them (cancel first), tidied of the client's spacing. A redraw inside it is
    not a new dialog, and the open dialogs say what the dialog says now."""
    async with PageRun() as run:
        await run.wait_for("connected")
        sid = await run.page.evaluate("""() => {
            const id = window.ZoomMtg._showDialog("  Recording\\n   consent ", "This meeting is being recorded.\\n  Anyone may see the recording.", ["Leave   Meeting ", " Got it"]);
            const dialog = document.getElementById(id), more = document.createElement("div");
            more.innerHTML = "<p>Ask the host.</p><p><b>Not</b> now</p><style>.zm-modal { color: red; }</style><script type='text/plain'>ignored()</script>";
            dialog.querySelector(".zm-modal-body-content").appendChild(more);
            const extras = document.createElement("div");      // the footer's own words, before its buttons: a checkbox and a link
            extras.innerHTML = '<div role="checkbox" aria-checked="false">Do not show this again</div><a class="zm-modal-footer-default-anchor" href="#">Learn more</a>';
            const actions = dialog.querySelector(".zm-modal-footer-default-actions");
            while (extras.firstChild) actions.parentNode.insertBefore(extras.firstChild, actions);
            return id;
        }""")
        [d] = await reported(run, "dialog")
        assert d == {"id": d["id"], "title": "Recording consent", "buttons": ["Leave Meeting", "Got it"],
                     "text": "This meeting is being recorded. Anyone may see the recording. Ask the host. Not now"}
        assert await open_dialogs(run) == [d]
        await run.page.evaluate("(id) => { document.querySelector('#' + id + ' p').textContent = 'This meeting is being recorded and transcribed.'; }", sid)
        await asyncio.sleep(0.4)
        assert len(await reported(run, "dialog")) == 1                                                  # a redraw inside is the same dialog
        assert (await open_dialogs(run))[0]["text"] == "This meeting is being recorded and transcribed. Ask the host. Not now"
        assert await presses(run) == [] and await run.calls("dialog") == []


@pytest.mark.parametrize("tag", ["h1", "h2", "h3"])
async def test_the_title_of_a_dialog_is_the_div_the_client_gives_it_or_a_heading(tag):
    """The client's title is a div with a class (div.zm-modal-body-title), not a heading; a dialog that has a heading has that. Either is
    the title, and neither is among the words."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await show(run, "Recording consent", "This meeting is being recorded.", ["Got it"])
        await run.page.evaluate("""(tag) => {
            const d = document.createElement("div"); d.setAttribute("role", "dialog");
            d.innerHTML = "<" + tag + ">Join breakout room</" + tag + "><p>Room 1 is ready.</p><button>Join</button>";
            document.body.appendChild(d);
        }""", tag)
        a, b = (await reported(run, "dialog", 2))[:2]
        assert (a["title"], a["text"]) == ("Recording consent", "This meeting is being recorded.")
        assert (b["title"], b["text"]) == ("Join breakout room", "Room 1 is ready.")


async def test_the_title_is_the_title_nodes_words_without_a_button_that_sits_in_it():
    """zmu's Banner puts the OK button inside its title node. The node is the title though it holds a button, and the title is its
    words without the button's: its text nodes joined as they are, so a word split by an inline element is one word."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const d = document.createElement("div"); d.setAttribute("role", "dialog");
            d.innerHTML = '<div class="zm-modal-body-title">Lea<b>ve</b> the meeting?<button>Help</button></div><p>You can rejoin.</p><button>Leave</button>';
            document.body.appendChild(d);
        }""")
        [d] = await reported(run, "dialog")
        assert (d["title"], d["text"], d["buttons"]) == ("Leave the meeting?", "You can rejoin.", ["Help", "Leave"])


@pytest.mark.parametrize("label, words, title, expected", [
    ("Recording has started This meeting is being recorded.", "", "Recording has started", "This meeting is being recorded."),
    ("  Recording   has started \n This meeting is being recorded.  ", "", "Recording has started", "This meeting is being recorded."),
    ("Recording has started", "", "Recording has started", ""),                                          # nothing but the title: no words
    ("Host request", "", "Unmute?", ""),                                                                 # not "<title> <description>": a developer's name for the modal, no words
    ("Aria words", "The words it has.", "Unmute?", "The words it has."),                                 # words of its own: the label is not wanted
])
async def test_a_dialog_with_no_words_of_its_own_has_its_aria_label_for_them(label, words, title, expected):
    """The consent banner has its description only in its aria-label ("<title> <description>"): when the text nodes give no words, the
    words are the label with the title taken off its start, tidied. A label that does not begin with the title is not that: a
    body-less react-modal's label is a developer's name for it ("remove all pinned videos", "Mute Video Modal"), and it must not
    become the dialog's words, which a policy matches on and the table shows."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""([label, words, title]) => {
            const d = document.createElement("div"); d.setAttribute("role", "dialog"); d.setAttribute("aria-label", label);
            d.innerHTML = '<div class="zm-modal-body-title"></div><p></p><button>OK</button>';
            d.querySelector(".zm-modal-body-title").textContent = title; d.querySelector("p").textContent = words;
            document.body.appendChild(d);
        }""", [label, words, title])
        [d] = await reported(run, "dialog")
        assert (d["title"], d["text"]) == (title, expected)


@pytest.mark.parametrize("classes", ["zm-modal", "zm-modal-legacy"])
async def test_a_modal_drawn_without_a_role_is_a_dialog_by_its_class(classes):
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""(classes) => {
            const panel = document.createElement("div"); panel.className = "participants-panel";           // a part of the client that is no modal, with a button
            panel.innerHTML = "<button>Invite</button>"; document.body.appendChild(panel);
            const d = document.createElement("div"); d.className = classes;
            d.innerHTML = '<div class="zm-modal-body-title">Leave meeting?</div><p>Are you sure?</p><button>Leave</button>';
            document.body.appendChild(d);
        }""", classes)
        [d] = await reported(run, "dialog")
        await asyncio.sleep(0.3)
        assert (d["title"], d["buttons"]) == ("Leave meeting?", ["Leave"]) and len(await reported(run, "dialog")) == 1


async def test_the_consent_banner_the_client_draws_in_the_meeting_is_a_dialog_too():
    """The unified consent notice (recording started, captions, AI Companion) is div.consent-banner[role=banner], never role=dialog, and
    zmu's Banner builds it oddly: its OK button sits inside its title node, and its description is no text node, only the banner's
    aria-label ("<title> <description>"). It is reported with its title, the description for its words and its OK, and answered like
    any other, or it stays on the TV. The icon that tells more (a div with role=button, not a button) is no button of its, and a page
    header that carries role=banner is no banner of the client's."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const header = document.createElement("header"); header.setAttribute("role", "banner"); header.innerHTML = "<button>Menu</button>"; document.body.appendChild(header);
        }""")
        sid = await run.page.evaluate("window.ZoomMtg._showBanner('Recording has started', 'This meeting is being recorded.')")
        shape = await run.page.evaluate("""(id) => [document.querySelectorAll('[role=dialog]').length,
            document.querySelector('#' + id + ' .ui-Banner-title button.ok-button') !== null,          // the OK is inside the title node
            document.querySelector('#' + id + ' [role=button]:not(button)') !== null,                  // the icon that tells more
            document.getElementById(id).textContent.includes('being recorded')]                        // the description is no text node""", sid)
        assert shape == [0, True, True, False]
        [d] = await reported(run, "dialog")
        assert d == {"id": d["id"], "title": "Recording has started", "text": "This meeting is being recorded.", "buttons": ["OK"]}
        await asyncio.sleep(0.3)
        assert len(await reported(run, "dialog")) == 1 and await presses(run) == []
        assert await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, 'ok')") is True
        assert await run.calls("dialog") == [["dialog", "Recording has started", "OK"]] and await presses(run) == [sid]
        assert (await reported(run, "dialog-closed"))[0] == {"id": d["id"]} and await open_dialogs(run) == []
        bare = await run.page.evaluate("window.ZoomMtg._showBanner('Captions are on', '')")           # no description: no icon, and nothing to say but the title
        assert await run.page.evaluate("(id) => document.querySelector('#' + id + ' [role=button]:not(button)')", bare) is None
        e = (await reported(run, "dialog", 2))[1]
        assert e == {"id": e["id"], "title": "Captions are on", "text": "", "buttons": ["OK"]} and e["id"] != d["id"]
        assert await run.page.evaluate("window.__reactErrors") == []


@pytest.mark.parametrize("how", ["class", "aria-disabled", "disabled attribute"])
async def test_a_disabled_button_of_a_dialog_is_not_pressed_however_the_client_marks_it(how):
    """Zoom's own button has no disabled attribute, only the class zm-btn--disabled (and ignores a press); its other buttons say
    aria-disabled. Either is said at once, not after the two seconds a press that does nothing takes, which hold every control behind it."""
    async with PageRun() as run:
        await run.wait_for("connected")
        sid = await show(run, "Unmute?", "The host would like you to unmute.", ["Stay muted", {"label": "Unmute myself", "disabled": how == "class"}])
        [d] = await reported(run, "dialog")
        if how != "class":
            await run.page.evaluate("""([id, how]) => {
                const b = document.querySelectorAll('#' + id + ' button')[1];
                if (how === "aria-disabled") b.setAttribute("aria-disabled", "true"); else b.disabled = true;
            }""", [sid, how])
        said, took = await run.page.evaluate("""async (id) => {
            const t = performance.now();
            return [await window.crystalMeet.answerDialog(id, "Unmute myself").then(() => "answered", (e) => e.message), performance.now() - t];
        }""", d["id"])
        assert "button is not ready yet" in said and took < 1000, (said, took)
        assert await presses(run) == [] and await run.calls("dialog") == []                              # not even tried
        assert await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, 'Stay muted')") is True


async def test_a_dialogs_words_are_cut_at_four_hundred_characters():
    async with PageRun() as run:
        await run.wait_for("connected")
        await show(run, "Terms", "word " * 200, ["OK"])
        [d] = await reported(run, "dialog")
        assert len(d["text"]) == 400 and d["text"].startswith("word word") and d["title"] == "Terms"


async def test_each_dialog_has_an_id_of_its_own_and_one_drawn_again_is_a_new_dialog():
    async with PageRun() as run:
        await run.wait_for("connected")
        first = await show(run, "Unmute?", "The host would like you to unmute.", ["Unmute", "Stay muted"])
        await show(run, "Breakout rooms", "Join Room 1?", ["Join", "Later"])
        a, b = (await reported(run, "dialog", 2))[:2]
        assert [a["title"], b["title"]] == ["Unmute?", "Breakout rooms"] and a["id"] != b["id"]
        assert [d["id"] for d in await open_dialogs(run)] == [a["id"], b["id"]]
        await run.page.evaluate(f"window.ZoomMtg._closeDialog('{first}')")
        assert (await reported(run, "dialog-closed"))[0] == {"id": a["id"]}
        assert [d["id"] for d in await open_dialogs(run)] == [b["id"]]                                  # the other stays
        await show(run, "Unmute?", "The host would like you to unmute.", ["Unmute", "Stay muted"])      # the same words again: another dialog
        c = (await reported(run, "dialog", 3))[2]
        assert c["id"] not in (a["id"], b["id"]) and c["title"] == "Unmute?"
        assert len(await reported(run, "dialog-closed", 1)) == 1


async def test_a_banner_with_no_buttons_is_not_a_dialog_until_it_has_one():
    """Nothing in it can be answered from the table (spec section 5: a banner with no buttons is never a dialog): it is reported once a
    button appears in it, and nothing is said of one that goes without ever having had one."""
    async with PageRun() as run:
        await run.wait_for("connected")
        passing = await show(run, "", "Recording has started.", [])
        await asyncio.sleep(0.4)
        assert not [s for s, _ in run.events if s.startswith("dialog")] and await open_dialogs(run) == []
        await run.page.evaluate(f"window.ZoomMtg._closeDialog('{passing}')")
        await asyncio.sleep(0.4)
        assert not [s for s, _ in run.events if s.startswith("dialog")]                                 # it went, and nobody was told of it
        banner = await show(run, "Recording", "The host is recording.", [])
        await asyncio.sleep(0.2)
        await run.page.evaluate("(id) => { const b = document.createElement('button'); b.textContent = 'OK'; document.getElementById(id).appendChild(b); }", banner)
        [d] = await reported(run, "dialog")
        assert d["buttons"] == ["OK"] and d["text"] == "The host is recording."
        await run.page.evaluate("(id) => document.querySelector('#' + id + ' button').remove()", banner)                      # it has nothing to answer again: closed
        assert (await reported(run, "dialog-closed"))[0] == {"id": d["id"]} and await open_dialogs(run) == []
        await run.page.evaluate("(id) => { const b = document.createElement('button'); b.textContent = 'Later'; document.getElementById(id).appendChild(b); }", banner)
        again = (await reported(run, "dialog", 2))[1]
        assert again["id"] != d["id"] and again["buttons"] == ["Later"]                                                       # and a dialog again, another one


async def test_zooms_share_toolbar_is_no_dialog_shown_or_hidden():
    """Zoom 6.5.0 keeps its screen-share toolbar in a role="dialog" from the join on (a room device reported "Preview Pause Share Stop
    Share" with its buttons as a dialog, Oct 15): it asks nothing, and a tap on it from the table would stop the share."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const bar = document.createElement("div"); bar.setAttribute("role", "dialog");
            bar.innerHTML = '<span>Preview</span><button aria-label="screen share preview"></button><button aria-label="more options"></button>'
                + '<button>Pause share</button><button>Stop Share</button>';
            document.body.appendChild(bar);
        }""")
        await asyncio.sleep(0.4)
        assert not [s for s, _ in run.events if s.startswith("dialog")] and await open_dialogs(run) == []


async def test_a_dialog_nobody_can_see_is_no_dialog_until_it_is_shown():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            for (const how of ["display: none", "visibility: hidden"]) {
                const d = document.createElement("div"); d.setAttribute("role", "dialog"); d.setAttribute("style", how); d.className = "hidden-one";
                d.innerHTML = '<div class="zm-modal-body-title">Kept for later</div><button>OK</button>';
                document.body.appendChild(d);
            }
            const outer = document.createElement("div"); outer.setAttribute("aria-hidden", "true");
            outer.innerHTML = '<div role="dialog"><div class="zm-modal-body-title">Behind</div><button>OK</button></div>';
            document.body.appendChild(outer);
        }""")
        await asyncio.sleep(0.4)
        assert not [s for s, _ in run.events if s.startswith("dialog")] and await open_dialogs(run) == []
        await run.page.evaluate("() => document.querySelector('.hidden-one').removeAttribute('style')")
        [d] = await reported(run, "dialog")
        assert (d["title"], d["buttons"]) == ("Kept for later", ["OK"])


async def test_the_parts_of_one_modal_are_one_dialog():
    """react-modal draws Zoom's modal in layers that both look like a dialog: the role is on the outer node (div.ReactModal__Content) and
    div.zm-modal.zm-modal-legacy is inside it. They are one dialog, whose buttons are in the order the client's footer draws them
    (cancel first), answered once and closed once."""
    async with PageRun() as run:
        await run.wait_for("connected")
        sid = await show(run, "Recording consent", "This meeting is being recorded.", ["Leave Meeting", "Got it"])
        layers = await run.page.evaluate("""(id) => [document.querySelectorAll('[role=dialog]').length, document.querySelectorAll('.zm-modal').length,
                                                     document.querySelector('#' + id + ' > .zm-modal.zm-modal-legacy') !== null]""", sid)
        assert layers == [1, 1, True]                                                                   # the layers are there: the role outside, the classes inside
        [d] = await reported(run, "dialog")
        await asyncio.sleep(0.4)
        assert len(await reported(run, "dialog")) == 1 and [x["id"] for x in await open_dialogs(run)] == [d["id"]]
        assert d["buttons"] == ["Leave Meeting", "Got it"]
        assert await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, 'Got it')") is True
        assert (await reported(run, "dialog-closed"))[0] == {"id": d["id"]} and await open_dialogs(run) == []
        await asyncio.sleep(0.3)
        assert len(await reported(run, "dialog-closed")) == 1                                           # said once
        assert await run.page.evaluate("window.__reactErrors") == []


async def test_a_dialogs_buttons_are_found_by_what_they_say_whatever_the_case_and_spacing():
    """The same names the popovers' items go by: lower case, only letters and digits. A button may say its name in its label alone
    (an icon), and one with no name at all is no button the table can be offered."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const d = document.createElement("div"); d.setAttribute("role", "dialog");
            const add = (html, name) => {
                const b = document.createElement("button"); b.innerHTML = html;
                b.addEventListener("click", () => { window.__presses.push(["built", name]); queueMicrotask(() => d.parentNode.removeChild(d)); });
                d.appendChild(b);
            };
            add('<svg width="8" height="8"></svg>', "unnamed");
            add('<svg width="8" height="8"></svg>', "icon"); d.lastChild.setAttribute("aria-label", "Close");
            add("\\n   Got\\n     it\\n", "text");
            document.body.appendChild(d);
        }""")
        [d] = await reported(run, "dialog")
        assert d["buttons"] == ["Close", "Got it"]
        assert await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, ' GOT-it ')") is True
        assert await run.page.evaluate("window.__presses") == [["built", "text"]]
    async with PageRun() as run:                                                                        # by its label alone
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const d = document.createElement("div"); d.setAttribute("role", "alertdialog");
            const b = document.createElement("button"); b.setAttribute("aria-label", "Close"); b.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "svg"));
            b.addEventListener("click", () => { window.__presses.push(["built", "icon"]); queueMicrotask(() => d.parentNode.removeChild(d)); });
            d.appendChild(b); document.body.appendChild(d);
        }""")
        [d] = await reported(run, "dialog")
        assert d["buttons"] == ["Close"] and await run.page.evaluate(f"window.crystalMeet.answerDialog('{d['id']}', 'close')") is True      # the id as text is the same id
        assert await run.page.evaluate("window.__presses") == [["built", "icon"]]


async def test_an_answer_that_cannot_be_given_says_why_and_presses_nothing():
    async with PageRun() as run:
        await run.wait_for("connected")
        sid = await show(run, "Unmute?", "The host would like you to unmute.", ["Unmute", "Stay muted"])
        [d] = await reported(run, "dialog")
        await run.page.evaluate("(id) => { document.querySelector('#' + id + ' button').disabled = true; }", sid)
        for call, words in [(f"answerDialog({d['id']}, 'Maybe')", "has no button called Maybe"), (f"answerDialog({d['id']})", "Say which button"),
                            (f"answerDialog({d['id']}, '  ')", "Say which button"), (f"answerDialog({d['id']}, 'Unmute')", "not ready yet"),
                            ("answerDialog(99, 'Unmute')", "no longer on the screen"), ("answerDialog()", "Say which button")]:
            with pytest.raises(Exception, match=words):
                await run.page.evaluate(f"window.crystalMeet.{call}")
        assert await run.calls("dialog") == [] and await presses(run) == []
        assert [x["id"] for x in await open_dialogs(run)] == [d["id"]]                                  # still there, and still to be answered
        assert await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, 'Stay muted')") is True
        assert await run.calls("dialog") == [["dialog", "Unmute?", "Stay muted"]]


async def test_a_dialog_taken_out_of_the_page_cannot_be_answered_even_before_the_page_has_noticed():
    async with PageRun() as run:
        await run.wait_for("connected")
        sid = await show(run, "Unmute?", "The host would like you to unmute.", ["Unmute", "Stay muted"])
        [d] = await reported(run, "dialog")
        with pytest.raises(Exception, match="no longer on the screen"):
            await run.page.evaluate("""([id, answer]) => {
                window.ZoomMtg._closeDialog(id);                       // gone, and the page has not looked yet
                return window.crystalMeet.answerDialog(answer, "Unmute");
            }""", [sid, d["id"]])
        assert await run.calls("dialog") == []


async def test_a_press_that_does_not_close_the_dialog_is_a_failure_in_words_and_holds_nothing_behind_it():
    """The press is made once; when the dialog is still there two seconds later the answer says so, and what waited behind it goes on."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.dialogStays = true;") as run:
        await run.wait_for("connected")
        await show(run, "Unmute?", "The host would like you to unmute.", ["Stay muted", "Unmute"])
        [d] = await reported(run, "dialog")
        answer = asyncio.ensure_future(run.page.evaluate("""async (id) => {
            const t = performance.now();
            return [await window.crystalMeet.answerDialog(id, "Unmute").then(() => "answered", (e) => e.message), performance.now() - t];
        }""", d["id"]))
        behind = asyncio.ensure_future(run.page.evaluate("window.crystalMeet.control('captions')"))
        said, took = await asyncio.wait_for(answer, timeout=8)
        assert "did not close" in said and 1900 <= took < 2800, (said, took)                            # two seconds, the bound
        m = await asyncio.wait_for(behind, timeout=8)
        assert m["buttons"]["captions"] == {"on": True}
        assert await run.calls("dialog") == [["dialog", "Unmute?", "Unmute"]]                           # pressed once
        assert [x["id"] for x in await open_dialogs(run)] == [d["id"]]                                  # and it is still there, still reported open
        assert not [s for s, _ in run.events if s == "dialog-closed"]


async def test_a_dialog_that_closes_a_moment_after_the_press_is_waited_for():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.dialogClosesAfterMs = 600;") as run:
        await run.wait_for("connected")
        await show(run, "Recording consent", "This meeting is being recorded.", ["Got it"])
        [d] = await reported(run, "dialog")
        answered, took = await run.page.evaluate("""async (id) => {
            const t = performance.now(), r = await window.crystalMeet.answerDialog(id, "Got it");
            return [r, performance.now() - t];
        }""", d["id"])
        assert answered is True and 500 <= took < 2000, (answered, took)
        assert (await reported(run, "dialog-closed"))[0] == {"id": d["id"]}


async def test_the_answer_comes_back_after_the_dialog_has_been_said_closed():
    """The device hears that the dialog is gone before it hears the answer, and hears it once: the page's later look at the client has
    nothing more to say of it."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => { window.__sent = []; const real = window.crystalMeetEvent;
                                          window.crystalMeetEvent = (state, detail) => { window.__sent.push(state); return real(state, detail); }; }""")
        await show(run, "Recording consent", "This meeting is being recorded.", ["Got it", "Leave meeting"])
        [d] = await reported(run, "dialog")
        sent = await run.page.evaluate("""async (id) => {
            await window.crystalMeet.answerDialog(id, "Got it");
            return window.__sent.filter((s) => s.startsWith("dialog"));
        }""", d["id"])
        assert sent == ["dialog", "dialog-closed"]
        await asyncio.sleep(0.3)
        assert await run.page.evaluate("window.__sent.filter((s) => s.startsWith('dialog'))") == ["dialog", "dialog-closed"]   # and once


async def test_answering_a_dialog_waits_its_turn_among_the_controls():
    """It is one of the page's presses: never between those of a control, which would find the dialog's buttons in its popover."""
    async with PageRun(stub=client(role="host")) as run:
        await settled(run)
        sid = await show(run, "Recording consent", "This meeting is being recorded.", ["Got it", "Leave meeting"])
        [d] = await reported(run, "dialog")
        await run.page.evaluate("""() => {            // Zoom answers this a moment after it is asked
            const real = window.ZoomMtg.muteAll;
            window.ZoomMtg.muteAll = function (o) { window.__presses.push(["muteAll asked", ""]); setTimeout(() => { window.__presses.push(["muteAll answered", ""]); real.call(this, o); }, 300); };
        }""")
        results = await run.page.evaluate(f"""Promise.allSettled([window.crystalMeet.control('host.mute_all'), window.crystalMeet.answerDialog({d['id']}, 'Got it'),
                                              window.crystalMeet.control('captions')]).then((r) => r.map((x) => x.status))""")
        assert results == ["fulfilled"] * 3
        assert await presses(run) == ["muteAll asked", "muteAll answered", sid, "stub-captions"]


async def test_answering_a_dialog_waits_for_the_inventory_too():
    async with PageRun(stub=client(popoverOpensAfterMs=300)) as run:
        await settled(run)
        sid = await show(run, "Recording consent", "This meeting is being recorded.", ["Got it", "Leave meeting"])
        [d] = await reported(run, "dialog")
        results = await run.page.evaluate(f"""Promise.allSettled([window.crystalMeet.diagnostics(), window.crystalMeet.answerDialog({d['id']}, 'Got it')])
                                              .then((r) => r.map((x) => x.status))""")
        assert results == ["fulfilled"] * 2
        assert await presses(run) == ["stub-reactions", "stub-reactions", "stub-view", "stub-view", sid]      # the openers and their closing, then the dialog
        await tidy(run)


async def test_a_dialog_drawn_while_a_popover_opens_is_not_taken_for_the_popovers_items():
    """Whatever is new on the page after an opener is pressed is taken for the popover's items; a dialog that the client draws in that
    moment is not one of them (its buttons would be, and the reaction would be refused, with the opener pressed twice). The reaction is
    sent, the picker is closed, and the dialog is left as it was, for the table."""
    async with PageRun(stub=client(popoverOpensAfterMs=400)) as run:
        await settled(run)
        sent = asyncio.ensure_future(run.page.evaluate("window.crystalMeet.control('reactions', 'clap')"))
        await asyncio.sleep(0.15)                                                                       # the picker is not there yet
        await show(run, "Recording consent", "This meeting is being recorded.", ["Leave Meeting", "Got it"])
        await asyncio.wait_for(sent, timeout=8)
        assert ["reaction", "clap"] in await run.page.evaluate("window.__zoomCalls")
        await tidy(run)
        [d] = await reported(run, "dialog")
        assert [x["id"] for x in await open_dialogs(run)] == [d["id"]] and await run.calls("dialog") == []
        assert await presses(run) == ["stub-reactions", "stub-reaction"]                                # the opener and the pick: nothing of the dialog's


async def test_a_button_of_a_dialog_is_never_taken_for_the_clients_own_view_button():
    """The View widget is not always drawn. A dialog's button that says "view" (a screen being shared: "View screen") must not be found
    for it: not offered as the layout control, not pressed by it, and not listed by the inventory as the widget's, wherever the client
    draws the dialog."""
    async with PageRun(stub=client()) as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => { const widget = document.querySelector('.full-screen-icon-widget'); widget.parentNode.removeChild(widget); }""")      # the stub's own node
        await show(run, "A screen is being shared", "Ann is sharing a screen.", ["Dismiss", "View screen"])
        await reported(run, "dialog")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert "layout" not in m["buttons"] and "hide_self" not in m["buttons"]
        with pytest.raises(Exception, match="layout button is not on the screen yet"):
            await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        assert await run.calls("dialog") == [] and await presses(run) == []
        inventory = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert not [b for b in inventory["buttons"] if b.get("where") == "view widget"] and inventory["popovers"]["view"]["found"] is False
        await run.page.evaluate("""() => {            // drawn inside the widget's own container, which the inventory finds by its class
            const widget = document.createElement("div"); widget.className = "full-screen-icon";
            widget.innerHTML = '<div role="dialog"><h3>Screen shared</h3><button>View</button></div>'; document.body.appendChild(widget);
        }""")
        await reported(run, "dialog", 2)
        inventory = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert not [b for b in inventory["buttons"] if b.get("where") == "view widget"]
        assert await run.calls("dialog") == [] and set(await presses(run)) == {"stub-reactions"}          # the inventory opened and closed the picker, and that is all


async def test_a_dialog_drawn_before_the_room_is_in_the_meeting_is_reported_once_it_is():
    """Nothing on the device follows the meeting before it is connected (what it hears then is let go), so what is open when the room
    comes in is said then, and what came and went before is not said at all."""
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.delayMs = 1500; window.ZoomMtg._behaviour.noToolbar = true;") as run:
        for _ in range(100):
            if await run.page.evaluate("window.crystalMeet.state") == "joining":
                break
            await asyncio.sleep(0.02)
        # Nothing of the client changes as the room comes in (no footer is drawn, and the page's status label is gone), which the
        # observer would see: only the look the page makes itself at that moment can say what is open.
        await run.page.evaluate("document.getElementById('crystal-meet-status').remove()")
        await show(run, "Recording consent", "This meeting is being recorded.", ["Got it", "Leave meeting"])
        passing = await show(run, "Passing", "Gone before the room is in.", ["OK"])
        await run.page.evaluate(f"window.ZoomMtg._closeDialog('{passing}')")
        await asyncio.sleep(0.4)
        assert await run.page.evaluate("window.crystalMeet.state") == "joining" and not [s for s, _ in run.events if s.startswith("dialog")]
        await run.wait_for("connected")
        [d] = await reported(run, "dialog")
        assert [s for s, _ in run.events].index("connected") < [s for s, _ in run.events].index("dialog") and d["title"] == "Recording consent"
        await asyncio.sleep(0.4)
        assert [s for s, _ in run.events if s.startswith("dialog")] == ["dialog"]                       # the one that came and went is not heard of


async def test_across_a_reconnect_a_dialog_still_open_is_not_said_again_and_one_that_went_is_said_closed():
    """While Zoom reconnects the page looks at nothing (the room is not in the meeting); when it is in again, one look says what went."""
    async with PageRun() as run:
        await run.wait_for("connected")
        first = await show(run, "Unmute?", "The host would like you to unmute.", ["Unmute", "Stay muted"])
        await show(run, "Breakout rooms", "Join Room 1?", ["Join", "Later"])
        a, b = (await reported(run, "dialog", 2))[:2]
        await run.page.evaluate("window.ZoomMtg._status(4)")                                            # Zoom is reconnecting ...
        await run.page.evaluate(f"window.ZoomMtg._closeDialog('{first}')")                              # ... and a dialog goes meanwhile
        await asyncio.sleep(0.4)
        assert not [s for s, _ in run.events if s == "dialog-closed"]
        await run.page.evaluate("window.ZoomMtg._status(2)")                                            # ... connected again
        assert (await reported(run, "dialog-closed"))[0] == {"id": a["id"]}
        await asyncio.sleep(0.3)
        assert [s for s, _ in run.events if s.startswith("dialog")] == ["dialog", "dialog", "dialog-closed"]       # the other was not said again
        assert [x["id"] for x in await open_dialogs(run)] == [b["id"]]


async def test_a_burst_of_changes_to_the_page_is_looked_at_once():
    """Zoom's client draws a great deal: the page is looked at after the changes, once, and not once for each of them."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await asyncio.sleep(0.3)
        looks = await run.page.evaluate("""async () => {
            let looks = 0;
            for (const proto of [Document.prototype, Element.prototype]) {
                const real = proto.querySelectorAll;
                proto.querySelectorAll = function (selector) { if (String(selector).includes("dialog")) looks += 1; return real.call(this, selector); };
            }
            for (let i = 0; i < 20; i++) { document.body.appendChild(document.createElement("div")); await null; }      // twenty changes, each seen by the observer
            await new Promise((resolve) => setTimeout(resolve, 400));
            return looks;
        }""")
        assert looks <= 2                                                                               # one (a stall of the machine's in the middle of the twenty could make it two), never twenty


async def test_chat_messages_come_as_zoom_6_5_0_sends_them():
    """{sender: <name>, timestamp: "10:32 AM", type: "text", content: {text, t}}: the words are content.text, a file's are its name
    (content.file.fileName), and the moment is content.t, as milliseconds or as ten digits of seconds. The payload's own timestamp is only a
    time of day in the client's language and is no moment ("10.32" read as a number is 10 ms of 1970); so is any number that is no date
    this century and any text that is not an ISO date (a lenient parser reads "Nov 5" as 2001), and a message with no moment is dated by
    its arrival."""
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const Z = window.ZoomMtg;
            Z._chat("Ann Lee", "Can you hear us?", 1792000000000);
            Z._chatFile("Cy Dunn", "agenda.pdf", 1792000060000);
            Z._fire("onReceiveChatMsg", { sender: "Di", timestamp: "10.32", type: "text", content: { text: "Seconds", t: "1792000120" } });
            Z._fire("onReceiveChatMsg", { sender: "Gus", timestamp: 1792000240000, type: "text", content: { text: "t comes first", t: 1792000180000 } });
            Z._fire("onReceiveChatMsg", { sender: "Hal", type: "file", content: { file: {}, t: 1792000300000 } });
            Z._fire("onReceiveChatMsg", { sender: "Ed", timestamp: "10.32", type: "text", content: { text: "A time of day only" } });
            Z._fire("onReceiveChatMsg", { sender: "Flo", timestamp: "10:32 AM", type: "text", content: { text: "Not a date", t: 10.32 } });
            Z._fire("onReceiveChatMsg", { sender: "Ivy", timestamp: "Nov 5", type: "text", content: { text: "A date with no year", t: "1032" } });
            Z._fire("onReceiveChatMsg", { sender: "Jo", timestamp: "10:34 AM", type: "text", content: { text: "Eleven digits", t: 99999999999 } });      // read as seconds: the year 5138
            Z._fire("onReceiveChatMsg", { sender: "Kai", timestamp: "10:35 AM", type: "text", content: { text: "A day and a half ahead", t: Date.now() + 36 * 3600 * 1000 } });
        }""")
        ahead = await run.page.evaluate("""() => { const t = Date.now() + 3600 * 1000; window.ZoomMtg._chat("Lou", "An hour ahead", t); return t; }""")        # a moment a little ahead of the clock is taken
        got = await reported(run, "chat", 11)
        assert [(c["from"], c["text"]) for c in got] == [("Ann Lee", "Can you hear us?"), ("Cy Dunn", "agenda.pdf"), ("Di", "Seconds"), ("Gus", "t comes first"),
                                                          ("Hal", ""), ("Ed", "A time of day only"), ("Flo", "Not a date"), ("Ivy", "A date with no year"),
                                                          ("Jo", "Eleven digits"), ("Kai", "A day and a half ahead"), ("Lou", "An hour ahead")]
        assert [c["at"] for c in got[:5]] == ["2026-10-14T17:46:40.000Z", "2026-10-14T17:47:40.000Z", "2026-10-14T17:48:40.000Z", "2026-10-14T17:49:40.000Z", "2026-10-14T17:51:40.000Z"]
        arrival = await run.page.evaluate("(ats) => ats.map((at) => Math.abs(Date.parse(at) - Date.now()) < 10000)", [c["at"] for c in got[5:10]])
        assert arrival == [True] * 5                                                                    # dated by its arrival
        assert got[10]["at"] == await run.page.evaluate("(t) => new Date(t).toISOString()", ahead)
        assert run.errors == []


async def test_chat_messages_from_other_builds_come_in_other_shapes():
    """The sender is a name or a record, the words and the time have more than one name (a field that is null is no field: content null
    falls through to message), and a time that is no time (not a date, too large for one) is the moment the message came."""
    async with PageRun() as run:
        await run.wait_for("connected")
        payloads = [{"senderName": "Raj Patel", "message": "Hello", "timestamp": 1700000000000}, {"sender": "Ann Lee", "text": "Hi", "time": "1700000060000"},
                    {"sender": {"userName": "Cy"}, "msg": "Yo"}, {"content": "Anonymous"}, {"content": "Late", "timestamp": 1e20},
                    {"sender": {"name": "Di"}, "content": "Dated", "timeStamp": "2023-11-14T22:15:20.000Z"}, {"sender": "Fay", "content": None, "message": "Fallback"}]
        for payload in payloads:
            await run.page.evaluate("(p) => window.ZoomMtg._fire('onReceiveChatMsg', p)", payload)
        got = await reported(run, "chat", len(payloads))
        assert [(c["from"], c["text"]) for c in got] == [("Raj Patel", "Hello"), ("Ann Lee", "Hi"), ("Cy", "Yo"), ("", "Anonymous"), ("", "Late"), ("Di", "Dated"), ("Fay", "Fallback")]
        assert [c["at"] for c in got[:2]] == ["2023-11-14T22:13:20.000Z", "2023-11-14T22:14:20.000Z"] and got[5]["at"] == "2023-11-14T22:15:20.000Z"
        assert all(c["at"].endswith("Z") and c["at"].startswith("20") for c in got[2:5])
        assert run.errors == []


# ---- Diagnostics for the first real meeting (spec 2026-10-13, section 7): the attendees' field names, the dialogs seen and the SDK version ----


async def test_diagnostics_name_the_attendees_fields_every_record_s_once_in_the_order_met():
    """The inventory pins Zoom's real field names: the attendees' as well as the current user's. A person in the waiting room may have a field the
    others lack, so every record is read, not only the first."""
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("""() => { const Z = window.ZoomMtg, record = Z._record;
            Z._record = function (a) { const r = record.call(this, a); if (a.userName === "Raj Patel") r.inWaitingRoom = true; return r; }; }""")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        keys = data["attendee_keys"]
        assert {"userId", "userName", "muted", "video", "raiseHand", "isHold", "isHost"} <= set(keys)
        assert keys[:3] == ["userId", "self", "userName"] and keys[-1] == "inWaitingRoom"         # the room's record first, a field only another record has last
        assert len(keys) == len(set(keys))                                                         # none twice
        assert "video" in data["user_keys"] and data["state"]["videoOn"] is True                    # the current user's, as before
        assert [(p["name"], p["waiting"]) for p in data["meeting"]["participants"]] == [("Room 1", False), ("Ann Lee", False), ("Raj Patel", False)]
        assert data["meeting"]["role"] == "participant" and data["meeting"]["me"]["muted"] is False   # the whole meeting as the bridge reads it


async def test_diagnostics_have_no_attendee_fields_while_zoom_lists_nobody():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.silentAttendees = true;") as run:
        await settled(run)
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert data["attendee_keys"] == []


async def test_diagnostics_say_which_sdk_version_the_page_loaded():
    async with PageRun({"sdkVersion": "6.9.9"}) as run:                                           # what the device asked for
        await settled(run)
        assert (await run.page.evaluate("window.crystalMeet.diagnostics()"))["sdk_version"] == "6.9.9"
        assert ["setZoomJSLib", "https://source.zoom.us/6.9.9/lib", "/av"] in await run.page.evaluate("window.__zoomCalls")      # the library path was built from it
    async with PageRun({"sdkVersion": None}) as run:                                              # nothing asked for: the page's own, from the site's meta tag
        await settled(run)
        assert (await run.page.evaluate("window.crystalMeet.diagnostics()"))["sdk_version"] == SDK_VERSION


async def test_diagnostics_list_the_dialogs_open_and_every_dialog_met_and_how_each_went():
    """What the inventory run reads back after a real meeting: the dialogs still on the screen, and every dialog the page met with its words, its buttons, when
    it was first seen, the button the page pressed on it (whoever asked: the device's acknowledgement or a tap on the table) and whether it has gone."""
    async with PageRun() as run:
        await settled(run)
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert data["dialogs"] == [] and data["dialogs_seen"] == []                                # nothing yet
        await show(run, "Recording consent", "This meeting is being recorded.", ["Leave Meeting", "Got it"])
        passing = await show(run, "Transcription", "This meeting is being transcribed.", ["OK"])
        await show(run, "Unmute?", "The host would like you to unmute.", ["Stay muted", "Unmute"])
        first, second, third = await reported(run, "dialog", 3)
        assert await run.page.evaluate(f"window.crystalMeet.answerDialog({third['id']}, ' UN-mute ')") is True     # the last of the three is the one pressed, and the client took it
        await run.page.evaluate(f"window.ZoomMtg._closeDialog('{passing}')")                                       # the middle one went by itself
        await reported(run, "dialog-closed", 2)
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert data["dialogs"] == [first]                                                           # the first is still waiting, as it was reported
        seen = data["dialogs_seen"]
        assert [{k: d[k] for k in ("id", "title", "text", "buttons")} for d in seen] == [first, second, third]
        assert [d["title"] for d in seen] == ["Recording consent", "Transcription", "Unmute?"] and seen[0]["buttons"] == ["Leave Meeting", "Got it"]
        assert [(d["pressed"], d["closed"]) for d in seen] == [(None, False), (None, True), ("Unmute", True)]    # waiting; went by itself; answered
        assert [d["label"] for d in seen] == ["Recording consent", "Transcription", "Unmute?"]                      # the aria-label of each one's root
        for d in seen:                                                                              # first seen now, as an ISO date
            assert abs((datetime.now(timezone.utc) - datetime.fromisoformat(d["at"].replace("Z", "+00:00"))).total_seconds()) < 60, d["at"]
        assert run.errors == []


async def test_diagnostics_give_the_label_of_a_modal_that_has_no_body_for_its_words():
    """A body-less modal has nothing to say but a developer's name for itself in its root's aria-label, which the dialog's words do not take for
    theirs (the label does not begin with the title): the inventory is where it is read."""
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("""() => {
            const d = document.createElement("div"); d.setAttribute("role", "dialog"); d.setAttribute("aria-label", "Mute Video Modal");
            d.innerHTML = '<button>Mute</button><button>Cancel</button>'; document.body.appendChild(d);
        }""")
        [d] = await reported(run, "dialog")
        assert (d["title"], d["text"], d["buttons"]) == ("", "", ["Mute", "Cancel"])
        [seen] = (await run.page.evaluate("window.crystalMeet.diagnostics()"))["dialogs_seen"]
        assert seen["label"] == "Mute Video Modal" and seen["text"] == ""


async def test_diagnostics_show_a_press_the_client_did_not_take_as_pressed_and_not_closed():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.dialogStays = true;") as run:
        await settled(run)
        await show(run, "Unmute?", "The host would like you to unmute.", ["Stay muted", "Unmute"])
        [d] = await reported(run, "dialog")
        with pytest.raises(Exception, match="did not close"):
            await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, 'Unmute')")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert [(x["id"], x["pressed"], x["closed"]) for x in data["dialogs_seen"]] == [(d["id"], "Unmute", False)] and [x["id"] for x in data["dialogs"]] == [d["id"]]


async def test_diagnostics_keep_the_last_fifty_dialogs_met():
    async with PageRun() as run:
        await settled(run)
        await run.page.evaluate("""() => { for (let i = 1; i <= 60; i++) window.ZoomMtg._showDialog("Dialog " + i, "Words " + i, ["OK"]); }""")
        await reported(run, "dialog", 60, timeout=10)
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        seen = data["dialogs_seen"]
        assert len(seen) == 50 and seen[0]["title"] == "Dialog 11" and seen[-1]["title"] == "Dialog 60"      # the newest are kept
        assert len(data["dialogs"]) == 60                                                                     # what is on the screen is not the record's to cap
