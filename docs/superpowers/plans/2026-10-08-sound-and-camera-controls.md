# Sound and Camera Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The room page gets a Sound panel (the room speaker's volume and mute) and a Camera panel (hold-to-move pan and tilt, zoom, Home, three timed presets, a one-time Set up flow), the TV shows a live camera preview while the panel is open and the room is idle, and `croom --check-meet` lists the devices the browser sees.

**Architecture:** A new `devices` service in the agent owns two small modules: `RoomVolume` drives PipeWire through `pw-dump` and `wpctl`; `RoomCamera` drives the Logitech MeetUp's V4L2 speed and zoom controls on a second file descriptor beside Chromium's stream, with a dead-reckoned position (seconds of travel from the end stops) that presets are stored against. The control service exposes both on new JSON routes and in `/api/status`; the room page and the TV page render from that status. A `SettingsStore` keeps the screensaver choice, the camera home and the presets in the existing `control-settings.json`.

**Tech Stack:** Python 3.12, asyncio subprocesses, V4L2 ioctls through `src/croom/video/v4l2_ioctl.py`, aiohttp control service, plain HTML/CSS/JS pages tested with Playwright (sync API), pytest with `asyncio_mode = auto`.

**Spec:** `docs/superpowers/specs/2026-10-08-sound-and-camera-controls-design.md`

## Global Constraints

- Internal names stay `croom`; what people see says Crystal Meet.
- Branch `room-devices` (cut from `tv-screensaver`); it lands after PR #2.
- The upstream audio and video services, and the Teams and Webex providers, are not touched.
- Every POST is JSON-only (415 otherwise) and answers with the block it changed; refusals are 400 (bad input) or 409 (device unavailable, not ready, interrupted) with `{"error": "<reason>"}`.
- Volume levels are 0..100; the camera's zoom is clamped to the queried range (the MeetUp: 100..500); pan and tilt directions are exactly -1, 0 or 1; the watchdog stops motion 1.5 s after the last `move`; the page re-sends every 750 ms while held; preset slots are 1..3; the preview expires 180 s after the last renewal.
- Config keys: `audio.output_device` (exists, default `auto`), `video.device` (exists, default `auto`), `video.ptz_travel_seconds` (new, default 8.0), `control.camera_presets` (new, default `["Wide", "Table", "Whiteboard"]`).
- Settings file keys: `screensaver` (unchanged), `camera: {"home": {"pan_s", "tilt_s"}, "presets": {"1": {"pan_s", "tilt_s", "zoom"}}}`; atomic writes, mode 600.
- Tests: `.venv/bin/pytest -q -p no:cacheprovider <path>`. Before the final commit: `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh room-devices | grep "GATE:"` (81 upstream failures is the baseline).
- Commit messages are plain, no attribution lines.

## Review Focus

1. A camera unplugged mid-meeting: the next camera POST answers 409 with a reason and the panel disappears on the next status, nothing raises into the control service (Task 4, `test_a_failing_control_call_makes_the_camera_unavailable`).
2. A second command while the camera is homing or recalling: the long move stops, the position becomes unknown, the new command runs (Task 5, `test_a_new_command_interrupts_a_long_move_and_the_position_is_unknown`).
3. The page goes quiet while an arrow is held (tablet put to sleep, Wi-Fi drop): motion stops on its own (Task 4, `test_motion_stops_on_its_own_when_the_page_goes_quiet`).
4. `wpctl` reports a level above 1.0 (PipeWire allows up to 1.5): the page shows 100, never more (Task 3, `test_levels_above_one_show_as_one_hundred`).
5. The preview is requested during a meeting, or left on when a meeting starts: refused with 409, and the TV never swaps the meeting for the camera picture (Task 7, `test_preview_is_refused_during_a_meeting`; Task 9, `test_preview_never_shows_during_a_meeting`).

---

### Task 1: `SettingsStore`

**Files:**
- Create: `src/croom/control/settings.py`
- Modify: `src/croom/control/service.py` (the screensaver's `_load_style`/`_save_style`, constructor, `from_config`)
- Create: `tests/unit/control/test_settings.py`

**Interfaces:**
- Produces: `SettingsStore(path)` with `path` property, `load() -> dict`, `get(key, default=None)`, `save(key, value)`; `ControlService(config, meeting=None, calendar=None, devices=None, store=None)` and `ControlService.from_config(config, meeting=None, calendar=None, devices=None, store=None)`; the service's `self._store` and `self._devices`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/control/test_settings.py`:

```python
"""
One small JSON file next to the agent's state holds the screensaver choice and the
camera's home and presets (spec 2026-10-08 sound and camera, section 4.5).
"""

from croom.control.settings import SettingsStore


def test_missing_file_reads_as_empty_and_save_creates_it_private(tmp_path):
    store = SettingsStore(tmp_path / "state" / "control-settings.json")
    assert store.load() == {} and store.get("screensaver", "info") == "info"
    store.save("screensaver", "bounce")
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    assert SettingsStore(store.path).get("screensaver") == "bounce"


def test_keys_are_kept_side_by_side(tmp_path):
    store = SettingsStore(tmp_path / "control-settings.json")
    store.save("screensaver", "quiet")
    store.save("camera", {"home": {"pan_s": 3.2, "tilt_s": 1.0}, "presets": {}})
    again = SettingsStore(store.path)
    assert again.get("screensaver") == "quiet"
    assert again.get("camera")["home"] == {"pan_s": 3.2, "tilt_s": 1.0}


def test_a_corrupt_file_reads_as_empty_and_is_replaced_on_save(tmp_path):
    path = tmp_path / "control-settings.json"
    path.write_text("{not json")
    store = SettingsStore(path)
    assert store.get("screensaver") is None
    store.save("screensaver", "brand")
    assert SettingsStore(path).load() == {"screensaver": "brand"}


def test_a_file_that_is_not_an_object_reads_as_empty(tmp_path):
    path = tmp_path / "control-settings.json"
    path.write_text("[1, 2]")
    assert SettingsStore(path).load() == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_settings.py`
Expected: FAIL at import, `croom.control.settings` does not exist.

- [ ] **Step 3: Write the store**

Create `src/croom/control/settings.py`:

```python
"""
Small settings kept next to the agent's state: the screensaver choice and the
camera's home and presets. One JSON file, written atomically, readable by the
service user only (spec 2026-10-08 sound and camera, section 4.5).
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict


class SettingsStore:
    def __init__(self, path):
        self._path = Path(path)
        self._data: Dict[str, Any] = self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Dict[str, Any]:
        """The file's contents; a missing, unreadable or broken file is simply empty."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def save(self, key: str, value: Any) -> None:
        """Set one key and write the whole file atomically, mode 600."""
        self._data[key] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=str(self._path.parent), prefix=f".{self._path.name}-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle)
            os.chmod(temp, 0o600)
            os.replace(temp, self._path)
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
```

- [ ] **Step 4: Move the control service's screensaver onto the store**

In `src/croom/control/service.py`:

- add `from croom.control.settings import SettingsStore` to the imports;
- change the constructor signature to `def __init__(self, config=None, meeting=None, calendar=None, devices=None, store=None)` and replace the line `self._settings_file = Path(self.config.get("settings_file", "control-settings.json"))` with:

```python
        self._store = store or SettingsStore(self.config.get("settings_file", "control-settings.json"))
        self._devices = devices
```

- replace `_load_style` and `_save_style`:

```python
    def _load_style(self) -> str:
        """The stored style, else the configured default; a missing or broken file is not an error."""
        style = self._store.get("screensaver")
        if style in STYLES:
            return style
        return self._default_style if self._default_style in STYLES else "info"

    def _save_style(self, style: str) -> None:
        self._store.save("screensaver", style)
```

- in `_handle_set_screensaver`, the existing `except OSError` log line reads `self._settings_file`; change it to `self._store.path`;
- change `from_config` to `def from_config(cls, config, meeting=None, calendar=None, devices=None, store=None)` and pass `devices=devices, store=store` to `cls(...)`;
- delete the now unused imports `json`, `os`, `tempfile` from the module if nothing else uses them (`ruff check --select F src/croom/control/service.py` tells you).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_settings.py tests/unit/control/test_service.py`
Expected: PASS, including every `TestScreensaver` test (same file, same format, mode 600).

- [ ] **Step 6: Commit**

```bash
git add src/croom/control/settings.py src/croom/control/service.py tests/unit/control/test_settings.py
git commit -m "feat(control): a settings store shared by the screensaver choice and the camera"
```

---

### Task 2: Config keys

**Files:**
- Modify: `src/croom/core/config.py` (`VideoConfig`, `ControlConfig`, `to_dict`)
- Modify: `tests/unit/core/test_config.py`

**Interfaces:**
- Produces: `config.video.ptz_travel_seconds: float` (default 8.0), `config.control.camera_presets: List[str]` (default `["Wide", "Table", "Whiteboard"]`), both round-tripping through `to_dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/core/test_config.py`:

```python


class TestRoomDeviceConfig:
    """Volume and camera settings (spec 2026-10-08 sound and camera, section 4.5)."""

    def test_defaults(self):
        config = Config()
        assert config.audio.output_device == "auto"
        assert config.video.device == "auto"
        assert config.video.ptz_travel_seconds == 8.0
        assert config.control.camera_presets == ["Wide", "Table", "Whiteboard"]

    def test_round_trips_through_dict(self):
        config = Config.from_dict({"audio": {"output_device": "HDMI"},
                                   "video": {"device": "/dev/video2", "ptz_travel_seconds": 6.5},
                                   "control": {"camera_presets": ["Room", "Desk", "Board"]}})
        again = Config.from_dict(config.to_dict())
        assert again.audio.output_device == "HDMI"
        assert again.video.device == "/dev/video2" and again.video.ptz_travel_seconds == 6.5
        assert again.control.camera_presets == ["Room", "Desk", "Board"]
```

In the existing `TestControlConfig.test_round_trips_through_dict`, the exact control block assertion becomes:

```python
        assert config.to_dict()["control"] == {"enabled": False, "host": "127.0.0.1", "port": 9090, "screensaver": "info",
                                               "camera_presets": ["Wide", "Table", "Whiteboard"]}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/core/test_config.py -k "RoomDevice or round_trips_through_dict"`
Expected: FAIL, `VideoConfig` has no `ptz_travel_seconds` and the control block lacks `camera_presets`.

- [ ] **Step 3: Add the keys**

In `src/croom/core/config.py`:

- `VideoConfig` gains, after `framerate: int = 30`:

```python
    ptz_travel_seconds: float = 8.0  # how long the camera is driven to reach an end stop when homing
```

- `ControlConfig` gains, after `screensaver`:

```python
    camera_presets: List[str] = field(default_factory=lambda: ["Wide", "Table", "Whiteboard"])  # the three slot names
```

- `to_dict`: `"video"` block gains `"ptz_travel_seconds": self.video.ptz_travel_seconds,` and `"control"` gains `"camera_presets": list(self.control.camera_presets),`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/core/test_config.py tests/unit/deploy/test_room_configs.py tests/unit/control/test_service.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/core/config.py tests/unit/core/test_config.py
git commit -m "feat(config): camera travel time and preset names"
```

---

### Task 3: `RoomVolume`

**Files:**
- Create: `src/croom/devices/__init__.py`, `src/croom/devices/errors.py`, `src/croom/devices/volume.py`
- Create: `tests/unit/devices/__init__.py`, `tests/unit/devices/test_volume.py`

**Interfaces:**
- Produces: `DeviceUnavailable(RuntimeError)`, `NotReady(RuntimeError)`, `Interrupted(RuntimeError)` in `croom.devices.errors`; `RoomVolume(preference="auto", runner=run_command, clock=time.monotonic)` with `from_config(config)`, `available` property, `state() -> dict` (`available, device, level, muted, reason`), `async refresh(force=False) -> dict`, `async set_level(level) -> dict`, `async step(delta) -> dict`, `async set_muted(muted) -> dict`; `run_command(args) -> (code, text)`; module constants `COMMAND_TIMEOUT_S = 2.0`, `CACHE_S = 3.0`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/devices/__init__.py` (empty) and `tests/unit/devices/test_volume.py`:

```python
"""
The room's sound level through PipeWire, against a fake pw-dump and wpctl in the
shape PiMeet-3 produces (spec 2026-10-08 sound and camera, section 4.2).
"""

import json
import logging

import pytest

from croom.devices import volume as volume_module
from croom.devices.errors import DeviceUnavailable
from croom.devices.volume import RoomVolume, run_command

MEETUP = "alsa_output.usb-Logitech_MeetUp-00.analog-stereo"
PW_DUMP = json.dumps([
    {"id": 36, "type": "PipeWire:Interface:Metadata", "version": 3, "props": {"metadata.name": "default"},
     "metadata": [{"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON", "value": {"name": MEETUP}},
                  {"subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON",
                   "value": {"name": "alsa_input.usb-Logitech_MeetUp-00.analog-stereo"}}]},
    {"id": 35, "type": "PipeWire:Interface:Node", "version": 3,
     "info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.platform-fef00700.hdmi.hdmi-stereo",
                        "node.description": "Built-in Audio Digital Stereo (HDMI)"}}},
    {"id": 57, "type": "PipeWire:Interface:Node", "version": 3,
     "info": {"props": {"media.class": "Audio/Sink", "node.name": MEETUP,
                        "node.description": "Logitech MeetUp Speakerphone Analog Stereo"}}},
    {"id": 58, "type": "PipeWire:Interface:Node", "version": 3,
     "info": {"props": {"media.class": "Audio/Source", "node.name": "alsa_input.usb-Logitech_MeetUp-00.analog-stereo",
                        "node.description": "Logitech MeetUp Speakerphone Analog Stereo"}}},
])
NO_SINKS = json.dumps([{"id": 36, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"}, "metadata": []}])


class FakeRunner:
    def __init__(self, dump=PW_DUMP, volume="Volume: 0.40\n"):
        self.dump, self.volume, self.calls, self.fail = dump, volume, [], {}

    async def __call__(self, args):
        self.calls.append(args)
        if args[0] in self.fail:
            return 1, self.fail[args[0]]
        if args == ["pw-dump"]:
            return 0, self.dump
        if args[:2] == ["wpctl", "get-volume"]:
            return 0, self.volume
        return 0, ""


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def volume_for(preference="auto", **runner_args):
    runner = FakeRunner(**runner_args)
    clock = FakeClock()
    return RoomVolume(preference, runner=runner, clock=clock), runner, clock


async def test_default_sink_is_the_meetup_with_its_level():
    volume, runner, _ = volume_for()
    state = await volume.refresh()
    assert state == {"available": True, "device": "Logitech MeetUp Speakerphone Analog Stereo", "level": 40, "muted": False, "reason": None}
    assert runner.calls == [["pw-dump"], ["wpctl", "get-volume", "57"]]


async def test_a_preference_picks_the_sink_whose_name_contains_it():
    volume, runner, _ = volume_for("hdmi")
    state = await volume.refresh()
    assert state["device"] == "Built-in Audio Digital Stereo (HDMI)" and runner.calls[-1] == ["wpctl", "get-volume", "35"]


async def test_no_matching_sink_is_unavailable_with_the_reason():
    volume, _, _ = volume_for("Jabra")
    state = await volume.refresh()
    assert state["available"] is False and state["reason"] == "no sink matches 'Jabra'"
    with pytest.raises(DeviceUnavailable):
        await volume.set_level(50)


async def test_no_sinks_at_all():
    volume, _, _ = volume_for(dump=NO_SINKS)
    assert (await volume.refresh())["reason"] == "no audio sink"


async def test_muted_is_read_from_wpctl():
    volume, _, _ = volume_for(volume="Volume: 0.40 [MUTED]\n")
    assert (await volume.refresh())["muted"] is True


async def test_levels_above_one_show_as_one_hundred():
    volume, _, _ = volume_for(volume="Volume: 1.30\n")
    assert (await volume.refresh())["level"] == 100


async def test_set_level_clamps_and_calls_wpctl():
    volume, runner, _ = volume_for()
    state = await volume.set_level(150)
    assert state["level"] == 100 and runner.calls[-1] == ["wpctl", "set-volume", "57", "1.00"]
    state = await volume.set_level(-3)
    assert state["level"] == 0 and runner.calls[-1] == ["wpctl", "set-volume", "57", "0.00"]
    await volume.set_level(55)
    assert runner.calls[-1] == ["wpctl", "set-volume", "57", "0.55"]


async def test_step_moves_from_the_level_wpctl_reports():
    volume, runner, _ = volume_for()
    assert (await volume.step(5))["level"] == 45 and runner.calls[-1] == ["wpctl", "set-volume", "57", "0.45"]
    runner.volume = "Volume: 0.45\n"
    assert (await volume.step(-50))["level"] == 0


async def test_mute_and_unmute():
    volume, runner, _ = volume_for()
    assert (await volume.set_muted(True))["muted"] is True and runner.calls[-1] == ["wpctl", "set-mute", "57", "1"]
    assert (await volume.set_muted(False))["muted"] is False and runner.calls[-1] == ["wpctl", "set-mute", "57", "0"]


async def test_the_state_is_cached_for_three_seconds_and_refreshed_after_a_change():
    volume, runner, clock = volume_for()
    await volume.refresh()
    await volume.refresh()
    assert runner.calls.count(["pw-dump"]) == 1
    clock.now += 3.1
    await volume.refresh()
    assert runner.calls.count(["pw-dump"]) == 2
    runner.volume = "Volume: 0.80\n"
    await volume.set_level(80)
    assert (await volume.refresh())["level"] == 80


async def test_a_failing_command_makes_it_unavailable_and_warns_once(caplog):
    volume, runner, clock = volume_for()
    runner.fail["pw-dump"] = "connection refused"
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            clock.now += 5
            state = await volume.refresh()
    assert state["available"] is False and "pw-dump failed: connection refused" in state["reason"]
    assert sum("pw-dump failed" in r.getMessage() for r in caplog.records) == 1
    del runner.fail["pw-dump"]
    clock.now += 5
    assert (await volume.refresh())["available"] is True


async def test_run_command_reports_a_timeout_and_a_missing_command(monkeypatch):
    monkeypatch.setattr(volume_module, "COMMAND_TIMEOUT_S", 0.2)
    code, text = await run_command(["sleep", "5"])
    assert code == 124 and "timed out" in text
    code, text = await run_command(["no-such-command-crystal-meet"])
    assert code == 127
    code, text = await run_command(["echo", "hello"])
    assert (code, text.strip()) == (0, "hello")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/devices/test_volume.py`
Expected: FAIL at import, `croom.devices` does not exist.

- [ ] **Step 3: Write the errors module and the volume module**

Create `src/croom/devices/__init__.py`:

```python
"""Room devices the room page controls: the speaker's volume and the camera's framing."""
```

Create `src/croom/devices/errors.py`:

```python
"""Why a device command could not be done; the control API answers 409 with the message."""


class DeviceUnavailable(RuntimeError):
    """No usable device, or the device stopped answering."""


class NotReady(RuntimeError):
    """The device is there but the action needs something first (a home, a known position, a saved preset)."""


class Interrupted(RuntimeError):
    """A long camera move was cut short by another command."""
```

Create `src/croom/devices/volume.py`:

```python
"""
The room's sound level through PipeWire (spec 2026-10-08 sound and camera, section
4.2): pw-dump lists the sinks and the default one, wpctl reads and sets a sink's
level and mute. Nothing here touches the upstream audio service.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from croom.devices.errors import DeviceUnavailable

logger = logging.getLogger(__name__)

Runner = Callable[[List[str]], Awaitable[Tuple[int, str]]]
COMMAND_TIMEOUT_S = 2.0
CACHE_S = 3.0
VOLUME_LINE = re.compile(r"Volume:\s*([0-9.]+)(\s*\[MUTED\])?")


async def run_command(args: List[str]) -> Tuple[int, str]:
    """Run a command with a short timeout: (exit code, its stdout, or stderr when it failed).
    A timeout is code 124, a missing command 127; neither raises."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (OSError, ValueError) as e:
        return 127, str(e)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=COMMAND_TIMEOUT_S)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"{args[0]} timed out after {COMMAND_TIMEOUT_S:.0f} s"
    text = out if process.returncode == 0 else (err or out)
    return process.returncode, text.decode("utf-8", "replace")


class RoomVolume:
    def __init__(self, preference: str = "auto", runner: Runner = run_command, clock=time.monotonic):
        self._preference = "" if preference in ("", "auto") else preference
        self._run = runner
        self._clock = clock
        self._sink_id: Optional[int] = None
        self._device: Optional[str] = None
        self._level = 0
        self._muted = False
        self._reason: Optional[str] = "not checked yet"
        self._checked_at: Optional[float] = None
        self._warned: Optional[str] = None

    @classmethod
    def from_config(cls, config) -> "RoomVolume":
        return cls(preference=config.audio.output_device or "auto")

    @property
    def available(self) -> bool:
        return self._sink_id is not None and self._reason is None

    def state(self) -> Dict[str, Any]:
        return {"available": self.available, "device": self._device, "level": self._level,
                "muted": self._muted, "reason": self._reason}

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def refresh(self, force: bool = False) -> Dict[str, Any]:
        """Find the sink and read its level; cached for a few seconds unless forced."""
        now = self._clock()
        if not force and self._checked_at is not None and now - self._checked_at < CACHE_S:
            return self.state()
        self._checked_at = now
        code, out = await self._run(["pw-dump"])
        if code != 0:
            return self._fail(f"pw-dump failed: {out.strip() or code}")
        try:
            sinks, default_name = self._parse_dump(out)
        except ValueError as e:
            return self._fail(f"pw-dump output not understood: {e}")
        chosen = self._choose(sinks, default_name)
        if chosen is None:
            return self._fail(f"no sink matches {self._preference!r}" if self._preference else "no audio sink")
        sink_id, device = chosen
        code, out = await self._run(["wpctl", "get-volume", str(sink_id)])
        if code != 0:
            return self._fail(f"wpctl get-volume failed: {out.strip() or code}")
        level, muted = self._parse_volume(out)
        if level is None:
            return self._fail(f"wpctl get-volume output not understood: {out.strip()!r}")
        if self._reason is not None:
            logger.info(f"Speaker: {device} (PipeWire sink {sink_id}), level {level}")
        self._sink_id, self._device, self._level, self._muted = sink_id, device, level, muted
        self._reason = None
        self._warned = None
        return self.state()

    @staticmethod
    def _parse_dump(text: str) -> Tuple[List[Tuple[int, str, str]], Optional[str]]:
        """Sinks as (id, node.name, description) and the default sink's node.name, from pw-dump's JSON."""
        objects = json.loads(text)
        if not isinstance(objects, list):
            raise ValueError("not a list")
        sinks, default_name = [], None
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "PipeWire:Interface:Node":
                props = (obj.get("info") or {}).get("props") or {}
                if props.get("media.class") == "Audio/Sink":
                    name = props.get("node.name", "")
                    description = props.get("node.description") or props.get("node.nick") or name
                    sinks.append((int(obj.get("id")), name, description))
            elif obj.get("type") == "PipeWire:Interface:Metadata" and (obj.get("props") or {}).get("metadata.name") == "default":
                for entry in obj.get("metadata") or []:
                    if entry.get("key") == "default.audio.sink" and isinstance(entry.get("value"), dict):
                        default_name = entry["value"].get("name")
        return sinks, default_name

    def _choose(self, sinks, default_name) -> Optional[Tuple[int, str]]:
        if self._preference:
            wanted = self._preference.casefold()
            for sink_id, name, description in sinks:
                if wanted in description.casefold() or wanted in name.casefold():
                    return sink_id, description
            return None
        for sink_id, name, description in sinks:
            if name == default_name:
                return sink_id, description
        return (sinks[0][0], sinks[0][2]) if sinks else None

    @staticmethod
    def _parse_volume(text: str) -> Tuple[Optional[int], bool]:
        match = VOLUME_LINE.search(text)
        if not match:
            return None, False
        return round(min(1.0, float(match.group(1))) * 100), bool(match.group(2))

    def _fail(self, reason: str) -> Dict[str, Any]:
        if self._warned != reason:
            logger.warning(f"Speaker unavailable: {reason}")
            self._warned = reason
        self._sink_id = None
        self._reason = reason
        return self.state()

    # ------------------------------------------------------------------
    # Changing
    # ------------------------------------------------------------------

    async def _sink(self) -> int:
        await self.refresh()
        if not self.available:
            raise DeviceUnavailable(self._reason or "no audio sink")
        return self._sink_id

    async def set_level(self, level: int) -> Dict[str, Any]:
        sink_id = await self._sink()
        level = max(0, min(100, int(level)))
        code, out = await self._run(["wpctl", "set-volume", str(sink_id), f"{level / 100:.2f}"])
        if code != 0:
            self._fail(f"wpctl set-volume failed: {out.strip() or code}")
            raise DeviceUnavailable(self._reason)
        self._level = level
        self._checked_at = None  # the next status reads it back
        return self.state()

    async def step(self, delta: int) -> Dict[str, Any]:
        await self.refresh(force=True)
        return await self.set_level(self._level + int(delta))

    async def set_muted(self, muted: bool) -> Dict[str, Any]:
        sink_id = await self._sink()
        code, out = await self._run(["wpctl", "set-mute", str(sink_id), "1" if muted else "0"])
        if code != 0:
            self._fail(f"wpctl set-mute failed: {out.strip() or code}")
            raise DeviceUnavailable(self._reason)
        self._muted = bool(muted)
        self._checked_at = None
        return self.state()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/devices/test_volume.py`
Expected: PASS (12 tests). Note `test_the_state_is_cached...`: after `set_level(80)` the cache is dropped, so the next `refresh` reads `Volume: 0.80` from the fake.

- [ ] **Step 5: Commit**

```bash
git add src/croom/devices tests/unit/devices
git commit -m "feat(devices): the room speaker's volume through PipeWire"
```

---

### Task 4: `RoomCamera`, part one: the device, moving, zoom, the watchdog

**Files:**
- Create: `src/croom/devices/v4l2.py`, `src/croom/devices/camera.py`
- Create: `tests/unit/devices/fake_v4l2.py`, `tests/unit/devices/test_camera.py`

**Interfaces:**
- Produces: `croom.devices.v4l2`: `V4L2_CID_PAN_SPEED`, `V4L2_CID_TILT_SPEED`, `V4L2_CID_ZOOM_ABSOLUTE`, `Range = Tuple[int, int, int, int]`, class `V4l2Controls` with `nodes() -> List[str]`, `open(path) -> int`, `close(fd)`, `query(fd, cid) -> Optional[Range]`, `get(fd, cid) -> int`, `set(fd, cid, value)`. `croom.devices.camera.RoomCamera(device="auto", travel_s=8.0, preset_names=None, store=None, v4l2=None, clock=time.monotonic, sleep=asyncio.sleep)` with `from_config(config, store)`, `available`, `busy`, `position_known`, `home_saved`, `preview` properties, `state() -> dict`, `async discover() -> bool`, `async move(pan, tilt) -> dict`, `async stop() -> dict`, `async zoom(level) -> int`, `async zoom_step(delta) -> int`, `async read_zoom() -> Optional[int]`, `async close()`; class constants `WATCHDOG_S = 1.5`, `ZOOM_STEP = 25`, `PREVIEW_S = 180.0`, `ZOOM_CACHE_S = 3.0`, `SLOTS = 3`. Part two (Task 5) adds `find_stops`, `home`, `save_home`, `save`, `recall`, `set_preview`.

- [ ] **Step 1: Write the fake V4L2 layer and the failing tests**

Create `tests/unit/devices/fake_v4l2.py`:

```python
"""A V4L2 stand-in: nodes with the controls PiMeet-3's cameras report, and a clock tests can move."""

import asyncio

from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED, V4L2_CID_ZOOM_ABSOLUTE

MEETUP = {V4L2_CID_ZOOM_ABSOLUTE: (100, 500, 1, 100), V4L2_CID_PAN_SPEED: (-1, 1, 1, 1), V4L2_CID_TILT_SPEED: (-1, 1, 1, 1)}
DECODER = {}   # /dev/video19, the Pi's HEVC decoder: no camera controls


class FakeV4l2:
    def __init__(self, nodes=None):
        self._nodes = dict(nodes if nodes is not None else {"/dev/video0": MEETUP, "/dev/video1": {}, "/dev/video19": DECODER})
        self.values = {}
        self.calls = []
        self.fail = False

    def nodes(self):
        return list(self._nodes)

    def _path(self, fd):
        return list(self._nodes)[fd - 10]

    def open(self, path):
        self.calls.append(("open", path))
        return 10 + list(self._nodes).index(path)

    def close(self, fd):
        self.calls.append(("close", fd))

    def query(self, fd, cid):
        return self._nodes[self._path(fd)].get(cid)

    def get(self, fd, cid):
        if self.fail:
            raise OSError(19, "No such device")
        return self.values.get((fd, cid), self._nodes[self._path(fd)][cid][3])

    def set(self, fd, cid, value):
        if self.fail:
            raise OSError(19, "No such device")
        self.calls.append(("set", fd, cid, value))
        self.values[(fd, cid)] = value

    def sets(self, cid):
        """The values set on one control, in order."""
        return [c[3] for c in self.calls if c[0] == "set" and c[2] == cid]


class FakeClock:
    """A clock that only moves when something sleeps on it; each sleep also yields once to the loop."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)
```

Create `tests/unit/devices/test_camera.py`:

```python
"""
The MeetUp's framing through V4L2 (spec 2026-10-08 sound and camera, section 4.3):
discovery, hold-to-move with a watchdog, zoom; then (Task 5) the position estimate,
homing, presets and the preview.
"""

import asyncio
import logging

import pytest

from croom.control.settings import SettingsStore
from croom.devices.errors import DeviceUnavailable
from croom.devices.camera import RoomCamera
from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED, V4L2_CID_ZOOM_ABSOLUTE
from tests.unit.devices.fake_v4l2 import MEETUP, FakeClock, FakeV4l2


def camera_for(tmp_path=None, nodes=None, device="auto", travel_s=8.0, names=None):
    v4l2 = FakeV4l2(nodes)
    clock = FakeClock()
    store = SettingsStore(tmp_path / "control-settings.json") if tmp_path is not None else None
    camera = RoomCamera(device=device, travel_s=travel_s, preset_names=names, store=store,
                        v4l2=v4l2, clock=clock, sleep=clock.sleep)
    return camera, v4l2, clock


async def test_auto_picks_the_first_node_with_camera_controls(caplog):
    camera, v4l2, _ = camera_for()
    with caplog.at_level(logging.INFO):
        assert await camera.discover() is True
    state = camera.state()
    assert state["available"] is True and state["device"] == "/dev/video0"
    assert state["zoom"] == {"level": 100, "min": 100, "max": 500}
    assert v4l2.calls[0] == ("open", "/dev/video0")
    assert ("open", "/dev/video1") not in v4l2.calls   # found on the first node, the rest is left alone
    assert any("/dev/video0" in r.getMessage() for r in caplog.records)


async def test_a_node_without_controls_is_closed_again_and_the_next_one_tried():
    camera, v4l2, _ = camera_for(nodes={"/dev/video0": {}, "/dev/video2": MEETUP})
    assert await camera.discover() is True
    assert ("close", 10) in v4l2.calls and camera.state()["device"] == "/dev/video2"


async def test_a_configured_node_is_used_as_is():
    camera, v4l2, _ = camera_for(nodes={"/dev/video0": MEETUP, "/dev/video2": MEETUP}, device="/dev/video2")
    await camera.discover()
    assert camera.state()["device"] == "/dev/video2"


async def test_no_controllable_camera_is_unavailable_with_a_reason():
    camera, _, _ = camera_for(nodes={"/dev/video19": {}})
    assert await camera.discover() is False
    assert camera.state()["available"] is False and camera.state()["reason"] == "no controllable camera found"
    with pytest.raises(DeviceUnavailable):
        await camera.move(1, 0)


async def test_move_sets_the_speeds_and_stop_clears_them():
    camera, v4l2, _ = camera_for()
    await camera.move(1, 0)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1] and v4l2.sets(V4L2_CID_TILT_SPEED) == [0]
    assert camera.state()["moving"] is True
    await camera.move(0, -1)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1, 0] and v4l2.sets(V4L2_CID_TILT_SPEED) == [0, -1]
    await camera.stop()
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0
    assert camera.state()["moving"] is False


async def test_move_refuses_other_directions():
    camera, _, _ = camera_for()
    with pytest.raises(ValueError):
        await camera.move(2, 0)
    with pytest.raises(ValueError):
        await camera.move(0, "up")


async def test_motion_stops_on_its_own_when_the_page_goes_quiet():
    camera, v4l2, _ = camera_for()
    camera.WATCHDOG_S = 0.05
    await camera.move(1, 1)
    await asyncio.sleep(0.03)
    await camera.move(1, 1)              # the page is still holding the button
    await asyncio.sleep(0.03)
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 1   # not stopped yet: the second request reset the watchdog
    await asyncio.sleep(0.06)
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0
    assert camera.state()["moving"] is False


async def test_zoom_is_clamped_to_the_cameras_range():
    camera, v4l2, _ = camera_for()
    assert await camera.zoom(250) == 250
    assert await camera.zoom(900) == 500
    assert await camera.zoom(1) == 100
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE) == [250, 500, 100]
    assert await camera.zoom_step(25) == 125
    assert await camera.zoom_step(-500) == 100


async def test_zoom_is_read_from_the_camera_and_cached():
    camera, v4l2, clock = camera_for()
    await camera.discover()
    v4l2.values[(10, V4L2_CID_ZOOM_ABSOLUTE)] = 180
    assert await camera.read_zoom() == 180
    v4l2.values[(10, V4L2_CID_ZOOM_ABSOLUTE)] = 300
    assert await camera.read_zoom() == 180      # cached
    clock.now += 3.1
    assert await camera.read_zoom() == 300


async def test_a_failing_control_call_makes_the_camera_unavailable():
    camera, v4l2, _ = camera_for()
    await camera.discover()
    v4l2.fail = True
    with pytest.raises(DeviceUnavailable) as failure:
        await camera.move(1, 0)
    assert "No such device" in str(failure.value)
    assert camera.state()["available"] is False and ("close", 10) in v4l2.calls
    v4l2.fail = False
    assert await camera.discover() is True     # plugged back in: picked up again


async def test_close_stops_motion_and_releases_the_node():
    camera, v4l2, _ = camera_for()
    await camera.move(1, 0)
    await camera.close()
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.calls[-1] == ("close", 10)
    assert camera.state()["available"] is False


def test_from_config_reads_the_device_travel_time_and_preset_names(tmp_path):
    from croom.core.config import Config
    config = Config.from_dict({"video": {"device": "/dev/video2", "ptz_travel_seconds": 6.0},
                               "control": {"camera_presets": ["Room", "Desk"]}})
    camera = RoomCamera.from_config(config, SettingsStore(tmp_path / "s.json"))
    assert camera.state()["presets"] == [{"slot": 1, "name": "Room", "saved": False},
                                         {"slot": 2, "name": "Desk", "saved": False},
                                         {"slot": 3, "name": "Preset 3", "saved": False}]
    assert camera._device_pref == "/dev/video2" and camera._travel_s == 6.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/devices/test_camera.py`
Expected: FAIL at import, `croom.devices.v4l2` does not exist.

- [ ] **Step 3: Write the V4L2 layer**

Create `src/croom/devices/v4l2.py`:

```python
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
```

- [ ] **Step 4: Write the camera module, part one**

Create `src/croom/devices/camera.py`:

```python
"""
The room camera's framing through V4L2 (spec 2026-10-08 sound and camera, section
4.3). The Logitech MeetUp reports no pan or tilt position: it has speed controls
(move until told to stop) and an absolute zoom. So the camera keeps an estimate of
where it is in seconds of travel from the end stops, found by homing, and presets
are stored against that estimate. Control calls go through a second file
descriptor next to Chromium's stream; nothing here streams video.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED, V4L2_CID_ZOOM_ABSOLUTE, V4l2Controls

logger = logging.getLogger(__name__)

NO_CAMERA = "no controllable camera found"


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


class RoomCamera:
    WATCHDOG_S = 1.5       # motion stops this long after the last move request
    ZOOM_STEP = 25
    ZOOM_CACHE_S = 3.0
    PREVIEW_S = 180.0
    SLOTS = 3

    def __init__(self, device: str = "auto", travel_s: float = 8.0, preset_names: Optional[List[str]] = None,
                 store=None, v4l2=None, clock=time.monotonic, sleep=asyncio.sleep):
        self._device_pref = device or "auto"
        self._travel_s = float(travel_s)
        names = [str(n) for n in (preset_names or [])][:self.SLOTS]
        self._names = names + [f"Preset {i}" for i in range(len(names) + 1, self.SLOTS + 1)]
        self._store = store
        self._v4l2 = v4l2 or V4l2Controls()
        self._clock = clock
        self._sleep = sleep
        self._fd: Optional[int] = None
        self._path: Optional[str] = None
        self._reason: Optional[str] = "not checked yet"
        self._warned: Optional[str] = None
        self._zoom_range = (100, 500)
        self._has_pan = False
        self._has_tilt = False
        self._zoom: Optional[int] = None
        self._zoom_read_at: Optional[float] = None
        self._pan = 0
        self._tilt = 0
        self._segment_started: Optional[float] = None
        self._pan_s = 0.0
        self._tilt_s = 0.0
        self._position_known = False
        self._watchdog: Optional[asyncio.Task] = None
        self._busy = False
        self._interrupt = asyncio.Event()
        self._long_done = asyncio.Event()
        self._long_done.set()
        self._preview_until = 0.0
        saved = (store.get("camera") if store is not None else None) or {}
        self._home = saved.get("home") if isinstance(saved.get("home"), dict) else None
        self._presets = {str(k): v for k, v in (saved.get("presets") or {}).items() if isinstance(v, dict)}

    @classmethod
    def from_config(cls, config, store) -> "RoomCamera":
        return cls(device=config.video.device, travel_s=config.video.ptz_travel_seconds,
                   preset_names=config.control.camera_presets, store=store)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._fd is not None

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def position_known(self) -> bool:
        return self._position_known

    @property
    def home_saved(self) -> bool:
        return self._home is not None

    @property
    def preview(self) -> bool:
        return self._clock() < self._preview_until

    def state(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "device": self._path,
            "reason": None if self.available else self._reason,
            "moving": bool(self._pan or self._tilt),
            "busy": self._busy,
            "zoom": {"level": self._zoom if self._zoom is not None else self._zoom_range[0],
                     "min": self._zoom_range[0], "max": self._zoom_range[1]},
            "position_known": self._position_known,
            "home_saved": self.home_saved,
            "presets": [{"slot": slot, "name": self._names[slot - 1], "saved": str(slot) in self._presets}
                        for slot in range(1, self.SLOTS + 1)],
            "preview": self.preview,
        }

    # ------------------------------------------------------------------
    # The device
    # ------------------------------------------------------------------

    async def discover(self) -> bool:
        """Open the configured node, or the first one with a zoom or pan control."""
        if self._fd is not None:
            return True
        candidates = [self._device_pref] if self._device_pref != "auto" else self._v4l2.nodes()
        for path in candidates:
            try:
                fd = self._v4l2.open(path)
            except OSError as e:
                logger.debug(f"Could not open {path}: {e}")
                continue
            zoom = self._v4l2.query(fd, V4L2_CID_ZOOM_ABSOLUTE)
            pan = self._v4l2.query(fd, V4L2_CID_PAN_SPEED)
            tilt = self._v4l2.query(fd, V4L2_CID_TILT_SPEED)
            if zoom is None and pan is None:
                self._v4l2.close(fd)
                continue
            self._fd, self._path = fd, path
            self._has_pan, self._has_tilt = pan is not None, tilt is not None
            if zoom is not None:
                self._zoom_range = (zoom[0], zoom[1])
                self._zoom = zoom[3]
            self._reason = None
            self._warned = None
            logger.info(f"Camera: {path} (zoom {self._zoom_range[0]}..{self._zoom_range[1]}, "
                        f"pan {'yes' if self._has_pan else 'no'}, tilt {'yes' if self._has_tilt else 'no'})")
            return True
        self._fail(NO_CAMERA)
        return False

    def _fail(self, reason: str) -> None:
        if self._warned != reason:
            logger.warning(f"Camera unavailable: {reason}")
            self._warned = reason
        if self._fd is not None:
            try:
                self._v4l2.close(self._fd)
            except OSError:
                pass
        self._fd, self._path = None, None
        self._reason = reason
        self._pan = self._tilt = 0
        self._segment_started = None
        self._position_known = False

    async def _require(self) -> None:
        if not await self.discover():
            raise DeviceUnavailable(self._reason or NO_CAMERA)

    async def _set(self, cid: int, value: int) -> None:
        try:
            await asyncio.to_thread(self._v4l2.set, self._fd, cid, value)
        except OSError as e:
            self._fail(f"camera call failed: {e}")
            raise DeviceUnavailable(self._reason) from e

    async def _get(self, cid: int) -> int:
        try:
            return await asyncio.to_thread(self._v4l2.get, self._fd, cid)
        except OSError as e:
            self._fail(f"camera call failed: {e}")
            raise DeviceUnavailable(self._reason) from e

    async def close(self) -> None:
        self._cancel_watchdog()
        if self._fd is not None:
            try:
                await self._apply_speeds(0, 0)
            except DeviceUnavailable:
                pass
        if self._fd is not None:
            try:
                self._v4l2.close(self._fd)
            except OSError:
                pass
        self._fd, self._path = None, None
        self._reason = "closed"

    # ------------------------------------------------------------------
    # Moving
    # ------------------------------------------------------------------

    async def move(self, pan: int, tilt: int) -> Dict[str, Any]:
        """Start, change or stop motion; a second request within WATCHDOG_S keeps it going."""
        if pan not in (-1, 0, 1) or tilt not in (-1, 0, 1):
            raise ValueError("pan and tilt must each be -1, 0 or 1")
        await self._require()
        await self._cancel_long_move()
        await self._apply_speeds(int(pan), int(tilt))
        self._cancel_watchdog()
        if pan or tilt:
            self._watchdog = asyncio.create_task(self._watch())
        return self.state()

    async def stop(self) -> Dict[str, Any]:
        await self._cancel_long_move()
        self._cancel_watchdog()
        if self._fd is not None:
            await self._apply_speeds(0, 0)
        return self.state()

    async def _watch(self) -> None:
        await asyncio.sleep(self.WATCHDOG_S)
        if self._fd is not None and (self._pan or self._tilt):
            logger.info("Camera motion stopped: the page went quiet")
            try:
                await self._apply_speeds(0, 0)
            except DeviceUnavailable:
                pass

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None and not self._watchdog.done() and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None

    async def _apply_speeds(self, pan: int, tilt: int) -> None:
        """Set the speed controls and close the motion segment that ends here (the position estimate)."""
        self._account()
        if self._has_pan:
            await self._set(V4L2_CID_PAN_SPEED, pan)
        if self._has_tilt:
            await self._set(V4L2_CID_TILT_SPEED, tilt)
        self._pan, self._tilt = (pan if self._has_pan else 0), (tilt if self._has_tilt else 0)
        self._segment_started = self._clock() if (self._pan or self._tilt) else None

    def _account(self) -> None:
        if self._segment_started is None:
            return
        elapsed = max(0.0, self._clock() - self._segment_started)
        self._pan_s = min(self._travel_s, max(0.0, self._pan_s + self._pan * elapsed))
        self._tilt_s = min(self._travel_s, max(0.0, self._tilt_s + self._tilt * elapsed))
        self._segment_started = None

    async def _cancel_long_move(self) -> None:
        """Interrupt a running find_stops/home/recall (Task 5) and wait for it to stop."""
        if self._busy:
            self._interrupt.set()
            await self._long_done.wait()

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    async def read_zoom(self) -> Optional[int]:
        await self._require()
        now = self._clock()
        if self._zoom is None or self._zoom_read_at is None or now - self._zoom_read_at >= self.ZOOM_CACHE_S:
            self._zoom = await self._get(V4L2_CID_ZOOM_ABSOLUTE)
            self._zoom_read_at = now
        return self._zoom

    async def zoom(self, level: int) -> int:
        await self._require()
        level = max(self._zoom_range[0], min(self._zoom_range[1], int(level)))
        await self._set(V4L2_CID_ZOOM_ABSOLUTE, level)
        self._zoom = level
        self._zoom_read_at = self._clock()
        return level

    async def zoom_step(self, delta: int) -> int:
        current = await self.read_zoom()
        return await self.zoom((current if current is not None else self._zoom_range[0]) + int(delta))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/devices/test_camera.py`
Expected: PASS (12 tests). `test_motion_stops_on_its_own...` uses real time with the watchdog shortened to 50 ms.

- [ ] **Step 6: Commit**

```bash
git add src/croom/devices/v4l2.py src/croom/devices/camera.py tests/unit/devices/fake_v4l2.py tests/unit/devices/test_camera.py
git commit -m "feat(devices): the room camera: discovery, hold-to-move with a watchdog, zoom"
```

---

### Task 5: `RoomCamera`, part two: position, homing, presets, preview

**Files:**
- Modify: `src/croom/devices/camera.py`
- Modify: `tests/unit/devices/test_camera.py`

**Interfaces:**
- Produces: `async find_stops() -> dict`, `async home() -> dict`, `async save_home() -> dict`, `async save(slot) -> dict`, `async recall(slot) -> dict`, `set_preview(on: bool) -> bool`; `NotReady` reasons `"save a home first"`, `"home the camera first"`, `"nothing saved in this slot"`; `ValueError` for a slot outside 1..3; `Interrupted` from a long move cut short; the settings key `camera` written through the store.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/devices/test_camera.py`:

```python


# --- position, homing, presets, preview (part two) ---

from croom.devices.errors import Interrupted, NotReady  # noqa: E402


async def test_find_stops_drives_both_axes_to_the_corner_and_the_position_becomes_known():
    camera, v4l2, clock = camera_for()
    before = clock.now
    await camera.find_stops()
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [-1, 0] and v4l2.sets(V4L2_CID_TILT_SPEED) == [-1, 0]
    assert clock.now - before == 8.0                      # ptz_travel_seconds
    assert camera.position_known and (camera._pan_s, camera._tilt_s) == (0.0, 0.0)


async def test_moves_are_counted_in_seconds_of_travel_from_the_stop():
    camera, v4l2, clock = camera_for()
    await camera.find_stops()
    await camera.move(1, 1)
    await clock.sleep(2.5)
    await camera.move(0, 1)        # pan stops after 2.5 s, tilt keeps going
    await clock.sleep(1.0)
    await camera.stop()
    assert (camera._pan_s, camera._tilt_s) == (2.5, 3.5)
    await camera.move(-1, 0)
    await clock.sleep(10.0)        # longer than the travel: clamped at the stop
    await camera.stop()
    assert camera._pan_s == 0.0


async def test_save_home_needs_a_known_position_and_home_returns_there(tmp_path):
    camera, v4l2, clock = camera_for(tmp_path)
    with pytest.raises(NotReady) as failure:
        await camera.save_home()
    assert str(failure.value) == "home the camera first"
    with pytest.raises(NotReady) as failure:
        await camera.home()
    assert str(failure.value) == "save a home first"
    await camera.find_stops()
    await camera.move(1, 1)
    await clock.sleep(3.0)
    await camera.move(0, 1)
    await clock.sleep(1.0)
    await camera.stop()
    await camera.zoom(200)
    await camera.save_home()
    assert camera.home_saved
    assert SettingsStore(tmp_path / "control-settings.json").get("camera")["home"] == {"pan_s": 3.0, "tilt_s": 4.0}
    v4l2.calls.clear()
    await camera.home()
    # to the stops, then 3 s of pan and 4 s of tilt at once (pan stops first), then zoom back to 100
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [-1, 0, 1, 0, 0]
    assert v4l2.sets(V4L2_CID_TILT_SPEED) == [-1, 0, 1, 1, 0]
    assert (camera._pan_s, camera._tilt_s) == (3.0, 4.0) and camera.position_known
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE)[-1] == 100


async def test_presets_are_saved_against_the_estimate_and_recalled_by_the_difference(tmp_path):
    camera, v4l2, clock = camera_for(tmp_path, names=["Wide", "Table", "Whiteboard"])
    with pytest.raises(NotReady) as failure:
        await camera.save(2)
    assert str(failure.value) == "home the camera first"
    await camera.find_stops()
    await camera.save_home()
    await camera.move(1, 1)
    await clock.sleep(4.0)
    await camera.stop()
    await camera.zoom(230)
    await camera.save(2)
    assert camera.state()["presets"][1] == {"slot": 2, "name": "Table", "saved": True}
    assert SettingsStore(tmp_path / "control-settings.json").get("camera")["presets"] == {"2": {"pan_s": 4.0, "tilt_s": 4.0, "zoom": 230}}
    await camera.move(-1, 0)        # elsewhere: pan back 1.5 s
    await clock.sleep(1.5)
    await camera.stop()
    await camera.zoom(100)
    v4l2.calls.clear()
    await camera.recall(2)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1, 0] and v4l2.sets(V4L2_CID_TILT_SPEED) == [0, 0]
    assert (camera._pan_s, camera._tilt_s) == (4.0, 4.0) and v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE) == [230]
    with pytest.raises(NotReady) as failure:
        await camera.recall(3)
    assert str(failure.value) == "nothing saved in this slot"
    with pytest.raises(ValueError):
        await camera.recall(4)


async def test_recall_from_an_unknown_position_homes_first(tmp_path):
    camera, v4l2, clock = camera_for(tmp_path)
    await camera.find_stops()
    await camera.save_home()
    await camera.move(1, 0)
    await clock.sleep(2.0)
    await camera.stop()
    await camera.save(1)
    camera._position_known = False         # what an interrupted move leaves behind
    v4l2.calls.clear()
    await camera.recall(1)
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[:2] == [-1, 0]   # homed first
    assert camera.position_known and camera._pan_s == 2.0
    other, _, _ = camera_for(tmp_path)
    await other.find_stops()
    other._home = None
    other._position_known = False
    other._presets = {"1": {"pan_s": 2.0, "tilt_s": 0.0, "zoom": 100}}
    with pytest.raises(NotReady) as failure:
        await other.recall(1)
    assert str(failure.value) == "home the camera first"


async def test_a_new_command_interrupts_a_long_move_and_the_position_is_unknown(tmp_path):
    camera, v4l2, _ = camera_for(tmp_path)

    async def slow_sleep(seconds):          # a long move that takes real time, so it can be interrupted
        await asyncio.sleep(0.02 * seconds)

    camera._sleep = slow_sleep
    homing = asyncio.create_task(camera.find_stops())
    await asyncio.sleep(0.03)
    assert camera.busy
    await camera.move(0, 1)                   # a person presses an arrow while the camera is homing
    with pytest.raises(Interrupted):
        await homing
    assert not camera.busy and not camera.position_known
    assert v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 1 and v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0


async def test_preview_lasts_three_minutes_unless_renewed_or_cleared():
    camera, _, clock = camera_for()
    assert camera.preview is False
    assert camera.set_preview(True) is True and camera.preview is True
    clock.now += 170
    assert camera.preview is True
    camera.set_preview(True)
    clock.now += 170
    assert camera.preview is True
    clock.now += 11
    assert camera.preview is False
    camera.set_preview(True)
    assert camera.set_preview(False) is False and camera.preview is False


def test_saved_home_and_presets_are_read_back_from_the_store(tmp_path):
    store = SettingsStore(tmp_path / "control-settings.json")
    store.save("camera", {"home": {"pan_s": 1.0, "tilt_s": 2.0}, "presets": {"3": {"pan_s": 0.5, "tilt_s": 0.5, "zoom": 300}}})
    camera = RoomCamera(store=store, v4l2=FakeV4l2())
    assert camera.home_saved and camera.state()["presets"][2]["saved"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/devices/test_camera.py`
Expected: the part-one tests PASS, the new ones FAIL with `AttributeError: 'RoomCamera' object has no attribute 'find_stops'` and friends.

- [ ] **Step 3: Write part two**

Append to the `RoomCamera` class in `src/croom/devices/camera.py`:

```python
    # ------------------------------------------------------------------
    # Position: long moves (find_stops, home, recall) and what they are stored against
    # ------------------------------------------------------------------

    async def _wait(self, seconds: float) -> None:
        """Sleep, but wake at once when another command interrupts the long move."""
        if seconds <= 0:
            return
        waiter = asyncio.ensure_future(self._interrupt.wait())
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        try:
            await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiter, sleeper):
                if not task.done():
                    task.cancel()
        if self._interrupt.is_set():
            raise Interrupted("the camera move was interrupted")

    async def _long(self, work) -> None:
        """Run a long move: busy while it runs, stopped and unknown if it is cut short."""
        await self._require()
        await self._cancel_long_move()
        self._cancel_watchdog()
        self._busy = True
        self._interrupt.clear()
        self._long_done.clear()
        try:
            await work()
        except (Interrupted, DeviceUnavailable):
            self._position_known = False
            if self._fd is not None:
                try:
                    await self._apply_speeds(0, 0)
                except DeviceUnavailable:
                    pass
            raise
        finally:
            self._busy = False
            self._long_done.set()

    async def _find_stops_work(self) -> None:
        await self._apply_speeds(-1, -1)
        await self._wait(self._travel_s)
        await self._apply_speeds(0, 0)
        self._pan_s, self._tilt_s = 0.0, 0.0
        self._position_known = True

    async def _move_to(self, pan_s: float, tilt_s: float) -> None:
        """Move both axes at once by the difference from the estimate; the shorter leg stops first."""
        legs = sorted([(abs(pan_s - self._pan_s), "pan"), (abs(tilt_s - self._tilt_s), "tilt")])
        pan_dir, tilt_dir = _sign(pan_s - self._pan_s), _sign(tilt_s - self._tilt_s)
        await self._apply_speeds(pan_dir, tilt_dir)
        applied = (pan_dir, tilt_dir)
        elapsed = 0.0
        for duration, axis in legs:
            await self._wait(duration - elapsed)
            elapsed = max(elapsed, duration)
            if axis == "pan":
                pan_dir = 0
            else:
                tilt_dir = 0
            if (pan_dir, tilt_dir) != applied:      # a leg of zero length changes nothing: no extra control call
                await self._apply_speeds(pan_dir, tilt_dir)
                applied = (pan_dir, tilt_dir)

    async def _home_work(self) -> None:
        await self._find_stops_work()
        await self._move_to(float(self._home["pan_s"]), float(self._home["tilt_s"]))
        await self.zoom(self._zoom_range[0])

    async def find_stops(self) -> Dict[str, Any]:
        await self._long(self._find_stops_work)
        return self.state()

    async def home(self) -> Dict[str, Any]:
        if self._home is None:
            raise NotReady("save a home first")
        await self._long(self._home_work)
        return self.state()

    async def save_home(self) -> Dict[str, Any]:
        if not self._position_known:
            raise NotReady("home the camera first")
        self._account()
        self._home = {"pan_s": round(self._pan_s, 3), "tilt_s": round(self._tilt_s, 3)}
        self._persist()
        logger.info(f"Camera home saved at {self._home}")
        return self.state()

    def _slot(self, slot) -> str:
        if slot not in range(1, self.SLOTS + 1):
            raise ValueError(f"preset slots are 1 to {self.SLOTS}")
        return str(slot)

    async def save(self, slot: int) -> Dict[str, Any]:
        key = self._slot(slot)
        if not self._position_known:
            raise NotReady("home the camera first")
        self._account()
        zoom = await self.read_zoom()
        self._presets[key] = {"pan_s": round(self._pan_s, 3), "tilt_s": round(self._tilt_s, 3),
                              "zoom": zoom if zoom is not None else self._zoom_range[0]}
        self._persist()
        logger.info(f"Camera preset {key} ({self._names[slot - 1]}) saved: {self._presets[key]}")
        return self.state()

    async def recall(self, slot: int) -> Dict[str, Any]:
        key = self._slot(slot)
        preset = self._presets.get(key)
        if preset is None:
            raise NotReady("nothing saved in this slot")
        if not self._position_known and self._home is None:
            raise NotReady("home the camera first")

        async def work():
            if not self._position_known:
                await self._home_work()
            await self._move_to(float(preset["pan_s"]), float(preset["tilt_s"]))
            await self.zoom(int(preset.get("zoom", self._zoom_range[0])))

        await self._long(work)
        return self.state()

    def _persist(self) -> None:
        if self._store is not None:
            self._store.save("camera", {"home": self._home, "presets": self._presets})

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def set_preview(self, on: bool) -> bool:
        self._preview_until = self._clock() + self.PREVIEW_S if on else 0.0
        return self.preview
```

Note on `_move_to`: `_wait` with the fake clock advances it by the leg's duration, and `_apply_speeds` runs `_account()` first, so after the pan leg the estimate reads exactly the stored seconds. A leg of zero length calls `_wait(0)`, which returns at once, and applies nothing new, so a recall that only pans produces `[1, 0]` on the pan control and `[0, 0]` on tilt (the first `0` from the start of the move). In the home test the tilt-only `_apply_speeds(0, 1)` after the pan leg is what produces the `1, 1` pair in the tilt list.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/devices/test_camera.py`
Expected: PASS (20 tests). If `test_save_home_needs...` disagrees on the exact speed lists, print `v4l2.calls` and check the order: stops (-1,-1), stop (0,0), both legs start (1,1), pan leg ends (0,1), tilt leg ends (0,0); the zoom call is separate.

- [ ] **Step 5: Commit**

```bash
git add src/croom/devices/camera.py tests/unit/devices/test_camera.py
git commit -m "feat(devices): the camera's position estimate, homing, presets and preview"
```

---

### Task 6: The `devices` service and the agent wiring

**Files:**
- Create: `src/croom/devices/service.py`
- Modify: `src/croom/core/agent.py` (`_initialize_services`, the control service block)
- Create: `tests/unit/devices/test_service.py`
- Modify: `tests/unit/core/test_agent.py`

**Interfaces:**
- Produces: `DevicesService(volume, camera)` (name `devices`), `DevicesService.from_config(config, store)`, attributes `volume` and `camera`, `REDISCOVER_S = 30.0`; the agent registers it before `control` and passes `devices=` and `store=` to `ControlService.from_config`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/devices/test_service.py`:

```python
"""
The devices service starts the speaker and the camera, homes the camera when a
home is saved, and keeps looking for devices that are missing.
"""

import asyncio

from croom.control.settings import SettingsStore
from croom.devices.camera import RoomCamera
from croom.devices.service import DevicesService
from croom.devices.v4l2 import V4L2_CID_PAN_SPEED
from croom.devices.volume import RoomVolume
from tests.unit.devices.fake_v4l2 import MEETUP, FakeClock, FakeV4l2
from tests.unit.devices.test_volume import FakeRunner


def service_for(tmp_path, home=None, nodes=None):
    store = SettingsStore(tmp_path / "control-settings.json")
    if home is not None:
        store.save("camera", {"home": home, "presets": {}})
    clock = FakeClock()
    v4l2 = FakeV4l2(nodes)
    camera = RoomCamera(store=store, v4l2=v4l2, clock=clock, sleep=clock.sleep)
    runner = FakeRunner()
    return DevicesService(RoomVolume(runner=runner), camera), v4l2, runner


async def test_start_finds_both_devices_and_homes_the_camera_when_a_home_is_saved(tmp_path):
    service, v4l2, runner = service_for(tmp_path, home={"pan_s": 1.0, "tilt_s": 0.5})
    await service.start()
    try:
        assert service.volume.available and ["pw-dump"] in runner.calls
        assert service.camera.available and service.camera.position_known
        assert v4l2.sets(V4L2_CID_PAN_SPEED)[:2] == [-1, 0]   # it went to the stops first
    finally:
        await service.stop()
    assert v4l2.calls[-1] == ("close", 10)


async def test_start_does_not_move_the_camera_without_a_saved_home(tmp_path):
    service, v4l2, _ = service_for(tmp_path)
    await service.start()
    try:
        assert service.camera.available and not service.camera.position_known
        assert not any(c[0] == "set" for c in v4l2.calls)
    finally:
        await service.stop()


async def test_a_missing_camera_is_looked_for_again(tmp_path):
    service, v4l2, _ = service_for(tmp_path, nodes={"/dev/video19": {}})
    service.REDISCOVER_S = 0.05
    await service.start()
    try:
        assert not service.camera.available
        v4l2._nodes["/dev/video0"] = dict(MEETUP)
        await asyncio.sleep(0.12)
        assert service.camera.available
    finally:
        await service.stop()


def test_from_config_builds_both_from_the_config(tmp_path):
    from croom.core.config import Config
    config = Config.from_dict({"audio": {"output_device": "HDMI"}, "video": {"device": "/dev/video2"}})
    service = DevicesService.from_config(config, SettingsStore(tmp_path / "s.json"))
    assert service.name == "devices"
    assert service.volume._preference == "HDMI" and service.camera._device_pref == "/dev/video2"
```

Append to `tests/unit/core/test_agent.py`, inside the class that holds `test_control_is_registered_after_meeting_and_calendar` (same indentation, same fixtures; mirror how that test builds and initialises the agent):

```python
    def test_devices_are_registered_and_handed_to_the_control_page(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        devices = agent.service_manager.get_service("devices")
        control = agent.service_manager.get_service("control")
        assert devices is not None and devices.name == "devices"
        assert control._devices is devices
        assert control._store.path == agent.config.resolve_data_dir() / "control-settings.json"
```

If `test_control_is_registered_after_meeting_and_calendar` calls `agent._initialize_services()` itself before `get_service`, do the same here.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/devices/test_service.py tests/unit/core/test_agent.py -k "devices"`
Expected: FAIL, `croom.devices.service` does not exist and the agent registers no `devices` service.

- [ ] **Step 3: Write the service**

Create `src/croom/devices/service.py`:

```python
"""
The devices service (spec 2026-10-08 sound and camera, section 4.1): owns the room
speaker's volume and the camera's framing, starts them, homes the camera once a
home has been saved, and keeps looking for a device that is missing.
"""

import asyncio
import logging
from typing import Optional

from croom.core.service import Service
from croom.devices.camera import RoomCamera
from croom.devices.volume import RoomVolume

logger = logging.getLogger(__name__)


class DevicesService(Service):
    REDISCOVER_S = 30.0

    def __init__(self, volume: RoomVolume, camera: RoomCamera):
        super().__init__("devices")
        self.volume = volume
        self.camera = camera
        self._task: Optional[asyncio.Task] = None

    @classmethod
    def from_config(cls, config, store) -> "DevicesService":
        return cls(RoomVolume.from_config(config), RoomCamera.from_config(config, store))

    async def start(self) -> None:
        await self.volume.refresh(force=True)
        if await self.camera.discover() and self.camera.home_saved:
            try:
                await self.camera.home()
            except Exception as e:  # noqa: BLE001 - a camera that will not home is still usable by hand
                logger.warning(f"Could not home the camera at start: {e}")
        self._task = asyncio.create_task(self._rediscover())
        logger.info("Devices service started")

    async def _rediscover(self) -> None:
        while True:
            await asyncio.sleep(self.REDISCOVER_S)
            try:
                if not self.camera.available:
                    await self.camera.discover()
                if not self.volume.available:
                    await self.volume.refresh(force=True)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Device check failed: {e}")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        await self.camera.close()
        logger.info("Devices service stopped")
```

- [ ] **Step 4: Wire the agent**

In `src/croom/core/agent.py`, inside `_initialize_services`, just before the `# Room control page` block, add:

```python
        # Room devices: the speaker's volume and the camera's framing (spec 2026-10-08)
        settings_store = None
        try:
            from croom.control.settings import SettingsStore
            from croom.devices.service import DevicesService
            settings_store = SettingsStore(self.config.resolve_data_dir() / "control-settings.json")
            self.service_manager.register(DevicesService.from_config(self.config, settings_store))
            logger.info("Room devices registered")
        except ImportError as e:
            logger.warning(f"Room devices not available: {e}")
```

and change the control block's construction to:

```python
                control_service = ControlService.from_config(
                    self.config,
                    meeting=self.service_manager.get_service("meeting"),
                    calendar=self.service_manager.get_service("calendar"),
                    devices=self.service_manager.get_service("devices"),
                    store=settings_store,
                )
                self.service_manager.register(control_service, dependencies=["meeting", "calendar", "devices"])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/devices tests/unit/core/test_agent.py tests/unit/control/test_service.py`
Expected: PASS. If `test_agent.py`'s `no_hardware` fixture patches audio and video modules only, the devices service's `pw-dump` call on this machine fails fast (code 127) and the camera finds no node; both are fine.

- [ ] **Step 6: Commit**

```bash
git add src/croom/devices/service.py src/croom/core/agent.py tests/unit/devices/test_service.py tests/unit/core/test_agent.py
git commit -m "feat(devices): the devices service, started by the agent before the room page"
```

---

### Task 7: The control API and status blocks

**Files:**
- Modify: `src/croom/control/service.py` (routes, status, handlers)
- Modify: `tests/unit/control/test_service.py` (stub devices and the route tests)

**Interfaces:**
- Consumes: `RoomVolume` and `RoomCamera` methods and `state()` (Tasks 3 to 5), the errors in `croom.devices.errors`, `self._devices` from Task 1.
- Produces: `GET/POST /api/audio/volume`, `POST /api/camera/{move,zoom,home,setup,preview}`, `POST /api/camera/presets/{slot}`; `/api/status` gains `audio` and `camera`; `StubVolume`, `StubCamera`, `StubDevices` in `tests/unit/control/test_service.py` (reused by the page tests); `make_service(meeting=None, calendar=None, devices=None, **config)`.

- [ ] **Step 1: Write the stubs and the failing tests**

Add to `tests/unit/control/test_service.py` after `StubCalendar`:

```python


class StubVolume:
    def __init__(self, available=True, level=40, muted=False, device="Logitech MeetUp Speakerphone Analog Stereo"):
        self.available, self.level, self.muted, self.device = available, level, muted, device
        self.reason = None if available else "no audio sink"
        self.calls = []

    def state(self):
        return {"available": self.available, "device": self.device if self.available else None,
                "level": self.level, "muted": self.muted, "reason": self.reason}

    async def refresh(self, force=False):
        return self.state()

    def _check(self):
        from croom.devices.errors import DeviceUnavailable
        if not self.available:
            raise DeviceUnavailable(self.reason)

    async def set_level(self, level):
        self._check()
        self.level = max(0, min(100, int(level)))
        self.calls.append(("level", self.level))
        return self.state()

    async def step(self, delta):
        return await self.set_level(self.level + int(delta))

    async def set_muted(self, muted):
        self._check()
        self.muted = bool(muted)
        self.calls.append(("muted", self.muted))
        return self.state()


class StubCamera:
    def __init__(self, available=True, home_saved=True, position_known=True, saved=(1, 2)):
        self.available, self.home_saved, self.position_known = available, home_saved, position_known
        self.saved = set(saved)
        self.zoom_level, self.moving, self.busy = 100, False, False
        self.preview_on = False
        self.moves, self.zooms, self.homes, self.recalls, self.saves, self.setups, self.previews = [], [], 0, [], [], [], []
        self.names = ["Wide", "Table", "Whiteboard"]

    @property
    def preview(self):
        return self.preview_on

    def state(self):
        return {"available": self.available, "device": "/dev/video0" if self.available else None,
                "reason": None if self.available else "no controllable camera found",
                "moving": self.moving, "busy": self.busy, "zoom": {"level": self.zoom_level, "min": 100, "max": 500},
                "position_known": self.position_known, "home_saved": self.home_saved,
                "presets": [{"slot": s, "name": self.names[s - 1], "saved": s in self.saved} for s in (1, 2, 3)],
                "preview": self.preview_on}

    def _check(self):
        from croom.devices.errors import DeviceUnavailable
        if not self.available:
            raise DeviceUnavailable("no controllable camera found")

    async def read_zoom(self):
        return self.zoom_level

    async def move(self, pan, tilt):
        if pan not in (-1, 0, 1) or tilt not in (-1, 0, 1):
            raise ValueError("pan and tilt must each be -1, 0 or 1")
        self._check()
        self.moves.append((pan, tilt))
        self.moving = bool(pan or tilt)
        return self.state()

    async def zoom(self, level):
        self._check()
        self.zoom_level = max(100, min(500, int(level)))
        self.zooms.append(self.zoom_level)
        return self.zoom_level

    async def zoom_step(self, delta):
        return await self.zoom(self.zoom_level + int(delta))

    async def home(self):
        from croom.devices.errors import NotReady
        self._check()
        if not self.home_saved:
            raise NotReady("save a home first")
        self.homes += 1
        self.position_known = True
        return self.state()

    async def find_stops(self):
        self._check()
        self.setups.append("find_stops")
        self.position_known = True
        return self.state()

    async def save_home(self):
        from croom.devices.errors import NotReady
        self._check()
        if not self.position_known:
            raise NotReady("home the camera first")
        self.setups.append("save_home")
        self.home_saved = True
        return self.state()

    async def save(self, slot):
        from croom.devices.errors import NotReady
        if slot not in (1, 2, 3):
            raise ValueError("preset slots are 1 to 3")
        self._check()
        if not self.position_known:
            raise NotReady("home the camera first")
        self.saves.append(slot)
        self.saved.add(slot)
        return self.state()

    async def recall(self, slot):
        from croom.devices.errors import NotReady
        if slot not in (1, 2, 3):
            raise ValueError("preset slots are 1 to 3")
        self._check()
        if slot not in self.saved:
            raise NotReady("nothing saved in this slot")
        self.recalls.append(slot)
        return self.state()

    def set_preview(self, on):
        self.previews.append(bool(on))
        self.preview_on = bool(on)
        return self.preview_on


class StubDevices:
    def __init__(self, volume=None, camera=None):
        self.volume = volume if volume is not None else StubVolume()
        self.camera = camera if camera is not None else StubCamera()
```

Change `make_service` so it can take devices:

```python
def make_service(meeting=None, calendar=None, devices=None, **config):
    settings = {"host": "127.0.0.1", "port": 0, "room_name": "Lab", "room_location": "2nd floor"}
    settings.update(config)
    return ControlService(config=settings, meeting=meeting, calendar=calendar, devices=devices)
```

Append the tests:

```python


class TestRoomDevices:
    """Volume and camera routes (spec 2026-10-08 sound and camera, section 4.4)."""

    async def test_status_carries_both_blocks_and_nothing_breaks_without_a_devices_service(self, client_factory):
        client = await client_factory(make_service(devices=StubDevices()))
        data = await (await client.get("/api/status")).json()
        assert data["audio"]["level"] == 40 and data["camera"]["presets"][0]["name"] == "Wide"
        bare = await client_factory(make_service())
        data = await (await bare.get("/api/status")).json()
        assert data["audio"]["available"] is False and data["camera"]["available"] is False

    async def test_volume_get_and_the_three_ways_to_change_it(self, client_factory):
        devices = StubDevices()
        client = await client_factory(make_service(devices=devices))
        assert (await (await client.get("/api/audio/volume")).json())["level"] == 40
        assert (await (await client.post("/api/audio/volume", json={"level": 70})).json())["level"] == 70
        assert (await (await client.post("/api/audio/volume", json={"step": -5})).json())["level"] == 65
        assert (await (await client.post("/api/audio/volume", json={"muted": True})).json())["muted"] is True
        assert devices.volume.calls == [("level", 70), ("level", 65), ("muted", True)]
        assert (await client.post("/api/audio/volume", json={})).status == 400
        assert (await client.post("/api/audio/volume", json={"level": "loud"})).status == 400
        assert (await client.post("/api/audio/volume", data="level=5")).status == 415

    async def test_an_unavailable_speaker_answers_409_with_the_reason(self, client_factory):
        client = await client_factory(make_service(devices=StubDevices(volume=StubVolume(available=False))))
        response = await client.post("/api/audio/volume", json={"level": 10})
        assert response.status == 409 and (await response.json())["error"] == "no audio sink"

    async def test_camera_move_zoom_and_home(self, client_factory):
        devices = StubDevices()
        client = await client_factory(make_service(devices=devices))
        assert (await (await client.post("/api/camera/move", json={"pan": 1, "tilt": 0})).json())["moving"] is True
        assert (await client.post("/api/camera/move", json={"pan": 2, "tilt": 0})).status == 400
        assert (await client.post("/api/camera/move", json={"pan": 0})).status == 400
        assert (await (await client.post("/api/camera/zoom", json={"level": 300})).json())["zoom"]["level"] == 300
        assert (await (await client.post("/api/camera/zoom", json={"step": 25})).json())["zoom"]["level"] == 325
        assert (await client.post("/api/camera/zoom", json={})).status == 400
        assert (await client.post("/api/camera/home", json={})).status == 200
        assert devices.camera.moves == [(1, 0)] and devices.camera.zooms == [300, 325] and devices.camera.homes == 1

    async def test_presets_and_setup(self, client_factory):
        devices = StubDevices(camera=StubCamera(home_saved=False, position_known=False, saved=()))
        client = await client_factory(make_service(devices=devices))
        response = await client.post("/api/camera/home", json={})
        assert response.status == 409 and (await response.json())["error"] == "save a home first"
        response = await client.post("/api/camera/presets/1", json={"action": "save"})
        assert response.status == 409 and (await response.json())["error"] == "home the camera first"
        assert (await client.post("/api/camera/setup", json={"action": "find_stops"})).status == 200
        assert (await client.post("/api/camera/setup", json={"action": "save_home"})).status == 200
        assert (await client.post("/api/camera/setup", json={"action": "dance"})).status == 400
        assert (await (await client.post("/api/camera/presets/2", json={"action": "save"})).json())["presets"][1]["saved"] is True
        assert (await client.post("/api/camera/presets/2", json={"action": "recall"})).status == 200
        response = await client.post("/api/camera/presets/3", json={"action": "recall"})
        assert response.status == 409 and (await response.json())["error"] == "nothing saved in this slot"
        assert (await client.post("/api/camera/presets/9", json={"action": "recall"})).status == 400
        assert (await client.post("/api/camera/presets/x", json={"action": "recall"})).status == 400
        assert (await client.post("/api/camera/presets/2", json={"action": "eat"})).status == 400
        assert devices.camera.setups == ["find_stops", "save_home"] and devices.camera.saves == [2] and devices.camera.recalls == [2]

    async def test_an_unavailable_camera_answers_409_everywhere(self, client_factory):
        client = await client_factory(make_service(devices=StubDevices(camera=StubCamera(available=False))))
        for path, body in (("/api/camera/move", {"pan": 1, "tilt": 0}), ("/api/camera/zoom", {"step": 25}),
                           ("/api/camera/home", {}), ("/api/camera/presets/1", {"action": "recall"}),
                           ("/api/camera/setup", {"action": "find_stops"})):
            response = await client.post(path, json=body)
            assert response.status == 409, path
            assert (await response.json())["error"] == "no controllable camera found"

    async def test_preview_is_refused_during_a_meeting(self, client_factory):
        devices = StubDevices()
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting, devices=devices))
        assert (await (await client.post("/api/camera/preview", json={"on": True})).json())["preview"] is True
        meeting.set_state(MeetingState.CONNECTED)
        response = await client.post("/api/camera/preview", json={"on": True})
        assert response.status == 409 and "meeting" in (await response.json())["error"]
        assert (await (await client.post("/api/camera/preview", json={"on": False})).json())["preview"] is False
        assert devices.camera.previews == [True, False]
        assert (await client.post("/api/camera/preview", json={"on": "yes"})).status == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_service.py -k RoomDevices`
Expected: FAIL, 404 on the new routes and no `audio` in the status.

- [ ] **Step 3: Implement the routes**

In `src/croom/control/service.py`:

- add `from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady` to the imports;
- in `create_app`, after the screensaver routes:

```python
        app.router.add_get("/api/audio/volume", self._handle_volume)
        app.router.add_post("/api/audio/volume", self._handle_set_volume)
        app.router.add_post("/api/camera/move", self._handle_camera_move)
        app.router.add_post("/api/camera/zoom", self._handle_camera_zoom)
        app.router.add_post("/api/camera/home", self._handle_camera_home)
        app.router.add_post("/api/camera/presets/{slot}", self._handle_camera_preset)
        app.router.add_post("/api/camera/setup", self._handle_camera_setup)
        app.router.add_post("/api/camera/preview", self._handle_camera_preview)
```

- in `_status()`'s dict add `"audio": self._audio_state(), "camera": self._camera_state(),`;
- change `_handle_status` so the cached device reads happen first:

```python
    async def _handle_status(self, request: web.Request) -> web.Response:
        await self._refresh_devices()
        return web.json_response(self._status())
```

- add the device section (next to the screensaver section):

```python
    # ------------------------------------------------------------------
    # Room devices: the speaker's volume and the camera (spec 2026-10-08)
    # ------------------------------------------------------------------

    NO_DEVICES = {"available": False, "reason": "no devices service"}

    def _volume(self):
        return getattr(self._devices, "volume", None)

    def _camera(self):
        return getattr(self._devices, "camera", None)

    async def _refresh_devices(self) -> None:
        """Cached reads, so a status poll costs nothing most of the time and never raises."""
        for reader in (getattr(self._volume(), "refresh", None), getattr(self._camera(), "read_zoom", None)):
            if reader is None:
                continue
            try:
                await reader()
            except Exception as e:  # noqa: BLE001 - the status must always answer
                logger.debug(f"Device read failed: {e}")

    def _audio_state(self) -> Dict[str, Any]:
        volume = self._volume()
        return volume.state() if volume is not None else dict(self.NO_DEVICES, level=0, muted=False, device=None)

    def _camera_state(self) -> Dict[str, Any]:
        camera = self._camera()
        return camera.state() if camera is not None else dict(self.NO_DEVICES, device=None, moving=False, busy=False,
                                                              zoom={"level": 0, "min": 0, "max": 0}, position_known=False,
                                                              home_saved=False, presets=[], preview=False)

    async def _device_call(self, request: web.Request, device, action):
        """Run one device action: JSON in, the device's block out; 400, 409 or 415 when it cannot be done."""
        refused = self._require_json(request)
        if refused is not None:
            return refused
        if device is None:
            return self._error_response("no devices service", 409)
        data = await self._read_object(request)
        if data is None:
            return self._error_response("Send a JSON object", 400)
        try:
            return web.json_response(await action(data))
        except ValueError as e:
            return self._error_response(str(e), 400)
        except (DeviceUnavailable, NotReady, Interrupted) as e:
            return self._error_response(str(e), 409)

    @staticmethod
    def _int_field(data: Dict[str, Any], key: str) -> int:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        return int(value)

    async def _handle_volume(self, request: web.Request) -> web.Response:
        await self._refresh_devices()
        return web.json_response(self._audio_state())

    async def _handle_set_volume(self, request: web.Request) -> web.Response:
        volume = self._volume()

        async def action(data):
            if "level" in data:
                return await volume.set_level(self._int_field(data, "level"))
            if "step" in data:
                return await volume.step(self._int_field(data, "step"))
            if "muted" in data:
                if not isinstance(data["muted"], bool):
                    raise ValueError("muted must be true or false")
                return await volume.set_muted(data["muted"])
            raise ValueError("Send level, step or muted")

        return await self._device_call(request, volume, action)

    async def _handle_camera_move(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            if "pan" not in data or "tilt" not in data:
                raise ValueError("Send pan and tilt, each -1, 0 or 1")
            return await camera.move(self._int_field(data, "pan"), self._int_field(data, "tilt"))

        return await self._device_call(request, camera, action)

    async def _handle_camera_zoom(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            if "level" in data:
                await camera.zoom(self._int_field(data, "level"))
            elif "step" in data:
                await camera.zoom_step(self._int_field(data, "step"))
            else:
                raise ValueError("Send level or step")
            return camera.state()

        return await self._device_call(request, camera, action)

    async def _handle_camera_home(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            return await camera.home()

        return await self._device_call(request, camera, action)

    async def _handle_camera_preset(self, request: web.Request) -> web.Response:
        camera = self._camera()
        slot_text = request.match_info.get("slot", "")

        async def action(data):
            if not slot_text.isdigit():
                raise ValueError("preset slots are 1 to 3")
            slot = int(slot_text)
            verb = data.get("action")
            if verb == "recall":
                return await camera.recall(slot)
            if verb == "save":
                return await camera.save(slot)
            raise ValueError("action must be recall or save")

        return await self._device_call(request, camera, action)

    async def _handle_camera_setup(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            verb = data.get("action")
            if verb == "find_stops":
                return await camera.find_stops()
            if verb == "save_home":
                return await camera.save_home()
            raise ValueError("action must be find_stops or save_home")

        return await self._device_call(request, camera, action)

    async def _handle_camera_preview(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            on = data.get("on")
            if not isinstance(on, bool):
                raise ValueError("on must be true or false")
            if on and self._meeting_state() in ("joining", "in_lobby", "connected", "leaving"):
                raise NotReady("the TV is in a meeting")
            camera.set_preview(on)
            return camera.state()

        return await self._device_call(request, camera, action)
```

`self._meeting_state()` already exists (used by `_status`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_service.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/service.py tests/unit/control/test_service.py
git commit -m "feat(control): volume and camera routes, both in the status"
```

---

### Task 8: The room page's Sound and Camera panels

**Files:**
- Modify: `src/croom/control/static/index.html`, `app.js`, `style.css`
- Modify: `tests/unit/control/test_page.py` (`PageServer` takes devices; new tests)

**Interfaces:**
- Consumes: the routes and status blocks from Task 7; `StubDevices`, `StubCamera`, `StubVolume` from `tests/unit/control/test_service.py`.
- Produces: elements `#sound-panel`, `#sound-note`, `#sound-device`, `#volume-slider`, `#quieter`, `#louder`, `#speaker-mute`, `#volume-level`; `#camera-panel` (a `<details>`), `#camera-note`, `#camera-status`, `#arrow-pad` with buttons `[data-pan][data-tilt]`, `#camera-home`, `#zoom-in`, `#zoom-out`, `#preset-row` with buttons `[data-slot]` (class `saved` when saved), `#preset-save-mode`, `#camera-setup` with `#find-stops`, `#save-home`, `#setup-link`; `PageServer(devices=...)` with `server.devices`.

- [ ] **Step 1: Let the page server take devices and write the failing tests**

In `tests/unit/control/test_page.py`, change the import line to `from tests.unit.control.test_service import StubCalendar, StubDevices, StubMeeting, event` and `PageServer.__init__` to accept `devices=None` (stored as `self.devices = devices if devices is not None else StubDevices()`), passing `devices=self.devices` to `ControlService(...)` in `_main`. Then append:

```python


# --- the Sound and Camera panels (spec 2026-10-08 sound and camera, section 4.6) ---

from tests.unit.control.test_service import StubCamera, StubVolume  # noqa: E402


def hold(page, selector, ms=120):
    box = page.locator(selector).bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(ms)
    page.mouse.up()


def test_sound_panel_shows_the_speaker_and_changes_the_level(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        assert "MeetUp" in page.locator("#sound-device").inner_text()
        assert page.locator("#volume-level").inner_text() == "40"
        page.click("#louder")
        page.wait_for_function("document.getElementById('volume-level').innerText === '45'", timeout=5000)
        page.click("#quieter")
        page.wait_for_function("document.getElementById('volume-level').innerText === '40'", timeout=5000)
        page.locator("#volume-slider").evaluate("el => { el.value = 70; el.dispatchEvent(new Event('change', {bubbles: true})); }")
        page.wait_for_function("document.getElementById('volume-level').innerText === '70'", timeout=5000)
        page.click("#speaker-mute")
        page.wait_for_function("document.getElementById('speaker-mute').getAttribute('aria-pressed') === 'true'", timeout=5000)
        assert server.devices.volume.calls == [("level", 45), ("level", 40), ("level", 70), ("muted", True)]
        page.close()


def test_holding_an_arrow_sends_start_and_stop(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
        hold(page, "#arrow-pad button[data-pan='1'][data-tilt='0']", ms=900)
        page.wait_for_timeout(300)
        moves = server.devices.camera.moves
        assert moves[0] == (1, 0) and moves[-1] == (0, 0)
        assert moves.count((1, 0)) >= 2            # re-sent while held
        page.close()


def test_zoom_home_and_presets(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
        page.click("#zoom-in")
        page.wait_for_function("document.getElementById('camera-status').innerText.includes('125')", timeout=5000)
        page.click("#camera-home")
        page.click("#preset-row button[data-slot='2']")
        page.click("#preset-save-mode")
        page.click("#preset-row button[data-slot='3']")
        page.wait_for_function("document.querySelector(\"#preset-row button[data-slot='3']\").classList.contains('saved')", timeout=5000)
        camera = server.devices.camera
        assert camera.zooms == [125] and camera.homes == 1 and camera.recalls == [2] and camera.saves == [3]
        assert page.locator("#preset-save-mode").get_attribute("aria-pressed") == "false"   # one save, then back
        page.close()


def test_the_setup_flow_until_a_home_is_saved(browser):
    with PageServer(devices=StubDevices(camera=StubCamera(home_saved=False, position_known=False, saved=()))) as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
        assert page.locator("#preset-row").is_hidden() and page.locator("#camera-home").is_disabled()
        assert page.locator("#save-home").is_disabled()
        page.click("#find-stops")
        page.wait_for_function("!document.getElementById('save-home').disabled", timeout=5000)
        page.click("#save-home")
        page.wait_for_function("!document.getElementById('preset-row').hidden", timeout=5000)
        assert server.devices.camera.setups == ["find_stops", "save_home"]
        assert page.locator("#camera-setup").is_hidden()
        page.click("#setup-link")
        assert page.locator("#camera-setup").is_visible()
        page.close()


def test_panels_hide_with_a_note_when_the_devices_are_missing(browser):
    with PageServer(devices=StubDevices(volume=StubVolume(available=False), camera=StubCamera(available=False))) as server:
        page = open_page(browser, server)
        assert page.locator("#sound-panel").is_hidden() and page.locator("#sound-note").inner_text() == "No speaker found"
        assert page.locator("#camera-panel").is_hidden() and page.locator("#camera-note").inner_text() == "No controllable camera found"
        page.close()


def test_opening_the_camera_panel_while_idle_asks_for_the_preview(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
        page.wait_for_timeout(300)
        assert server.devices.camera.previews == [True]
        page.click("#camera-panel summary")
        page.wait_for_timeout(300)
        assert server.devices.camera.previews == [True, False]
        page.close()


def test_the_panels_fit_a_phone_width(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.click("#camera-panel summary")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py -k "sound or arrow or zoom_home or setup_flow or panels or preview"`
Expected: FAIL, no `#sound-device`.

- [ ] **Step 3: The markup**

In `src/croom/control/static/index.html`, insert after the `</section>` of the link section and before the screensaver section:

```html

  <section class="sound-section">
    <p class="kicker">Sound</p>
    <p id="sound-note" class="note"></p>
    <div id="sound-panel" class="sound" hidden>
      <p id="sound-device" class="device"></p>
      <label class="visually-hidden" for="volume-slider">Volume</label>
      <input id="volume-slider" type="range" min="0" max="100" step="1" value="0">
      <div class="sound-buttons">
        <button type="button" id="quieter" class="button quiet">Quieter</button>
        <span id="volume-level" class="level" aria-live="polite"></span>
        <button type="button" id="louder" class="button quiet">Louder</button>
        <button type="button" id="speaker-mute" class="button quiet" aria-pressed="false">Mute speaker</button>
      </div>
    </div>
  </section>

  <section class="camera-section">
    <p class="kicker">Camera</p>
    <p id="camera-note" class="note"></p>
    <details id="camera-panel" class="camera" hidden>
      <summary class="button quiet">Camera framing</summary>
      <p id="camera-status" class="note"></p>
      <div class="camera-grid">
        <div id="arrow-pad" class="arrow-pad" role="group" aria-label="Pan and tilt">
          <span></span>
          <button type="button" class="button quiet pad" data-pan="0" data-tilt="1" aria-label="Tilt up">&#9650;</button>
          <span></span>
          <button type="button" class="button quiet pad" data-pan="-1" data-tilt="0" aria-label="Pan left">&#9664;</button>
          <button type="button" id="camera-home" class="button quiet pad" aria-label="Home">&#8962;</button>
          <button type="button" class="button quiet pad" data-pan="1" data-tilt="0" aria-label="Pan right">&#9654;</button>
          <span></span>
          <button type="button" class="button quiet pad" data-pan="0" data-tilt="-1" aria-label="Tilt down">&#9660;</button>
          <span></span>
        </div>
        <div class="zoom-buttons">
          <button type="button" id="zoom-out" class="button quiet" data-zoom="-25">Zoom out</button>
          <button type="button" id="zoom-in" class="button quiet" data-zoom="25">Zoom in</button>
        </div>
      </div>
      <div id="preset-row" class="presets" hidden></div>
      <button type="button" id="preset-save-mode" class="button quiet" aria-pressed="false" hidden>Save</button>
      <button type="button" id="setup-link" class="link-button" hidden>Set up</button>
      <div id="camera-setup" class="setup" hidden>
        <p class="note">Set up once: find the camera's end stops, nudge it to the view you want, then save that as Home.</p>
        <div class="setup-buttons">
          <button type="button" id="find-stops" class="button quiet">Find the stops</button>
          <button type="button" id="save-home" class="button primary" disabled>Save as Home</button>
        </div>
      </div>
    </details>
  </section>
```

- [ ] **Step 4: The script**

In `src/croom/control/static/app.js`:

- next to `let lastPickerKey = null;` add `let lastSoundKey = null; let lastCameraKey = null; let presetSaveMode = false; let setupOpen = false;`
- in `render()`, in the offline branch before its `return`, add `document.querySelector(".sound-section").hidden = true; document.querySelector(".camera-section").hidden = true;` and right after the offline check add `document.querySelector(".sound-section").hidden = false; document.querySelector(".camera-section").hidden = false;`
- at the end of `render()` after `renderPicker();` add `renderSound(s.audio); renderCamera(s.camera);`
- add these functions and listeners (before the `tick()` call at the bottom):

```js
  // --- Sound (spec 2026-10-08, section 4.6) ---
  const quietPost = (path, body) => post(path, body).then(() => refreshStatus()).catch((e) => { model.error = e.message; render(); });

  function renderSound(audio) {
    const key = JSON.stringify([audio && audio.available, audio && audio.device, audio && audio.level, audio && audio.muted]);
    if (key === lastSoundKey) return;
    lastSoundKey = key;
    const panel = el("sound-panel");
    if (!audio || !audio.available) {
      panel.hidden = true;
      el("sound-note").textContent = "No speaker found";
      return;
    }
    panel.hidden = false;
    el("sound-note").textContent = "";
    el("sound-device").textContent = audio.device;
    el("volume-slider").value = audio.level;
    el("volume-level").textContent = String(audio.level);
    el("speaker-mute").setAttribute("aria-pressed", audio.muted ? "true" : "false");
    el("speaker-mute").textContent = audio.muted ? "Unmute speaker" : "Mute speaker";
  }

  el("volume-slider").addEventListener("change", (e) => quietPost("/api/audio/volume", { level: Number(e.target.value) }));
  el("quieter").addEventListener("click", () => quietPost("/api/audio/volume", { step: -5 }));
  el("louder").addEventListener("click", () => quietPost("/api/audio/volume", { step: 5 }));
  el("speaker-mute").addEventListener("click", () => {
    const muted = el("speaker-mute").getAttribute("aria-pressed") === "true";
    quietPost("/api/audio/volume", { muted: !muted });
  });

  // --- Camera ---
  const IN_MEETING = ["joining", "in_lobby", "connected", "leaving"];
  let previewTimer = null;

  function renderCamera(camera) {
    const key = JSON.stringify([camera && camera.available, camera && camera.busy, camera && camera.position_known,
      camera && camera.home_saved, camera && camera.zoom, camera && camera.presets, presetSaveMode, setupOpen]);
    if (key === lastCameraKey) return;
    lastCameraKey = key;
    const panel = el("camera-panel");
    if (!camera || !camera.available) {
      panel.hidden = true;
      el("camera-note").textContent = "No controllable camera found";
      return;
    }
    panel.hidden = false;
    el("camera-note").textContent = "";
    el("camera-status").textContent = camera.busy ? "Moving…" : "Zoom " + camera.zoom.level + " of " + camera.zoom.min + " to " + camera.zoom.max;
    for (const b of document.querySelectorAll("#arrow-pad .pad")) b.disabled = camera.busy;
    el("camera-home").disabled = camera.busy || !camera.home_saved;
    const ready = camera.home_saved;
    el("preset-row").hidden = !ready;
    el("preset-save-mode").hidden = !ready;
    el("setup-link").hidden = !ready;
    el("camera-setup").hidden = ready && !setupOpen;
    el("save-home").disabled = !camera.position_known || camera.busy;
    el("find-stops").disabled = camera.busy;
    el("preset-save-mode").setAttribute("aria-pressed", presetSaveMode ? "true" : "false");
    el("preset-save-mode").textContent = presetSaveMode ? "Tap a preset to save it here" : "Save";
    el("preset-row").replaceChildren(...camera.presets.map((p) => {
      const b = button(p.name, p.saved ? "quiet saved" : "quiet", () => presetAction(p.slot), camera.busy);
      b.dataset.slot = String(p.slot);
      if (!p.saved && !presetSaveMode) b.title = "Nothing saved yet";
      return b;
    }));
  }

  function presetAction(slot) {
    const action = presetSaveMode ? "save" : "recall";
    presetSaveMode = false;
    lastCameraKey = null;
    quietPost("/api/camera/presets/" + slot, { action: action });
  }

  el("preset-save-mode").addEventListener("click", () => { presetSaveMode = !presetSaveMode; lastCameraKey = null; render(); });
  el("setup-link").addEventListener("click", () => { setupOpen = !setupOpen; lastCameraKey = null; render(); });
  el("find-stops").addEventListener("click", () => quietPost("/api/camera/setup", { action: "find_stops" }));
  el("save-home").addEventListener("click", () => { setupOpen = false; quietPost("/api/camera/setup", { action: "save_home" }); });
  el("camera-home").addEventListener("click", () => quietPost("/api/camera/home", {}));

  // Hold to move: press starts, release stops; re-sent every 750 ms while held so the agent's watchdog stays quiet.
  function holdToMove(buttonEl, start, repeatMs, stop) {
    let timer = null;
    const release = () => {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
      if (stop) stop();
    };
    buttonEl.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      if (buttonEl.disabled || timer !== null) return;
      buttonEl.setPointerCapture(e.pointerId);
      start();
      timer = setInterval(start, repeatMs);
    });
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) buttonEl.addEventListener(type, release);
  }
  const moveCamera = (pan, tilt) => post("/api/camera/move", { pan: pan, tilt: tilt }).catch(() => {});
  for (const b of document.querySelectorAll("#arrow-pad [data-pan]")) {
    const pan = Number(b.dataset.pan), tilt = Number(b.dataset.tilt);
    holdToMove(b, () => moveCamera(pan, tilt), 750, () => moveCamera(0, 0).then(() => refreshStatus()));
  }
  for (const b of document.querySelectorAll("[data-zoom]")) {
    holdToMove(b, () => quietPost("/api/camera/zoom", { step: Number(b.dataset.zoom) }), 400, null);
  }

  // The idle preview on the TV: asked for while the panel is open and no meeting runs, renewed every 30 s.
  function previewWanted() {
    return el("camera-panel").open && model.status && !IN_MEETING.includes(model.status.meeting.state);
  }
  function setPreview(on) {
    post("/api/camera/preview", { on: on }).catch(() => {});
  }
  el("camera-panel").addEventListener("toggle", () => {
    clearInterval(previewTimer);
    previewTimer = null;
    if (previewWanted()) {
      setPreview(true);
      previewTimer = setInterval(() => { if (previewWanted()) setPreview(true); }, 30000);
    } else {
      setPreview(false);
    }
  });
  window.addEventListener("pagehide", () => {
    if (!el("camera-panel").open) return;
    fetch("/api/camera/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: false }), keepalive: true }).catch(() => {});
  });
```

`button(label, className, onClick, disabled)` and `post` already exist in the file. The preset button test clicks `#preset-row button[data-slot='3']` after Save mode, so `presetAction` reads the mode before resetting it. Zoom buttons repeat while held: the single click in the test produces one `pointerdown`/`pointerup`, so exactly one step.

- [ ] **Step 5: The styles**

Append to `src/croom/control/static/style.css`:

```css

/* Sound and Camera panels (spec 2026-10-08). */
.sound-section { grid-area: sound; }
.camera-section { grid-area: camera; }
.device { margin: 0 0 10px; color: var(--periwinkle-300); overflow-wrap: anywhere; }
.sound input[type="range"] { width: 100%; max-width: 520px; height: 44px; accent-color: var(--blue-600); }
.sound-buttons { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 8px; }
.level { min-width: 3ch; text-align: center; font-size: 1.4rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.camera summary { list-style: none; display: inline-flex; align-items: center; cursor: pointer; }
.camera summary::-webkit-details-marker { display: none; }
.camera[open] summary { background: var(--blue-600); border-color: var(--blue-600); }
.camera-grid { display: flex; flex-wrap: wrap; gap: 24px; align-items: center; margin-top: 14px; }
.arrow-pad { display: grid; grid-template-columns: repeat(3, 64px); grid-template-rows: repeat(3, 64px); gap: 8px; }
.arrow-pad .pad { min-height: 64px; padding: 0; font-size: 1.3rem; touch-action: none; }
.zoom-buttons { display: flex; flex-direction: column; gap: 10px; }
.zoom-buttons .button { touch-action: none; }
.presets { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
.presets .button.saved { border-color: var(--blue-600); }
#preset-save-mode { margin-top: 10px; }
#preset-save-mode[aria-pressed="true"] { background: var(--blue-600); border-color: var(--blue-600); }
.link-button { background: none; border: 0; color: var(--periwinkle-300); font: inherit; text-decoration: underline; cursor: pointer; padding: 0; margin-top: 12px; display: block; }
.setup { margin-top: 12px; }
.setup-buttons { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px; }
```

and add the two grid areas: the narrow layout becomes `grid-template-areas: "top" "now" "link" "sound" "camera" "screen" "schedule";` and the wide one:

```css
    grid-template-areas:
      "top top"
      "now schedule"
      "link schedule"
      "sound schedule"
      "camera schedule"
      "screen schedule";
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py tests/unit/control/test_tv_page.py`
Expected: PASS, including the older phone-width and keyboard tests. If `test_holding_an_arrow_sends_start_and_stop` sees only one `(1, 0)`, the hold was shorter than one repeat: the test holds 900 ms against a 750 ms repeat, so check that `setInterval(start, repeatMs)` is reached (the `disabled` guard must be false: `camera.busy` is false on the stub).

- [ ] **Step 7: Look at it**

Screenshot the page at 1280×800 and 390×844 with the camera panel open (the way the TV styles were checked) and fix anything that overflows or overlaps.

- [ ] **Step 8: Commit**

```bash
git add src/croom/control/static/index.html src/croom/control/static/app.js src/croom/control/static/style.css tests/unit/control/test_page.py
git commit -m "feat(control): Sound and Camera panels on the room page"
```

---

### Task 9: The TV's camera preview

**Files:**
- Modify: `src/croom/control/static/tv.html`, `tv.css`, `tv.js`
- Modify: `tests/unit/control/test_tv_page.py`

**Interfaces:**
- Consumes: `camera.preview` and `meeting.state` from `/api/status`; `StubDevices`/`StubCamera` through `PageServer(devices=...)`.
- Produces: `<video id="camera-preview">`, `#preview-caption`, `body[data-preview="on"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/control/test_tv_page.py`:

```python


# --- the idle camera preview (spec 2026-10-08 sound and camera, section 4.7) ---

from tests.unit.control.test_service import StubCamera, StubDevices  # noqa: E402


@pytest.fixture(scope="module")
def camera_browser():
    """Chromium with its built-in fake camera, so getUserMedia yields a real stream."""
    with playwright.sync_playwright() as p:
        try:
            instance = p.chromium.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {e}")
        yield instance
        instance.close()


def test_preview_shows_the_camera_while_asked_for_and_the_screensaver_returns(camera_browser):
    camera = StubCamera()
    with PageServer(devices=StubDevices(camera=camera)) as server:
        context = camera_browser.new_context(permissions=["camera"], viewport={"width": 1280, "height": 720})
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.port}/tv", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#camera-preview").is_hidden()
        camera.preview_on = True
        page.wait_for_function("document.body.dataset.preview === 'on'", timeout=5000)
        page.wait_for_function("document.getElementById('camera-preview').videoWidth > 0", timeout=8000)
        assert page.locator("#camera-preview").is_visible() and page.locator("#headline").is_hidden()
        assert page.locator("#preview-caption").inner_text() == "Camera preview"
        camera.preview_on = False
        page.wait_for_function("document.body.dataset.preview !== 'on'", timeout=5000)
        assert page.locator("#camera-preview").is_hidden() and page.locator("#headline").is_visible()
        assert page.evaluate("document.getElementById('camera-preview').srcObject === null")
        context.close()


def test_preview_never_shows_during_a_meeting(camera_browser):
    camera = StubCamera()
    camera.preview_on = True
    with PageServer(devices=StubDevices(camera=camera)) as server:
        context = camera_browser.new_context(permissions=["camera"], viewport={"width": 1280, "height": 720})
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.port}/tv", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.preview === 'on'", timeout=5000)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'occupied'", timeout=8000)
        assert page.evaluate("document.body.dataset.preview") != "on"
        assert page.locator("#camera-preview").is_hidden()
        context.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_tv_page.py -k preview`
Expected: FAIL, no `#camera-preview` element.

- [ ] **Step 3: Implement**

In `tv.html`, before the bounce logo `<div>`:

```html
  <video id="camera-preview" class="camera-preview" autoplay muted playsinline hidden></video>
  <p id="preview-caption" class="preview-caption" hidden>Camera preview</p>
```

In `tv.css`:

```css

/* The idle camera preview: the room, full screen on black, while the room page's Camera panel is open. */
body[data-preview="on"] { background: #000; }
body[data-preview="on"] .top, body[data-preview="on"] .status, body[data-preview="on"] .hint,
body[data-preview="on"] .brand-logo, body[data-preview="on"] .bounce-logo { display: none; }
.camera-preview { position: fixed; inset: 0; width: 100%; height: 100%; object-fit: contain; background: #000; }
.preview-caption { position: fixed; left: var(--gutter); bottom: var(--gutter); margin: 0; font-size: clamp(1rem, 1.6vw, 1.6rem); color: var(--periwinkle-300); }
```

In `tv.js`, add the module before `render()` and call `preview.update(model.status);` inside `render()` right after `bounce.setActive(model.style === "bounce");` (before the loading and offline guards, so an offline status clears the preview too):

```js
  // The idle camera preview (spec 2026-10-08, section 4.7): on while the room page's Camera
  // panel is open and no meeting runs; the stream is opened once and stopped when it ends.
  const preview = (function () {
    const video = el("camera-preview");
    const caption = el("preview-caption");
    let stream = null, starting = false;
    async function start() {
      if (stream || starting) return;
      starting = true;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: true });
        video.srcObject = stream;
        caption.textContent = "Camera preview";
      } catch (e) {
        caption.textContent = "Camera preview unavailable";
      }
      starting = false;
    }
    function stop() {
      if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
      video.srcObject = null;
    }
    return {
      update(status) {
        const on = Boolean(status && status.camera && status.camera.preview && !IN_PROGRESS.includes(status.meeting.state));
        if (on) {
          document.body.dataset.preview = "on";
          video.hidden = false;
          caption.hidden = false;
          start();
        } else {
          delete document.body.dataset.preview;
          video.hidden = true;
          caption.hidden = true;
          stop();
        }
      },
    };
  })();
```

`IN_PROGRESS` already exists in `tv.js`. When the page is offline `model.status` is stale; pass `model.offline ? null : model.status` so the preview clears. Because `.camera-preview` has no `display` rule, the `hidden` attribute hides it on its own.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_tv_page.py`
Expected: PASS. The fake camera device produces frames, so `videoWidth > 0` becomes true within a second.

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/static/tv.html src/croom/control/static/tv.css src/croom/control/static/tv.js tests/unit/control/test_tv_page.py
git commit -m "feat(control): the TV shows the camera while the room page frames it"
```

---

### Task 10: Diagnostics, installer and docs

**Files:**
- Modify: `src/croom/meeting/meet_check.py`, `tests/unit/meeting/test_meet_check.py`
- Modify: `installer/install.sh`, `tests/unit/installer/test_install_script.py`
- Modify: `README.md`, `docs/guides/crystal-meet-room-setup/index.html` (+ rebuilt PDF), `docs/guides/crystal-meet-google-meet/index.html` (+ rebuilt PDF), `tests/unit/docs/test_readme.py`, `tests/unit/docs/test_room_setup_guide.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/meeting/test_meet_check.py`:

```python


async def test_reports_the_devices_the_browser_sees(tmp_path):
    page = tmp_path / "prejoin.html"
    page.write_text(PREJOIN)
    out = io.StringIO()
    await check_meet(page.as_uri(), out=out, headless=True, settle_ms=200)
    text = out.getvalue()
    assert "Devices the browser sees:" in text
    assert "microphones:" in text and "speakers:" in text and "cameras:" in text
```

Append to `tests/unit/installer/test_install_script.py`:

```python


def test_installer_adds_the_pulse_client_library_for_the_browsers_audio():
    """Chromium reaches PipeWire through libpulse; a fresh Pi may not have it."""
    body = SCRIPT.read_text().split("install_dependencies() {", 1)[1].split("\n}", 1)[0]
    assert "libpulse0" in body
```

In `tests/unit/docs/test_readme.py`, add the needles `"audio.output_device"`, `"ptz_travel_seconds"`, `"camera_presets"`, `"/api/audio/volume"` and `"Find the stops"`. In `tests/unit/docs/test_room_setup_guide.py` append:

```python


def test_guide_sets_up_the_camera_and_the_sound():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "Find the stops" in html and "Save as Home" in html and "Mute speaker" in html
    assert "Devices the browser sees" in html
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_check.py::test_reports_the_devices_the_browser_sees tests/unit/installer/test_install_script.py::test_installer_adds_the_pulse_client_library_for_the_browsers_audio tests/unit/docs`
Expected: FAIL on all four fronts.

- [ ] **Step 3: The check's Devices section**

In `src/croom/meeting/meet_check.py` add the constant next to `DESCRIBE_JS`:

```python
DEVICES_JS = """
async () => {
  try { const s = await navigator.mediaDevices.getUserMedia({audio: true, video: true}); s.getTracks().forEach(t => t.stop()); } catch (e) {}
  const all = await navigator.mediaDevices.enumerateDevices();
  const names = (kind) => all.filter(d => d.kind === kind && d.label).map(d => d.label);
  return { microphones: names('audioinput'), speakers: names('audiooutput'), cameras: names('videoinput') };
}
"""
```

and, inside `check_meet`'s `try:` block right after `await page.wait_for_timeout(settle_ms)`:

```python
            try:
                devices = await page.evaluate(DEVICES_JS)
            except Exception as e:  # noqa: BLE001 - a page without media APIs still gets the rest of the report
                devices = {"microphones": [], "speakers": [], "cameras": [], "error": str(e)}

            def listed(names):
                return ", ".join(names) if names else "none"

            print(f"Devices the browser sees: microphones: {listed(devices['microphones'])}; "
                  f"speakers: {listed(devices['speakers'])}; cameras: {listed(devices['cameras'])}", file=out)
```

- [ ] **Step 4: The installer**

In `installer/install.sh`, inside `install_dependencies`'s first `apt-get install -y` list, add `libpulse0 \` on the line after `pulseaudio \`.

- [ ] **Step 5: The docs**

README:
- pieces table, Room page row: append to "What it does": "Sound (the room speaker's level and mute) and Camera (hold-to-move, zoom, Home, three presets) panels, with a live camera preview on the TV while framing between meetings."
- config table: add rows `| audio | output_device | The speaker the Sound panel drives: auto means PipeWire's default output; otherwise the first output whose name contains this text (HDMI, MeetUp). |` and `| video | device, ptz_travel_seconds | The camera the Camera panel drives (auto: the first video node with zoom or pan controls) and how long it is driven to reach an end stop when homing (8 s). |`, and extend the `control` row's keys with `camera_presets` ("the three preset names").
- decisions: `- The MeetUp reports no pan or tilt position, so presets are timed moves from a home found against the end stops (the camera homes once at service start and after Home); the position is tracked in seconds of travel and recalls move only the difference. Volume goes through PipeWire's own tools (pw-dump, wpctl), never through the upstream audio service.`
- the control API sentence in the decisions or pieces section gains `/api/audio/volume` and `/api/camera/...` (one sentence).
- implementation notes: the row added by the kiosk work says "plan to follow"; replace it with `[plan](docs/superpowers/plans/2026-10-08-sound-and-camera-controls.md)`.

Room setup guide: after the "Set up the door sign" bullet add:

```html
  <li><strong>Set up the camera and sound.</strong> On the room page, open <span class="ui">Camera framing</span>: the TV shows the camera's picture while the room is idle. Press <span class="ui">Find the stops</span> (the camera moves to its corner), nudge it with the arrows and zoom until the view is right, then <span class="ui">Save as Home</span>. Frame the table or the whiteboard, press <span class="ui">Save</span> and a preset button to keep it; a tap on the preset brings the camera back there, even during a call. Under <span class="ui">Sound</span>, set the room's level and try <span class="ui">Mute speaker</span>; to send sound to the TV instead of the speakerphone, set <span class="chip">audio.output_device: HDMI</span> in the room config.</li>
```

and in the troubleshooting card "No sound or picture in the call" add: `From the Pi, <span class="chip wrap">PLAYWRIGHT_BROWSERS_PATH=/opt/croom/browsers DISPLAY=:0 /opt/croom/venv/bin/croom --check-meet &lt;link&gt;</span> prints "Devices the browser sees" with the microphones, speakers and cameras the browser can use; if the MeetUp is missing there but <span class="chip">wpctl status</span> lists it, restart the service.` Rebuild: `.venv/bin/python docs/guides/crystal-meet-room-setup/build.py`.

Meet guide: where the check's output is described, add one sentence that it now also prints the devices the browser sees; rebuild with its `build.py`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_check.py tests/unit/installer tests/unit/docs`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/croom/meeting/meet_check.py tests/unit/meeting/test_meet_check.py installer/install.sh tests/unit/installer/test_install_script.py README.md docs/guides tests/unit/docs
git commit -m "docs: sound and camera on the room page, the browser's device list in the Meet check, libpulse0"
```

---

### Task 11: The gate and the acceptance

- [ ] **Step 1: Run the full gate**

Run: `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh room-devices | grep "GATE:"`
Expected: `GATE: PASSED`.

- [ ] **Step 2: Run the agent on this machine once**

With the scratch config used for the TV work (platforms zoom and google_meet, kiosk off, a free port), run `.venv/bin/croom -v -c <scratch config>` for half a minute: the log must show the devices service starting (speaker unavailable with a reason on this machine, no camera), `/api/status` must carry `audio` and `camera`, and the room page's panels must show their "not found" notes.

- [ ] **Step 3: Acceptance on PiMeet-3**

After merge, from the spec's section 6: volume moves the MeetUp; `audio.output_device: HDMI` moves the TV; Set up (find the stops, save Home, save Table and Whiteboard); the camera homes at service start; a recall during a Meet call lands where it was saved; the idle preview shows on the TV and the screensaver returns; `croom --check-meet` lists the MeetUp microphone and speaker.
