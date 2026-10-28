"""
The devices service (spec 2026-10-08 sound and camera, section 4.1): owns the room
speaker's volume and the camera's framing, starts them, homes the camera once a
home has been saved, and keeps looking for a device that is missing.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from croom.core.service import Service
from croom.devices.camera import RoomCamera
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.devices.volume import RoomVolume

logger = logging.getLogger(__name__)


class DevicesService(Service):
    REDISCOVER_S = 30.0
    RECLAIM_S = 15.0        # the MeetUp takes a few seconds to come back as a camera and a speaker after the switch moves it
    RECLAIM_POLL_S = 0.5

    def __init__(self, volume: RoomVolume, camera: RoomCamera):
        super().__init__("devices")
        self.volume = volume
        self.camera = camera
        # Homing drives the camera into its stops and back: up to twice its travel time (16 s by default). The
        # manager starts the services one after another, so homing runs in the background and the room page is
        # not held back by it at every boot and restart. stop() and the tests wait for it through this task.
        self.homing: Optional[asyncio.Task] = None
        self._rediscover_task: Optional[asyncio.Task] = None
        self._last_failure: Optional[str] = None
        self._away = False   # the MeetUp is lent to a laptop (spec 2026-10-27): the background check does not look for it

    @classmethod
    def from_config(cls, config, store) -> "DevicesService":
        return cls(RoomVolume.from_config(config), RoomCamera.from_config(config, store))

    async def start(self) -> None:
        await self._check(self.volume.refresh, force=True)
        await self._check(self.camera.discover)
        speaker_state, camera_state = self.volume.state(), self.camera.state()
        logger.info(f"Devices found: speaker {speaker_state['device'] if speaker_state['available'] else 'none'}, "
                    f"camera {camera_state['device'] if camera_state['available'] else 'none'}")
        will_home = self.camera.available and self.camera.home_saved
        self.homing = asyncio.create_task(self._home_at_start()) if will_home else None
        self._rediscover_task = asyncio.create_task(self._rediscover())
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

    async def _home_at_start(self) -> None:
        """Take the camera to its saved home. Nothing escapes: stop() waits for this task, and a camera that
        will not home is still usable by hand."""
        try:
            await self.camera.home()
            logger.info("Camera homed at start")
        except Interrupted:
            logger.info("Camera homing at start was interrupted by a request on the page, or by shutdown")
        except (NotReady, DeviceUnavailable) as e:
            logger.warning(f"Could not home the camera at start: {e}")
        except Exception:  # noqa: BLE001 - outside the camera's contract, but nothing may escape this task
            logger.exception("Could not home the camera at start")

    async def _rediscover(self) -> None:
        while True:
            await asyncio.sleep(self.REDISCOVER_S)
            if self._away:
                continue
            if not self.camera.available:
                await self._check(self.camera.discover)
            if not self.volume.available:
                await self._check(self.volume.refresh, force=True)

    async def release(self) -> None:
        """The MeetUp is going to a laptop (spec 2026-10-27 Laptop mode): the preview ends, the camera is closed (a homing under way
        is cut short), and the background check leaves the camera and the speaker alone until reclaim()."""
        self._away = True
        self.camera.set_preview(False)
        await self.camera.close()

    async def reclaim(self) -> None:
        """The MeetUp is back: look for it now, not at the next background check, giving it RECLAIM_S to reappear as a camera, then
        home the camera, which the laptop may have moved."""
        self._away = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.RECLAIM_S
        while True:
            await self._check(self.camera.discover)
            await self._check(self.volume.refresh, force=True)
            if self.camera.available or loop.time() >= deadline:
                break
            await asyncio.sleep(self.RECLAIM_POLL_S)
        if self.camera.available and self.camera.home_saved and (self.homing is None or self.homing.done()):
            self.homing = asyncio.create_task(self._home_at_start())

    async def stop(self) -> None:
        task, self._rediscover_task = self._rediscover_task, None
        if task is not None:
            task.cancel()
            # asyncio.wait, not await or suppress(): it waits for the task to finish without raising the task's
            # own cancellation here, and a cancel aimed at stop() itself still gets through.
            await asyncio.wait({task})
        # Closing the camera cuts a homing that is still under way short and waits for it to stop the motors
        await self.camera.close()
        if self.homing is not None:
            await asyncio.wait({self.homing})    # also when something else has cancelled it
        logger.info("Devices service stopped")
