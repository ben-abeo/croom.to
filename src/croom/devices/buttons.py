"""
The speakerphone's own buttons and mute light: the Rally Plus's mic pods, table hub and remote, or the MeetUp's. Its mute key mutes and
unmutes the call, its volume keys step the room's volume as Quieter and Louder do, and its mute light shows the call's mute (the
devices service tells it). They reach the Pi as USB input devices (HID: KEY_MICMUTE, KEY_VOLUMEUP, KEY_VOLUMEDOWN and the mute LED),
which the agent reads and writes as a member of the input group. evdev is imported only where the devices are looked for, so the
tests run on fakes.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# linux/input-event-codes.h, as evdev.ecodes names them
EV_KEY, EV_LED = 1, 17
KEY_VOLUMEDOWN, KEY_VOLUMEUP, KEY_MICMUTE = 114, 115, 248
LED_MUTE = 7
PRESSED = 1                 # a key's value: 1 down, 0 up, 2 held (the Rally's keys do not repeat)
VOLUME_STEP = 5             # as the page's Quieter and Louder
KEYS = {KEY_MICMUTE, KEY_VOLUMEUP, KEY_VOLUMEDOWN}


def usb_devices() -> List[Any]:
    """The input devices on a USB port (phys "usb-..."; not the room's own virtual mouse and keyboard, nor the TV's HDMI remote) that
    have a mute or volume key or a mute light, open. The others are closed again."""
    import evdev
    found = []
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
        except OSError:
            continue
        capabilities = device.capabilities()
        if (device.phys or "").startswith("usb-") and (KEYS & set(capabilities.get(EV_KEY, []))
                                                       or LED_MUTE in capabilities.get(EV_LED, [])):
            found.append(device)
        else:
            device.close()
    return found


class RoomButtons:
    RESCAN_S = 10.0     # how often to look again while none is found, or after one has gone (unplugged, lent to a laptop)

    def __init__(self, on_mute: Callable[[], Awaitable[Any]], on_volume: Callable[[int], Awaitable[Any]],
                 finder: Callable[[], List[Any]] = usb_devices, sleep=asyncio.sleep):
        self._on_mute, self._on_volume = on_mute, on_volume
        self._finder, self._sleep = finder, sleep
        self._devices: List[Any] = []
        self._light: Optional[bool] = None      # what the mute light was last told; None: tell it at the next set_light
        self._task: Optional[asyncio.Task] = None

    @property
    def found(self) -> List[str]:
        return [device.name for device in self._devices]

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task})
        self._close()

    def set_light(self, muted: bool) -> None:
        """The mute light on (the call is muted) or off, on every device that has one; written only when it changes."""
        if muted == self._light:
            return
        for device in self._devices:
            if LED_MUTE in device.capabilities().get(EV_LED, []):
                try:
                    device.set_led(LED_MUTE, 1 if muted else 0)
                except OSError as e:
                    logger.debug(f"Could not set the mute light on {device.name}: {e}")
        self._light = muted if self._devices else None

    def _close(self) -> None:
        for device in self._devices:
            try:
                device.close()
            except OSError:
                pass
        self._devices = []

    async def _run(self) -> None:
        said: Optional[str] = None
        while True:
            try:
                self._devices = await asyncio.to_thread(self._finder)
            except Exception as e:  # noqa: BLE001 - no evdev, no /dev/input: no buttons, said once
                if said != str(e):
                    logger.warning(f"Could not look for the speakerphone's buttons: {e}")
                    said = str(e)
                self._devices = []
            if self._devices:
                logger.info(f"Speakerphone buttons: {', '.join(sorted(set(self.found)))}")
                said = None
                self._light = None                       # found again: tell the light again
                readers = [asyncio.create_task(self._read(device)) for device in self._devices]
                try:
                    await asyncio.wait(readers, return_when=asyncio.FIRST_COMPLETED)   # one gone: look for them all again
                finally:
                    for reader in readers:
                        reader.cancel()
                    await asyncio.wait(readers)
                    self._close()
                logger.info("Speakerphone buttons gone: looking for them again")
            await self._sleep(self.RESCAN_S)

    async def _read(self, device: Any) -> None:
        try:
            async for event in device.async_read_loop():
                if event.type == EV_KEY and event.value == PRESSED and event.code in KEYS:
                    await self._pressed(event.code)
        except OSError as e:
            logger.debug(f"{device.name} stopped: {e}")

    async def _pressed(self, code: int) -> None:
        try:
            if code == KEY_MICMUTE:
                await self._on_mute()
            else:
                await self._on_volume(VOLUME_STEP if code == KEY_VOLUMEUP else -VOLUME_STEP)
        except Exception:  # noqa: BLE001 - a key that could not be carried out leaves the buttons working
            logger.exception("A speakerphone button could not be carried out")
