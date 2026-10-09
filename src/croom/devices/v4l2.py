"""
The thin V4L2 layer the camera module drives: open a video node for control calls
only (next to Chromium's stream), query a control's range, get and set it. The
real ioctls live here; tests replace the class with a fake.
"""

import fcntl
import glob
import os
from typing import List, Optional, Tuple

from croom.video.v4l2_ioctl import (
    V4L2_CID_CAMERA_CLASS_BASE,
    V4L2_CID_ZOOM_ABSOLUTE,
    VIDIOC_G_CTRL,
    VIDIOC_QUERYCTRL,
    VIDIOC_S_CTRL,
    v4l2_control,
    v4l2_queryctrl,
)

V4L2_CID_PAN_SPEED = V4L2_CID_CAMERA_CLASS_BASE + 32    # 0x009a0920: -1 left, 0 stop, 1 right, until stopped
V4L2_CID_TILT_SPEED = V4L2_CID_CAMERA_CLASS_BASE + 33   # 0x009a0921: -1 down, 0 stop, 1 up
V4L2_CTRL_FLAG_DISABLED = 0x0001
Range = Tuple[int, int, int, int]   # minimum, maximum, step, default

__all__ = ["V4L2_CID_PAN_SPEED", "V4L2_CID_TILT_SPEED", "V4L2_CID_ZOOM_ABSOLUTE", "Range", "V4l2Controls"]


def _node_number(path: str) -> int:
    digits = path[len("/dev/video"):]
    return int(digits) if digits.isdigit() else 10 ** 6


class V4l2Controls:
    def nodes(self) -> List[str]:
        """Every /dev/video* node, lowest number first."""
        return sorted(glob.glob("/dev/video*"), key=_node_number)

    def open(self, path: str) -> int:
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self, fd: int) -> None:
        os.close(fd)

    def query(self, fd: int, cid: int) -> Optional[Range]:
        """The control's range, or None when the node does not have it (or has it disabled)."""
        query = v4l2_queryctrl()
        query.id = cid
        try:
            fcntl.ioctl(fd, VIDIOC_QUERYCTRL, query)
        except OSError:
            return None
        if query.flags & V4L2_CTRL_FLAG_DISABLED:
            return None
        return (query.minimum, query.maximum, query.step, query.default_value)

    def get(self, fd: int, cid: int) -> int:
        control = v4l2_control()
        control.id = cid
        fcntl.ioctl(fd, VIDIOC_G_CTRL, control)
        return control.value

    def set(self, fd: int, cid: int, value: int) -> None:
        control = v4l2_control()
        control.id = cid
        control.value = value
        fcntl.ioctl(fd, VIDIOC_S_CTRL, control)
