# Agent startup and dashboard enrollment: design

Date: 2026-09-24. Branch: `dev` on the ben-abeo/croom.to fork. Status: approved in discussion, spec under review.

## 1. Goal

Make the Croom agent start on a machine without conference-room hardware and register itself with the management dashboard, so the device/dashboard loop can be tested on a developer machine (WSL2) before deploying to a Raspberry Pi.

Success criteria:

1. `croom -c config.yaml -v` on an x86_64 Linux machine with no camera, microphone, or TV starts every registered service and stays running until SIGINT.
2. With `dashboard.url` and a valid `dashboard.enrollment_token` configured, the device appears in the dashboard's Devices list as Online, stays Online through heartbeats, and becomes Offline when the agent stops.
3. New pytest coverage exercises the service wiring and the enroll, auth, heartbeat, and status sequence without a Node backend.
4. Tests that pass on `main` today still pass.
5. The Raspberry Pi install path keeps working unchanged (systemd unit, `/etc/croom/config.yaml`, `/var/lib/croom`).

## 2. Background: what is broken on `main`

- Upstream issue #11. `CroomAgent._initialize_services()` registers `AudioService`, `VideoService`, `DisplayService`, `CalendarService`, and `DashboardClient` through `ServiceManager.register()`, which reads `service.name`. Only `AIService` and `MeetingService` subclass `croom.core.service.Service`; the other five are plain classes, so registration raises `AttributeError` and the agent exits. Reproduced on x86_64 WSL2 and reported upstream on Pi 4 with Bookworm and Trixie.
- The agent passes a `Config` dataclass to constructors that read a plain dict with different key names (for example `VideoService` reads `camera` and `fps` while `VideoConfig` has `device` and `framerate`).
- `ServiceManager.start_all()` aborts the agent if any `start()` raises, so every optional service must contain its own failures. `AIService.start()` already does: with no model files (the installer ships none) it logs the missing models and keeps running, verified on this machine. The other optional services get the same treatment in this change.
- `DashboardClient` speaks a protocol the backend does not implement. It sends `{"type": "register", "device_id": ...}` while `src/croom-dashboard/backend/src/websocket/server.ts` expects `{"type": "auth", "payload": {"deviceId": ...}}` for a device row that was created earlier by `POST /api/provisioning/enroll`. `croom/provisioning/enrollment.py` posts to `/api/devices/enroll`, which does not exist. The client also imports `websockets`, which is neither declared in `pyproject.toml` nor installed, and passes the `extra_headers` keyword that websockets 14 removed.

## 3. Scope

In scope: everything in section 4.

Out of scope: AI model files or downloads, camera passthrough on WSL2, meeting-join behaviour, dashboard-to-device commands (the backend never sends any today), periodic metrics, the Qt touch UI, the installer script, and the 87 pre-existing test failures unrelated to this work.

## 4. Design

### 4.1 Service integration

Each of the five classes subclasses `Service` and passes its name to `super().__init__()`.

| Class | Service name | `start()` | `stop()` |
|---|---|---|---|
| `AudioService` | `audio` | run `initialize()` if it has not run, then the existing start logic | unchanged |
| `VideoService` | `video` | same | unchanged |
| `CalendarService` | `calendar` | run `initialize()` if it has not run; if it returns False or no provider is configured, log a warning and do not start polling | unchanged |
| `DisplayService` | `display` | run `initialize()` if it has not run, then the existing start logic | unchanged |
| `DashboardClient` | `dashboard` | see 4.4 | send `status` offline if connected, then disconnect |

Rules:

- `initialize()` stays public and becomes idempotent through an `_initialized` flag, so existing tests that call it directly keep working.
- Constructors keep accepting a plain dict (`config: Optional[Dict[str, Any]] = None`), so existing tests keep working.
- `Service` and `ServiceManager` are unchanged.
- `agent.py` constructs services through `from_config` (4.2) and registers them with the same names and dependency declarations as today.

### 4.2 Config mapping: `from_config`

Each service gets a classmethod that builds the dict its constructor already understands from the `Config` dataclass and returns `cls(config=mapped)`.

`AudioService.from_config(config)`:

- `input_device` from `audio.input_device`, with `auto` translated to `default`
- `output_device` from `audio.output_device`, with `auto` translated to `default`
- `noise_reduction` is `audio.noise_reduction_level != "off"`
- `echo_cancellation` from `audio.echo_cancellation`
- sample rate, channels, noise backend, AGC, and VAD keep the service defaults

`VideoService.from_config(config)`:

- `camera` from `video.device`, with `auto` translated to `default`
- `resolution` from `video.resolution`, translated: `4k` to `3840x2160`, `1080p` to `1920x1080`, `720p` to `1280x720`, `480p` to `640x480`; a `WxH` string passes through unchanged
- `fps` from `video.framerate`
- `mirror`, `rotation`, and `background_blur` keep the service defaults

`DisplayService.from_config(config)`:

- `cec_enabled` is `display.backend in ("auto", "hdmi_cec")`
- `ddc_enabled` is `display.backend in ("auto", "ddc")`
- `auto_power_on` from `display.power_on_boot`
- `auto_power_off` stays False. `display.power_off_shutdown` describes a shutdown behaviour that `DisplayService` does not implement, so it is not mapped.

`CalendarService.from_config(config)`:

- `provider` is the first entry of `calendar.providers`; an empty list means the service starts idle
- `poll_interval` from `calendar.sync_interval_seconds`
- `auto_join_minutes` from `meeting.join_early_minutes`
- `credentials`: for `google`, `{"service_account_file": calendar.google_credentials_path}` when the path is non-empty, otherwise `{}`; for `microsoft`, `{"client_id": calendar.microsoft_client_id, "tenant_id": calendar.microsoft_tenant_id}`. The Microsoft provider also needs `client_secret` and `user_email`, which the config schema does not carry; without them authentication fails and the service runs idle. Adding those fields is out of scope.

`DashboardClient.from_config(config, platform_info, capabilities)`:

- `url` from `dashboard.url`, the base HTTP or HTTPS URL of the backend, for example `http://localhost:3001`
- `enrollment_token` from `dashboard.enrollment_token`
- `room_name` from `room.name`
- `heartbeat_interval` from `dashboard.heartbeat_interval_seconds`
- `state_file` is `<data_dir>/dashboard-state.json` (4.5)
- `device_info` is `{"name": room.name, "platform": platform_info.device.value, "softwareVersion": croom.__version__, "capabilities": capabilities.to_dict()}`

### 4.3 Failure policy

Optional services never raise out of `start()`.

- Audio and video: with no device found, `initialize()` already returns True with a warning and `start()` skips capture. Exceptions inside `initialize()` are already caught and turn into a False return; `start()` then logs a warning that the service is running without devices and continues.
- Calendar: no provider, no credentials, or an authentication failure logs a warning and skips polling.
- Display: no CEC or DDC control logs a warning and the service runs without power control, which is its existing behaviour.
- AI: already tolerant. `_load_model()` catches load errors, logs them, and leaves the model unloaded. No code change; a regression test pins this behaviour.
- Dashboard: an unreachable backend or a failed enrollment keeps retrying in the background (4.4). `start()` returns as soon as the connection task is launched.

`MeetingService` is unchanged: a failure there still aborts the agent through `ServiceManager.start_all()`.

### 4.4 Dashboard client

Rewritten on `aiohttp`, already a dependency, for both REST and WebSocket. Public surface kept: `device_id`, `is_connected`, `connect()`, `disconnect()`, `send_status()`, `send_metrics()`, `on_connected()`, `on_disconnected()`, `on_config_update()`, `register_command()`. Renamed: `state` becomes `connection_state`, because `Service.state` now carries the lifecycle state. Signatures follow the backend payloads: `send_status(status, message=None)` and `send_metrics(metrics_type, data)`. Added: `send_meeting_event(event, meeting_id, platform)`. Removed: `send_event()`, `send_log()`, and `create_dashboard_client()`; the backend has no handlers for the first two and nothing in the repo calls any of them.

Lifecycle, entered from `start()`:

1. Load the state file. It is valid only if it contains `device_id` and its `dashboard_url` equals the configured `dashboard.url`; otherwise the device is treated as not enrolled.
2. If not enrolled and a token is configured: `POST {url}/api/provisioning/enroll` with `{"token": ..., "deviceInfo": {...}}`. On HTTP 200, store `device_id` (response field `deviceId`), `dashboard_url`, and `enrolled_at` in the state file. On any other status or a network error, log and retry after the backoff delay. If not enrolled and no token is configured, log once that the dashboard is enabled without an enrollment token and stay idle without retrying.
3. Open the WebSocket at `dashboard.url` with the scheme swapped (`http` to `ws`, `https` to `wss`) and the path `/ws`. The `websocketUrl` in the enroll response is logged when it differs but is not used, because the backend's default for it is `ws://localhost:3001`, which is wrong from a remote device.
4. Send `auth`. On `auth_success` the client is connected: it invokes `on_connected` callbacks, passes `payload.config` to `on_config_update` callbacks, sends `status` online, and starts the heartbeat loop. On `auth_error` whose message is `Unknown device`, it deletes the state file and restarts from step 2. On any other `auth_error` it logs and retries after the backoff delay.
5. Send `heartbeat` every `heartbeat_interval` seconds (default 30). The backend marks a device offline after 60 seconds without one, so intervals above 55 seconds are clamped to 55 with a warning.
6. Any connection loss returns to step 3, or to step 2 if the device is not enrolled, after a backoff that starts at 5 seconds, doubles, and caps at 60 seconds, for as long as the service is running.
7. `stop()` sends `status` offline if connected, cancels the loops, and closes the socket.

Messages. Every message is `{"type": <string>, "payload": {...}}`.

Device to dashboard:

- `auth` with `{deviceId}`
- `heartbeat` with `{deviceId}`
- `status` with `{deviceId, status, message?}` where status is `online`, `offline`, or `error`
- `metrics` with `{deviceId, type, data}` (method only; nothing schedules it in this change)
- `meeting_event` with `{deviceId, event, meetingId, platform}` where event is `joined` or `left` (method only)

Dashboard to device:

- `auth_success` with `{deviceId, config}`
- `auth_error` with `{message}`
- `error` with `{message}`, logged
- anything else is logged at debug level and ignored. The backend sends no commands today; `register_command` remains as the hook for a future `command` message.

Messages sent while disconnected are dropped with a debug log. The offline queue in the current implementation is removed: its only producers were heartbeats and status updates, which are meaningless when replayed later.

### 4.5 Data directory and state file

New top-level config key `data_dir: str = ""`, parsed and serialised like the other top-level keys. `Config.resolve_data_dir()` returns, in order:

1. `data_dir` if set, created if missing;
2. otherwise `/var/lib/croom` if it exists and is writable by the current user (the Pi service runs as the `croom` user that owns it);
3. otherwise `$XDG_DATA_HOME/croom`, defaulting to `~/.local/share/croom`, created on demand.

The state file `dashboard-state.json` lives there and holds `device_id`, `dashboard_url`, and `enrolled_at`. It is written atomically (temporary file then rename) with mode 0600.

### 4.6 Dependencies

No new packages. `websockets` is no longer imported anywhere. `aiohttp` is already declared in `pyproject.toml`.

## 5. Testing

Unit tests, all offline and deterministic, with hardware probes patched to find nothing:

- `tests/unit/core/test_agent.py` (new): with AI and dashboard disabled, `CroomAgent._initialize_services()` registers `audio`, `video`, `display`, `meeting`, and `calendar`; `start_all()` returns True with `get_audio_devices`, `get_cameras`, the CEC and DDC probes, and the meeting providers patched; `stop_all()` completes.
- Per service, extending the existing test modules: the service is an instance of `Service` with the expected name; `start()` followed by `stop()` with no devices raises nothing and leaves the service's running flag cleared (the RUNNING and STOPPED transitions are asserted through `ServiceManager` in the agent test); `from_config` produces the mappings in 4.2, including `auto` to `default`, resolution translation, display backend flags, calendar provider selection, and credentials.
- `tests/unit/ai/test_service.py` (new): `start()` with missing model files does not raise, loads no models, and logs the missing files (regression test for existing behaviour).
- `tests/unit/dashboard/test_client.py` (new): an in-process fake dashboard, an aiohttp web app serving `/api/provisioning/enroll` and `/ws`, verifies the enroll request body and the state file contents; `auth` sent with the stored id; `status` online after `auth_success`; a heartbeat within a short configured interval; `auth_error` with `Unknown device` clearing the state and re-enrolling; `stop()` sending `status` offline; no enrollment attempt without a token; and WebSocket URL derivation for both `http` and `https`.
- `tests/unit/core/test_config.py`: `data_dir` round-trips through `from_dict` and `to_dict`; `resolve_data_dir()` precedence with a temporary HOME and patched writability.

Acceptance test on the development machine: create a token on the Provisioning page or with `POST /api/provisioning/token` as admin, write a config with `dashboard.url: http://localhost:3001`, that token, and `ai.enabled: false`, run `croom -c config.yaml -v`, confirm the device row becomes Online in the dashboard and Offline after Ctrl-C.

## 6. Files

- `src/croom/core/config.py`: `data_dir` and `resolve_data_dir()`
- `src/croom/core/agent.py`: build services through `from_config`; pass platform info and capabilities to the dashboard client
- `src/croom/audio/service.py`, `src/croom/video/service.py`, `src/croom/calendar/service.py`, `src/croom/display/service.py`: inherit `Service`, add `from_config`, start semantics from 4.1 and 4.3
- `src/croom/dashboard/client.py`: rewrite per 4.4
- the tests listed in section 5
- this document

## 7. Notes and risks

- The systemd unit runs the agent as the `croom` user with `DISPLAY=:0`. Browser-based meeting joins on the Pi will need that user to reach the desktop session. Unrelated to this change, but the next likely blocker on real hardware.
- `croom/provisioning/enrollment.py` (`DashboardEnrollment`) targets an API the backend does not have and is left as is; the client performs its own enrollment. Consolidating the two is a later cleanup.
- The backend's `WS_URL` and `BASE_URL` environment variables only affect strings the backend reports; devices derive the WebSocket URL themselves.
