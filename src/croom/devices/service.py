"""
The devices service (spec 2026-10-08 sound and camera, section 4.1): owns the room
speaker's volume and the camera's framing, starts them, homes the camera once a
home has been saved, and keeps looking for a device that is missing. It also owns the
microphone's level (spec 2026-10-29, Mic level), which it puts back to the level the
room chose when a meeting joins and every few seconds while one is connected. And it
listens to the speakerphone's own buttons (buttons.py): the mute key mutes the call,
the volume keys step the room's volume, and the mute light shows the call's mute.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional

from croom.core.service import Service
from croom.devices.autoframe import AutoFramer
from croom.devices.buttons import RoomButtons, usb_devices
from croom.devices.camera import RoomCamera
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.devices.mic import RoomMic
from croom.devices.volume import RoomVolume
from croom.meeting.providers.base import MeetingControlError, MeetingState

logger = logging.getLogger(__name__)


class DevicesService(Service):
    REDISCOVER_S = 30.0
    RECLAIM_S = 15.0        # the MeetUp takes a few seconds to come back as a camera and a speaker after the switch moves it
    RECLAIM_POLL_S = 0.5
    MIC_REAPPLY_S = 5.0     # how often the chosen microphone level is put back while a meeting is connected
    LIGHT_S = 1.0           # how often the speakerphone's mute light is brought into line with the call's mute

    def __init__(self, volume: RoomVolume, camera: RoomCamera, mic: Optional[RoomMic] = None, meeting=None,
                 buttons_finder: Optional[Callable[[], List[Any]]] = None, auto_framing: Any = "off"):
        super().__init__("devices")
        self.volume = volume
        self.camera = camera
        self.mic = mic          # None: a room (or a test) without the microphone's level
        self._meeting = meeting  # the meeting service, whose state says when the microphone's level is kept
        self._callback_registered = False
        self._mic_task: Optional[asyncio.Task] = None
        self._running = False   # between start() and stop(): a meeting's state outside that starts nothing
        # Homing drives the camera into its stops and back: up to twice its travel time (16 s by default). The
        # manager starts the services one after another, so homing runs in the background and the room page is
        # not held back by it at every boot and restart. stop() and the tests wait for it through this task.
        self.homing: Optional[asyncio.Task] = None
        self._called = False    # a call has connected since the camera was last sent home (_on_meeting_state)
        self._rediscover_task: Optional[asyncio.Task] = None
        self._last_failure: Optional[str] = None
        self._away = False   # the MeetUp is lent to a laptop (spec 2026-10-27): the background check does not look for it
        # Laptop mode as the camera's live picture sees it: from the start of release() until reclaim() has looked for the MeetUp again
        self._lent = False
        self._lent_callbacks: List[Callable[[bool], None]] = []
        # The speakerphone's buttons, looked for with buttons_finder (None: a room, or a test, without them)
        self.buttons = RoomButtons(self._mute_pressed, self._volume_pressed, finder=buttons_finder) if buttons_finder else None
        self._light_task: Optional[asyncio.Task] = None
        # Auto framing (autoframe.py): Frame people by itself during calls, with the room's setting on; it needs the meeting
        self.autoframer = AutoFramer(camera, meeting, mode=auto_framing) if meeting is not None else None

    @classmethod
    def from_config(cls, config, store, meeting=None) -> "DevicesService":
        return cls(RoomVolume.from_config(config), RoomCamera.from_config(config, store), RoomMic.from_config(config, store), meeting,
                   buttons_finder=usb_devices, auto_framing=getattr(config.control, "camera_auto_framing", "off"))

    async def start(self) -> None:
        await self._check(self.volume.refresh, force=True)
        await self._check(self.camera.discover)
        speaker_state, camera_state = self.volume.state(), self.camera.state()
        found = (f"Devices found: speaker {speaker_state['device'] if speaker_state['available'] else 'none'}, "
                 f"camera {camera_state['device'] if camera_state['available'] else 'none'}")
        if self.mic is not None:
            await self._check(self.mic.refresh, force=True)
            mic_state = self.mic.state()
            found += f", microphone {mic_state['device'] if mic_state['available'] else 'none'}"
        logger.info(found)
        self._running = True
        if self._meeting is not None and not self._callback_registered:
            self._meeting.add_state_callback(self._on_meeting_state)
            self._callback_registered = True
        will_home = self.camera.available and self.camera.home_saved
        self.homing = asyncio.create_task(self._home_at_start()) if will_home else None
        self._rediscover_task = asyncio.create_task(self._rediscover())
        if self.buttons is not None:
            self.buttons.start()
            self._light_task = asyncio.create_task(self._keep_light())
        if self.autoframer is not None:
            self.autoframer.start()
        logger.info("Devices service started")

    async def _check(self, look: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> None:
        """One look for a device, at start or in the background check.

        A device that is missing never gets here: discover() and refresh() turn that into an unavailable state
        and warn about it themselves. So whatever is caught here is a bug, in this code or in a device module,
        and it must not take the service down: a service that fails to start makes the manager stop every
        service, the room page included. It is warned about with its traceback, once per distinct message, so a
        bug that repeats every round does not fill the journal.
        """
        try:
            await look(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            message = f"Device check failed: {type(e).__name__}: {e}"
            if message != self._last_failure:
                self._last_failure = message
                logger.warning(message, exc_info=True)

    async def _home_at_start(self, when: str = "at start") -> None:
        """Take the camera to its saved home. Nothing escapes: stop() waits for this task, and a camera that
        will not home is still usable by hand."""
        try:
            await self.camera.home(by_person=False)      # the room's own, not a person taking the camera over
            logger.info(f"Camera homed {when}")
        except Interrupted:
            logger.info(f"Camera homing {when} was interrupted by a request on the page, or by shutdown")
        except (NotReady, DeviceUnavailable) as e:
            logger.warning(f"Could not home the camera {when}: {e}")
        except Exception:  # noqa: BLE001 - outside the camera's contract, but nothing may escape this task
            logger.exception(f"Could not home the camera {when}")

    def _home_again(self, when: str) -> None:
        """Send a camera that says where it points (the Rally) back to its home when a call is over, or when it has come back after it
        went (unplugged, or restarted by the Rally system, which wakes it at Logitech's own default): the room wants it at Home when
        nobody uses it (Ben, 2026-11-05). Not without a home, while the camera is lent to a laptop, while a homing runs, or outside
        start() and stop(). The MeetUp is left as it was: it homes by driving into its stops."""
        if not (self._running and not self._away and self.camera.available and self.camera.says_where and self.camera.home_saved):
            return
        if self.homing is not None and not self.homing.done():
            return
        try:
            self.homing = asyncio.get_running_loop().create_task(self._home_at_start(when))
        except RuntimeError:     # no loop running: a callback from outside the agent's loop has nothing to start
            logger.debug(f"The camera was not homed {when}: no event loop")

    async def _rediscover(self) -> None:
        while True:
            await asyncio.sleep(self.REDISCOVER_S)
            if self._away:
                continue
            if not self.camera.available:
                await self._check(self.camera.discover)
                self._home_again("now that it is back")
            if not self.volume.available:
                await self._check(self.volume.refresh, force=True)
            if self.mic is not None and not self.mic.available:
                await self._check(self.mic.refresh, force=True)

    # ------------------------------------------------------------------
    # The speakerphone's buttons and mute light
    # ------------------------------------------------------------------

    def _in_call(self) -> bool:
        return self._meeting is not None and self._meeting.state == MeetingState.CONNECTED

    def _call_muted(self) -> bool:
        current = self._meeting.current_meeting if self._in_call() else None
        return bool(current is not None and current.is_muted)

    async def _mute_pressed(self) -> None:
        """The mute key: the call's microphone muted or unmuted, as the Mute tile does. Outside a call there is nothing to mute."""
        if self._away or not self._in_call():
            logger.info("The speakerphone's mute key was pressed outside a call: nothing to mute")
            return
        try:
            muted = await self._meeting.toggle_mute()
        except MeetingControlError as e:
            logger.info(f"The mute key did not change the call's microphone: {e}")
            return
        logger.info(f"The speakerphone's mute key {'muted' if muted else 'unmuted'} the call")
        if self.buttons is not None:
            self.buttons.set_light(bool(muted))

    async def _volume_pressed(self, delta: int) -> None:
        """A volume key: the room's volume a step quieter or louder, as Quieter and Louder do."""
        if self._away:
            return
        try:
            await self.volume.step(delta)
        except (DeviceUnavailable, NotReady) as e:
            logger.info(f"The volume key did not change the volume: {e}")

    async def _keep_light(self) -> None:
        """Every LIGHT_S: the mute light on while the call is muted (from the Mute tile, the call's own page or the key), off otherwise.
        Nothing escapes: a bug is warned about once per message."""
        while True:
            if not self._away:
                await self._check(self._set_light)
            await asyncio.sleep(self.LIGHT_S)

    async def _set_light(self) -> None:
        self.buttons.set_light(self._call_muted())

    # ------------------------------------------------------------------
    # The microphone's level during a meeting (spec 2026-10-29, Mic level)
    # ------------------------------------------------------------------

    def _on_meeting_state(self, state: MeetingState) -> None:
        """The meeting service's state callback: a meeting that is connected keeps the chosen microphone level, from its first moment
        and every MIC_REAPPLY_S; anything else (leaving, over, failed, back in a lobby) ends that. A call that was connected and is
        over sends a camera that says where it points back to its home (_home_again)."""
        if state == MeetingState.CONNECTED:
            self._called = True
        elif state == MeetingState.IDLE and self._called:
            self._called = False
            self._home_again("after the call")
        if self.mic is None:
            return
        if state == MeetingState.CONNECTED and self._running:
            if self._mic_task is None or self._mic_task.done():
                try:
                    self._mic_task = asyncio.get_running_loop().create_task(self._keep_mic_level())
                except RuntimeError:     # no loop running: a callback from outside the agent's loop has nothing to start
                    logger.debug("Meeting connected outside the event loop: the microphone level is not kept")
        elif self._mic_task is not None:
            self._mic_task.cancel()
            self._mic_task = None

    async def _keep_mic_level(self) -> None:
        """Put the chosen level back now and every MIC_REAPPLY_S, until cancelled. Not while the MeetUp is lent to a laptop: the room
        does not touch it then. Nothing escapes: _check warns about a bug once per message."""
        while True:
            if not self._away:
                await self._check(self.mic.reapply)
            await asyncio.sleep(self.MIC_REAPPLY_S)

    @property
    def lent(self) -> bool:
        """Whether the MeetUp is lent to a laptop: from the start of release() until reclaim() has looked for it again."""
        return self._lent

    def add_lent_callback(self, callback: Callable[[bool], None]) -> None:
        """`callback(lent)` when Laptop mode starts (before the camera is closed and the switch moves) and when it ends."""
        self._lent_callbacks.append(callback)

    def _set_lent(self, lent: bool) -> None:
        if lent == self._lent:
            return
        self._lent = lent
        for callback in list(self._lent_callbacks):
            try:
                callback(lent)
            except Exception:  # noqa: BLE001 - a listener's fault must not stop the switch
                logger.exception("A Laptop mode listener failed")

    async def release(self) -> None:
        """The MeetUp is going to a laptop (spec 2026-10-27 Laptop mode): the preview and the camera's live picture end, the camera is
        closed (a homing under way is cut short), and the background check leaves the camera, the speaker and the microphone alone until
        reclaim()."""
        self._away = True
        self._set_lent(True)
        self.camera.set_preview(False)
        await self.camera.close()
        self.camera.forget_position()

    async def reclaim(self, home: bool = True) -> None:
        """The MeetUp is back: look for it now, not at the next background check, giving it RECLAIM_S to reappear as a camera, then
        (when `home`) home the camera, which the laptop may have moved. Not in a meeting: the camera is in use."""
        self._away = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.RECLAIM_S
        try:
            while True:
                await self._check(self.camera.discover)
                await self._check(self.volume.refresh, force=True)
                if self.mic is not None:
                    await self._check(self.mic.refresh, force=True)
                if self.camera.available or loop.time() >= deadline:
                    break
                await asyncio.sleep(self.RECLAIM_POLL_S)
        finally:
            self._set_lent(False)   # the camera's picture may start again: the MeetUp is back (or as back as it will be)
        if home and self.camera.available and self.camera.home_saved and (self.homing is None or self.homing.done()):
            self.homing = asyncio.create_task(self._home_at_start())

    async def stop(self) -> None:
        self._running = False
        for name in ("_rediscover_task", "_mic_task", "_light_task"):
            task = getattr(self, name)
            setattr(self, name, None)
            if task is not None:
                task.cancel()
                # asyncio.wait, not await or suppress(): it waits for the task to finish without raising the task's
                # own cancellation here, and a cancel aimed at stop() itself still gets through.
                await asyncio.wait({task})
        if self.buttons is not None:
            await self.buttons.stop()
        if self.autoframer is not None:
            await self.autoframer.stop()
        # Closing the camera cuts a homing that is still under way short and waits for it to stop the motors
        await self.camera.close()
        if self.homing is not None:
            await asyncio.wait({self.homing})    # also when something else has cancelled it
        logger.info("Devices service stopped")
