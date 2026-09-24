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
