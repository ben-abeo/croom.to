"""A V4L2 stand-in: nodes with the controls PiMeet-3's cameras report, and a clock tests can move."""

import asyncio

from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED, V4L2_CID_ZOOM_ABSOLUTE

MEETUP = {V4L2_CID_ZOOM_ABSOLUTE: (100, 500, 1, 100), V4L2_CID_PAN_SPEED: (-1, 1, 1, 1), V4L2_CID_TILT_SPEED: (-1, 1, 1, 1)}
DECODER = {}   # /dev/video19, the Pi's HEVC decoder: no camera controls


class FakeV4l2:
    def __init__(self, nodes=None):
        self._nodes = dict(nodes if nodes is not None else {"/dev/video0": MEETUP, "/dev/video1": {}, "/dev/video19": DECODER})
        self.values = {}
        self.calls = []
        self.fail = False

    def nodes(self):
        return list(self._nodes)

    def _path(self, fd):
        return list(self._nodes)[fd - 10]

    def open(self, path):
        self.calls.append(("open", path))
        return 10 + list(self._nodes).index(path)

    def close(self, fd):
        self.calls.append(("close", fd))

    def query(self, fd, cid):
        return self._nodes[self._path(fd)].get(cid)

    def get(self, fd, cid):
        if self.fail:
            raise OSError(19, "No such device")
        return self.values.get((fd, cid), self._nodes[self._path(fd)][cid][3])

    def set(self, fd, cid, value):
        if self.fail:
            raise OSError(19, "No such device")
        self.calls.append(("set", fd, cid, value))
        self.values[(fd, cid)] = value

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
