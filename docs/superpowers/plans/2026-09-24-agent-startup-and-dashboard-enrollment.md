# Agent Startup and Dashboard Enrollment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Croom agent start on a machine with no conference-room hardware and register itself with the management dashboard, so device and dashboard can be tested together on WSL2 before touching a Raspberry Pi.

**Architecture:** The five plain service classes (audio, video, calendar, display, dashboard client) become `Service` subclasses with a `from_config(Config)` classmethod that maps the config dataclasses onto the dict keys they already read, so `agent.py` stays thin and the existing dict-based constructors and tests keep working. Optional services contain their own failures instead of raising out of `start()`. The dashboard client is rewritten on `aiohttp` to follow the backend's real contract: REST enrollment with a one-time token, then a WebSocket `auth`/`heartbeat`/`status` session with reconnection and a small state file.

**Tech Stack:** Python 3.12 virtualenv at `.venv` (pytest 9, pytest-asyncio 1.4 with `asyncio_mode = "auto"`, so `async def` tests need no marker), aiohttp 3.x (already a dependency; `aiohttp.test_utils.TestServer` provides the in-process fake dashboard for tests), the Node dashboard backend on `localhost:3001` for the acceptance run.

**Spec:** `docs/superpowers/specs/2026-09-24-agent-startup-and-dashboard-enrollment-design.md`

## Global Constraints

- Python `>=3.10` (pyproject). No new runtime dependencies. Nothing under `src/croom` may import `websockets`.
- Service names are exactly `audio`, `video`, `calendar`, `display`, `dashboard`. The agent's dependency declarations keep using `ai`, `audio`, `video`.
- Every service constructor keeps the signature `__init__(self, config: Optional[Dict[str, Any]] = None)`.
- `initialize()` on audio, video, calendar and display stays public and becomes idempotent through an `_initialized` flag that is set only when it succeeds.
- Raspberry Pi path unchanged: `/var/lib/croom` is preferred as data dir when writable; the `/etc/croom/config.yaml` search order is untouched.
- Dashboard protocol: every WebSocket message is `{"type": <string>, "payload": {...}}`; enrollment is `POST {url}/api/provisioning/enroll` with `{"token", "deviceInfo"}`; heartbeat interval is clamped to 55 seconds; reconnect backoff starts at 5 seconds, doubles, caps at 60 seconds.
- Work on branch `dev` (remote `origin` is the ben-abeo fork). Commit after each task with a `type(scope): summary` message and no attribution trailers.
- Run tests with `.venv/bin/pytest` from the repo root. Baseline on `main`: 340 passed, 87 failed (stale upstream tests; do not fix them). After every task, `.venv/bin/pytest -q --tb=no 2>&1 | tail -1` must report at most 87 failed, and every test added by the task must pass.

## Review Focus

1. A malformed or non-object WebSocket frame from the dashboard: the session stays up and heartbeats continue. Pinned by Task 6 `test_malformed_message_does_not_break_the_session`.
2. `dashboard.url` with a trailing slash or a path prefix: the enroll URL and the WebSocket URL are still correct. Pinned by Task 6 `TestWebsocketUrl` and `test_from_config_builds_client_settings`.
3. A corrupt or truncated state file: treated as not enrolled, never a crash. Pinned by Task 6 `test_corrupt_file_is_ignored`.
4. The backend restarts or drops the socket: the client reconnects and re-authenticates with its saved id without enrolling again. Pinned by Task 6 `test_reconnects_after_server_closes_without_reenrolling`.
5. `heartbeat_interval_seconds` above the backend's 60 second timeout: clamped to 55 with a warning instead of flapping online/offline. Pinned by Task 6 `test_heartbeat_interval_is_clamped_to_backend_timeout`.

---

## File Structure

| File | Responsibility after this plan |
|---|---|
| `src/croom/core/config.py` | Config dataclasses. Gains `data_dir`, `SYSTEM_DATA_DIR`, `resolve_data_dir()`, and the `auto_to_default()` helper shared by audio and video mapping. |
| `src/croom/audio/service.py` | `AudioService(Service)` named `audio`; `from_config`; `start()` initializes first. |
| `src/croom/video/service.py` | `VideoService(Service)` named `video`; `from_config` with resolution aliases; `start()` initializes first. |
| `src/croom/display/service.py` | `DisplayService(Service)` named `display`; `from_config` maps `display.backend` to CEC/DDC flags. |
| `src/croom/calendar/service.py` | `CalendarService(Service)` named `calendar`; `from_config`; idle without a provider. |
| `src/croom/dashboard/client.py` | Rewritten: enrollment, WebSocket session, heartbeats, status, reconnection, state file. |
| `src/croom/dashboard/__init__.py` | Drops the removed `create_dashboard_client` export. |
| `src/croom/core/agent.py` | `_initialize_services()` builds services through `from_config`. |
| `tests/unit/core/test_config.py` | Adds `TestDataDir`, `TestAutoToDefault`. |
| `tests/unit/audio/test_service.py` | Adds `TestAudioServiceAsService`. |
| `tests/unit/video/test_service.py` | New: `TestVideoServiceAsService`. |
| `tests/unit/display/test_service.py` | Adds `TestDisplayServiceAsService`. |
| `tests/unit/calendar/test_service.py` | Adds `TestCalendarServiceAsService`. |
| `tests/unit/dashboard/test_client.py` | New: fake dashboard and protocol tests. |
| `tests/unit/ai/test_service.py` | New: regression test for startup without model files. |
| `tests/unit/core/test_agent.py` | New: registration and `start_all()` without hardware. |

Every task follows the same rhythm: write the failing tests, watch them fail, make the smallest change that passes them, run the module's tests, run the whole suite against the constraint above, commit.

---

### Task 1: `data_dir` config key and `resolve_data_dir()`

**Files:**
- Modify: `src/croom/core/config.py` (imports at top; `Config` dataclass; `from_dict`; `to_dict`)
- Test: `tests/unit/core/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Config.data_dir: str` (default `""`); `Config.resolve_data_dir() -> pathlib.Path`; module constant `SYSTEM_DATA_DIR = Path("/var/lib/croom")`; module function `_writable_dir(path: Path) -> bool`; module function `auto_to_default(value: str) -> str` (returns `"default"` for `""` or `"auto"`, otherwise the value unchanged). Tasks 2, 3 and 6 use these.

- [ ] **Step 1: Write the failing tests**

Append to the end of `tests/unit/core/test_config.py`:

```python
class TestDataDir:
    """Tests for the data_dir key and Config.resolve_data_dir() (spec 4.5)."""

    def test_defaults_to_empty(self):
        assert Config().data_dir == ""

    def test_round_trips_through_dict(self):
        config = Config.from_dict({"data_dir": "/srv/croom-data"})
        assert config.data_dir == "/srv/croom-data"
        assert config.to_dict()["data_dir"] == "/srv/croom-data"
        assert Config.from_dict(config.to_dict()).data_dir == "/srv/croom-data"

    def test_explicit_dir_is_created_and_returned(self, tmp_path):
        target = tmp_path / "explicit" / "nested"
        config = Config(data_dir=str(target))
        assert config.resolve_data_dir() == target
        assert target.is_dir()

    def test_system_dir_wins_when_writable(self, tmp_path, monkeypatch):
        system_dir = tmp_path / "var-lib-croom"
        system_dir.mkdir()
        monkeypatch.setattr("croom.core.config.SYSTEM_DATA_DIR", system_dir)
        assert Config().resolve_data_dir() == system_dir

    def test_unwritable_system_dir_is_skipped(self, tmp_path, monkeypatch):
        system_dir = tmp_path / "var-lib-croom"
        system_dir.mkdir()
        monkeypatch.setattr("croom.core.config.SYSTEM_DATA_DIR", system_dir)
        monkeypatch.setattr("croom.core.config._writable_dir", lambda path: False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert Config().resolve_data_dir() == tmp_path / "xdg" / "croom"

    def test_falls_back_to_xdg_data_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr("croom.core.config.SYSTEM_DATA_DIR", tmp_path / "does-not-exist")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert Config().resolve_data_dir() == tmp_path / "xdg" / "croom"
        assert (tmp_path / "xdg" / "croom").is_dir()

    def test_falls_back_to_home_local_share(self, tmp_path, monkeypatch):
        monkeypatch.setattr("croom.core.config.SYSTEM_DATA_DIR", tmp_path / "does-not-exist")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert Config().resolve_data_dir() == tmp_path / ".local" / "share" / "croom"


class TestAutoToDefault:
    """auto_to_default() translates the config's device selector for the services."""

    def test_auto_and_empty_become_default(self):
        from croom.core.config import auto_to_default
        assert auto_to_default("auto") == "default"
        assert auto_to_default("") == "default"

    def test_explicit_device_passes_through(self):
        from croom.core.config import auto_to_default
        assert auto_to_default("hw:1,0") == "hw:1,0"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/core/test_config.py -q -k "TestDataDir or TestAutoToDefault"`
Expected: FAIL. `test_defaults_to_empty` with `AttributeError: 'Config' object has no attribute 'data_dir'`, `Config(data_dir=...)` with `TypeError: unexpected keyword argument`, the `auto_to_default` tests with `ImportError`.

- [ ] **Step 3: Implement**

In `src/croom/core/config.py`, the imports at the top already include `os`, `dataclass`, `field`, `Optional`, `Dict`, `Any`, `List` and `Path`. Directly below the `CONFIG_PATHS` list (the list that starts with `"/etc/croom/config.yaml"`), add:

```python
# Preferred location for runtime state on an installed device (the installer
# creates it and the croom service user owns it). See Config.resolve_data_dir().
SYSTEM_DATA_DIR = Path("/var/lib/croom")


def auto_to_default(value: str) -> str:
    """Translate the config's 'auto' (or empty) device selector into the 'default' the services expect."""
    return "default" if value in ("", "auto") else value


def _writable_dir(path: Path) -> bool:
    """True when path is an existing directory this process can write to."""
    return path.is_dir() and os.access(path, os.W_OK)
```

In the `Config` dataclass, directly after the line `platform_type: str = "auto"  # 'rpi5', 'rpi4', 'pc', 'auto'`, add:

```python
    # Directory for runtime state such as the dashboard enrollment file.
    # Empty means: /var/lib/croom when writable, otherwise $XDG_DATA_HOME/croom.
    data_dir: str = ""
```

In `Config.from_dict`, directly after the two lines

```python
        if "platform_type" in data:
            config.platform_type = data["platform_type"]
```

add:

```python
        if "data_dir" in data:
            config.data_dir = data["data_dir"]
```

In `Config.to_dict`, directly after the line `"platform_type": self.platform_type,` add:

```python
            "data_dir": self.data_dir,
```

Add this method to `Config` (for example directly before `def save(`):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/core/test_config.py -q`
Expected: all tests in the module pass, including the pre-existing ones.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -q --tb=no 2>&1 | tail -1`
Expected: at most 87 failed.

- [ ] **Step 6: Commit**

```bash
git add src/croom/core/config.py tests/unit/core/test_config.py
git commit -m "feat(config): add data_dir with resolve_data_dir() and auto_to_default()"
```

---

### Task 2: `AudioService` joins the service framework

**Files:**
- Modify: `src/croom/audio/service.py` (imports; `class AudioService`; `__init__`; `initialize`; `start`)
- Test: `tests/unit/audio/test_service.py`

**Interfaces:**
- Consumes: `Service` from `croom.core.service`; `Config`, `auto_to_default` from `croom.core.config` (Task 1).
- Produces: `AudioService(Service)` with `name == "audio"`; `AudioService.from_config(config: Config) -> AudioService`; `AudioService.initialize() -> bool` idempotent; `start()` that never raises when no devices exist. Task 7 calls `from_config`.

- [ ] **Step 1: Write the failing tests**

Append to the end of `tests/unit/audio/test_service.py` (the module already imports `MagicMock, patch, AsyncMock` and `pytest`):

```python
class TestAudioServiceAsService:
    """AudioService participates in the Service framework (spec 4.1 to 4.3)."""

    def test_is_a_service_named_audio(self):
        from croom.audio.service import AudioService
        from croom.core.service import Service, ServiceState

        service = AudioService()
        assert isinstance(service, Service)
        assert service.name == "audio"
        assert service.state == ServiceState.STOPPED

    @pytest.mark.asyncio
    async def test_start_and_stop_without_devices(self):
        with patch("croom.audio.service.get_audio_devices", return_value=[]):
            from croom.audio.service import AudioService

            service = AudioService()
            await service.start()
            assert service._running is True
            assert service._input_device is None
            assert service._output_device is None
            await service.stop()
            assert service._running is False

    @pytest.mark.asyncio
    async def test_initialize_runs_once(self):
        with patch("croom.audio.service.get_audio_devices", return_value=[]) as probe:
            from croom.audio.service import AudioService

            service = AudioService()
            assert await service.initialize() is True
            assert await service.initialize() is True
            assert probe.call_count == 1

    def test_from_config_maps_dataclass_to_service_keys(self):
        from croom.audio.service import AudioService
        from croom.core.config import Config

        config = Config()
        config.audio.input_device = "auto"
        config.audio.output_device = "alsa_output.usb-Jabra"
        config.audio.noise_reduction_level = "off"
        config.audio.echo_cancellation = False
        service = AudioService.from_config(config)
        assert service.config == {
            "input_device": "default",
            "output_device": "alsa_output.usb-Jabra",
            "noise_reduction": False,
            "echo_cancellation": False,
        }

    def test_from_config_noise_reduction_is_on_for_any_level_but_off(self):
        from croom.audio.service import AudioService
        from croom.core.config import Config

        config = Config()
        config.audio.noise_reduction_level = "aggressive"
        assert AudioService.from_config(config).config["noise_reduction"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/audio/test_service.py -q -k TestAudioServiceAsService`
Expected: FAIL. `isinstance` assertion false, `AttributeError: 'AudioService' object has no attribute 'name'`, `AttributeError: type object 'AudioService' has no attribute 'from_config'`, `probe.call_count == 2`.

- [ ] **Step 3: Implement**

In `src/croom/audio/service.py`:

1. Directly after the line `import numpy as np` add:

```python
from croom.core.config import Config, auto_to_default
from croom.core.service import Service
```

2. Change `class AudioService:` to `class AudioService(Service):`.

3. In `__init__`, replace the line `self.config = config or {}` with:

```python
        super().__init__("audio")
        self.config = config or {}
        self._initialized = False
```

4. Directly after `__init__` (before `async def initialize`), add:

```python
    @classmethod
    def from_config(cls, config: Config) -> "AudioService":
        """Build the service from the agent's Config (spec section 4.2)."""
        audio = config.audio
        return cls(config={
            "input_device": auto_to_default(audio.input_device),
            "output_device": auto_to_default(audio.output_device),
            "noise_reduction": audio.noise_reduction_level != "off",
            "echo_cancellation": audio.echo_cancellation,
        })
```

5. In `initialize()`, insert as the first statements of the method body (before `try:`):

```python
        if self._initialized:
            return True
```

and directly before the line `logger.info("Audio service initialized")` insert:

```python
            self._initialized = True
```

6. In `start()`, directly after

```python
        if self._running:
            return
```

insert:

```python
        if not self._initialized and not await self.initialize():
            logger.warning("Audio service running without devices")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/audio/test_service.py -q`
Expected: the new tests pass; the pre-existing tests in the module keep their previous result.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -q --tb=no 2>&1 | tail -1`
Expected: at most 87 failed.

- [ ] **Step 6: Commit**

```bash
git add src/croom/audio/service.py tests/unit/audio/test_service.py
git commit -m "fix(audio): make AudioService a Service with from_config and safe start"
```

---

### Task 3: `VideoService` joins the service framework

**Files:**
- Modify: `src/croom/video/service.py` (imports; `class VideoService`; `__init__`; `initialize`; `start`)
- Create: `tests/unit/video/test_service.py`

**Interfaces:**
- Consumes: `Service`; `Config`, `auto_to_default` (Task 1).
- Produces: `VideoService(Service)` with `name == "video"`; `VideoService.from_config(config: Config) -> VideoService`; module constant `RESOLUTION_ALIASES`. Task 7 calls `from_config`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/video/test_service.py`:

```python
"""
Tests for croom.video.service: Service integration and config mapping.
"""

from unittest.mock import patch

import pytest

from croom.core.config import Config
from croom.core.service import Service, ServiceState
from croom.video.camera import RESOLUTION_1080P
from croom.video.service import VideoService


class TestVideoServiceAsService:
    """VideoService participates in the Service framework (spec 4.1 to 4.3)."""

    def test_is_a_service_named_video(self):
        service = VideoService()
        assert isinstance(service, Service)
        assert service.name == "video"
        assert service.state == ServiceState.STOPPED

    async def test_start_and_stop_without_camera(self):
        with patch("croom.video.service.get_cameras", return_value=[]):
            service = VideoService()
            await service.start()
            assert service._running is True
            assert service._camera is None
            assert service._capture_task is None
            await service.stop()
            assert service._running is False

    async def test_initialize_runs_once(self):
        with patch("croom.video.service.get_cameras", return_value=[]) as probe:
            service = VideoService()
            assert await service.initialize() is True
            assert await service.initialize() is True
            assert probe.call_count == 1

    async def test_invalid_resolution_falls_back_to_1080p(self):
        with patch("croom.video.service.get_cameras", return_value=[]):
            service = VideoService(config={"resolution": "not-a-resolution"})
            assert await service.initialize() is True
            assert service._resolution == RESOLUTION_1080P

    def test_from_config_maps_dataclass_to_service_keys(self):
        config = Config()
        config.video.device = "auto"
        config.video.resolution = "720p"
        config.video.framerate = 25
        service = VideoService.from_config(config)
        assert service.config == {
            "camera": "default",
            "resolution": "1280x720",
            "fps": 25,
        }

    @pytest.mark.parametrize(
        "configured, expected",
        [("4k", "3840x2160"), ("1080p", "1920x1080"), ("720p", "1280x720"),
         ("480p", "640x480"), ("1600x900", "1600x900"), ("1080P", "1920x1080")],
    )
    def test_from_config_translates_resolution_aliases(self, configured, expected):
        config = Config()
        config.video.resolution = configured
        assert VideoService.from_config(config).config["resolution"] == expected

    def test_from_config_keeps_explicit_camera(self):
        config = Config()
        config.video.device = "/dev/video2"
        assert VideoService.from_config(config).config["camera"] == "/dev/video2"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/video/test_service.py -q`
Expected: FAIL. `isinstance` assertion false, `AttributeError: 'VideoService' object has no attribute 'name'`, `AttributeError: type object 'VideoService' has no attribute 'from_config'`, `probe.call_count == 2`. (`test_invalid_resolution_falls_back_to_1080p` may already pass; that is fine.)

- [ ] **Step 3: Implement**

In `src/croom/video/service.py`:

1. Directly after the line `import numpy as np` add:

```python
from croom.core.config import Config, auto_to_default
from croom.core.service import Service
```

2. Directly after the line `logger = logging.getLogger(__name__)` add:

```python
# Config uses short names; VideoService and Resolution.from_string() want "WxH".
RESOLUTION_ALIASES = {
    "4k": "3840x2160",
    "1080p": "1920x1080",
    "720p": "1280x720",
    "480p": "640x480",
}
```

3. Change `class VideoService:` to `class VideoService(Service):`.

4. In `__init__`, replace the line `self.config = config or {}` with:

```python
        super().__init__("video")
        self.config = config or {}
        self._initialized = False
```

5. Directly after `__init__` (before `async def initialize`), add:

```python
    @classmethod
    def from_config(cls, config: Config) -> "VideoService":
        """Build the service from the agent's Config (spec section 4.2)."""
        video = config.video
        resolution = video.resolution.strip().lower()
        return cls(config={
            "camera": auto_to_default(video.device),
            "resolution": RESOLUTION_ALIASES.get(resolution, video.resolution),
            "fps": video.framerate,
        })
```

6. In `initialize()`, insert as the first statements of the method body (before `try:`):

```python
        if self._initialized:
            return True
```

and directly before the line `logger.info("Video service initialized")` insert:

```python
            self._initialized = True
```

7. In `start()`, directly after

```python
        if self._running:
            return
```

insert:

```python
        if not self._initialized and not await self.initialize():
            logger.warning("Video service running without a camera")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/video/test_service.py -q`
Expected: all pass.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -q --tb=no 2>&1 | tail -1`
Expected: at most 87 failed.

- [ ] **Step 6: Commit**

```bash
git add src/croom/video/service.py tests/unit/video/test_service.py
git commit -m "fix(video): make VideoService a Service with from_config and safe start"
```

---

### Task 4: `DisplayService` joins the service framework

**Files:**
- Modify: `src/croom/display/service.py` (imports; `class DisplayService`; its `__init__`; its `initialize`; its `start`)
- Test: `tests/unit/display/test_service.py`

**Interfaces:**
- Consumes: `Service`; `Config` (Task 1).
- Produces: `DisplayService(Service)` with `name == "display"`; `DisplayService.from_config(config: Config) -> DisplayService`. Task 7 calls `from_config`.

Note: `src/croom/display/service.py` also defines `DDCDisplayInfo` and `DDCController`; only the `DisplayService` class (starting around line 395) changes.

- [ ] **Step 1: Write the failing tests**

`tests/unit/display/test_service.py` already imports `MagicMock, patch, AsyncMock`, `pytest`, and `DisplayService`/`DisplayState` from `croom.display.service`. Add this import below the existing imports:

```python
from croom.core.config import Config
from croom.core.service import Service
```

Append to the end of the file:

```python
class TestDisplayServiceAsService:
    """DisplayService participates in the Service framework (spec 4.1 to 4.3)."""

    def test_is_a_service_named_display(self):
        service = DisplayService()
        assert isinstance(service, Service)
        assert service.name == "display"

    @pytest.mark.asyncio
    async def test_start_and_stop_without_display_control(self):
        service = DisplayService(config={"cec_enabled": False, "ddc_enabled": False})
        with patch.object(service, "_detect_displays", new=AsyncMock()):
            await service.start()
            assert service._running is True
            assert service._control_method is None
            await service.stop()
            assert service._running is False

    @pytest.mark.asyncio
    async def test_start_runs_initialize_once(self):
        service = DisplayService(config={"cec_enabled": False, "ddc_enabled": False})
        with patch.object(service, "_detect_displays", new=AsyncMock()) as detect:
            await service.start()
            await service.stop()
            await service.start()
            await service.stop()
        assert detect.await_count == 1

    def test_from_config_auto_enables_both_controllers(self):
        config = Config()
        config.display.backend = "auto"
        config.display.power_on_boot = False
        service = DisplayService.from_config(config)
        assert service.config == {
            "cec_enabled": True,
            "ddc_enabled": True,
            "auto_power_on": False,
            "auto_power_off": False,
        }

    @pytest.mark.parametrize(
        "backend, cec, ddc",
        [("hdmi_cec", True, False), ("ddc", False, True), ("none", False, False)],
    )
    def test_from_config_backend_selects_controllers(self, backend, cec, ddc):
        config = Config()
        config.display.backend = backend
        service = DisplayService.from_config(config)
        assert (service._cec_enabled, service._ddc_enabled) == (cec, ddc)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/display/test_service.py -q -k TestDisplayServiceAsService`
Expected: FAIL. `isinstance` assertion false, `AttributeError ... 'name'`, `AttributeError: type object 'DisplayService' has no attribute 'from_config'`, `detect.await_count == 2`.

- [ ] **Step 3: Implement**

In `src/croom/display/service.py`:

1. Directly after the line `from pathlib import Path` add:

```python
from croom.core.config import Config
from croom.core.service import Service
```

2. Change `class DisplayService:` to `class DisplayService(Service):`.

3. In `DisplayService.__init__`, replace the line `self.config = config or {}` with:

```python
        super().__init__("display")
        self.config = config or {}
        self._initialized = False
```

4. Directly after `DisplayService.__init__` (before its `async def initialize`), add:

```python
    @classmethod
    def from_config(cls, config: Config) -> "DisplayService":
        """Build the service from the agent's Config (spec section 4.2)."""
        backend = config.display.backend
        return cls(config={
            "cec_enabled": backend in ("auto", "hdmi_cec"),
            "ddc_enabled": backend in ("auto", "ddc"),
            "auto_power_on": config.display.power_on_boot,
            # display.power_off_shutdown describes shutdown behaviour this
            # service does not implement; inactivity power-off stays off.
            "auto_power_off": False,
        })
```

5. In `DisplayService.initialize()`, insert as the first statements of the method body (before `try:`):

```python
        if self._initialized:
            return True
```

and directly before the final `return True` inside the `try` block (the one that follows the `logger.info(f"Display service initialized (control: ...")` / `logger.warning("Display service initialized (no power control available)")` pair) insert:

```python
            self._initialized = True
```

6. In `DisplayService.start()`, directly after

```python
        if self._running:
            return
```

insert:

```python
        if not self._initialized and not await self.initialize():
            logger.warning("Display service running without display control")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/display/test_service.py -q`
Expected: the new tests pass; pre-existing tests keep their previous result.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -q --tb=no 2>&1 | tail -1`
Expected: at most 87 failed.

- [ ] **Step 6: Commit**

```bash
git add src/croom/display/service.py tests/unit/display/test_service.py
git commit -m "fix(display): make DisplayService a Service with from_config and safe start"
```

---

### Task 5: `CalendarService` joins the service framework

**Files:**
- Modify: `src/croom/calendar/service.py` (imports; `class CalendarService`; `__init__`; `initialize`; `start`)
- Test: `tests/unit/calendar/test_service.py`

**Interfaces:**
- Consumes: `Service`; `Config` (Task 1).
- Produces: `CalendarService(Service)` with `name == "calendar"`; `CalendarService.from_config(config: Config) -> CalendarService`; `initialize()` returns False without touching a provider when `config["provider"]` is falsy; `start()` polls only when `_initialized` is True. Task 7 calls `from_config`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/calendar/test_service.py` already imports `MagicMock, patch, AsyncMock`, `pytest`, and `CalendarService` from `croom.calendar.service`. Add below the existing imports:

```python
from croom.core.config import Config
from croom.core.service import Service
```

Append to the end of the file:

```python
class TestCalendarServiceAsService:
    """CalendarService participates in the Service framework (spec 4.1 to 4.3)."""

    def test_is_a_service_named_calendar(self):
        service = CalendarService()
        assert isinstance(service, Service)
        assert service.name == "calendar"

    @pytest.mark.asyncio
    async def test_initialize_without_provider_returns_false(self):
        service = CalendarService(config={"provider": None})
        assert await service.initialize() is False
        assert service._initialized is False
        assert service._provider is None

    @pytest.mark.asyncio
    async def test_start_without_credentials_runs_idle(self):
        service = CalendarService(config={"provider": "google", "credentials": {}})
        with patch(
            "croom.calendar.service.GoogleCalendarProvider.authenticate",
            new=AsyncMock(return_value=False),
        ), patch.object(service, "_fetch_events", new=AsyncMock()) as fetch:
            await service.start()
            assert service._running is True
            assert service._initialized is False
            assert service._poll_task is None
            fetch.assert_not_awaited()
            await service.stop()
        assert service._running is False

    @pytest.mark.asyncio
    async def test_start_polls_when_initialized(self):
        service = CalendarService(config={"provider": "google", "poll_interval": 60})
        service._initialized = True
        with patch.object(service, "_fetch_events", new=AsyncMock()) as fetch:
            await service.start()
            fetch.assert_awaited_once()
            assert service._poll_task is not None
            await service.stop()
        assert service._poll_task is None

    def test_from_config_google_with_service_account(self):
        config = Config()
        config.calendar.providers = ["google", "microsoft"]
        config.calendar.google_credentials_path = "/etc/croom/google-sa.json"
        config.calendar.sync_interval_seconds = 120
        config.meeting.join_early_minutes = 3
        service = CalendarService.from_config(config)
        assert service.config == {
            "provider": "google",
            "credentials": {"service_account_file": "/etc/croom/google-sa.json"},
            "poll_interval": 120,
            "auto_join_minutes": 3,
        }
        assert service._poll_interval == 120
        assert service._auto_join_minutes == 3

    def test_from_config_google_without_path_has_no_credentials(self):
        config = Config()
        config.calendar.providers = ["google"]
        assert CalendarService.from_config(config).config["credentials"] == {}

    def test_from_config_microsoft(self):
        config = Config()
        config.calendar.providers = ["microsoft"]
        config.calendar.microsoft_client_id = "client-123"
        config.calendar.microsoft_tenant_id = "tenant-abc"
        assert CalendarService.from_config(config).config["credentials"] == {
            "client_id": "client-123",
            "tenant_id": "tenant-abc",
        }

    def test_from_config_without_providers_is_idle(self):
        config = Config()
        config.calendar.providers = []
        assert CalendarService.from_config(config).config["provider"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/calendar/test_service.py -q -k TestCalendarServiceAsService`
Expected: FAIL. `isinstance` assertion false, `AttributeError ... 'name'`, `AttributeError: 'CalendarService' object has no attribute '_initialized'`, `AttributeError: type object 'CalendarService' has no attribute 'from_config'`.

- [ ] **Step 3: Implement**

In `src/croom/calendar/service.py`:

1. Directly after the line `from croom.calendar.providers.microsoft import MicrosoftCalendarProvider` add:

```python
from croom.core.config import Config
from croom.core.service import Service
```

2. Change `class CalendarService:` to `class CalendarService(Service):`.

3. In `__init__`, replace the line `self.config = config or {}` with:

```python
        super().__init__("calendar")
        self.config = config or {}
        self._initialized = False
```

4. Directly after `__init__` (before the `@property` block), add:

```python
    @classmethod
    def from_config(cls, config: Config) -> "CalendarService":
        """Build the service from the agent's Config (spec section 4.2)."""
        calendar = config.calendar
        provider = calendar.providers[0] if calendar.providers else None
        credentials: Dict[str, Any] = {}
        if provider == "google" and calendar.google_credentials_path:
            credentials = {"service_account_file": calendar.google_credentials_path}
        elif provider == "microsoft":
            credentials = {
                "client_id": calendar.microsoft_client_id,
                "tenant_id": calendar.microsoft_tenant_id,
            }
        return cls(config={
            "provider": provider,
            "credentials": credentials,
            "poll_interval": calendar.sync_interval_seconds,
            "auto_join_minutes": config.meeting.join_early_minutes,
        })
```

5. In `initialize()`, replace the line `provider_name = self.config.get('provider', 'google')` with:

```python
        if self._initialized:
            return True
        provider_name = self.config.get('provider', 'google')
        if not provider_name:
            logger.info("No calendar provider configured; calendar service idle")
            return False
```

and directly before the line `logger.info(f"Calendar service initialized with {provider_name}")` insert:

```python
            self._initialized = True
```

6. Replace the whole `start()` method with:

```python
    async def start(self) -> None:
        """Start polling the calendar. Runs idle when no provider could be initialized."""
        if self._running:
            return
        if not self._initialized and not await self.initialize():
            logger.warning("Calendar service running without a provider; polling disabled")
        self._running = True
        if not self._initialized:
            return
        await self._fetch_events()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(f"Calendar polling started (interval: {self._poll_interval}s)")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/calendar/test_service.py -q`
Expected: the new tests pass; pre-existing tests keep their previous result.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -q --tb=no 2>&1 | tail -1`
Expected: at most 87 failed.

- [ ] **Step 6: Commit**

```bash
git add src/croom/calendar/service.py tests/unit/calendar/test_service.py
git commit -m "fix(calendar): make CalendarService a Service with from_config and idle start"
```

---

### Task 6: Dashboard client on aiohttp with the backend's enrollment and WebSocket contract

**Files:**
- Rewrite: `src/croom/dashboard/client.py`
- Modify: `src/croom/dashboard/__init__.py`
- Create: `tests/unit/dashboard/test_client.py`

**Interfaces:**
- Consumes: `Service`; `Config.resolve_data_dir()` (Task 1); `PlatformInfo.device.value` and `Capabilities.to_dict()` from `croom.platform`.
- Produces: `DashboardClient(Service)` named `dashboard` with `from_config(config: Config, platform_info: PlatformInfo, capabilities: Capabilities) -> DashboardClient`; properties `connection_state`, `device_id`, `is_connected`; coroutines `connect() -> bool`, `disconnect()`, `send_status(status: str, message: Optional[str] = None) -> bool`, `send_metrics(metrics_type: str, data: Dict) -> bool`, `send_meeting_event(event: str, meeting_id: str, platform: str) -> bool`; callbacks `on_connected(cb)`, `on_disconnected(cb)`, `on_config_update(cb)`, `register_command(name, handler)`; module functions `websocket_url(dashboard_url: str) -> str`, `load_state(path) -> Optional[dict]`, `save_state(path, state)`, `clear_state(path)`; constants `MAX_HEARTBEAT_INTERVAL = 55.0`, `INITIAL_BACKOFF_SECONDS = 5.0`, `MAX_BACKOFF_SECONDS = 60.0`, `UNKNOWN_DEVICE_MESSAGE = "Unknown device"`. Task 7 calls `from_config`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/dashboard/test_client.py`:

```python
"""
Tests for croom.dashboard.client against an in-process fake dashboard.

The fake mirrors the Node backend's contract (src/croom-dashboard/backend):
POST /api/provisioning/enroll creates the device row and returns its id; the
WebSocket at /ws speaks {"type": ..., "payload": {...}} messages and answers
"auth" with "auth_success" for known devices or "auth_error" otherwise.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from croom import __version__
from croom.core.config import Config
from croom.core.service import Service
from croom.dashboard.client import (
    MAX_HEARTBEAT_INTERVAL,
    ConnectionState,
    DashboardClient,
    load_state,
    save_state,
    websocket_url,
)


class FakeDashboard:
    """Minimal stand-in for the dashboard backend."""

    def __init__(self, known_device_ids=(), enroll_status=200):
        self.known_devices = set(known_device_ids)
        self.enroll_status = enroll_status
        self.enroll_requests = []
        self.messages = []
        self.sockets = []
        self.device_counter = 0
        app = web.Application()
        app.router.add_post("/api/provisioning/enroll", self.enroll)
        app.router.add_get("/ws", self.websocket)
        self.server = TestServer(app)

    async def __aenter__(self):
        await self.server.start_server()
        return self

    async def __aexit__(self, *exc):
        await self.server.close()

    @property
    def url(self) -> str:
        return str(self.server.make_url("/")).rstrip("/")

    async def enroll(self, request):
        self.enroll_requests.append(await request.json())
        if self.enroll_status != 200:
            return web.json_response({"error": "Invalid enrollment token"}, status=self.enroll_status)
        self.device_counter += 1
        device_id = f"device-{self.device_counter}"
        self.known_devices.add(device_id)
        return web.json_response({
            "deviceId": device_id,
            "config": {"roomName": "Lab"},
            "websocketUrl": "ws://localhost:3001/ws",
            "message": "Device enrolled successfully",
        })

    async def websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        async for msg in ws:
            data = json.loads(msg.data)
            self.messages.append(data)
            if data["type"] == "auth":
                device_id = data["payload"].get("deviceId")
                if device_id in self.known_devices:
                    await ws.send_json({
                        "type": "auth_success",
                        "payload": {"deviceId": device_id, "config": {"roomName": "Lab"}},
                    })
                else:
                    await ws.send_json({"type": "auth_error", "payload": {"message": "Unknown device"}})
        return ws

    def count(self, msg_type: str) -> int:
        return sum(1 for m in self.messages if m["type"] == msg_type)

    async def wait_for(self, msg_type: str, count: int = 1, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.count(msg_type) >= count:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out waiting for {count} x {msg_type!r}; received {self.messages}")


def make_client(fake: FakeDashboard, tmp_path, **overrides) -> DashboardClient:
    config = {
        "url": fake.url,
        "enrollment_token": "tok-1",
        "room_name": "Lab",
        "heartbeat_interval": 0.05,
        "initial_backoff": 0.05,
        "max_backoff": 0.1,
        "state_file": str(tmp_path / "dashboard-state.json"),
        "device_info": {"name": "Lab", "platform": "pc", "softwareVersion": "2.0.0-dev", "capabilities": {}},
    }
    config.update(overrides)
    return DashboardClient(config=config)


class TestWebsocketUrl:
    @pytest.mark.parametrize(
        "dashboard_url, expected",
        [
            ("http://localhost:3001", "ws://localhost:3001/ws"),
            ("https://croom.example.com", "wss://croom.example.com/ws"),
            ("http://10.0.0.5:3001/", "ws://10.0.0.5:3001/ws"),
            ("https://croom.example.com/dashboard/", "wss://croom.example.com/dashboard/ws"),
        ],
    )
    def test_derives_websocket_endpoint(self, dashboard_url, expected):
        assert websocket_url(dashboard_url) == expected


class TestStateFile:
    def test_round_trip_creates_parents_and_restricts_permissions(self, tmp_path):
        path = tmp_path / "nested" / "state.json"
        state = {"device_id": "d1", "dashboard_url": "http://x", "enrolled_at": "now"}
        save_state(path, state)
        assert load_state(path) == state
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_missing_file_is_none(self, tmp_path):
        assert load_state(tmp_path / "missing.json") is None

    def test_corrupt_file_is_ignored(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json")
        assert load_state(path) is None

    def test_non_object_file_is_ignored(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]")
        assert load_state(path) is None


class TestDashboardClientService:
    def test_is_a_service_named_dashboard(self):
        client = DashboardClient(config={"url": "http://localhost:3001"})
        assert isinstance(client, Service)
        assert client.name == "dashboard"
        assert client.connection_state == ConnectionState.DISCONNECTED
        assert client.device_id is None

    def test_heartbeat_interval_is_clamped_to_backend_timeout(self):
        client = DashboardClient(config={"url": "http://localhost:3001", "heartbeat_interval": 120})
        assert client._heartbeat_interval == MAX_HEARTBEAT_INTERVAL

    def test_short_heartbeat_interval_is_kept(self):
        client = DashboardClient(config={"url": "http://localhost:3001", "heartbeat_interval": 10})
        assert client._heartbeat_interval == 10

    async def test_send_while_disconnected_returns_false(self):
        client = DashboardClient(config={"url": "http://localhost:3001"})
        assert await client.send_status("online") is False
        assert await client.send_metrics("system", {"cpu": 1}) is False
        assert await client.send_meeting_event("joined", "abc-defg-hij", "google_meet") is False

    def test_from_config_builds_client_settings(self, tmp_path):
        config = Config()
        config.data_dir = str(tmp_path)
        config.room.name = "Board Room"
        config.dashboard.url = "http://dash.local:3001/"
        config.dashboard.enrollment_token = "tok-9"
        config.dashboard.heartbeat_interval_seconds = 15
        platform_info = SimpleNamespace(device=SimpleNamespace(value="rpi5"))
        capabilities = MagicMock()
        capabilities.to_dict.return_value = {"has_ai": False}

        client = DashboardClient.from_config(config, platform_info, capabilities)

        assert client.config == {
            "url": "http://dash.local:3001/",
            "enrollment_token": "tok-9",
            "room_name": "Board Room",
            "heartbeat_interval": 15,
            "state_file": str(tmp_path / "dashboard-state.json"),
            "device_info": {
                "name": "Board Room",
                "platform": "rpi5",
                "softwareVersion": __version__,
                "capabilities": {"has_ai": False},
            },
        }
        assert client._url == "http://dash.local:3001"


class TestDashboardClientProtocol:
    async def test_enrolls_authenticates_reports_status_and_heartbeats(self, tmp_path):
        async with FakeDashboard() as fake:
            client = make_client(fake, tmp_path)
            configs = []
            client.on_config_update(configs.append)
            await client.start()
            await fake.wait_for("heartbeat")

            assert fake.enroll_requests == [{
                "token": "tok-1",
                "deviceInfo": {"name": "Lab", "platform": "pc", "softwareVersion": "2.0.0-dev", "capabilities": {}},
            }]
            assert [m["type"] for m in fake.messages[:3]] == ["auth", "status", "heartbeat"]
            assert fake.messages[0]["payload"] == {"deviceId": "device-1"}
            assert fake.messages[1]["payload"]["status"] == "online"
            assert fake.messages[1]["payload"]["deviceId"] == "device-1"
            assert fake.messages[2]["payload"] == {"deviceId": "device-1"}
            assert client.is_connected
            assert client.connection_state == ConnectionState.CONNECTED
            assert client.device_id == "device-1"
            assert configs == [{"roomName": "Lab"}]
            state = json.loads((tmp_path / "dashboard-state.json").read_text())
            assert state["device_id"] == "device-1"
            assert state["dashboard_url"] == fake.url
            assert "enrolled_at" in state

            await client.stop()
            await fake.wait_for("status", count=2)
            assert fake.messages[-1] == {"type": "status", "payload": {"deviceId": "device-1", "status": "offline"}}
            assert not client.is_connected
            assert client.connection_state == ConnectionState.DISCONNECTED

    async def test_reuses_saved_enrollment(self, tmp_path):
        async with FakeDashboard(known_device_ids=["device-9"]) as fake:
            save_state(tmp_path / "dashboard-state.json",
                       {"device_id": "device-9", "dashboard_url": fake.url, "enrolled_at": "earlier"})
            client = make_client(fake, tmp_path)
            await client.start()
            await fake.wait_for("status")
            assert fake.enroll_requests == []
            assert fake.messages[0] == {"type": "auth", "payload": {"deviceId": "device-9"}}
            await client.stop()

    async def test_ignores_state_from_another_dashboard(self, tmp_path):
        async with FakeDashboard(known_device_ids=["device-9"]) as fake:
            save_state(tmp_path / "dashboard-state.json",
                       {"device_id": "device-9", "dashboard_url": "http://other-dashboard:3001", "enrolled_at": "earlier"})
            client = make_client(fake, tmp_path)
            await client.start()
            await fake.wait_for("status")
            assert len(fake.enroll_requests) == 1
            assert fake.messages[0]["payload"] == {"deviceId": "device-1"}
            await client.stop()

    async def test_unknown_device_clears_state_and_reenrolls(self, tmp_path):
        async with FakeDashboard() as fake:
            save_state(tmp_path / "dashboard-state.json",
                       {"device_id": "stale-id", "dashboard_url": fake.url, "enrolled_at": "earlier"})
            client = make_client(fake, tmp_path)
            await client.start()
            await fake.wait_for("status")
            auths = [m["payload"]["deviceId"] for m in fake.messages if m["type"] == "auth"]
            assert auths == ["stale-id", "device-1"]
            assert len(fake.enroll_requests) == 1
            assert load_state(tmp_path / "dashboard-state.json")["device_id"] == "device-1"
            await client.stop()

    async def test_no_token_and_not_enrolled_stays_idle(self, tmp_path, caplog):
        async with FakeDashboard() as fake:
            client = make_client(fake, tmp_path, enrollment_token="")
            await client.start()
            await asyncio.sleep(0.2)
            assert fake.enroll_requests == []
            assert fake.messages == []
            assert client.connection_state == ConnectionState.DISCONNECTED
            assert "no enrollment_token" in caplog.text
            await client.stop()

    async def test_enrollment_rejection_retries_until_it_succeeds(self, tmp_path):
        async with FakeDashboard(enroll_status=401) as fake:
            client = make_client(fake, tmp_path)
            await client.start()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5
            while len(fake.enroll_requests) < 2 and loop.time() < deadline:
                await asyncio.sleep(0.02)
            assert len(fake.enroll_requests) >= 2
            assert fake.messages == []
            fake.enroll_status = 200
            await fake.wait_for("status")
            assert client.is_connected
            await client.stop()

    async def test_reconnects_after_server_closes_without_reenrolling(self, tmp_path):
        async with FakeDashboard() as fake:
            client = make_client(fake, tmp_path)
            await client.start()
            await fake.wait_for("status")
            await fake.sockets[0].close()
            await fake.wait_for("status", count=2)
            auths = [m["payload"]["deviceId"] for m in fake.messages if m["type"] == "auth"]
            assert auths == ["device-1", "device-1"]
            assert len(fake.enroll_requests) == 1
            assert client.is_connected
            await client.stop()

    async def test_malformed_message_does_not_break_the_session(self, tmp_path):
        async with FakeDashboard() as fake:
            client = make_client(fake, tmp_path)
            await client.start()
            await fake.wait_for("status")
            before = fake.count("heartbeat")
            await fake.sockets[-1].send_str("this is not json")
            await fake.sockets[-1].send_json(["not", "an", "object"])
            await fake.sockets[-1].send_json({"type": "something_new", "payload": {}})
            await fake.wait_for("heartbeat", count=before + 2)
            assert client.is_connected
            await client.stop()

    async def test_registered_command_handler_is_called(self, tmp_path):
        async with FakeDashboard() as fake:
            client = make_client(fake, tmp_path)
            seen = []

            async def restart(params):
                seen.append(params)

            client.register_command("restart", restart)
            await client.start()
            await fake.wait_for("status")
            await fake.sockets[-1].send_json({"type": "command", "payload": {"command": "restart", "params": {"delay": 5}}})
            deadline = asyncio.get_running_loop().time() + 2
            while not seen and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            assert seen == [{"delay": 5}]
            await client.stop()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/dashboard/test_client.py -q`
Expected: FAIL at import time with `ImportError: cannot import name 'MAX_HEARTBEAT_INTERVAL' from 'croom.dashboard.client'` (the old module lacks every new name).

- [ ] **Step 3: Replace `src/croom/dashboard/client.py` with the new implementation**

Write the whole file:

```python
"""
Dashboard client for Croom devices.

Enrolls the device with the management dashboard over REST, then keeps a
WebSocket session open for authentication, heartbeats and status updates.

Protocol: docs/superpowers/specs/2026-09-24-agent-startup-and-dashboard-enrollment-design.md,
section 4.4. Every WebSocket message is {"type": <string>, "payload": {...}}.
"""

import asyncio
import inspect
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from croom.core.config import Config
from croom.core.service import Service
from croom.platform.capabilities import Capabilities
from croom.platform.detector import PlatformInfo

logger = logging.getLogger(__name__)

# The backend (src/croom-dashboard/backend/src/websocket/server.ts) drops a
# device after 60 seconds without a heartbeat.
MAX_HEARTBEAT_INTERVAL = 55.0
INITIAL_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 60.0
UNKNOWN_DEVICE_MESSAGE = "Unknown device"
ENROLL_PATH = "/api/provisioning/enroll"
WEBSOCKET_PATH = "/ws"


def _version() -> str:
    # Imported lazily: croom/__init__.py imports the agent, which imports this
    # module while the package is still initializing.
    from croom import __version__
    return __version__


class ConnectionState(Enum):
    """WebSocket connection state."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


class MessageType(Enum):
    """Dashboard message types."""
    # Device -> Dashboard
    AUTH = "auth"
    HEARTBEAT = "heartbeat"
    STATUS = "status"
    METRICS = "metrics"
    MEETING_EVENT = "meeting_event"
    # Dashboard -> Device
    AUTH_SUCCESS = "auth_success"
    AUTH_ERROR = "auth_error"
    ERROR = "error"
    COMMAND = "command"


def websocket_url(dashboard_url: str) -> str:
    """Derive the WebSocket endpoint from the dashboard's base HTTP(S) URL."""
    parts = urlsplit(dashboard_url.strip())
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/") + WEBSOCKET_PATH
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def load_state(path: Path) -> Optional[Dict[str, Any]]:
    """Read the enrollment state file; None when missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"Ignoring unreadable dashboard state file {path}: {e}")
        return None
    return data if isinstance(data, dict) else None


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """Write the enrollment state file atomically with mode 0600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def clear_state(path: Path) -> None:
    """Delete the enrollment state file if it exists."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


class DashboardClient(Service):
    """
    Dashboard connection service ("dashboard" under the ServiceManager).

    Handles enrollment, WebSocket authentication, heartbeats, status updates
    and reconnection with capped exponential backoff.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Args:
            config: Client configuration with:
                - url: Base HTTP(S) URL of the dashboard backend (default http://localhost:3001)
                - enrollment_token: One-time token from the dashboard's Provisioning page
                - room_name: Room display name (default "Conference Room")
                - device_info: Dict sent as deviceInfo when enrolling
                - heartbeat_interval: Seconds between heartbeats (default 30, clamped to 55)
                - state_file: Path of the enrollment state file (default ./dashboard-state.json)
                - initial_backoff / max_backoff: Reconnect delays in seconds (default 5 / 60)
        """
        super().__init__("dashboard")
        self.config = config or {}
        self._url = str(self.config.get("url", "http://localhost:3001")).strip().rstrip("/")
        self._enrollment_token = self.config.get("enrollment_token") or ""
        self._room_name = self.config.get("room_name", "Conference Room")
        self._device_info: Dict[str, Any] = self.config.get("device_info") or {"name": self._room_name}
        self._heartbeat_interval = self._clamp_heartbeat(self.config.get("heartbeat_interval", 30))
        self._state_file = Path(self.config.get("state_file", "dashboard-state.json"))
        self._initial_backoff = float(self.config.get("initial_backoff", INITIAL_BACKOFF_SECONDS))
        self._max_backoff = float(self.config.get("max_backoff", MAX_BACKOFF_SECONDS))

        self._device_id: Optional[str] = None
        self._connection_state = ConnectionState.DISCONNECTED
        self._running = False
        self._warned_no_token = False
        self._backoff = self._initial_backoff

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._connection_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        self._command_handlers: Dict[str, Callable] = {}
        self._on_connected: List[Callable] = []
        self._on_disconnected: List[Callable] = []
        self._on_config_update: List[Callable[[Dict], None]] = []

    @staticmethod
    def _clamp_heartbeat(value: Any) -> float:
        interval = float(value)
        if interval > MAX_HEARTBEAT_INTERVAL:
            logger.warning(
                f"heartbeat_interval {interval:.0f}s exceeds the dashboard timeout; "
                f"using {MAX_HEARTBEAT_INTERVAL:.0f}s"
            )
            return MAX_HEARTBEAT_INTERVAL
        return interval

    @classmethod
    def from_config(
        cls,
        config: Config,
        platform_info: PlatformInfo,
        capabilities: Capabilities,
    ) -> "DashboardClient":
        """Build the client from the agent's Config plus detected platform and capabilities."""
        return cls(config={
            "url": config.dashboard.url,
            "enrollment_token": config.dashboard.enrollment_token,
            "room_name": config.room.name,
            "heartbeat_interval": config.dashboard.heartbeat_interval_seconds,
            "state_file": str(config.resolve_data_dir() / "dashboard-state.json"),
            "device_info": {
                "name": config.room.name,
                "platform": platform_info.device.value,
                "softwareVersion": _version(),
                "capabilities": capabilities.to_dict(),
            },
        })

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connection_state(self) -> ConnectionState:
        """WebSocket connection state (Service.state is the lifecycle state)."""
        return self._connection_state

    @property
    def device_id(self) -> Optional[str]:
        """Device id assigned by the dashboard, or None before enrollment."""
        return self._device_id

    @property
    def is_connected(self) -> bool:
        return (
            self._connection_state == ConnectionState.CONNECTED
            and self._ws is not None
            and not self._ws.closed
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self.connect()

    async def stop(self) -> None:
        await self.disconnect()

    async def connect(self) -> bool:
        """Start the background connection loop and return immediately."""
        if self._running:
            return self.is_connected
        self._running = True
        self._backoff = self._initial_backoff
        self._session = aiohttp.ClientSession()
        self._connection_task = asyncio.create_task(self._connection_loop())
        return self.is_connected

    async def disconnect(self) -> None:
        """Report offline, stop the loops and close the socket and session."""
        if not self._running:
            return
        self._running = False
        await self._cancel(self._heartbeat_task)
        self._heartbeat_task = None
        if self.is_connected:
            await self.send_status("offline")
        await self._cancel(self._connection_task)
        self._connection_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._connection_state = ConnectionState.DISCONNECTED

    @staticmethod
    async def _cancel(task: Optional[asyncio.Task]) -> None:
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        while self._running:
            try:
                if not await self._ensure_enrolled():
                    if not self._enrollment_token:
                        return  # nothing to do until a token is configured
                    raise ConnectionError("enrollment failed")
                self._connection_state = ConnectionState.CONNECTING
                await self._run_session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Dashboard connection error: {e}")
            if not self._running:
                break
            self._connection_state = ConnectionState.RECONNECTING
            logger.info(f"Retrying dashboard connection in {self._backoff:.0f}s")
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, self._max_backoff)
        self._connection_state = ConnectionState.DISCONNECTED

    async def _ensure_enrolled(self) -> bool:
        """Load or obtain a device id. False when enrollment is not possible."""
        if self._device_id:
            return True
        state = load_state(self._state_file)
        if state and state.get("device_id") and state.get("dashboard_url") == self._url:
            self._device_id = state["device_id"]
            logger.info(f"Using enrolled device id {self._device_id}")
            return True
        if not self._enrollment_token:
            if not self._warned_no_token:
                logger.warning(
                    "Dashboard enabled but the device is not enrolled and no enrollment_token is configured"
                )
                self._warned_no_token = True
            return False
        return await self._enroll()

    async def _enroll(self) -> bool:
        url = f"{self._url}{ENROLL_PATH}"
        payload = {"token": self._enrollment_token, "deviceInfo": self._device_info}
        logger.info(f"Enrolling with dashboard at {url}")
        try:
            async with self._session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    logger.error(f"Enrollment rejected by dashboard ({response.status}): {body}")
                    return False
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.error(f"Enrollment request failed: {e}")
            return False
        device_id = data.get("deviceId") if isinstance(data, dict) else None
        if not device_id:
            logger.error("Enrollment response did not include a deviceId")
            return False
        reported = data.get("websocketUrl")
        if reported and reported != websocket_url(self._url):
            logger.info(
                f"Dashboard reports WebSocket URL {reported}; "
                f"using {websocket_url(self._url)} derived from dashboard.url"
            )
        self._device_id = device_id
        save_state(self._state_file, {
            "device_id": device_id,
            "dashboard_url": self._url,
            "enrolled_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info(f"Enrolled with dashboard as device {device_id}")
        return True

    async def _run_session(self) -> None:
        """Hold one WebSocket session until it closes."""
        ws_url = websocket_url(self._url)
        logger.info(f"Connecting to dashboard: {ws_url}")
        async with self._session.ws_connect(ws_url, heartbeat=20) as ws:
            self._ws = ws
            try:
                await self._send(MessageType.AUTH, {"deviceId": self._device_id})
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        if not await self._handle_text(message.data):
                            break
                    elif message.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                was_connected = self._connection_state == ConnectionState.CONNECTED
                self._ws = None
                await self._cancel(self._heartbeat_task)
                self._heartbeat_task = None
                self._connection_state = ConnectionState.DISCONNECTED
                if was_connected:
                    logger.warning("Dashboard connection closed")
                    self._fire(self._on_disconnected)

    # ------------------------------------------------------------------
    # Incoming messages
    # ------------------------------------------------------------------

    async def _handle_text(self, raw: str) -> bool:
        """Handle one text frame. Returns False when the session should end."""
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning(f"Ignoring malformed dashboard message: {raw[:100]!r}")
            return True
        if not isinstance(data, dict):
            logger.warning("Ignoring non-object dashboard message")
            return True
        msg_type = data.get("type", "")
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        if msg_type == MessageType.AUTH_SUCCESS.value:
            await self._on_auth_success(payload)
        elif msg_type == MessageType.AUTH_ERROR.value:
            self._on_auth_error(str(payload.get("message", "")))
            return False
        elif msg_type == MessageType.ERROR.value:
            logger.error(f"Dashboard error: {payload.get('message', '')}")
        elif msg_type == MessageType.COMMAND.value:
            await self._handle_command(payload)
        else:
            logger.debug(f"Ignoring dashboard message type {msg_type!r}")
        return True

    async def _on_auth_success(self, payload: Dict[str, Any]) -> None:
        self._backoff = self._initial_backoff
        self._connection_state = ConnectionState.CONNECTED
        logger.info(f"Authenticated with dashboard as device {self._device_id}")
        self._fire(self._on_connected)
        config = payload.get("config")
        if isinstance(config, dict):
            self._fire(self._on_config_update, config)
        await self.send_status(
            "online", f"croom {_version()} on {self._device_info.get('platform', 'unknown')}"
        )
        await self._cancel(self._heartbeat_task)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def _on_auth_error(self, message: str) -> None:
        if message == UNKNOWN_DEVICE_MESSAGE:
            logger.warning(
                "Dashboard does not know this device; clearing enrollment state "
                "(a new enrollment token is needed if the old one was already used)"
            )
            self._device_id = None
            clear_state(self._state_file)
        else:
            logger.error(f"Dashboard authentication failed: {message}")

    async def _handle_command(self, payload: Dict[str, Any]) -> None:
        command = str(payload.get("command", ""))
        handler = self._command_handlers.get(command)
        if handler is None:
            logger.warning(f"No handler for dashboard command {command!r}")
            return
        try:
            result = handler(payload.get("params") or {})
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.error(f"Dashboard command {command!r} failed: {e}")

    def _fire(self, callbacks: List[Callable], *args: Any) -> None:
        for callback in list(callbacks):
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"Dashboard callback error: {e}")

    async def _heartbeat_loop(self) -> None:
        try:
            while self._running and self.is_connected:
                await asyncio.sleep(self._heartbeat_interval)
                if not self.is_connected:
                    break
                await self._send(MessageType.HEARTBEAT, {"deviceId": self._device_id})
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Outgoing messages
    # ------------------------------------------------------------------

    async def _send(self, msg_type: MessageType, payload: Dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None or ws.closed:
            logger.debug(f"Not connected to dashboard; dropping {msg_type.value}")
            return False
        try:
            await ws.send_str(json.dumps({"type": msg_type.value, "payload": payload}))
            return True
        except Exception as e:
            logger.error(f"Failed to send {msg_type.value} to dashboard: {e}")
            return False

    async def send_status(self, status: str, message: Optional[str] = None) -> bool:
        """Send a status update: status is 'online', 'offline' or 'error'."""
        payload: Dict[str, Any] = {"deviceId": self._device_id, "status": status}
        if message:
            payload["message"] = message
        return await self._send(MessageType.STATUS, payload)

    async def send_metrics(self, metrics_type: str, data: Dict[str, Any]) -> bool:
        """Send one metrics record of the given type."""
        return await self._send(
            MessageType.METRICS, {"deviceId": self._device_id, "type": metrics_type, "data": data}
        )

    async def send_meeting_event(self, event: str, meeting_id: str, platform: str) -> bool:
        """Send a meeting event: event is 'joined' or 'left'."""
        return await self._send(
            MessageType.MEETING_EVENT,
            {"deviceId": self._device_id, "event": event, "meetingId": meeting_id, "platform": platform},
        )

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def register_command(self, command: str, handler: Callable) -> None:
        """Register a handler for a dashboard 'command' message (sync or async)."""
        self._command_handlers[command] = handler
        logger.debug(f"Registered command handler: {command}")

    def on_connected(self, callback: Callable) -> None:
        """Register a callback for a successful authentication."""
        self._on_connected.append(callback)

    def on_disconnected(self, callback: Callable) -> None:
        """Register a callback for a lost connection."""
        self._on_disconnected.append(callback)

    def on_config_update(self, callback: Callable[[Dict], None]) -> None:
        """Register a callback for the config delivered with auth_success."""
        self._on_config_update.append(callback)
```

- [ ] **Step 4: Update the package export**

In `src/croom/dashboard/__init__.py`, change the line

```python
from croom.dashboard.client import DashboardClient, create_dashboard_client
```

to

```python
from croom.dashboard.client import DashboardClient
```

and delete the line `    "create_dashboard_client",` from `__all__`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/dashboard/test_client.py -q`
Expected: all pass. If a protocol test times out, the assertion message lists every message the fake received, which shows exactly how far the client got.

- [ ] **Step 6: Confirm no module imports websockets**

Run: `grep -rn "websockets" src/croom || echo "clean"`
Expected: `clean`.

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -q --tb=no 2>&1 | tail -1`
Expected: at most 87 failed.

- [ ] **Step 8: Commit**

```bash
git add src/croom/dashboard/client.py src/croom/dashboard/__init__.py tests/unit/dashboard/test_client.py
git commit -m "feat(dashboard): rewrite client on aiohttp with token enrollment and backend protocol"
```

---

### Task 7: Agent wiring, AI regression test, and the end-to-end acceptance run

**Files:**
- Modify: `src/croom/core/agent.py` (`_initialize_services`)
- Create: `tests/unit/core/test_agent.py`
- Create: `tests/unit/ai/test_service.py`

**Interfaces:**
- Consumes: `AudioService.from_config`, `VideoService.from_config`, `DisplayService.from_config`, `CalendarService.from_config` (Tasks 2 to 5); `DashboardClient.from_config(config, platform_info, capabilities)` (Task 6); `ServiceManager.get_all_services() -> Dict[str, Service]`; `ServiceManager.start_all() -> bool`; `ServiceManager.stop_all()`.
- Produces: an agent that starts on a machine without hardware. No new interfaces.

- [ ] **Step 1: Write the failing agent tests**

Create `tests/unit/core/test_agent.py`:

```python
"""
Tests for croom.core.agent: service registration and startup without hardware.
"""

from unittest.mock import AsyncMock, patch

import pytest

from croom.core.agent import CroomAgent
from croom.core.config import Config
from croom.core.service import ServiceState


def make_config(tmp_path, dashboard_url: str = "") -> Config:
    config = Config()
    config.ai.enabled = False
    config.dashboard.enabled = bool(dashboard_url)
    config.dashboard.url = dashboard_url
    config.data_dir = str(tmp_path)
    return config


@pytest.fixture
def no_hardware():
    """Make every hardware probe find nothing and keep meeting providers from launching browsers."""
    with patch("croom.audio.service.get_audio_devices", return_value=[]), \
         patch("croom.video.service.get_cameras", return_value=[]), \
         patch("croom.display.service.DisplayService.initialize", new=AsyncMock(return_value=False)), \
         patch("croom.calendar.service.CalendarService.initialize", new=AsyncMock(return_value=False)), \
         patch("croom.meeting.service.get_provider", return_value=None):
        yield


def make_agent(config: Config) -> CroomAgent:
    with patch("croom.core.agent.load_config", return_value=config):
        return CroomAgent()


class TestServiceRegistration:
    def test_registers_every_optional_service_without_crashing(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path))
        agent._initialize_services()
        assert sorted(agent.service_manager.get_all_services()) == [
            "audio", "calendar", "display", "meeting", "video",
        ]

    def test_registers_dashboard_client_when_enabled(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, dashboard_url="http://localhost:3001"))
        agent._initialize_services()
        dashboard = agent.service_manager.get_service("dashboard")
        assert dashboard is not None
        assert dashboard.config["url"] == "http://localhost:3001"
        assert dashboard.config["state_file"] == str(tmp_path / "dashboard-state.json")
        assert dashboard.config["device_info"]["name"] == agent.config.room.name


class TestStartup:
    async def test_start_all_runs_without_hardware(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path))
        agent._initialize_services()
        try:
            assert await agent.service_manager.start_all() is True
            states = {name: svc.state for name, svc in agent.service_manager.get_all_services().items()}
            assert all(state == ServiceState.RUNNING for state in states.values()), states
        finally:
            await agent.service_manager.stop_all()
        assert all(
            svc.state == ServiceState.STOPPED
            for svc in agent.service_manager.get_all_services().values()
        )
```

- [ ] **Step 2: Write the AI regression test**

Create `tests/unit/ai/test_service.py`:

```python
"""
Tests for croom.ai.service: startup without model files (spec 4.3 regression).
"""

import logging

from croom.ai.service import AIService
from croom.core.config import Config
from croom.platform.capabilities import CapabilityDetector


async def test_start_without_model_files_logs_and_continues(tmp_path, monkeypatch, caplog):
    # Model paths are relative (models/yolov8n.onnx); none exist in an empty directory.
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="croom.ai.service")
    service = AIService(Config(), CapabilityDetector.detect())

    await service.start()

    assert service._backend is not None
    assert service._loaded_models == {}
    assert "Model not found" in caplog.text
    await service.stop()
```

- [ ] **Step 3: Run the new tests to verify the expected failures**

Run: `.venv/bin/pytest tests/unit/core/test_agent.py tests/unit/ai/test_service.py -q`
Expected: the three agent tests FAIL with `TypeError` from `VideoService(self.config, self.capabilities)` (the old two-argument call) or `AttributeError` from the old dashboard registration; the AI regression test PASSES already (it pins current behaviour).

- [ ] **Step 4: Rewire `_initialize_services`**

In `src/croom/core/agent.py`, inside `_initialize_services`, make these five replacements. Leave the AI and meeting registrations exactly as they are.

Replace

```python
            audio_service = AudioService(self.config)
```

with

```python
            audio_service = AudioService.from_config(self.config)
```

Replace

```python
            video_service = VideoService(self.config, self.capabilities)
```

with

```python
            video_service = VideoService.from_config(self.config)
```

Replace

```python
            display_service = DisplayService(self.config, self.capabilities)
```

with

```python
            display_service = DisplayService.from_config(self.config)
```

Replace

```python
            calendar_service = CalendarService(self.config)
```

with

```python
            calendar_service = CalendarService.from_config(self.config)
```

Replace

```python
                dashboard_client = DashboardClient(self.config, self.capabilities)
```

with

```python
                dashboard_client = DashboardClient.from_config(
                    self.config, self.platform_info, self.capabilities
                )
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/core/test_agent.py tests/unit/ai/test_service.py -q`
Expected: all pass.

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/pytest -q --tb=no 2>&1 | tail -1`
Expected: at most 87 failed.

- [ ] **Step 7: Commit**

```bash
git add src/croom/core/agent.py tests/unit/core/test_agent.py tests/unit/ai/test_service.py
git commit -m "fix(agent): build services through from_config so the agent starts (fixes #11)"
```

- [ ] **Step 8: Acceptance run against the real dashboard**

Preconditions on this machine: the Postgres container `croom_postgres` is running, the backend dev server is on `localhost:3001` and the frontend on `localhost:3000` (see the memory notes or start them with `npm run dev` in `src/croom-dashboard/backend` and `src/croom-dashboard/frontend`). The admin password is in `~/.config/croom/dashboard-admin-password.txt`. The shell must have the WSLg display available (`echo $DISPLAY` prints `:0`), because the unchanged Google Meet provider opens a visible Chromium window on the Windows desktop while the agent runs; that provider is out of scope here.

Run from the repo root:

```bash
ADMIN_PASSWORD="$(cat ~/.config/croom/dashboard-admin-password.txt)"
TOKEN=$(curl -s -X POST http://localhost:3001/api/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"admin@croom.local\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
ENROLL=$(curl -s -X POST http://localhost:3001/api/provisioning/token \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"roomName":"WSL Lab","location":"developer desk"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
mkdir -p ~/.config/croom
cat > ~/.config/croom/config.yaml <<EOF
version: 2
room:
  name: "WSL Lab"
meeting:
  platforms: [google_meet]
ai:
  enabled: false
dashboard:
  enabled: true
  url: "http://localhost:3001"
  enrollment_token: "$ENROLL"
  heartbeat_interval_seconds: 10
EOF
rm -f ~/.local/share/croom/dashboard-state.json
.venv/bin/croom -v > /tmp/croom-acceptance.log 2>&1 &
AGENT=$!
sleep 25
echo "== while running (expect status online, lastSeen within the last 10s) =="
curl -s http://localhost:3001/api/devices -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
kill -INT $AGENT; wait $AGENT
sleep 2
echo "== after Ctrl-C (expect status offline) =="
curl -s http://localhost:3001/api/devices -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
echo "== agent log =="
grep -E "Enrolled|Authenticated|Croom Agent started|Service started|running without|ERROR" /tmp/croom-acceptance.log
```

Expected:
- The log contains `Croom Agent started successfully`, `Enrolled with dashboard as device <uuid>`, `Authenticated with dashboard as device <uuid>`, and warnings such as `Audio service running without devices`, `Video service running without a camera`, `Calendar service running without a provider`.
- The first `/api/devices` output shows the `WSL Lab` device with `"status": "online"`, `"platform": "pc"`, `"softwareVersion": "2.0.0-dev"` and a recent `lastSeen`.
- The second output shows `"status": "offline"`.
- Open http://localhost:3000/devices in a browser to see the same row.

- [ ] **Step 9: Push the branch**

```bash
git push origin dev
```
