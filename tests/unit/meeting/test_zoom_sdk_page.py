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
        assert data["buttons"][1] == {"label": "More 0"}                 # the text is left out when it says the same
        assert len(data["buttons"]) == 40
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
