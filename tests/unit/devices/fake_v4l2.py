"""A V4L2 stand-in: nodes with the controls PiMeet-3's cameras report, and a clock tests can move."""

import asyncio
import threading
import time

from croom.devices.v4l2 import (V4L2_CID_AUTO_WHITE_BALANCE, V4L2_CID_BACKLIGHT_COMPENSATION, V4L2_CID_BRIGHTNESS,
                                V4L2_CID_CONTRAST, V4L2_CID_PAN_ABSOLUTE, V4L2_CID_PAN_SPEED, V4L2_CID_SATURATION, V4L2_CID_SHARPNESS,
                                V4L2_CID_TILT_ABSOLUTE, V4L2_CID_TILT_SPEED, V4L2_CID_WHITE_BALANCE_TEMPERATURE,
                                V4L2_CID_ZOOM_ABSOLUTE)

MEETUP = {V4L2_CID_ZOOM_ABSOLUTE: (100, 500, 1, 100), V4L2_CID_PAN_SPEED: (-1, 1, 1, 1), V4L2_CID_TILT_SPEED: (-1, 1, 1, 1)}
# The Rally (Plus) camera, as v4l2-ctl --list-ctrls showed it in the Boardroom (2026-11-04): the MeetUp's controls and its position
RALLY = {V4L2_CID_ZOOM_ABSOLUTE: (100, 1500, 1, 100), V4L2_CID_PAN_SPEED: (-1, 1, 1, 1), V4L2_CID_TILT_SPEED: (-1, 1, 1, 1),
         V4L2_CID_PAN_ABSOLUTE: (-324000, 324000, 3600, 0), V4L2_CID_TILT_ABSOLUTE: (-324000, 180000, 3600, 0)}
# ... with the picture controls it reported there too (v4l2-ctl --list-ctrls-menus, the same day)
RALLY_PICTURE = {**RALLY, V4L2_CID_BRIGHTNESS: (0, 255, 1, 128), V4L2_CID_CONTRAST: (0, 255, 1, 128),
                 V4L2_CID_SATURATION: (0, 255, 1, 128), V4L2_CID_AUTO_WHITE_BALANCE: (0, 1, 1, 1),
                 V4L2_CID_WHITE_BALANCE_TEMPERATURE: (2000, 7500, 10, 4000), V4L2_CID_SHARPNESS: (0, 255, 1, 128),
                 V4L2_CID_BACKLIGHT_COMPENSATION: (0, 1, 1, 0)}
DECODER = {}   # /dev/video19, the Pi's HEVC decoder: no camera controls


class FakeV4l2:
    def __init__(self, nodes=None, latency_s=0.0):
        # latency_s: how long each control call takes, as a USB round trip does. Tests that look for
        # overlapping calls set it; every other test leaves it at zero and stays instant.
        self._nodes = dict(nodes if nodes is not None else {"/dev/video0": MEETUP, "/dev/video1": {}, "/dev/video19": DECODER})
        self.values = {}
        self.calls = []
        self.fail = False         # the camera is gone: every control call raises
        self.fail_next = 0        # a transient fault: the next N control calls raise, then it answers again
        self.unopenable = set()   # nodes whose open() is refused, as a busy or unreadable one is
        self.in_flight = 0        # control calls running right now (the camera runs them in worker threads) ...
        self.overlapped = False   # ... and whether two of them were ever running at the same time
        self.set_many_calls = []  # one dict per set_many() call: the controls that were written together
        self.refuse = set()       # controls whose writes the camera refuses (EBUSY), as it does an inactive one
        self._latency_s = latency_s
        self._counter = threading.Lock()

    def nodes(self):
        return list(self._nodes)

    def _path(self, fd):
        return list(self._nodes)[fd - 10]

    def open(self, path):
        self.calls.append(("open", path))
        if path in self.unopenable:
            raise OSError(13, "Permission denied")
        return 10 + list(self._nodes).index(path)

    def close(self, fd):
        self.calls.append(("close", fd))

    def query(self, fd, cid):
        return self._nodes[self._path(fd)].get(cid)

    def _check_fault(self):
        if self.fail:
            raise OSError(19, "No such device")
        with self._counter:
            if self.fail_next > 0:
                self.fail_next -= 1
                raise OSError(5, "Input/output error")

    def get(self, fd, cid):
        self._check_fault()
        value = self.values.get((fd, cid), self._nodes[self._path(fd)][cid][3])
        self._round_trip()
        return value

    def set(self, fd, cid, value):
        self._check_fault()
        if cid in self.refuse:
            raise OSError(16, "Device or resource busy")
        self._round_trip()
        self.calls.append(("set", fd, cid, value))
        self.values[(fd, cid)] = value

    def set_many(self, fd, values):
        """Several controls in one call (VIDIOC_S_EXT_CTRLS): one round trip, one entry in set_many_calls, and
        one ("set", ...) entry per control, in order, so sets() sees them as it sees single writes."""
        self._check_fault()
        self._round_trip()
        self.set_many_calls.append(dict(values))
        for cid, value in values.items():
            self.calls.append(("set", fd, cid, value))
            self.values[(fd, cid)] = value

    def _round_trip(self):
        """A control call takes latency_s, like a USB round trip; two in flight at once are recorded."""
        with self._counter:
            self.in_flight += 1
        try:
            if self._latency_s:
                time.sleep(self._latency_s)
            with self._counter:
                if self.in_flight > 1:
                    self.overlapped = True
        finally:
            with self._counter:
                self.in_flight -= 1

    def sets(self, cid):
        """The values set on one control, in order."""
        return [c[3] for c in self.calls if c[0] == "set" and c[2] == cid]


class FakeClock:
    """A clock that only moves when something sleeps on it; each sleep also yields once to the loop."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)
