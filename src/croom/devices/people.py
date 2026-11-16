"""
The people in a picture, found on the room's AI HAT (a Hailo-8 or 8L on the Pi's PCIe) when the room has one, and by framing.py's
YuNet on the CPU when it has none or the HAT fails. `detect` is what Frame people and Auto framing look with: it answers as
framing.detect_faces does, so nothing else knows which found them.

The HAT runs Raspberry Pi's yolov5s_personface (apt's hailo-models, installed by hailo-all), which finds bodies as well as faces in
about 30 ms where YuNet takes ten times that. A body with no face found in it (someone turned away, or too far for a face) is given a
head where a head would be, so that person is framed too; `count` is how many people that makes (the room's occupancy).

HailoRT's Python (python3-hailort) is apt's, in the system Python: the room's venv does not see it, so it is loaded from there by its
path, and nothing else of the system's packages comes with it.
"""

import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Any, List, Sequence, Tuple

from croom.devices import framing
from croom.devices.framing import Face

logger = logging.getLogger(__name__)

DEVICE = Path("/dev/hailo0")
MODEL = Path("/usr/share/hailo-models/yolov5s_personface_h8l.hef")     # compiled for the 8L, which a Hailo-8 runs too
SYSTEM_PACKAGES = Path("/usr/lib/python3/dist-packages")
INPUT = 640                     # the model's picture: 640 x 640 RGB, the camera's letterboxed into it
GREY = 114                      # the letterbox's bars, as the model was trained with
SCORE = 0.5                     # a body or a face is kept at this confidence and up (Raspberry Pi's own threshold for this model)
# A head for a body with no face found, from the one body box: tune these in a room where people are framed too high or too wide.
HEAD_WIDE = 0.2                 # the head's width as a part of the body box's (a seated body's box is its arms and shoulders wide)
HEAD_TALL = 1.3                 # the head's height, in its widths
HEAD_DOWN = 0.1                 # the head's top below the body box's, in head heights (the box's top is the hair)

Box = Tuple[float, float, float, float]     # x, y, width, height in pixels of the picture, as a Face is

_lock = threading.Lock()        # one look at a time: the HAT is opened for each
_said: Any = None               # the last failure warned about, so that one that repeats at every look is said once


def available() -> bool:
    """Whether this Pi has the HAT, its driver and the model: what the rest (HailoRT's Python) needs to be worth trying."""
    return DEVICE.exists() and MODEL.exists()


def _hailo() -> Any:
    """HailoRT's Python: the venv's when it has one, else the system's, loaded by its path."""
    module = sys.modules.get("hailo_platform")
    if module is not None:
        return module
    try:
        import hailo_platform
        return hailo_platform
    except ImportError:
        pass
    init = SYSTEM_PACKAGES / "hailo_platform" / "__init__.py"
    spec = importlib.util.spec_from_file_location("hailo_platform", init, submodule_search_locations=[str(init.parent)])
    if spec is None or spec.loader is None or not init.exists():
        raise ImportError(f"HailoRT's Python is not installed ({init}): sudo apt install hailo-all")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hailo_platform"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules["hailo_platform"]
        raise
    return module


def _infer(square: Any) -> Sequence[Any]:
    """The model's answer for one 640 x 640 RGB picture: for each of its classes (person, face), rows of ymin, xmin, ymax, xmax (as
    fractions of the square) and score. The model's own last layer has merged overlapping finds already."""
    h = _hailo()
    hef = h.HEF(str(MODEL))
    # ponytail: the HAT is opened and closed for each look (about 0.2 s more than the 30 ms the look takes), so nothing is held
    # between looks and a fault never outlives one. Keep it open if anything ever looks more than about once a second.
    with h.VDevice() as device:
        group = device.configure(hef, h.ConfigureParams.create_from_hef(hef, interface=h.HailoStreamInterface.PCIe))[0]
        ins = h.InputVStreamParams.make(group, format_type=h.FormatType.UINT8)
        outs = h.OutputVStreamParams.make(group, format_type=h.FormatType.FLOAT32)
        with h.InferVStreams(group, ins, outs) as pipeline, group.activate(group.create_params()):
            answer = pipeline.infer({hef.get_input_vstream_infos()[0].name: square[None]})
    return next(iter(answer.values()))[0]


def find(jpeg: bytes, infer=_infer) -> Tuple[List[Box], List[Box], Tuple[int, int]]:
    """The bodies and the faces the HAT finds in a JPEG, and its size: ([body], [face], (width, height)), each box in the picture's
    pixels. A picture that does not decode has no one in it. Raises whatever the HAT does."""
    import cv2
    import numpy

    picture = cv2.imdecode(numpy.frombuffer(jpeg, numpy.uint8), cv2.IMREAD_COLOR)
    if picture is None:
        return [], [], (0, 0)
    height, width = picture.shape[:2]
    scale = INPUT / max(width, height)
    wide, tall = round(width * scale), round(height * scale)
    left, top = (INPUT - wide) // 2, (INPUT - tall) // 2
    square = numpy.full((INPUT, INPUT, 3), GREY, numpy.uint8)
    square[top:top + tall, left:left + wide] = cv2.cvtColor(cv2.resize(picture, (wide, tall)), cv2.COLOR_BGR2RGB)
    with _lock:
        classes = infer(square)
    found: List[List[Box]] = []
    for rows in list(classes)[:2]:
        boxes = []
        for ymin, xmin, ymax, xmax, score in numpy.asarray(rows, dtype=float).reshape(-1, 5):
            if score >= SCORE:
                x0, y0 = (xmin * INPUT - left) / scale, (ymin * INPUT - top) / scale
                x1, y1 = (xmax * INPUT - left) / scale, (ymax * INPUT - top) / scale
                boxes.append((x0, y0, x1 - x0, y1 - y0))
        found.append(boxes)
    while len(found) < 2:
        found.append([])
    return found[0], found[1], (width, height)


def _inside(face: Box, body: Box) -> bool:
    """Whether a face's middle is in a body's box."""
    x, y = face[0] + face[2] / 2, face[1] + face[3] / 2
    return body[0] <= x <= body[0] + body[2] and body[1] <= y <= body[1] + body[3]


def heads(bodies: Sequence[Box], faces: Sequence[Box]) -> List[Face]:
    """One head for each person: the faces found, and for a body with none of them in it, a head where its head would be."""
    people = list(faces)
    for body in bodies:
        if not any(_inside(face, body) for face in faces):
            x, y, w, _ = body
            wide = HEAD_WIDE * w
            tall = HEAD_TALL * wide
            people.append((x + (w - wide) / 2, y + HEAD_DOWN * tall, wide, tall))
    return people


def _fell_back(e: BaseException) -> None:
    global _said
    if str(e) != _said:
        _said = str(e)
        logger.warning(f"The AI HAT did not look for people ({type(e).__name__}: {e}): looking on the CPU instead")


def detect(jpeg: bytes) -> Tuple[List[Face], Tuple[int, int]]:
    """Everyone in a JPEG as a head each, and its size, as framing.detect_faces answers: on the HAT in a room that has one, by
    YuNet's faces otherwise (and when the HAT fails, which is said once). Call it from a worker thread."""
    if available():
        try:
            bodies, faces, size = find(jpeg)
            return heads(bodies, faces), size
        except Exception as e:  # noqa: BLE001 - a HAT that fails must not stop the room framing people
            _fell_back(e)
    return framing.detect_faces(jpeg)


def count(jpeg: bytes) -> int:
    """How many people are in a JPEG (the room's occupancy): bodies and faces on the HAT, faces alone without it."""
    return len(detect(jpeg)[0])
