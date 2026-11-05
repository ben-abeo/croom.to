"""
Frame people (spec 2026-11-01 camera view, section 5): from the faces found in one picture to the zoom and the turn that frame the
group. The geometry is pure (no camera, no OpenCV) so it is tested on its own; RoomCamera.frame_people does the moving. The face
detector, OpenCV's YuNet with its model committed beside this file, is at the end and imports OpenCV only when it runs.

Places in a picture are fractions of its width and height: x from 0 (left) to 1 (right), y from 0 (top) to 1 (bottom). The picture
is never mirrored, so its right is the camera's right. Pan +1 turns the camera right and tilt +1 turns it up (the arrow pad's
directions), and positions are seconds of travel from the end stops that Set up (Find the stops) drove into: pan 0 is the left
stop and tilt 0 the bottom one. A camera that says where it points (the Rally) is aimed in degrees instead (aim, nudge), with its
picture kept inside the limits set for it (within).
"""

import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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

# A camera that says where it is (the Rally) is turned to a pan and tilt in degrees, measured from where it points. Its lens at zoom 100:
# Logitech's figures for the Rally camera (90 deg diagonal), which pictures from the Boardroom's matched on 2026-11-04 (a 20 deg tilt moved
# the picture by 53.7 deg of its height; zoom 200 magnified 2.05 times). The knob to turn if a turn falls short or overshoots.
RALLY_FOV_DEG = (82.1, 52.2)    # wide, high
EDGES = ("down", "up", "left", "right")   # the limits: the furthest the picture's bottom, top, left and right edges may go, in degrees


def _half_tangents(fov_diagonal_deg: float = FOV_DIAGONAL_DEG, aspect: float = ASPECT) -> Tuple[float, float]:
    """The tangents of half the horizontal and half the vertical field of view at zoom 100, from the diagonal one (a pinhole)."""
    half_diagonal = math.tan(math.radians(fov_diagonal_deg) / 2)
    diagonal = math.hypot(aspect, 1.0)
    return half_diagonal * aspect / diagonal, half_diagonal / diagonal


RALLY_TANGENTS = (math.tan(math.radians(RALLY_FOV_DEG[0]) / 2), math.tan(math.radians(RALLY_FOV_DEG[1]) / 2))

# How far the Rally turns under its speed controls (pan_speed or tilt_speed at 1) for how long the turn is held, as measured in the
# Boardroom on 2026-11-05 against its own targets: the hold in seconds, and where it comes to rest in degrees, at zoom 100. It speeds up
# the longer it is held, comes to rest on a whole degree, and turns as far for the same hold every time (0.6 s: 5 deg, three times over);
# pan and tilt agree to a degree. Past the table it keeps up about RALLY_TURN_DEG_S. Zoomed in it turns slower: RALLY_TURN_ZOOM is the
# zoom and the part of the table's turn it makes there. The knobs to turn if a turn by an arrow ends somewhere the room does not think it
# is. ponytail: an open-loop table, right to a degree at zoom 100 to 250 and to about two at 500 and past; checking a turn against the
# picture is the upgrade if that is not close enough.
RALLY_TURN = ((0.0, 0.0), (0.15, 1.0), (0.3, 1.5), (0.45, 3.0), (0.6, 5.0), (0.7, 7.5), (0.8, 9.0), (1.0, 13.5), (1.2, 19.0),
              (1.4, 23.0))
RALLY_TURN_DEG_S = 24.0
RALLY_TURN_ZOOM = ((200, 1.0), (250, 0.9), (500, 0.75), (1000, 0.7))


def _between(table: Sequence[Tuple[float, float]], x: float) -> float:
    """Straight-line interpolation in a table of (x, y) rising in x, flat past its ends."""
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]


def turn_degrees(seconds: float, zoom: int) -> int:
    """How far the Rally comes to rest from where a turn by speed began, the turn held `seconds` at `zoom`: whole degrees."""
    seconds = max(0.0, seconds)
    end_s, end_deg = RALLY_TURN[-1]
    deg = _between(RALLY_TURN, seconds) if seconds <= end_s else end_deg + RALLY_TURN_DEG_S * (seconds - end_s)
    return math.floor(deg * _between(RALLY_TURN_ZOOM, zoom) + 0.5)


def _angles(place: Tuple[float, float], zoom: int, tangents: Tuple[float, float]) -> Tuple[float, float]:
    """The pan and tilt, in degrees, that bring a place of a picture taken at `zoom` to its middle. Up the picture is tilt +1."""
    magnification = max(zoom, ZOOM_MIN) / 100
    return (math.degrees(math.atan((place[0] - 0.5) * 2 * tangents[0] / magnification)),
            -math.degrees(math.atan((place[1] - 0.5) * 2 * tangents[1] / magnification)))


def _place(pan_deg: float, tilt_deg: float, tangents: Tuple[float, float]) -> Tuple[float, float]:
    """The place of a picture at zoom 100 that a turn of pan_deg and tilt_deg brings to its middle (_angles undone)."""
    return (0.5 + math.tan(math.radians(pan_deg)) / (2 * tangents[0]),
            0.5 - math.tan(math.radians(tilt_deg)) / (2 * tangents[1]))


def half_view(zoom: int, tangents: Tuple[float, float] = RALLY_TANGENTS) -> Tuple[float, float]:
    """Half the picture's width and height in degrees at `zoom`: how far its edges are from where the camera points."""
    magnification = max(zoom, ZOOM_MIN) / 100
    return math.degrees(math.atan(tangents[0] / magnification)), math.degrees(math.atan(tangents[1] / magnification))


def within(pan: float, tilt: float, zoom: int, limits: Dict[str, float],
           tangents: Tuple[float, float] = RALLY_TANGENTS) -> Tuple[float, float]:
    """The pan and tilt (degrees) nearest the ones asked for that keep the picture at `zoom` inside the limits (EDGES, degrees; any
    of them may be missing). Limits too close together for the picture to fit between: the lowest and leftmost win, so the camera's
    base, below it, stays out. Edges are taken as pan or tilt plus half the view, which a camera tilted far up or down bends a little."""
    half_w, half_h = half_view(zoom, tangents)
    if "right" in limits:
        pan = min(pan, limits["right"] - half_w)
    if "left" in limits:
        pan = max(pan, limits["left"] + half_w)
    if "up" in limits:
        tilt = min(tilt, limits["up"] - half_h)
    if "down" in limits:
        tilt = max(tilt, limits["down"] + half_h)
    return pan, tilt


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


def at_edge(faces: Sequence[Face], size: Tuple[int, int], margin: float) -> bool:
    """Whether anyone's head and shoulders reach within `margin` (a fraction of the picture) of its edge: someone coming in, or about
    to leave it (Auto framing)."""
    for face in faces:
        left, top, right, bottom = head_and_shoulders(face, size)
        if left < margin or top < margin or right > 1 - margin or bottom > 1 - margin:
            return True
    return False


def zoom_for(box: Box, middle: Tuple[float, float] = (0.5, 0.5), top: int = ZOOM_MAX) -> int:
    """The zoom level at which the box fills the picture once the place `middle` (of the picture at zoom 100) is in its middle:
    the box's furthest side from there sets it. 100 to `top` (the MeetUp's 500, or the camera's own)."""
    cx, cy = middle
    reach_x = max(cx - box[0], box[2] - cx, 1e-6)
    reach_y = max(cy - box[1], box[3] - cy, 1e-6)
    magnification = min(0.5 / reach_x, 0.5 / reach_y)
    return max(ZOOM_MIN, min(top, int(math.floor(100 * magnification))))


def turn_to(place: Tuple[float, float], zoom: int, travel_s: float) -> Tuple[float, float]:
    """The pan and tilt, in seconds of travel, that bring a place of the picture (taken at `zoom`) to its middle."""
    pan_deg, tilt_deg = _angles(place, zoom, _half_tangents())
    return pan_deg * travel_s / PAN_RANGE_DEG, tilt_deg * travel_s / TILT_RANGE_DEG


def place_after(pan_s: float, tilt_s: float, travel_s: float) -> Tuple[float, float]:
    """The place of the picture at zoom 100 that a turn of pan_s and tilt_s seconds brings to its middle (turn_to undone)."""
    return _place(pan_s * PAN_RANGE_DEG / travel_s, tilt_s * TILT_RANGE_DEG / travel_s, _half_tangents())


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


def aim(faces: Sequence[Face], size: Tuple[int, int], at: Tuple[float, float], limits: Dict[str, float], top: int,
        tangents: Tuple[float, float] = RALLY_TANGENTS) -> Tuple[int, float, float]:
    """Steps 4 to 6 for a camera that says where it is, from faces found in a picture taken at zoom 100 with the camera at `at` (pan,
    tilt in degrees): (zoom, pan, tilt) that put the group in the middle and fill the picture with it, up to the zoom `top`, with the
    picture kept inside the limits. A limit that holds the camera off the group's middle lowers the zoom so everyone stays in, and a
    lower zoom widens the picture, which can move the limit's hold again: a few rounds settle it."""
    box = group_box(faces, size)
    middle = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    turn = _angles(middle, ZOOM_MIN, tangents)
    want = (at[0] + turn[0], at[1] + turn[1])
    zoom = zoom_for(box, middle, top)
    for _ in range(4):
        pan, tilt = within(want[0], want[1], zoom, limits, tangents)
        zoom = min(zoom, zoom_for(box, _place(pan - at[0], tilt - at[1], tangents), top))
    pan, tilt = within(want[0], want[1], zoom, limits, tangents)
    return zoom, pan, tilt


def _off_centre(faces: Sequence[Face], size: Tuple[int, int], people: int) -> Optional[Tuple[float, float]]:
    """The group's middle when step 7 should correct toward it: never with fewer people than step 3 found (`people`), which would
    turn toward some of them and leave the rest out, and not when the middle is near enough."""
    if not faces or len(faces) < people:
        return None
    box = group_box(faces, size)
    middle = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    if abs(middle[0] - 0.5) <= OFF_CENTRE and abs(middle[1] - 0.5) <= OFF_CENTRE:
        return None
    return middle


def correction(faces: Sequence[Face], size: Tuple[int, int], zoom: int, travel_s: float,
               people: int = 0) -> Optional[Tuple[float, float]]:
    """Step 7: the small turn (pan, tilt seconds) that brings the group back to the middle of a picture taken at `zoom`, or None when it
    is near enough, no one is in it, or fewer people are in it than step 3 found (`people`)."""
    middle = _off_centre(faces, size, people)
    return None if middle is None else turn_to(middle, zoom, travel_s)


def nudge(faces: Sequence[Face], size: Tuple[int, int], zoom: int, people: int,
          tangents: Tuple[float, float] = RALLY_TANGENTS) -> Optional[Tuple[float, float]]:
    """Step 7 for a camera that says where it is: correction's turn, in degrees."""
    middle = _off_centre(faces, size, people)
    return None if middle is None else _angles(middle, zoom, tangents)


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
