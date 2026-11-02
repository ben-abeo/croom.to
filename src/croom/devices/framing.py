"""
Frame people (spec 2026-11-01 camera view, section 5): from the faces found in one picture to the zoom and the turn that frame the
group. The geometry is pure (no camera, no OpenCV) so it is tested on its own; RoomCamera.frame_people does the moving. The face
detector, OpenCV's YuNet with its model committed beside this file, is at the end and imports OpenCV only when it runs.

Places in a picture are fractions of its width and height: x from 0 (left) to 1 (right), y from 0 (top) to 1 (bottom). The picture
is never mirrored, so its right is the camera's right. Pan +1 turns the camera right and tilt +1 turns it up (the arrow pad's
directions), and positions are seconds of travel from the end stops that Set up (Find the stops) drove into: pan 0 is the left
stop and tilt 0 the bottom one.
"""

import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

Face = Tuple[float, float, float, float]       # x, y, width, height in pixels of the picture
Box = Tuple[float, float, float, float]        # left, top, right, bottom as fractions of the picture

ZOOM_MIN, ZOOM_MAX = 100, 500
ASPECT = 16 / 9                 # the group's box is fitted to the call's picture shape
HEAD_AND_SHOULDERS_WIDE = 2.0   # a face's box, widened to head and shoulders: twice as wide ...
HEAD_AND_SHOULDERS_TALL = 2.5   # ... and two and a half times as tall, extending downward from just above the face
HEADROOM = 0.25                 # the part of a face's height left above it, so the head is not cut at the top
MARGIN = 0.10                   # added to the group's box on every side, as a fraction of its width and height
OFF_CENTRE = 0.05               # the check after the turn corrects when the group's middle is further than this from the picture's

# The MeetUp's geometry. Estimates from the research (docs/superpowers/research/2026-11-01-meetup-framing.md, sections 2.4 and 3.3):
# checks T4 and T7 on the real MeetUp refine them.
FOV_DIAGONAL_DEG = 120.0        # the field of view at zoom 100, corner to corner; as a pinhole on 16:9, about 113 deg wide, 81 deg high
PAN_RANGE_DEG = 50.0            # the mechanical pan, -25 to +25 deg, covered in the travel time Set up uses (ptz_travel_seconds)
TILT_RANGE_DEG = 30.0           # the mechanical tilt, -15 to +15 deg, in the same travel time
# The zoom level is taken as the magnification times 100 (500: five times), the field of view narrowing with it as a pinhole's does.


def _half_tangents(fov_diagonal_deg: float = FOV_DIAGONAL_DEG, aspect: float = ASPECT) -> Tuple[float, float]:
    """The tangents of half the horizontal and half the vertical field of view at zoom 100, from the diagonal one (a pinhole)."""
    half_diagonal = math.tan(math.radians(fov_diagonal_deg) / 2)
    diagonal = math.hypot(aspect, 1.0)
    return half_diagonal * aspect / diagonal, half_diagonal / diagonal


@dataclass(frozen=True)
class Plan:
    zoom: int                   # the level that makes the group fill the picture, 100 to 500
    pan_s: Optional[float]      # where to turn to, in seconds of travel; None when the position is not known (zoom only)
    tilt_s: Optional[float]
    people: int


def head_and_shoulders(face: Face, size: Tuple[int, int]) -> Box:
    """A face's box widened to head and shoulders: centred on the face across, from just above it downward."""
    x, y, w, h = face
    width, height = size
    middle = x + w / 2
    top = y - HEADROOM * h
    wide, tall = HEAD_AND_SHOULDERS_WIDE * w, HEAD_AND_SHOULDERS_TALL * h
    return ((middle - wide / 2) / width, top / height, (middle + wide / 2) / width, (top + tall) / height)


def group_box(faces: Sequence[Face], size: Tuple[int, int]) -> Box:
    """Everyone's head and shoulders together, plus the margin, fitted to 16:9 about its middle (grown, never cut)."""
    boxes = [head_and_shoulders(face, size) for face in faces]
    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
    right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
    w, h = right - left, bottom - top
    left, right = left - MARGIN * w, right + MARGIN * w
    top, bottom = top - MARGIN * h, bottom + MARGIN * h
    # In fractions of a picture that is itself `picture_aspect`, a box of the call's shape has width / height = ASPECT / picture_aspect
    picture_aspect = size[0] / size[1]
    want = ASPECT / picture_aspect
    w, h = right - left, bottom - top
    cx, cy = (left + right) / 2, (top + bottom) / 2
    if w / h < want:
        w = h * want
    else:
        h = w / want
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def zoom_for(box: Box, middle: Tuple[float, float] = (0.5, 0.5)) -> int:
    """The zoom level at which the box fills the picture once the place `middle` (of the picture at zoom 100) is in its middle:
    the box's furthest side from there sets it. 100 to 500."""
    cx, cy = middle
    reach_x = max(cx - box[0], box[2] - cx, 1e-6)
    reach_y = max(cy - box[1], box[3] - cy, 1e-6)
    magnification = min(0.5 / reach_x, 0.5 / reach_y)
    return max(ZOOM_MIN, min(ZOOM_MAX, int(math.floor(100 * magnification))))


def turn_to(place: Tuple[float, float], zoom: int, travel_s: float) -> Tuple[float, float]:
    """The pan and tilt, in seconds of travel, that bring a place of the picture (taken at `zoom`) to its middle."""
    tan_x, tan_y = _half_tangents()
    magnification = max(zoom, ZOOM_MIN) / 100
    pan_deg = math.degrees(math.atan((place[0] - 0.5) * 2 * tan_x / magnification))
    tilt_deg = -math.degrees(math.atan((place[1] - 0.5) * 2 * tan_y / magnification))   # up the picture is tilt +1
    return pan_deg * travel_s / PAN_RANGE_DEG, tilt_deg * travel_s / TILT_RANGE_DEG


def place_after(pan_s: float, tilt_s: float, travel_s: float) -> Tuple[float, float]:
    """The place of the picture at zoom 100 that a turn of pan_s and tilt_s seconds brings to its middle (turn_to undone)."""
    tan_x, tan_y = _half_tangents()
    pan = math.radians(pan_s * PAN_RANGE_DEG / travel_s)
    tilt = math.radians(tilt_s * TILT_RANGE_DEG / travel_s)
    return 0.5 + math.tan(pan) / (2 * tan_x), 0.5 - math.tan(tilt) / (2 * tan_y)


def plan(faces: Sequence[Face], size: Tuple[int, int], travel_s: float,
         position: Optional[Tuple[float, float]] = None) -> Plan:
    """Steps 4 to 6 for faces found in a picture taken at zoom 100: the group's box, the turn toward its middle from `position`
    (pan_s, tilt_s; None when it is not known: no turn), and the zoom that fills the picture with it. A turn the stops cut short
    is allowed for: the zoom is then the one that keeps the whole group in where the camera does get to."""
    box = group_box(faces, size)
    middle = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    if position is None:
        return Plan(zoom_for(box), None, None, len(faces))
    pan_now, tilt_now = position
    pan_by, tilt_by = turn_to(middle, ZOOM_MIN, travel_s)
    pan_s = min(travel_s, max(0.0, pan_now + pan_by))
    tilt_s = min(travel_s, max(0.0, tilt_now + tilt_by))
    reached = place_after(pan_s - pan_now, tilt_s - tilt_now, travel_s)
    return Plan(zoom_for(box, reached), pan_s, tilt_s, len(faces))


def correction(faces: Sequence[Face], size: Tuple[int, int], zoom: int, travel_s: float) -> Optional[Tuple[float, float]]:
    """Step 7: the small turn (pan, tilt seconds) that brings the group back to the middle of a picture taken at `zoom`, or None when it
    is near enough (or no one is in it)."""
    if not faces:
        return None
    box = group_box(faces, size)
    middle = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    if abs(middle[0] - 0.5) <= OFF_CENTRE and abs(middle[1] - 0.5) <= OFF_CENTRE:
        return None
    return turn_to(middle, zoom, travel_s)


# ----------------------------------------------------------------------
# The detector: OpenCV's YuNet (MIT, from opencv/opencv_zoo), its model beside this file
# ----------------------------------------------------------------------

MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
SCORE = 0.8                     # YuNet's own defaults: a face is kept at this confidence and up ...
NMS = 0.3                       # ... and overlapping finds are merged above this overlap
_detector = None
_detector_lock = threading.Lock()   # one detector, used from worker threads: a run abandoned by a cancelled tap may still be going


def detect_faces(jpeg: bytes) -> Tuple[List[Face], Tuple[int, int]]:
    """The faces in a JPEG and its size: ([(x, y, width, height)], (width, height)). Runs for some tens of milliseconds: call it
    from a worker thread. A picture that does not decode has no faces in it."""
    import cv2
    import numpy

    global _detector
    picture = cv2.imdecode(numpy.frombuffer(jpeg, numpy.uint8), cv2.IMREAD_COLOR)
    if picture is None:
        return [], (0, 0)
    height, width = picture.shape[:2]
    with _detector_lock:
        if _detector is None:
            _detector = cv2.FaceDetectorYN.create(str(MODEL), "", (width, height), SCORE, NMS)
        _detector.setInputSize((width, height))
        _, found = _detector.detect(picture)
    faces = [] if found is None else [tuple(float(v) for v in row[:4]) for row in found]
    return faces, (width, height)
