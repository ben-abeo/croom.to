"""
The SDK page and its crystalMeet bridge, driven in the venv's Chromium against
the stub ZoomMtg with Zoom's CDN blocked (spec 2026-09-25 Zoom, section 4.5).
"""

import asyncio
import json
import time

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


async def test_diagnostics_list_the_footers_buttons_only():
    async with PageRun(stub=CLIENT_LABELS) as run:
        await run.wait_for("connected")
        await run.page.evaluate(DECOYS)
        await run.page.evaluate("""() => { const bar = document.getElementById('foot-bar');
            for (let i = 0; i < 50; i++) { const b = document.createElement('button');
                b.setAttribute('aria-label', 'More ' + i); b.textContent = 'More ' + i; bar.appendChild(b); } }""")
        data = await run.page.evaluate("window.crystalMeet.diagnostics()")
        assert data["buttons"][0] == {"label": "stop my video", "text": "Video"}
        assert data["buttons"][1] == {"label": "Raise Hand"}             # the text is left out when it says the same
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


async def test_a_reconnect_does_not_mute_the_room_again():
    """The join's mic-off default is armed once per join: when Zoom reconnects, an unmute the table made since stands."""
    async with PageRun({"micOn": False}) as run:
        await settled(run)
        assert [c[1]["mute"] for c in await run.calls("mute")] == [True]                          # the join's default
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


async def test_the_view_button_is_not_mistaken_for_the_self_view_button():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("""() => {
            const bar = document.getElementById("foot-bar");
            bar.insertBefore(document.getElementById("stub-self"), document.getElementById("stub-view"));     // self view comes first
        }""")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"]["layout"] == {"value": "speaker"} and m["buttons"]["hide_self"] == {"on": False}
        await run.page.evaluate("document.getElementById('stub-view').remove()")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert "layout" not in m["buttons"] and m["buttons"]["hide_self"] == {"on": False}     # "Hide Self View" is no view switcher


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
