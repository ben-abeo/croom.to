"""
Abstract base class for meeting providers.

All meeting platform providers must implement this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List, Callable
import re


class MeetingState(Enum):
    """Meeting lifecycle states."""
    IDLE = "idle"
    JOINING = "joining"
    IN_LOBBY = "in_lobby"
    CONNECTED = "connected"
    LEAVING = "leaving"
    ERROR = "error"


class MeetingControlError(RuntimeError):
    """A control the platform could not carry out just now (a button not on the screen yet, a page that does not
    answer): its words are for the person at the table, and the meeting goes on."""


@dataclass
class MeetingInfo:
    """Information about a meeting."""
    platform: str
    meeting_id: str
    meeting_url: str
    title: str = ""
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    organizer: str = ""
    calendar_event_id: str = ""

    # Runtime state
    state: MeetingState = MeetingState.IDLE
    participants: List[str] = field(default_factory=list)
    is_muted: bool = False
    is_camera_on: bool = True
    error_message: str = ""

    # What the table draws from (spec 2026-10-13, section 3), as the platform has it: filled and kept by the provider.
    controls: Dict[str, Any] = field(default_factory=dict)                                   # see controls_from
    people: Dict[str, Any] = field(default_factory=lambda: {"count": 0, "list": []})        # the roster, at most 50 listed
    chat: Dict[str, Any] = field(default_factory=lambda: {"unread": 0, "messages": []})     # the last 20 incoming messages
    notice: str = ""                                    # what the meeting says of itself (recorded, transcribed), from the notices pressed at once
    dialog: Optional[Dict[str, Any]] = None             # the dialog waiting for a tap on the table: {id, title, text, buttons}
    recording: bool = False
    locked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "meeting_id": self.meeting_id,
            "meeting_url": self.meeting_url,
            "title": self.title,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "organizer": self.organizer,
            "state": self.state.value,
            "participants": self.participants,
            "is_muted": self.is_muted,
            "is_camera_on": self.is_camera_on,
            "controls": self.controls,
            "people": self.people,
            "chat": self.chat,
            "notice": self.notice,
            "dialog": self.dialog,
            "recording": self.recording,
            "locked": self.locked,
        }


HOST_ROLES = ("host", "co-host")


def _name(text: Any) -> str:
    """A name as the page compares them: lower case, letters and digits only ("Thumbs Up", "thumbs_up" and " THUMBSUP " are one name)."""
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def controls_from(reading: Dict[str, Any], reactions: List[str]) -> Dict[str, Any]:
    """The table's control model (spec 2026-10-13, section 3) from the page's reading of the meeting: a control is available when the
    client has its button on the screen (or, for the host's, the role allows it), and its state is what the reading says."""
    buttons = reading.get("buttons") or {}
    me = reading.get("me") or {}
    role = reading.get("role") or "participant"
    hosting = role in HOST_ROLES
    waiting = [p for p in (reading.get("participants") or []) if p.get("waiting")]

    def button(name, **extra):
        state = buttons.get(name)
        return {"available": state is not None, **(state or {}), **extra}

    # The table offers the configured reactions by Zoom's names. When the page says which the picker holds (it does not yet: the picker is
    # only open during a control), those of them it has; with none to offer the Reactions button has nothing to open.
    held = (buttons.get("reactions") or {}).get("choices")
    offered = [r for r in reactions if held is None or _name(r) in {_name(h) for h in held}]
    picker = button("reactions", choices=offered)
    picker["available"] = picker["available"] and bool(offered)

    return {
        "mute": {"available": True, "on": not bool(me.get("muted"))},
        "camera": button("camera"),
        "raise_hand": button("raise_hand"),
        "reactions": picker,
        "captions": button("captions"),
        "layout": button("layout", choices=["speaker", "gallery"]),
        "hide_self": button("hide_self"),
        "pin": {"available": False},   # the client's per-participant menu is not driven in this version
        "leave": {"available": True},
        "host": {
            "role": role,
            "mute_all": {"available": hosting},
            "ask_unmute": {"available": hosting},
            "record": {"available": hosting, "on": bool(reading.get("recording"))},
            "lock": {"available": hosting, "on": bool(reading.get("locked"))},
            "waiting_room": {"available": hosting, "waiting": [{"id": p.get("id"), "name": p.get("name", "")} for p in waiting]},
            "mute_one": {"available": hosting},
            "remove": {"available": hosting},
            "end": {"available": role == "host"},
        },
    }


class DialogPolicy:
    """What the agent does with a dialog Zoom draws over the TV (spec 2026-10-13, section 5), in one place: a notice with a single button
    whose words are on the list is pressed at once and kept on the table as a line; any other dialog, and every dialog when
    ask_everything is set, waits for a tap on the table."""

    # Notices that say a state changed, which is read from Zoom again once the notice is gone: the room's own mute (the Mute button shows
    # it), and a recording or a transcript that stopped (what the meeting disclosed stays as it was). They add no line under the title.
    STATE_ONLY = ("has been muted", "has been unmuted", "have been muted", "have been unmuted",
                  "recording has stopped", "transcription has stopped")

    def __init__(self, acknowledge: List[str], ask_everything: bool = False):
        # A blank phrase is in every text, and a bare word is one phrase, not the letters it is spelt with.
        phrases = [acknowledge] if isinstance(acknowledge, str) else (acknowledge or [])
        self.acknowledge = [p.strip().lower() for p in phrases if isinstance(p, str) and p.strip()]
        self.ask_everything = bool(ask_everything)

    @staticmethod
    def _words(dialog: Dict[str, Any]) -> str:
        return f"{dialog.get('title') or ''} {dialog.get('text') or ''}".lower()

    def decide(self, dialog: Dict[str, Any]) -> str:
        """'acknowledge' or 'ask'."""
        buttons = dialog.get("buttons") or []
        if self.ask_everything or len(buttons) != 1:
            return "ask"    # a dialog with a choice in it is never answered for the room
        words = self._words(dialog)
        return "acknowledge" if any(p in words for p in self.acknowledge) else "ask"

    def answer(self, dialog: Dict[str, Any]) -> str:
        """The button to press for an acknowledged notice: the last one, which Zoom draws as the affirmative (its footer puts cancel
        first and OK last); for a notice with one button, that one."""
        buttons = dialog.get("buttons") or []
        return str(buttons[-1]) if buttons else ""

    def disclosure(self, dialog: Dict[str, Any]) -> str:
        """The sentence the table keeps for an acknowledged notice: the first that holds a phrase of the list (what the notice is about:
        recorded, transcribed, AI Companion), else the first; empty for a notice that only says a state changed."""
        if any(p in self._words(dialog) for p in self.STATE_ONLY):
            return ""
        text = (dialog.get("text") or dialog.get("title") or "").strip()
        sentences = [part.strip().rstrip(".") for part in text.split(". ")]
        sentences = [sentence for sentence in sentences if sentence]
        if not sentences:
            return ""
        about = next((sentence for sentence in sentences if any(p in sentence.lower() for p in self.acknowledge)), sentences[0])
        return about + "."


class MeetingProvider(ABC):
    """
    Abstract base class for meeting platform providers.

    Each provider handles joining, controlling, and leaving
    meetings on a specific platform.
    """

    def __init__(self):
        self._state = MeetingState.IDLE
        self._current_meeting: Optional[MeetingInfo] = None
        self._state_callbacks: List[Callable[[MeetingState], None]] = []

    @property
    @abstractmethod
    def name(self) -> str:
        """Return provider name (e.g., 'google_meet')."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Return human-readable provider name."""
        pass

    @property
    def state(self) -> MeetingState:
        """Get current meeting state."""
        return self._state

    @property
    def current_meeting(self) -> Optional[MeetingInfo]:
        """Get current meeting info."""
        return self._current_meeting

    def add_state_callback(self, callback: Callable[[MeetingState], None]) -> None:
        """Add callback for state changes."""
        self._state_callbacks.append(callback)

    def _set_state(self, state: MeetingState) -> None:
        """Update state and notify callbacks."""
        self._state = state
        if self._current_meeting:
            self._current_meeting.state = state

        for callback in self._state_callbacks:
            try:
                callback(state)
            except Exception:
                pass

    @classmethod
    @abstractmethod
    def can_handle_url(cls, url: str) -> bool:
        """
        Check if this provider can handle a meeting URL.

        Args:
            url: Meeting URL or code

        Returns:
            True if this provider can handle the URL
        """
        pass

    @classmethod
    @abstractmethod
    def extract_meeting_id(cls, url: str) -> Optional[str]:
        """
        Extract meeting ID from URL.

        Args:
            url: Meeting URL

        Returns:
            Meeting ID or None
        """
        pass

    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the provider.

        Set up browser, load required resources, etc.
        """
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """
        Shutdown the provider.

        Close browser, release resources, etc.
        """
        pass

    @abstractmethod
    async def join_meeting(
        self,
        meeting_url: str,
        display_name: str = "Conference Room",
        camera_on: bool = True,
        mic_on: bool = True,
        passcode: Optional[str] = None
    ) -> MeetingInfo:
        """
        Join a meeting.

        Args:
            meeting_url: Meeting URL or code
            display_name: Name to display in meeting
            camera_on: Start with camera enabled
            mic_on: Start with microphone enabled
            passcode: The meeting's passcode, typed on the room page or read from the booking; only Zoom uses it,
                and it wins over a link's own pwd

        Returns:
            MeetingInfo with connection details
        """
        pass

    @abstractmethod
    async def leave_meeting(self) -> None:
        """Leave the current meeting."""
        pass

    @abstractmethod
    async def toggle_camera(self) -> bool:
        """
        Toggle camera on/off.

        Returns:
            New camera state (True = on)
        """
        pass

    @abstractmethod
    async def toggle_mute(self) -> bool:
        """
        Toggle microphone mute.

        Returns:
            New mute state (True = muted)
        """
        pass

    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        """Carry out a control from the table (spec 2026-10-13, section 3) and answer with the state it left; the base offers none.

        Args:
            name: a key of the control model (raise_hand, reactions, captions, layout, hide_self, host.record, dialog, chat.seen, ...)
            value: the choice the control takes (a reaction, a view, a participant's id, a dialog's button), when it takes one

        Raises:
            MeetingControlError: in words for the table, when the platform will not or cannot just now
        """
        raise MeetingControlError("Not available in this meeting")

    async def set_camera(self, on: bool) -> bool:
        """
        Set camera state.

        Args:
            on: True to enable camera

        Returns:
            New camera state
        """
        if self._current_meeting and self._current_meeting.is_camera_on != on:
            return await self.toggle_camera()
        return self._current_meeting.is_camera_on if self._current_meeting else False

    async def set_mute(self, muted: bool) -> bool:
        """
        Set mute state.

        Args:
            muted: True to mute

        Returns:
            New mute state
        """
        if self._current_meeting and self._current_meeting.is_muted != muted:
            return await self.toggle_mute()
        return self._current_meeting.is_muted if self._current_meeting else False

    def get_status(self) -> Dict[str, Any]:
        """Get provider status."""
        return {
            "provider": self.name,
            "state": self._state.value,
            "meeting": self._current_meeting.to_dict() if self._current_meeting else None,
        }


def detect_platform(url: str) -> Optional[str]:
    """
    Detect meeting platform from URL.

    Args:
        url: Meeting URL

    Returns:
        Platform name or None
    """
    url_lower = url.lower()

    if "meet.google.com" in url_lower or "g.co/meet" in url_lower:
        return "google_meet"
    elif "teams.microsoft.com" in url_lower or "teams.live.com" in url_lower:
        return "teams"
    elif "zoom.us" in url_lower or "zoomgov.com" in url_lower:
        return "zoom"
    elif "webex.com" in url_lower:
        return "webex"

    return None
