"""
Magnifying what the TV shows in a meeting (Ben, 2026-11-22).

A screen someone shares is drawn by the platform at the size that fits the TV, and from the far end of the table its text is too
small to read. Neither Zoom's Meeting SDK nor Google Meet has a call that enlarges it for a viewer, so the room does it in its own
browser, the same way for both: the whole page the TV shows is scaled up about a point, as a magnifying glass does it, and the table
moves the point. Nothing of the platform's page is pressed or rearranged, so its controls go on reading as they did.

It is a magnifying glass, not a sharper picture: what is 20 pixels tall on the TV becomes 40, with no detail the platform did not draw.

The page's root element is given a transform. That makes it the box its fixed-position children are laid out in, which the window
was before, so while it is magnified the root is kept the size of the window, and the styles it had are put back at 1.
"""

import math
from typing import Any, Dict, Optional, Tuple

from croom.meeting.providers.base import MeetingControlError

MAX_ZOOM = 4.0
REFUSAL = "Send the zoom (1 to 4) and where to look (x and y, each from 0 to 1)"

# Run in the TV's page with [zoom, x, y]: the picture scaled by `zoom` with (x, y), fractions of its width and height, at the middle of
# the screen, as near as the picture's edges allow (the screen never shows past them). It answers what it did, {zoom, x, y}.
MAGNIFY = """([zoom, x, y]) => {
  const root = document.documentElement;
  const kept = window.__crystalMeetMagnify || (window.__crystalMeetMagnify = { styles: null });
  const NAMES = ["transform", "transformOrigin", "transition", "overflow", "width", "height"];
  if (!(zoom > 1)) {
    if (kept.styles) for (const name of NAMES) root.style[name] = kept.styles[name];
    kept.styles = null;
    return { zoom: 1, x: 0.5, y: 0.5 };
  }
  if (!kept.styles) {
    kept.styles = {};
    for (const name of NAMES) kept.styles[name] = root.style[name];
  }
  const width = window.innerWidth, height = window.innerHeight, half = 0.5 / zoom;
  const cx = Math.min(Math.max(x, half), 1 - half), cy = Math.min(Math.max(y, half), 1 - half);
  root.style.width = width + "px";
  root.style.height = height + "px";
  root.style.overflow = "hidden";
  root.style.transformOrigin = "0 0";
  root.style.transition = "transform 120ms linear";
  root.style.transform = "translate(" + (width * (0.5 - zoom * cx)) + "px, " + (height * (0.5 - zoom * cy)) + "px) scale(" + zoom + ")";
  return { zoom: zoom, x: cx, y: cy };
}"""


def _number(value: Any) -> Optional[float]:
    """A finite number as the table sends one (never True or False, which Python counts as 1 and 0), or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


class Magnifier:
    """What the TV is magnified to in this meeting: `zoom` (1 is the whole picture, up to MAX_ZOOM) and the point of the picture at the
    middle of the screen, `x` and `y` as fractions of its width and height. One per provider; reset() when a meeting ends."""

    def __init__(self) -> None:
        self.zoom, self.x, self.y = 1.0, 0.5, 0.5

    def reset(self) -> None:
        """The page it magnified is gone (the meeting ended), and the next one starts whole."""
        self.zoom, self.x, self.y = 1.0, 0.5, 0.5

    def state(self) -> Dict[str, Any]:
        """The `magnify` control of the table's model."""
        return {"available": True, "zoom": self.zoom, "x": self.x, "y": self.y, "max": MAX_ZOOM}

    def wanted(self, value: Any) -> Tuple[float, float, float]:
        """What the table asks for, {"zoom", "x", "y"} with any of them left out (what is left out stays as it is), as (zoom, x, y) within
        their bounds. Anything else is refused in words."""
        if not isinstance(value, dict) or not value or set(value) - {"zoom", "x", "y"}:
            raise MeetingControlError(REFUSAL)
        asked = {name: _number(value[name]) for name in value}
        if any(number is None for number in asked.values()):
            raise MeetingControlError(REFUSAL)
        zoom = min(max(asked.get("zoom", self.zoom), 1.0), MAX_ZOOM)
        x = min(max(asked.get("x", self.x), 0.0), 1.0)
        y = min(max(asked.get("y", self.y), 0.0), 1.0)
        return zoom, x, y

    async def set(self, page: Any, value: Any) -> Dict[str, Any]:
        """Magnify the TV's page as the table asks, and keep what the page says it did. The page failing is a MeetingControlError in words."""
        zoom, x, y = self.wanted(value)
        try:
            done = await page.evaluate(MAGNIFY, [zoom, x, y])
            self.zoom, self.x, self.y = float(done["zoom"]), float(done["x"]), float(done["y"])
        except Exception as e:  # noqa: BLE001 - the page gone or navigating: words for the table
            words = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            raise MeetingControlError(f"Could not magnify the TV: {words}") from e
        return self.state()
