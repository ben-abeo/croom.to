"""
The real V4L2 layer without a camera: the extended-control structures against the kernel's layout, and
the one call that writes several controls together (fcntl.ioctl is replaced to capture its argument).
"""

import ctypes
import fcntl
import struct

import pytest

from croom.devices.v4l2 import (
    V4L2_CID_PAN_SPEED,
    V4L2_CID_TILT_SPEED,
    V4L2_CTRL_CLASS_CAMERA,
    VIDIOC_S_EXT_CTRLS,
    V4l2Controls,
    v4l2_ext_control,
    v4l2_ext_controls,
)

POINTER_SIZE = ctypes.sizeof(ctypes.c_void_p)


def capture_ioctl(monkeypatch):
    """Replace fcntl.ioctl with a recorder that decodes the extended-control call it is handed."""
    calls = []

    def ioctl(fd, request, arg, *rest):
        raw = ctypes.string_at(ctypes.cast(arg.controls, ctypes.c_void_p).value, arg.count * ctypes.sizeof(v4l2_ext_control))
        controls = []
        for i in range(arg.count):
            ident, size, reserved, value = struct.unpack_from("=IIIi", raw, i * 20)
            controls.append((ident, size, reserved, value, raw[i * 20 + 16:i * 20 + 20]))   # the union's upper half stays zero
        calls.append({"fd": fd, "request": request, "which": arg.which, "count": arg.count,
                      "error_idx": arg.error_idx, "request_fd": arg.request_fd, "controls": controls})
        return 0

    monkeypatch.setattr(fcntl, "ioctl", ioctl)
    return calls


def test_the_extended_control_structures_match_the_kernels_layout():
    assert ctypes.sizeof(v4l2_ext_control) == 20                 # packed, as in the kernel header
    assert v4l2_ext_control.u.offset == 12
    assert ctypes.sizeof(v4l2_ext_controls) == (32 if POINTER_SIZE == 8 else 24)
    assert v4l2_ext_controls.controls.offset == (24 if POINTER_SIZE == 8 else 20)


def test_the_extended_control_request_number_is_the_kernels():
    assert VIDIOC_S_EXT_CTRLS & 0xFF == 72 and (VIDIOC_S_EXT_CTRLS >> 8) & 0xFF == ord("V")     # _IOWR('V', 72, ...)
    assert (VIDIOC_S_EXT_CTRLS >> 16) & 0x3FFF == ctypes.sizeof(v4l2_ext_controls)
    assert VIDIOC_S_EXT_CTRLS >> 30 == 3                         # read and write
    if POINTER_SIZE == 8:
        assert VIDIOC_S_EXT_CTRLS == 0xC0205648                  # as a 64-bit kernel's strace shows it


def test_set_many_writes_every_control_in_one_ioctl(monkeypatch):
    calls = capture_ioctl(monkeypatch)
    V4l2Controls().set_many(7, {V4L2_CID_PAN_SPEED: -1, V4L2_CID_TILT_SPEED: 1})
    # the kernel refuses a call whose controls are not all of the class named in `which`
    assert V4L2_CTRL_CLASS_CAMERA == 0x009A0000 == V4L2_CID_PAN_SPEED & 0x0FFF0000 == V4L2_CID_TILT_SPEED & 0x0FFF0000
    assert calls == [{
        "fd": 7, "request": VIDIOC_S_EXT_CTRLS, "which": 0x009A0000, "count": 2, "error_idx": 0, "request_fd": 0,
        "controls": [(V4L2_CID_PAN_SPEED, 0, 0, -1, b"\0\0\0\0"), (V4L2_CID_TILT_SPEED, 0, 0, 1, b"\0\0\0\0")]}]


def test_set_many_with_nothing_to_write_makes_no_call(monkeypatch):
    calls = capture_ioctl(monkeypatch)
    V4l2Controls().set_many(7, {})
    assert calls == []


def test_set_many_raises_what_the_driver_raises(monkeypatch):
    def refuse(fd, request, arg, *rest):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(fcntl, "ioctl", refuse)
    with pytest.raises(OSError) as failure:
        V4l2Controls().set_many(7, {V4L2_CID_PAN_SPEED: 1})
    assert failure.value.errno == 22
