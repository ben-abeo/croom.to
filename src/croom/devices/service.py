"""
The devices service (spec 2026-10-08 sound and camera, section 4.1): owns the room
speaker's volume and the camera's framing, starts them, homes the camera once a
home has been saved, and keeps looking for a device that is missing.
"""

import asyncio
import contextlib
import logging
from typing import Optional

from croom.core.service import Service
from croom.devices.camera import RoomCamera
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.devices.volume import RoomVolume

logger = logging.getLogger(__name__)


class DevicesService(Service):
    REDISCOVER_S = 30.0

    def __init__(self, volume: RoomVolume, camera: RoomCamera):
        super().__init__("devices")
        self.volume = volume
        self.camera = camera
        # Homing drives the camera into its stops and back: up to twice its travel time (16 s by default). The
        # manager starts the services one after another, so homing runs in the background and the room page is
        # not held back by it at every boot and restart. stop() and the tests wait for it through this task.
        self.homing: Optional[asyncio.Task] = None
        self._task: Optional[asyncio.Task] = None

    @classmethod
    def from_config(cls, config, store) -> "DevicesService":
        return cls(RoomVolume.from_config(config), RoomCamera.from_config(config, store))

    async def start(self) -> None:
        await self.volume.refresh(force=True)
        camera_found = await self.camera.discover()
        speaker, camera = self.volume.state(), self.camera.state()
        logger.info(f"Devices found: speaker {speaker['device'] if speaker['available'] else 'none'}, "
                    f"camera {camera['device'] if camera['available'] else 'none'}")
        will_home = camera_found and self.camera.home_saved
        self.homing = asyncio.create_task(self._home_at_start()) if will_home else None
        self._task = asyncio.create_task(self._rediscover())
        logger.info("Devices service started")

    async def _home_at_start(self) -> None:
        """Take the camera to its saved home. Nothing escapes: stop() awaits this task, and a camera that
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
            try:
                if not self.camera.available:
                    await self.camera.discover()
                if not self.volume.available:
                    await self.volume.refresh(force=True)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Device check failed: {e}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Closing the camera cuts a homing that is still under way short and waits for it to stop the motors
        await self.camera.close()
        if self.homing is not None:
            await self.homing
        logger.info("Devices service stopped")
