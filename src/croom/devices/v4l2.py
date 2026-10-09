"""
The thin V4L2 layer the camera module drives: open a video node for control calls
only (next to Chromium's stream), query a control's range, get and set it, or set
several together. The real ioctls live here; tests replace the class with a fake.
"""

import ctypes
import fcntl
import glob
import os
from typing import Dict, List, Optional, Tuple

from croom.video.v4l2_ioctl import (
    V4L2_CID_CAMERA_CLASS_BASE,
    V4L2_CID_ZOOM_ABSOLUTE,
    VIDIOC_G_CTRL,
    VIDIOC_QUERYCTRL,
    VIDIOC_S_CTRL,
    _IOWR,
    v4l2_control,
    v4l2_queryctrl,
)

V4L2_CID_PAN_SPEED = V4L2_CID_CAMERA_CLASS_BASE + 32    # 0x009a0920: -1 left, 0 stop, 1 right, until stopped
V4L2_CID_TILT_SPEED = V4L2_CID_CAMERA_CLASS_BASE + 33   # 0x009a0921: -1 down, 0 stop, 1 up
V4L2_CTRL_FLAG_DISABLED = 0x0001
Range = Tuple[int, int, int, int]   # minimum, maximum, step, default
V4L2_CTRL_CLASS_CAMERA = 0x009a0000   # the `which` of an extended-control call that sets camera-class controls


class _v4l2_ext_value(ctypes.Union):
    _pack_ = 1
    _fields_ = [("value", ctypes.c_int32), ("value64", ctypes.c_int64), ("ptr", ctypes.c_void_p)]


class v4l2_ext_control(ctypes.Structure):   # packed in the kernel header: 20 bytes
    _pack_ = 1
    _fields_ = [("id", ctypes.c_uint32), ("size", ctypes.c_uint32), ("reserved2", ctypes.c_uint32 * 1), ("u", _v4l2_ext_value)]


class v4l2_ext_controls(ctypes.Structure):  # 32 bytes on a 64-bit kernel
    _fields_ = [("which", ctypes.c_uint32), ("count", ctypes.c_uint32), ("error_idx", ctypes.c_uint32),
                ("request_fd", ctypes.c_int32), ("reserved", ctypes.c_uint32 * 1),
                ("controls", ctypes.POINTER(v4l2_ext_control))]


VIDIOC_S_EXT_CTRLS = _IOWR(ord("V"), 72, ctypes.sizeof(v4l2_ext_controls))

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

    def set_many(self, fd: int, values: Dict[int, int]) -> None:
        """Write several camera-class controls in one call, so the driver applies them together. That matters
        for pan_speed and tilt_speed: on a UVC camera they are the two halves of one relative control, and
        two separate writes could cancel each other."""
        if not values:
            return
        controls = (v4l2_ext_control * len(values))()
        for control, (cid, value) in zip(controls, values.items()):
            control.id = cid
            control.u.value = value
        ctrls = v4l2_ext_controls()
        ctrls.which = V4L2_CTRL_CLASS_CAMERA
        ctrls.count = len(values)
        ctrls.controls = controls
        fcntl.ioctl(fd, VIDIOC_S_EXT_CTRLS, ctrls)   # `controls` is still in scope here, so it outlives the call
