"""
Configuration management for Croom.

Handles loading, validation, and access to configuration settings.
"""

import logging
import os
import yaml
from dataclasses import dataclass, field, fields
from typing import Optional, Dict, Any, List
from pathlib import Path

logger = logging.getLogger(__name__)


# Default configuration paths
CONFIG_PATHS = [
    "/etc/croom/config.yaml",
    os.path.expanduser("~/.config/croom/config.yaml"),
    "config.yaml",
]

# Preferred location for runtime state on an installed device (the installer
# creates it and the croom service user owns it). See Config.resolve_data_dir().
SYSTEM_DATA_DIR = Path("/var/lib/croom")


def auto_to_default(value: str) -> str:
    """Translate the config's 'auto' (or empty) device selector into the 'default' the services expect."""
    return "default" if value in ("", "auto") else value


def _writable_dir(path: Path) -> bool:
    """True when path is an existing directory this process can write to."""
    return path.is_dir() and os.access(path, os.W_OK)



PLACEHOLDER_PREFIX = "REPLACE_WITH_"
DEFAULT_COMPANY = "Crystal Practice Management"   # whose room it is, on the door sign, when a config does not say


def without_placeholder(value: Any) -> str:
    """A config text, with blanks and the installer's REPLACE_WITH_... placeholder treated as unset."""
    text = str(value if value is not None else "").strip()
    return "" if text.startswith(PLACEHOLDER_PREFIX) else text


@dataclass
class RoomConfig:
    """Room-specific configuration."""
    name: str = "Conference Room"
    location: str = ""   # what people call the place; "" shows the room's name alone
    timezone: str = "UTC"
    company: str = DEFAULT_COMPANY   # the practice's name beside the logo on the door sign; "" shows the logo alone

    def __post_init__(self):
        self.location = without_placeholder(self.location)


# Dialogs Zoom draws over the TV that are only a notice (one button): pressed at once and kept on the table as a line under the title.
DEFAULT_ACKNOWLEDGE = ["is being recorded", "is being transcribed", "AI Companion", "Zoom AI is on",
                       "has been muted", "has been unmuted", "recording has started", "recording has stopped"]
# Zoom 6.5.0's reaction picker has no emoji: its items are named, and the table offers them by those names.
DEFAULT_REACTIONS = ["Thumbs Up", "Clap", "Heart", "Joy", "Open Mouth", "Tada"]


def _listed(value: Any, defaults: List[str], where: str = "a list setting") -> Any:
    """A list setting as a file gives it: nothing at all (`reactions:` with its lines deleted) is the defaults, a bare word is one item
    (not the letters it is spelt with), and anything else that is not a list (`reactions: no` is False in YAML) is the defaults with a
    warning, not a crash the first time the config is written out."""
    if value is None:
        return list(defaults)
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        logger.warning(f"{where} should be a list, not {value!r}: using the defaults")
        return list(defaults)
    return value


def _flag(value: Any, default: bool, where: str) -> bool:
    """An on/off setting as a file gives it. YAML turns a bare true/yes/on or false/no/off into a bool, but the same word in quotes is a
    string (and a string is true to Python, whatever it says): those words are read as they say. Nothing at all is the default, and
    anything else is the default with a warning, not a guess."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("true", "yes", "on"):
            return True
        if word in ("false", "no", "off"):
            return False
    logger.warning(f"{where} should be true or false, not {value!r}: using {str(default).lower()}")
    return default


@dataclass
class DialogsConfig:
    """What the agent does with the dialogs Zoom draws over the TV (spec 2026-10-13, section 5)."""
    acknowledge: List[str] = field(default_factory=lambda: list(DEFAULT_ACKNOWLEDGE))   # one-button notices pressed at once
    ask_everything: bool = False   # every dialog waits for a tap on the table, the acknowledgeable ones too

    def __post_init__(self):
        self.acknowledge = _listed(self.acknowledge, DEFAULT_ACKNOWLEDGE, "meeting.dialogs.acknowledge")
        self.ask_everything = _flag(self.ask_everything, False, "meeting.dialogs.ask_everything")


@dataclass
class ControlsConfig:
    """What the table offers from the meeting's controls (spec 2026-10-13, section 8)."""
    reactions: List[str] = field(default_factory=lambda: list(DEFAULT_REACTIONS))   # by the names Zoom's picker gives them

    def __post_init__(self):
        self.reactions = _listed(self.reactions, DEFAULT_REACTIONS, "meeting.controls.reactions")


def _section(cls, value: Any, where: str):
    """A nested section of the meeting config, as its dataclass. From a file it is a dict: a key the section does not know is left out
    with a warning (not a TypeError, which makes load_config throw the whole file away), and nothing at all, or anything that is not a
    mapping, is the defaults."""
    if isinstance(value, cls):
        return value
    if value is None:
        return cls()
    if not isinstance(value, dict):
        logger.warning(f"{where} should be a mapping of settings, not {value!r}: using the defaults")
        return cls()
    known = {f.name for f in fields(cls)}
    unknown = [str(key) for key in value if key not in known]
    if unknown:
        logger.warning(f"{where}: ignoring {', '.join(unknown)} (the settings are {', '.join(sorted(known))})")
    return cls(**{key: setting for key, setting in value.items() if key in known})


@dataclass
class MeetingConfig:
    """Meeting service configuration."""
    platforms: List[str] = field(default_factory=lambda: ["google_meet", "teams", "zoom"])
    default_platform: str = "auto"
    join_early_minutes: int = 1
    auto_leave: bool = True
    camera_default_on: bool = True
    mic_default_on: bool = True
    zoom_credentials_path: str = ""  # /etc/croom/zoom-credentials.json on a room device
    google_profile_dir: str = ""  # /var/lib/croom/meet-profile on a room device; empty means a guest browser
    kiosk: bool = True  # the TV browser fills the screen with no window chrome
    dialogs: DialogsConfig = field(default_factory=DialogsConfig)
    controls: ControlsConfig = field(default_factory=ControlsConfig)

    def __post_init__(self):
        self.dialogs = _section(DialogsConfig, self.dialogs, "meeting.dialogs")
        self.controls = _section(ControlsConfig, self.controls, "meeting.controls")


@dataclass
class CalendarConfig:
    """Calendar integration configuration."""
    providers: List[str] = field(default_factory=lambda: ["google", "microsoft"])
    sync_interval_seconds: int = 60
    google_credentials_path: str = ""
    google_calendar_id: str = ""  # the room's calendar address, e.g. c_18...@resource.calendar.google.com
    microsoft_tenant_id: str = ""
    microsoft_client_id: str = ""


@dataclass
class AIConfig:
    """AI features configuration."""
    enabled: bool = True
    backend: str = "auto"  # 'hailo', 'coral', 'nvidia', 'cpu', 'auto'

    # Feature toggles
    person_detection: bool = True
    noise_reduction: bool = True
    echo_cancellation: bool = True
    auto_framing: bool = True
    occupancy_counting: bool = True
    speaker_detection: bool = False  # Requires accelerator
    hand_raise_detection: bool = False  # Requires accelerator

    # Privacy
    privacy_mode: bool = False  # Disables all AI when True


@dataclass
class AudioConfig:
    """Audio configuration."""
    backend: str = "auto"  # 'pulseaudio', 'pipewire', 'auto'
    input_device: str = "auto"
    output_device: str = "auto"
    noise_reduction_level: str = "medium"  # 'off', 'light', 'medium', 'aggressive'
    echo_cancellation: bool = True


@dataclass
class VideoConfig:
    """Video configuration."""
    backend: str = "auto"  # 'v4l2', 'picamera', 'auto'
    device: str = "auto"
    resolution: str = "1080p"
    framerate: int = 30
    ptz_travel_seconds: float = 8.0  # how long the camera is driven to reach an end stop when homing


@dataclass
class DisplayConfig:
    """Display configuration."""
    backend: str = "auto"  # 'hdmi_cec', 'ddc', 'none', 'auto'
    power_on_boot: bool = True
    power_off_shutdown: bool = True
    touch_enabled: bool = True


@dataclass
class TvConfig:
    """The room's LG webOS TV (spec 2026-10-13 TV remote)."""
    enabled: bool = False
    host: str = ""          # the TV's address; Find TVs on the table fills it
    mac: str = ""           # for Wake-on-LAN (Mobile TV On must be enabled on the TV)
    pi_input: str = "HDMI_1"   # the input the room Pi is on
    keep_on: bool = True    # wake the TV at start, before a join, and after an unexpected off


@dataclass
class DashboardConfig:
    """Management dashboard connection configuration."""
    enabled: bool = True
    url: str = ""
    enrollment_token: str = ""
    heartbeat_interval_seconds: int = 30
    metrics_interval_seconds: int = 60


@dataclass
class ControlConfig:
    """Room control page served by the agent on the local network."""
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    screensaver: str = "info"  # what the TV shows when idle until someone picks another style on the room page
    camera_presets: List[str] = field(default_factory=lambda: ["Wide", "Table", "Whiteboard"])  # the three slot names


@dataclass
class UpdateConfig:
    """Update configuration."""
    auto_check: bool = True
    auto_install: bool = False  # Require manual admin approval
    check_interval_hours: int = 24
    channel: str = "stable"  # 'stable', 'beta', 'nightly'


@dataclass
class SecurityConfig:
    """Security configuration."""
    admin_pin: str = ""  # For local UI access
    ssh_enabled: bool = True
    require_encryption: bool = True


@dataclass
class Config:
    """Main configuration class."""
    version: int = 2
    room: RoomConfig = field(default_factory=RoomConfig)
    meeting: MeetingConfig = field(default_factory=MeetingConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    tv: TvConfig = field(default_factory=TvConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)

    # Platform override (usually auto-detected)
    platform_type: str = "auto"  # 'rpi5', 'rpi4', 'pc', 'auto'

    # Directory for runtime state such as the dashboard enrollment file.
    # Empty means: /var/lib/croom when writable, otherwise $XDG_DATA_HOME/croom.
    data_dir: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Create Config from dictionary."""
        config = cls()

        if "version" in data:
            config.version = data["version"]

        if "platform_type" in data:
            config.platform_type = data["platform_type"]
        if "data_dir" in data:
            config.data_dir = data["data_dir"]

        if "room" in data:
            config.room = RoomConfig(**data["room"])

        if "meeting" in data:
            config.meeting = MeetingConfig(**data["meeting"])

        if "calendar" in data:
            config.calendar = CalendarConfig(**data["calendar"])

        if "ai" in data:
            config.ai = AIConfig(**data["ai"])

        if "audio" in data:
            config.audio = AudioConfig(**data["audio"])

        if "video" in data:
            config.video = VideoConfig(**data["video"])

        if "display" in data:
            config.display = DisplayConfig(**data["display"])

        if "tv" in data:
            config.tv = TvConfig(**data["tv"])

        if "dashboard" in data:
            config.dashboard = DashboardConfig(**data["dashboard"])
        if "control" in data:
            config.control = ControlConfig(**data["control"])

        if "updates" in data:
            config.updates = UpdateConfig(**data["updates"])

        if "security" in data:
            config.security = SecurityConfig(**data["security"])

        return config

    def to_dict(self) -> Dict[str, Any]:
        """Convert Config to dictionary."""
        return {
            "version": self.version,
            "platform_type": self.platform_type,
            "data_dir": self.data_dir,
            "room": {
                "name": self.room.name,
                "location": self.room.location,
                "timezone": self.room.timezone,
                "company": self.room.company,
            },
            "meeting": {
                "platforms": self.meeting.platforms,
                "default_platform": self.meeting.default_platform,
                "join_early_minutes": self.meeting.join_early_minutes,
                "auto_leave": self.meeting.auto_leave,
                "camera_default_on": self.meeting.camera_default_on,
                "mic_default_on": self.meeting.mic_default_on,
                "zoom_credentials_path": self.meeting.zoom_credentials_path,
                "google_profile_dir": self.meeting.google_profile_dir,
                "kiosk": self.meeting.kiosk,
                "dialogs": {"acknowledge": list(self.meeting.dialogs.acknowledge), "ask_everything": self.meeting.dialogs.ask_everything},
                "controls": {"reactions": list(self.meeting.controls.reactions)},
            },
            "calendar": {
                "providers": self.calendar.providers,
                "sync_interval_seconds": self.calendar.sync_interval_seconds,
                "google_credentials_path": self.calendar.google_credentials_path,
                "google_calendar_id": self.calendar.google_calendar_id,
            },
            "ai": {
                "enabled": self.ai.enabled,
                "backend": self.ai.backend,
                "person_detection": self.ai.person_detection,
                "noise_reduction": self.ai.noise_reduction,
                "echo_cancellation": self.ai.echo_cancellation,
                "auto_framing": self.ai.auto_framing,
                "occupancy_counting": self.ai.occupancy_counting,
                "speaker_detection": self.ai.speaker_detection,
                "hand_raise_detection": self.ai.hand_raise_detection,
                "privacy_mode": self.ai.privacy_mode,
            },
            "audio": {
                "backend": self.audio.backend,
                "input_device": self.audio.input_device,
                "output_device": self.audio.output_device,
                "noise_reduction_level": self.audio.noise_reduction_level,
                "echo_cancellation": self.audio.echo_cancellation,
            },
            "video": {
                "backend": self.video.backend,
                "device": self.video.device,
                "resolution": self.video.resolution,
                "framerate": self.video.framerate,
                "ptz_travel_seconds": self.video.ptz_travel_seconds,
            },
            "display": {
                "backend": self.display.backend,
                "power_on_boot": self.display.power_on_boot,
                "power_off_shutdown": self.display.power_off_shutdown,
                "touch_enabled": self.display.touch_enabled,
            },
            "tv": {
                "enabled": self.tv.enabled,
                "host": self.tv.host,
                "mac": self.tv.mac,
                "pi_input": self.tv.pi_input,
                "keep_on": self.tv.keep_on,
            },
            "dashboard": {
                "enabled": self.dashboard.enabled,
                "url": self.dashboard.url,
                "heartbeat_interval_seconds": self.dashboard.heartbeat_interval_seconds,
                "metrics_interval_seconds": self.dashboard.metrics_interval_seconds,
            },
            "control": {
                "enabled": self.control.enabled,
                "host": self.control.host,
                "port": self.control.port,
                "screensaver": self.control.screensaver,
                "camera_presets": list(self.control.camera_presets),
            },
            "updates": {
                "auto_check": self.updates.auto_check,
                "auto_install": self.updates.auto_install,
                "check_interval_hours": self.updates.check_interval_hours,
                "channel": self.updates.channel,
            },
            "security": {
                "ssh_enabled": self.security.ssh_enabled,
                "require_encryption": self.security.require_encryption,
            },
        }

    def resolve_data_dir(self) -> Path:
        """
        Directory for runtime state files (spec section 4.5).

        Order: data_dir when set (created if missing); otherwise /var/lib/croom
        when it exists and is writable; otherwise $XDG_DATA_HOME/croom, which
        defaults to ~/.local/share/croom (created if missing).
        """
        if self.data_dir:
            path = Path(self.data_dir).expanduser()
        elif _writable_dir(SYSTEM_DATA_DIR):
            return SYSTEM_DATA_DIR
        else:
            xdg_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
                os.path.expanduser("~"), ".local", "share"
            )
            path = Path(xdg_home) / "croom"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, path: Optional[str] = None):
        """Save configuration to file."""
        if path is None:
            path = CONFIG_PATHS[0]

        # Ensure directory exists
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w") as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False)


def load_config(path: Optional[str] = None) -> Config:
    """
    Load configuration from file.

    Args:
        path: Path to config file. If None, searches default locations.

    Returns:
        Config object with loaded or default settings.
    """
    if path is not None:
        paths_to_try = [path]
    else:
        paths_to_try = CONFIG_PATHS

    for config_path in paths_to_try:
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    data = yaml.safe_load(f)
                    if data:
                        return Config.from_dict(data)
            except Exception as e:
                print(f"Warning: Failed to load config from {config_path}: {e}")

    # Return default config
    return Config()


def get_config_path() -> Optional[str]:
    """Get the path to the active config file."""
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            return path
    return None
