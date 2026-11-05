"""
The speakerphone's buttons (the Rally Plus's: event6 has the volume keys, event7 the mute key and the mute light, as /proc/bus/input showed
them in the Boardroom on 2026-11-04): the mute key toggles the call's microphone, the volume keys step the room's volume, the light
follows the call's mute, and a device that goes away is looked for again. evdev is not needed: the devices are fakes.
"""

import asyncio
import sys
import types
from dataclasses import dataclass

from croom.devices import buttons
from croom.devices.buttons import EV_KEY, EV_LED, KEY_MICMUTE, KEY_VOLUMEDOWN, KEY_VOLUMEUP, LED_MUTE, RoomButtons
from croom.devices.service import DevicesService
from croom.meeting.providers.base import MeetingState
from tests.unit.devices.test_service import service_for, until


@dataclass
class Event:
    type: int
    code: int
    value: int


class FakeDevice:
    def __init__(self, name, keys=(), led=False, phys="usb-xhci-hcd.0-1.1.1.1/input3"):
        self.name, self.phys = name, phys
        self._capabilities = {EV_KEY: list(keys)}
        if led:
            self._capabilities[EV_LED] = [LED_MUTE]
        self.events: asyncio.Queue = asyncio.Queue()
        self.leds, self.closed = [], False

    def capabilities(self):
        return self._capabilities

    async def async_read_loop(self):
        while True:
            event = await self.events.get()
            if event is None:
                raise OSError(19, "No such device")       # unplugged, or lent to a laptop
            yield event

    def press(self, code):
        for value in (1, 0):                              # down and up, as a key reports them
            self.events.put_nowait(Event(EV_KEY, code, value))

    def set_led(self, led, value):
        self.leds.append((led, value))

    def close(self):
        self.closed = True


def rally():
    return [FakeDevice("Logitech Logi Rally Audio Consumer Control", keys=(KEY_VOLUMEDOWN, KEY_VOLUMEUP)),
            FakeDevice("Logitech Logi Rally Audio", keys=(KEY_MICMUTE,), led=True)]


class FakeMeeting:
    def __init__(self, connected=True):
        self.state = MeetingState.CONNECTED if connected else MeetingState.IDLE
        self.current_meeting = types.SimpleNamespace(is_muted=False)
        self.toggles = 0

    def add_state_callback(self, callback):
        pass

    async def toggle_mute(self):
        self.toggles += 1
        self.current_meeting.is_muted = not self.current_meeting.is_muted
        return self.current_meeting.is_muted


def room(tmp_path, devices, meeting):
    service, _, runner = service_for(tmp_path)
    service = DevicesService(service.volume, service.camera, meeting=meeting, buttons_finder=lambda: devices)
    service.LIGHT_S = 0.01
    return service, runner


async def test_the_mute_key_mutes_the_call_and_the_light_follows_the_call(tmp_path):
    volume_keys, mute_key = devices = rally()
    meeting = FakeMeeting()
    service, _ = room(tmp_path, devices, meeting)
    await service.start()
    try:
        await until(lambda: service.buttons.found)
        mute_key.press(KEY_MICMUTE)
        await until(lambda: meeting.toggles == 1)
        await until(lambda: mute_key.leds and mute_key.leds[-1] == (LED_MUTE, 1))
        meeting.current_meeting.is_muted = False          # unmuted from the table's Mute tile, or the call's own page
        await until(lambda: mute_key.leds[-1] == (LED_MUTE, 0))
        assert volume_keys.leds == []                     # the volume keys' device has no light
        meeting.state = MeetingState.IDLE                 # the call is over: the key has nothing to mute, and the light stays off
        mute_key.press(KEY_MICMUTE)
        await asyncio.sleep(0.05)
        assert meeting.toggles == 1 and mute_key.leds[-1] == (LED_MUTE, 0)
    finally:
        await service.stop()
    assert all(device.closed for device in devices)


async def test_the_volume_keys_step_the_room_volume_as_quieter_and_louder_do(tmp_path):
    volume_keys, _ = devices = rally()
    service, _ = room(tmp_path, devices, FakeMeeting())
    steps = []

    async def step(delta):
        steps.append(delta)

    service.volume.step = step
    await service.start()
    try:
        await until(lambda: service.buttons.found)
        volume_keys.press(KEY_VOLUMEUP)
        volume_keys.press(KEY_VOLUMEDOWN)
        volume_keys.events.put_nowait(Event(EV_KEY, KEY_VOLUMEUP, 2))   # held: not another step
        await until(lambda: len(steps) == 2)
        await asyncio.sleep(0.02)
        assert steps == [5, -5]
    finally:
        await service.stop()


async def test_a_device_that_goes_away_is_looked_for_again_and_told_the_light_again():
    finds = []

    def finder():
        finds.append(rally())
        return finds[-1]

    async def nothing(*args):
        pass

    async def quick(seconds):
        await asyncio.sleep(0)

    keys = RoomButtons(nothing, nothing, finder=finder, sleep=quick)
    keys.start()
    try:
        await until(lambda: keys.found)
        keys.set_light(True)
        assert finds[0][1].leds == [(LED_MUTE, 1)]
        keys.set_light(True)                              # unchanged: not written again
        assert finds[0][1].leds == [(LED_MUTE, 1)]
        finds[0][1].events.put_nowait(None)               # unplugged
        await until(lambda: len(finds) >= 2 and keys.found)
        assert all(device.closed for device in finds[0])
        keys.set_light(True)                              # found again: told again
        assert finds[-1][1].leds == [(LED_MUTE, 1)]
    finally:
        await keys.stop()


def test_only_usb_devices_with_the_keys_or_the_light_are_kept(monkeypatch):
    made = {
        "/dev/input/event6": FakeDevice("Logitech Logi Rally Audio Consumer Control", keys=(KEY_VOLUMEDOWN, KEY_VOLUMEUP)),
        "/dev/input/event7": FakeDevice("Logitech Logi Rally Audio", keys=(KEY_MICMUTE,), led=True),
        "/dev/input/event8": FakeDevice("Logitech Logi Rally Audio"),                                  # only ABS_MISC
        "/dev/input/event3": FakeDevice("Crystal Meet keyboard", keys=(KEY_VOLUMEUP,), phys="py-evdev-uinput"),   # the room's own
        "/dev/input/event1": FakeDevice("vc4-hdmi-0", keys=(KEY_VOLUMEUP,), phys="vc4-hdmi-0/input0"),  # the TV's remote over HDMI
    }
    fake = types.SimpleNamespace(list_devices=lambda: list(made), InputDevice=lambda path: made[path])
    monkeypatch.setitem(sys.modules, "evdev", fake)
    found = buttons.usb_devices()
    assert [device.name for device in found] == ["Logitech Logi Rally Audio Consumer Control", "Logitech Logi Rally Audio"]
    assert [path for path, device in made.items() if device.closed] == ["/dev/input/event8", "/dev/input/event3", "/dev/input/event1"]
