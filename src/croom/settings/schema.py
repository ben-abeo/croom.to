"""
The settings schema (spec 2026-10-29, section 4): every setting of `config.yaml` that the Settings pages edit, with its label, its page,
its kind, the values it takes, and whether the room applies it at once (live) or after the room software restarts.

`validate(path, value)` turns what a page sent into what is written, or raises SettingError in words for the person at the screen.
`check_config(data, current)` checks a whole config as the root helper writes it: every setting the schema knows is checked where the
config has it, and the settings that would lock Settings out of the room (the room page turned off, or moved to another port or
address) are refused, since no page offers them.

The kinds: text, number, switch, choice, list, time, secret (shown as set or not, never read back) and credential (a key file's path,
which the credential pages set).
"""

import math
import re
import zoneinfo
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

KINDS = ("text", "number", "switch", "choice", "list", "time", "secret", "credential")
PAGES = ("room", "calendar", "meetings", "devices", "laptop", "ai")

CONTROL_DEFAULTS = {"enabled": True, "host": "0.0.0.0", "port": 8080}   # ControlConfig's own, for a config that says nothing
CONTROL_OFF = "The room page can't be turned off from Settings: Settings is on it"
CONTROL_MOVED = "The room page's {what} can't be changed from Settings: the room would not be found at its address"

Choices = Union[Sequence[Tuple[str, str]], Callable[[], Sequence[Tuple[str, str]]]]


class SettingError(ValueError):
    """A value a setting does not take, in words for the person at the screen. `errors` maps each setting's path to its words when a
    whole config is checked."""

    def __init__(self, words: str, errors: Optional[Dict[str, str]] = None):
        super().__init__(words)
        self.errors = dict(errors or {})


@dataclass(frozen=True)
class Setting:
    """One setting of config.yaml. `path` is its place in the file ("room.name"); `choices` are (value, label) pairs, or a function that
    gives them (the time zones); `range` is (lowest, highest) for a number; `live` is True when the room applies it at once."""
    path: str
    label: str
    page: str
    kind: str
    choices: Optional[Choices] = None
    range: Optional[Tuple[float, float]] = None
    live: bool = False
    help: str = ""
    integer: bool = False          # a number that is whole
    step: Optional[float] = None   # a number's or a time's step (a time's in minutes)
    max_length: int = 120          # text, a secret, each item of a list
    required: bool = False         # text that may not be blank; a list that may not be empty
    max_items: int = 20            # a list
    pattern: Optional[str] = None  # text that must match it whole (blank is allowed unless required)
    pattern_words: str = ""        # what the pattern asks for, in words

    def options(self) -> List[Tuple[str, str]]:
        choices = self.choices() if callable(self.choices) else self.choices
        return [(str(value), str(label)) for value, label in (choices or ())]

    def describe(self) -> Dict[str, Any]:
        """The setting as the page's form draws it."""
        entry: Dict[str, Any] = {"path": self.path, "label": self.label, "kind": self.kind, "live": self.live, "help": self.help}
        if self.choices is not None:
            entry["choices"] = [{"value": value, "label": label} for value, label in self.options()]
        if self.range is not None:
            entry["min"], entry["max"] = self.range
        if self.kind == "number":
            entry["integer"] = self.integer
        if self.step is not None:
            entry["step"] = self.step
        if self.kind in ("text", "secret", "list"):
            entry["max_length"] = self.max_length
        if self.kind == "list":
            entry["max_items"] = self.max_items
        if self.required:
            entry["required"] = True
        return entry


def time_zones() -> List[Tuple[str, str]]:
    """The system's time zones, sorted: what the Room page offers."""
    return [(name, name.replace("_", " ")) for name in sorted(zoneinfo.available_timezones())]


PLATFORMS = (("zoom", "Zoom"), ("google_meet", "Google Meet"), ("teams", "Teams"), ("webex", "Webex"))
# Every name croom.ai.backends.BACKEND_NAMES takes, and auto (kept here so that the root helper need not import the AI backends): a name the
# room accepts must never be one the schema refuses, or a hand-written config would block every save.
AI_BACKENDS = (("auto", "Automatic"), ("hailo", "Hailo"), ("coral", "Coral"), ("nvidia", "NVIDIA"), ("intel", "Intel (OpenVINO)"),
               ("openvino", "OpenVINO"), ("cpu", "Processor only"), ("onnx", "Processor only (ONNX)"))
URL = r"https?://[^\s/$.?#][^\s]*"
NO_SPACES = r"\S+"


SCHEMA: List[Setting] = [
    # Room
    Setting("room.name", "Name", "room", "text", required=True, max_length=80,
            help="On the door sign, the room page and the TV"),
    Setting("room.location", "Location", "room", "text", max_length=80, help="What people call the place, under the name"),
    Setting("room.company", "Company", "room", "text", max_length=80, help="Beside the logo on the door sign; blank shows the logo alone"),
    Setting("room.timezone", "Time zone", "room", "choice", choices=time_zones),
    # Calendar
    Setting("calendar.google_calendar_id", "Room calendar address", "calendar", "text", max_length=254, pattern=NO_SPACES,
            pattern_words="an address with no spaces", help="The room's calendar, e.g. c_18...@resource.calendar.google.com"),
    Setting("calendar.google_credentials_path", "Google service-account key", "calendar", "credential", max_length=4096),
    Setting("calendar.booking_start", "Bookings from", "calendar", "time", step=15, live=True,
            help="The first start time the door sign and the room page offer"),
    Setting("calendar.booking_end", "Bookings until", "calendar", "time", step=15, live=True,
            help="The time a booking made there ends by"),
    # Meetings
    Setting("meeting.platforms", "Platforms", "meetings", "list", choices=PLATFORMS, required=True, max_items=len(PLATFORMS)),
    Setting("meeting.camera_default_on", "Camera on at join", "meetings", "switch"),
    Setting("meeting.mic_default_on", "Microphone on at join", "meetings", "switch"),
    Setting("meeting.kiosk", "Kiosk", "meetings", "switch", help="The TV's browser fills the screen with no window around it"),
    Setting("meeting.dialogs.acknowledge", "Dialogs the room answers", "meetings", "list", max_items=40,
            help="Zoom's one-button notices, by words in them: pressed at once"),
    Setting("meeting.dialogs.ask_everything", "Ask about every dialog", "meetings", "switch",
            help="Every dialog waits for a tap at the table, the ones above too"),
    Setting("meeting.controls.reactions", "Reactions", "meetings", "list", max_items=12, max_length=40,
            help="By the names Zoom's picker gives them"),
    Setting("meeting.zoom_credentials_path", "Zoom SDK keys", "meetings", "credential", max_length=4096),
    # Devices
    Setting("audio.output_device", "Speaker", "devices", "text", max_length=200, help="auto, or the speaker's name"),
    Setting("audio.input_device", "Microphone", "devices", "text", max_length=200, help="auto, or the microphone's name"),
    Setting("video.device", "Camera", "devices", "text", max_length=200, help="auto, or the camera's device"),
    Setting("video.ptz_travel_seconds", "Camera travel time", "devices", "number", range=(1, 60), step=0.5,
            help="Seconds the camera takes from one end stop to the other"),
    Setting("control.camera_presets", "Camera preset names", "devices", "list", live=True, required=True, max_items=3, max_length=24),
    # Laptop switch
    Setting("laptop.enabled", "Laptop switch", "laptop", "switch"),
    Setting("laptop.gpio_pin", "GPIO pin", "laptop", "number", range=(0, 27), integer=True, help="BCM numbering: 17 is header pin 11"),
    Setting("laptop.active_low", "Switches when the pin goes low", "laptop", "switch",
            help="For a relay board that switches on when the pin goes low"),
    Setting("laptop.press_ms", "Press length (ms)", "laptop", "number", range=(50, 2000), integer=True, step=10),
    Setting("laptop.usb_id", "USB id of the camera", "laptop", "text", max_length=9, required=True, pattern=r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}",
            pattern_words="four hex digits, a colon and four more, as lsusb shows it (046d:0866)"),
    Setting("laptop.switch_wait_s", "Wait for a move (s)", "laptop", "number", range=(1, 60), step=0.5),
    # AI & dashboard
    Setting("ai.enabled", "AI features", "ai", "switch"),
    Setting("ai.backend", "AI backend", "ai", "choice", choices=AI_BACKENDS),
    Setting("ai.person_detection", "Person detection", "ai", "switch"),
    Setting("ai.noise_reduction", "Noise reduction", "ai", "switch"),
    Setting("ai.echo_cancellation", "Echo cancellation", "ai", "switch"),
    Setting("ai.auto_framing", "Auto framing", "ai", "switch"),
    Setting("ai.occupancy_counting", "Occupancy counting", "ai", "switch"),
    Setting("ai.speaker_detection", "Speaker detection", "ai", "switch", help="Needs an AI accelerator"),
    Setting("ai.hand_raise_detection", "Hand raise detection", "ai", "switch", help="Needs an AI accelerator"),
    Setting("ai.privacy_mode", "Privacy mode", "ai", "switch", help="Turns every AI feature off"),
    Setting("dashboard.enabled", "Dashboard", "ai", "switch"),
    Setting("dashboard.url", "Dashboard address", "ai", "text", max_length=500, pattern=URL, pattern_words="an http:// or https:// address"),
    Setting("dashboard.enrollment_token", "Enrollment token", "ai", "secret", max_length=500),
]

BY_PATH: Dict[str, Setting] = {s.path: s for s in SCHEMA}


def settings_of(page: str) -> List[Setting]:
    """A page's settings, in the order the page shows them."""
    return [s for s in SCHEMA if s.page == page]


def setting(path: str) -> Setting:
    try:
        return BY_PATH[path]
    except KeyError:
        raise SettingError(f"There is no setting {path}") from None


# --- one value


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TRUE, _FALSE = ("true", "yes", "on"), ("false", "no", "off")


def _text(s: Setting, value: Any, what: str = "") -> str:
    if not isinstance(value, str):
        raise SettingError(f"{what or s.label} must be text")
    text = value.strip()
    if _CONTROL.search(text):
        raise SettingError(f"{what or s.label} must be one line of text")
    if len(text) > s.max_length:
        raise SettingError(f"{what or s.label} is at most {s.max_length} characters")
    return text


def _number(s: Setting, value: Any) -> Union[int, float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SettingError(f"{s.label} must be a number")
    if s.integer:
        if value != int(value):
            raise SettingError(f"{s.label} must be a whole number")
        value = int(value)
    low, high = s.range or (-math.inf, math.inf)
    if not low <= value <= high:
        raise SettingError(f"{s.label} must be from {low:g} to {high:g}")
    return value


def _switch(s: Setting, value: Any) -> bool:
    # A hand-written file may say yes or "on" (the config reads those words as they say): a page sends true or false.
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
        return value.strip().lower() in _TRUE
    raise SettingError(f"{s.label} must be on or off")


def _choice(s: Setting, value: Any) -> str:
    options = s.options()
    if not isinstance(value, str) or value not in {v for v, _ in options}:
        raise SettingError(f"{s.label}: choose one of the list")
    return value


def _list(s: Setting, value: Any) -> List[str]:
    if isinstance(value, str):     # a bare word in a hand-written file is one item, as the config reads it
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise SettingError(f"{s.label} must be a list")
    allowed = {v for v, _ in s.options()} if s.choices is not None else None
    items: List[str] = []
    for item in value:
        text = _text(s, item, f"Each of {s.label.lower()}")
        if not text:
            continue                # an empty line in the list editor
        if allowed is not None and text not in allowed:
            raise SettingError(f"{s.label}: {text!r} is not one of the choices")
        if text not in items:
            items.append(text)
    if len(items) > s.max_items:
        raise SettingError(f"{s.label} has at most {s.max_items}")
    if s.required and not items:
        raise SettingError(f"{s.label} needs at least one")
    return items


def _time(s: Setting, value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 24 * 60:
        # YAML 1.1 reads an unquoted 18:00 as 1080 (base 60): what the person wrote was a time
        value = f"{value // 60:02d}:{value % 60:02d}"
    match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", value) if isinstance(value, str) else None
    if not match:
        raise SettingError(f"{s.label} is a time, HH:MM")
    hours, minutes = int(match.group(1)), int(match.group(2))
    step = int(s.step or 1)
    if minutes % step:
        raise SettingError(f"{s.label} is on the quarter hour" if step == 15 else f"{s.label} is in steps of {step} minutes")
    return f"{hours:02d}:{minutes:02d}"


def _credential(s: Setting, value: Any) -> str:
    text = _text(s, value)
    if text and not text.startswith("/"):
        raise SettingError(f"{s.label} is a file's full path")
    return text


def validate(path: str, value: Any) -> Any:
    """The value to write for the setting at `path`, from what was sent; SettingError, in words, when the setting does not take it."""
    s = setting(path)
    if s.kind == "number":
        return _number(s, value)
    if s.kind == "switch":
        return _switch(s, value)
    if s.kind == "choice":
        return _choice(s, value)
    if s.kind == "list":
        return _list(s, value)
    if s.kind == "time":
        return _time(s, value)
    if s.kind == "credential":
        return _credential(s, value)
    text = _text(s, value)            # text and secret
    if s.required and not text:
        raise SettingError(f"{s.label} can't be blank")
    if text and s.pattern and not re.fullmatch(s.pattern, text):
        raise SettingError(f"{s.label} is {s.pattern_words}")
    return text


# --- a whole config

_MISSING = object()


def get_path(data: Any, path: str, default: Any = _MISSING) -> Any:
    """The value at a dotted path of a config mapping, or `default` (a KeyError without one) where the config does not have it."""
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is _MISSING:
                raise KeyError(path)
            return default
        node = node[part]
    return node


def set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    """Put a value at a dotted path of a config mapping, making the sections on the way (one that is empty or not a mapping is replaced)."""
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def _control(data: Any, key: str) -> Any:
    section = data.get("control") if isinstance(data, dict) else None
    value = section.get(key) if isinstance(section, dict) else None
    return CONTROL_DEFAULTS[key] if value is None else value


def config_errors(data: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """What is wrong with a whole config, as {path: words}; empty when nothing is. `current` is the config on disk now, when there is one:
    the room page's port and address may not change from it."""
    errors: Dict[str, str] = {}
    for s in SCHEMA:
        value = get_path(data, s.path, None)
        if value is None:   # not in the file, or a blank line: the room's default
            continue
        try:
            validate(s.path, value)
        except SettingError as e:
            errors[s.path] = str(e)
    if "calendar.booking_start" not in errors and "calendar.booking_end" not in errors:
        start, end = (get_path(data, f"calendar.booking_{name}", None) for name in ("start", "end"))
        start = validate("calendar.booking_start", start) if start is not None else "08:00"
        end = validate("calendar.booking_end", end) if end is not None else "18:00"
        if end <= start:
            errors["calendar.booking_end"] = "Bookings until must be after Bookings from"
    enabled = _control(data, "enabled")
    if enabled is False or (isinstance(enabled, str) and enabled.strip().lower() in _FALSE):
        errors["control.enabled"] = CONTROL_OFF
    if current is not None:
        for key, what in (("port", "port"), ("host", "address")):
            if _control(data, key) != _control(current, key):
                errors[f"control.{key}"] = CONTROL_MOVED.format(what=what)
    return errors


def check_config(data: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> None:
    """Raise SettingError, with every setting's words in `errors` and the first as its own, when config_errors finds anything."""
    errors = config_errors(data, current)
    if errors:
        path, words = next(iter(errors.items()))
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        raise SettingError(words + more, errors)
