"""
The room page against the whole chain a tap on the table travels (the whole-branch review's demonstration): the room page in a browser, the control
service, the meeting service, the Zoom provider, the page's own bridge (meeting.js) and the stub SDK, in the headless Chromium the provider drives.
Nothing leaves the machine: the stub is Zoom, and every address is this machine's own.

What these pin is what the stubs of the other tests cannot: that a tap which arrives after the room has changed (the host muted it, the camera went
off at the TV) ends as the person asked, through every layer, and that a camera that is slow to switch holds nothing else on the table.
"""

import asyncio
import json
import threading
import time
import urllib.request

from croom.control.service import ControlService
from croom.core.config import Config
from croom.meeting.service import MeetingService
from tests.unit.control.test_page import CARD, browser, mute_and_camera_buttons, page_on_a_stopped_clock, poll_until  # noqa: F401 - the module's browser
from tests.unit.control.test_service import StubDevices
from tests.unit.meeting.test_zoom_sdk_provider import FakeApi, provider_for, shutdown
from tests.unit.meeting.zoom_stub import STUB_JS


class WholeChain:
    """The control service over the real meeting service and the real Zoom provider, which drives the stub SDK in its own browser, on an event loop
    of its own in a thread (the room page's browser is the test's, and synchronous). `behaviour` is script that runs in the stub's page first."""

    def __init__(self, behaviour=""):
        self.behaviour = behaviour
        self.port = None
        self.provider = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop = None
        self._failure = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except BaseException as e:  # noqa: BLE001 - said by __enter__, in the test's own thread
            self._failure = e
            self._ready.set()
        finally:
            self._loop.close()

    async def _main(self):
        self.provider = await provider_for(api=FakeApi(), stub=STUB_JS + self.behaviour)
        meeting = MeetingService(Config())
        meeting._providers["zoom"] = self.provider
        service = ControlService(config={"host": "127.0.0.1", "port": 0, "room_name": "Lab", "room_location": "2nd floor"},
                                 meeting=meeting, calendar=None, devices=StubDevices())
        await service.start()
        self.port = service.bound_port
        self._stop = asyncio.Event()
        self._ready.set()
        await self._stop.wait()
        await service.stop()
        await shutdown(self.provider)

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(60), "the chain did not start"
        if self._failure is not None:
            raise self._failure
        return self

    def __exit__(self, *exc):
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(30)

    def stub(self, script):
        """Run script in the stub Zoom's page (what Zoom, or someone at the TV, does), and give back what it answers."""
        return asyncio.run_coroutine_threadsafe(self.provider._page.evaluate(script), self._loop).result(15)

    def calls(self, name):
        """What the room asked Zoom's `name` call for, in order."""
        return [call[1] for call in self.stub("window.__zoomCalls") if call[0] == name]

    def meeting(self):
        """The meeting block of /api/status: what the agent says, whatever the room page has drawn."""
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=10) as answer:
            return json.load(answer)["meeting"]

    def wait_for(self, what, condition, seconds=10):
        for _ in range(int(seconds / 0.05)):
            if condition():
                return
            time.sleep(0.05)
        raise AssertionError(f"the agent never said: {what}")


def join_the_meeting(page):
    """Paste a meeting id and join, on a page whose timers are stopped: nothing polls by itself, so each wait asks for the status again."""
    page.fill("#link-input", "98765432100")
    page.click("#link-form button[type=submit]")
    for _ in range(600):
        page.evaluate("window.croomPage.refreshStatus()")
        meeting = page.evaluate("window.croomPage.model.status.meeting")
        if meeting["state"] == "connected" and (meeting["controls"] or {}).get("camera"):
            page.clock.run_for(600)                                                        # the screen's own moves at the join have settled: taps count again
            return
        page.wait_for_timeout(50)
    raise AssertionError("the room never joined")


def request_is_over(page, control):
    """Until the tap's request has been answered: the page holds the control (or the whole page, as it used to) while one is out."""
    page.wait_for_function("(control) => !window.croomPage.model.busy && !window.croomPage.model.pending.has(control)", arg=control, timeout=30000)


def test_a_tap_on_a_mute_that_is_a_poll_behind_leaves_the_room_the_host_muted_muted(browser):
    """The host mutes the room and someone at the table taps the Mute still on the page, before the next poll says so: the tap asks for muted, and
    the room ends muted. (With a toggle sent blind it ended live, the host's mute undone.)"""
    with WholeChain() as chain:
        page = page_on_a_stopped_clock(browser, chain)
        join_the_meeting(page)
        mute, camera = mute_and_camera_buttons(page)
        assert mute.inner_text() == "Mute" and chain.meeting()["muted"] is False
        chain.stub("window.ZoomMtg._setMuted(true)")                                       # the host mutes the room
        chain.wait_for("the room muted", lambda: chain.meeting()["muted"] is True)
        assert mute.inner_text() == "Mute"                                                 # the page's timers are stopped: it has not heard
        mute.click()
        request_is_over(page, "mute")
        assert chain.stub("window.ZoomMtg._muted") is True and chain.meeting()["muted"] is True          # the host's mute stands
        assert chain.calls("mute") == [{"userId": 16777216, "mute": True}]                  # Zoom was told muted; nothing the other way went out
        assert mute.inner_text() == "Unmute"                                                # the answer brought the page up to date
        poll_until(page, "document.querySelector('#meeting-actions .button').textContent.trim() === 'Unmute'")   # and the next poll agrees
        chain.stub("window.ZoomMtg._later(1000)")                                           # (Zoom takes one mute a second)
        page.click("#meeting-actions .button:has-text('Unmute')")                           # a tap on what the page shows now is the ordinary Unmute
        page.wait_for_function("document.querySelector('#meeting-actions .button').textContent.trim() === 'Mute'", timeout=15000)
        assert chain.stub("window.ZoomMtg._muted") is False
        page.close()


def test_a_tap_on_a_camera_button_that_is_a_poll_behind_leaves_the_camera_off_that_went_off_at_the_tv(browser):
    with WholeChain() as chain:
        page = page_on_a_stopped_clock(browser, chain)
        join_the_meeting(page)
        mute, camera = mute_and_camera_buttons(page)
        assert camera.inner_text() == "Turn camera off"
        chain.stub("document.getElementById('stub-video').click()")                        # someone at the TV turns the camera off
        chain.wait_for("the camera off", lambda: chain.meeting()["camera_on"] is False)
        assert camera.inner_text() == "Turn camera off"                                    # the page has not heard
        camera.click()
        request_is_over(page, "camera")
        assert chain.stub("window.__videoClicks") == 1                                      # the TV's own click: the table pressed nothing
        assert chain.stub("document.getElementById('stub-video').getAttribute('aria-label')") == "start my video" and chain.meeting()["camera_on"] is False
        assert camera.inner_text() == "Turn camera on"                                      # the answer brought the page up to date
        page.close()


def test_a_dialog_is_answered_from_the_table_while_the_camera_is_still_switching(browser):
    """Zoom takes five seconds to switch the camera. The tap's request is out all that while, and the card of a dialog that Zoom draws meanwhile is
    answered, and closes, with the camera's button the only one that waits."""
    with WholeChain() as chain:
        page = page_on_a_stopped_clock(browser, chain)
        join_the_meeting(page)
        mute, camera = mute_and_camera_buttons(page)
        chain.stub("window.ZoomMtg._behaviour.videoStartsAfterMs = 5000")                  # from here on a press takes five seconds to switch
        camera.click()
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button')[1].disabled", timeout=5000)
        chain.stub("window.ZoomMtg._showDialog('Recording consent', 'This meeting is being recorded.', ['Leave Meeting', 'Got it'])")
        poll_until(page, CARD)
        page.clock.run_for(600)                                                            # the card's half second of ignoring a tap
        assert camera.is_disabled() and mute.is_enabled() and page.locator("#dialog-actions .button:disabled").count() == 0
        assert page.evaluate("window.croomPage.model.busy") is False
        page.locator("#dialog-actions .button", has_text="Got it").click()
        page.wait_for_function("document.querySelector('#meeting-dialog').hidden", timeout=10000)
        assert chain.stub("window.__zoomCalls").count(["dialog", "Recording consent", "Got it"]) == 1
        assert chain.stub("window.ZoomMtg._videoOn") is True and camera.is_disabled()      # the camera had not switched yet: the card was answered meanwhile
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button')[1].textContent.trim() === 'Turn camera on'", timeout=20000)
        assert camera.is_enabled() and chain.stub("window.ZoomMtg._videoOn") is False
        page.close()
