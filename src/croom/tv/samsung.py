"""Samsung's local v2 remote channel, adapted to the room's TV client interface.

Wire format reference: xchwarze/samsung-tv-ws-api's connection.py and remote.py.
The remote channel acknowledges pairing, not individual key presses, and does not
report volume, mute, input or the foreground app. Those values remain unknown.
"""

import asyncio
import base64
import contextlib
import json
import logging
import re
from types import SimpleNamespace
from typing import Any, Optional

import aiohttp
from yarl import URL

from croom.tv.client import StateCallback
from croom.tv.errors import TvError

logger = logging.getLogger(__name__)

_KEYS = {
    "LEFT": "KEY_LEFT", "RIGHT": "KEY_RIGHT", "UP": "KEY_UP", "DOWN": "KEY_DOWN",
    "ENTER": "KEY_ENTER", "BACK": "KEY_RETURN", "EXIT": "KEY_EXIT",
    "HOME": "KEY_HOME", "MENU": "KEY_MENU", "QMENU": "KEY_TOOLS",
    "INFO": "KEY_INFO", "GUIDE": "KEY_GUIDE", "LIST": "KEY_CH_LIST",
    "INPUT_HUB": "KEY_SOURCE", "MUTE": "KEY_MUTE",
    "VOLUMEUP": "KEY_VOLUP", "VOLUMEDOWN": "KEY_VOLDOWN",
    "CHANNELUP": "KEY_CHUP", "CHANNELDOWN": "KEY_CHDOWN",
    "PLAY": "KEY_PLAY", "PAUSE": "KEY_PAUSE", "STOP": "KEY_STOP",
    "REWIND": "KEY_REWIND", "FASTFORWARD": "KEY_FF",
    "RED": "KEY_RED", "GREEN": "KEY_GREEN", "YELLOW": "KEY_YELLOW", "BLUE": "KEY_BLUE",
    "POWER": "KEY_POWEROFF",
    **{str(number): f"KEY_{number}" for number in range(10)},
}


class SamsungPairError(ConnectionRefusedError):
    """The TV declined the pairing prompt or no longer accepts the saved token."""


class SamsungClient:
    """One secure local connection and its receiver, owned by the TV service."""

    supported_keys = frozenset(_KEYS)
    capabilities = {
        "pointer": False, "direct_inputs": False, "mute_state": False,
        "supported_keys": sorted(supported_keys),
    }
    APP_LIST_TIMEOUT = 1.5
    POWER_POLL_INTERVAL = 5.0
    CLOSE_TIMEOUT = .5
    KEY_DELAY = .15

    def __init__(self, host: str, key: Optional[str]):
        self.host = host
        self.client_key = key
        self.tv_info = SimpleNamespace(hello_info={}, system_info={})
        self.tv_state = SimpleNamespace(
            power_state={}, is_on=None, volume=None, muted=None, current_input=None,
            current_app_id=None, current_channel={}, channel_number=None, channels=[],
            inputs={"SOURCE": {"id": "SOURCE", "label": "Source menu"}},
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._socket: Optional[aiohttp.ClientWebSocketResponse] = None
        self._receiver: Optional[asyncio.Task] = None
        self._power_poll: Optional[asyncio.Task] = None
        self._connected = False
        self._refused = False
        self._callbacks: list[StateCallback] = []
        self._send_lock = asyncio.Lock()
        self._apps_lock = asyncio.Lock()
        self._apps_ready = asyncio.Event()
        self._apps: Optional[list[dict[str, Any]]] = None

    def _url(self, scheme: str, path: str, query=None) -> URL:
        return URL.build(scheme=scheme, host=self.host, port=8002, path=path, query=query)

    async def connect(self) -> bool:
        if self.is_connected():
            return True
        await self.disconnect()
        self._refused = False
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        query = {"name": base64.b64encode(b"Crystal Meet").decode("ascii")}
        if self.client_key:
            query["token"] = self.client_key
        try:
            # Samsung's local TV certificate is self-signed. The exception is
            # scoped to these TV requests, never a global TLS setting.
            self._socket = await self._session.ws_connect(
                self._url("wss", "/api/v2/channels/samsung.remote.control", query),
                ssl=False, heartbeat=20,
            )
            while True:
                response = await self._receive(self._socket)
                event = response.get("event")
                if event == "ms.channel.unauthorized":
                    self._refused = True
                    raise SamsungPairError("The Samsung TV refused pairing; accept the TV prompt")
                if event == "ms.channel.connect":
                    self._take_token(response)
                    break
                if event not in ("ed.edenTV.update", "ms.voiceApp.hide", "ms.channel.ready"):
                    raise ConnectionError("The Samsung TV did not accept the remote connection")
            self._connected = True
            self._receiver = asyncio.create_task(self._listen(self._socket))
            await self._device_info()
            if self.is_connected():
                self._power_poll = asyncio.create_task(self._poll_power(self._socket))
            return self.is_connected()
        except BaseException:
            # This includes cancellation while the person is answering the prompt.
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        self._connected = False
        socket, self._socket = self._socket, None
        session, self._session = self._session, None
        receiver, self._receiver = self._receiver, None
        poller, self._power_poll = self._power_poll, None
        self.tv_state.power_state = {}
        self.tv_state.is_on = False
        self._apps = None
        self._apps_ready.set()
        try:
            tasks = [
                task for task in (receiver, poller)
                if task and task is not asyncio.current_task()
            ]
            if tasks:
                for task in tasks:
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), self.CLOSE_TIMEOUT,
                    )
            if socket:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(socket.close(), self.CLOSE_TIMEOUT)
        finally:
            if session:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(session.close(), self.CLOSE_TIMEOUT)

    def is_connected(self) -> bool:
        return bool(self._connected and self._socket and not self._socket.closed)

    async def register_state_update_callback(self, callback: StateCallback) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    async def _notify(self) -> None:
        for callback in self._callbacks:
            try:
                await asyncio.wait_for(callback(self.tv_state), self.CLOSE_TIMEOUT)
            except Exception:
                logger.debug("Samsung state callback failed", exc_info=True)

    @staticmethod
    async def _receive(socket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        message = await socket.receive()
        if message.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError("The Samsung TV closed the remote connection")
        try:
            response = json.loads(message.data)
        except (ValueError, TypeError) as error:
            raise ConnectionError("The Samsung TV sent an invalid remote response") from error
        if not isinstance(response, dict):
            raise ConnectionError("The Samsung TV sent an invalid remote response")
        return response

    def _take_token(self, response: dict[str, Any]) -> None:
        data = response.get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if isinstance(token, (str, int)) and not isinstance(token, bool) and token:
            self.client_key = str(token)

    async def _listen(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        try:
            while True:
                response = await self._receive(socket)
                event = response.get("event")
                if event == "ms.channel.unauthorized":
                    self._refused = True
                    break
                if event == "ms.channel.connect":
                    self._take_token(response)
                elif event == "ed.installedApp.get":
                    data = response.get("data")
                    apps = data.get("data") if isinstance(data, dict) else None
                    self._apps = [app for app in apps if isinstance(app, dict)] if isinstance(apps, list) else None
                    self._apps_ready.set()
        except (ConnectionError, aiohttp.ClientError):
            pass
        finally:
            if self._socket is socket:
                self._connected = False
                poller, self._power_poll = self._power_poll, None
                if poller:
                    poller.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.wait_for(poller, self.CLOSE_TIMEOUT)
                self.tv_state.power_state = {}
                self.tv_state.is_on = False
                self._apps = None
                self._apps_ready.set()
                await self._notify()

    async def _poll_power(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        """Standby can keep the remote socket alive, so its presence is not power state."""
        while self._socket is socket and self.is_connected():
            await asyncio.sleep(self.POWER_POLL_INTERVAL)
            if self._socket is not socket or not self.is_connected():
                return
            previous = self.tv_state.power_state
            await self._device_info()
            if self._socket is socket and self.is_connected():
                if self.tv_state.power_state != previous:
                    await self._notify()

    async def _device_info(self) -> dict[str, Any]:
        power: dict[str, Any] = {}
        if self._session and self.is_connected():
            try:
                async with self._session.get(
                    self._url("https", "/api/v2/"), ssl=False,
                    timeout=aiohttp.ClientTimeout(total=1.5),
                ) as response:
                    response.raise_for_status()
                    info = await response.json(content_type=None)
                device = info.get("device", {}) if isinstance(info, dict) else {}
                if isinstance(device, dict):
                    if isinstance(device.get("name"), str):
                        self.tv_info.hello_info = {"deviceName": device["name"]}
                    if isinstance(device.get("modelName"), str):
                        self.tv_info.system_info = {"modelName": device["modelName"]}
                    state = str(device.get("PowerState", "")).lower()
                    if state == "on":
                        power = {"state": "Active"}
                    elif state in ("standby", "off"):
                        power = {"state": "Active Standby" if state == "standby" else "Off"}
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
                # Some firmware supports the remote channel but not this REST read.
                pass
        self.tv_state.power_state = power
        self.tv_state.is_on = power["state"] == "Active" if power else None
        return power

    async def _send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._send_lock:
            if not self.is_connected():
                if self._refused:
                    raise SamsungPairError("The Samsung TV no longer accepts this remote; pair again")
                raise ConnectionError("The Samsung TV remote is disconnected")
            assert self._socket is not None
            await self._socket.send_json({"method": method, "params": params})
        return {}

    async def _key(self, wire_name: str) -> dict[str, Any]:
        return await self._send("ms.remote.control", {
            "Cmd": "Click", "DataOfCmd": wire_name,
            "Option": "false", "TypeOfRemote": "SendRemoteKey",
        })

    async def button(self, name: str) -> None:
        if name not in _KEYS:
            raise TvError(f"The Samsung TV does not support this remote key: {name}")
        await self._key(_KEYS[name])

    async def power_off(self) -> None:
        await self._key("KEY_POWEROFF")

    async def power_on(self) -> dict[str, Any]:
        # The service sends Wake-on-LAN. KEY_POWER would turn an already-on TV off.
        return {}

    async def volume_up(self) -> dict[str, Any]:
        return await self._key("KEY_VOLUP")

    async def volume_down(self) -> dict[str, Any]:
        return await self._key("KEY_VOLDOWN")

    async def set_volume(self, volume: int) -> dict[str, Any]:
        raise TvError("Samsung does not report volume here; use the volume up and down buttons")

    async def set_mute(self, mute: bool) -> dict[str, Any]:
        raise TvError("Samsung does not report mute state here; use the mute toggle button")

    async def channel_up(self) -> dict[str, Any]:
        return await self._key("KEY_CHUP")

    async def channel_down(self) -> dict[str, Any]:
        return await self._key("KEY_CHDOWN")

    async def set_channel(self, channel: str) -> dict[str, Any]:
        if not isinstance(channel, str) or not re.fullmatch(r"[0-9]{1,5}", channel):
            raise TvError("Samsung channel entry supports whole channel numbers using the number keys")
        for digit in channel:
            await self._key(f"KEY_{digit}")
            await asyncio.sleep(self.KEY_DELAY)
        return await self._key("KEY_ENTER")

    async def get_inputs(self) -> list[dict[str, Any]]:
        return [{"id": "SOURCE", "label": "Source menu"}]

    async def set_input(self, input_id: str) -> dict[str, Any]:
        if input_id != "SOURCE":
            raise TvError("Choose the Samsung Source menu, then select an input with the arrow keys")
        return await self._key("KEY_SOURCE")

    async def get_apps(self) -> Optional[list[dict[str, Any]]]:
        async with self._apps_lock:
            self._apps = None
            self._apps_ready.clear()
            await self._send("ms.channel.emit", {"event": "ed.installedApp.get", "to": "host"})
            try:
                await asyncio.wait_for(self._apps_ready.wait(), self.APP_LIST_TIMEOUT)
            except asyncio.TimeoutError:
                return None
            if self._apps is None:
                return None
            return [
                {"id": str(app["appId"]), "title": str(app.get("name") or app["appId"])}
                for app in self._apps if app.get("appId")
            ]

    async def launch_app(self, app: str) -> dict[str, Any]:
        app_type = next(
            (item.get("app_type") for item in self._apps or [] if str(item.get("appId")) == app),
            2,
        )
        return await self._send("ms.channel.emit", {
            "event": "ed.apps.launch", "to": "host",
            "data": {
                "action_type": "DEEP_LINK" if str(app_type) == "2" else "NATIVE_LAUNCH",
                "appId": app, "metaTag": "",
            },
        })

    async def send_message(self, message: str) -> dict[str, Any]:
        raise TvError("Samsung does not support an on-screen message through the local remote")

    async def move(self, d_x: int, d_y: int, down: int = 0) -> None:
        raise TvError("Samsung pointer control is unavailable; use the arrow keys and OK")

    async def click(self) -> None:
        raise TvError("Samsung pointer control is unavailable; use the arrow keys and OK")

    async def scroll(self, d_x: int, d_y: int) -> None:
        raise TvError("Samsung pointer control is unavailable; use the arrow keys and OK")

    async def get_volume(self) -> Optional[int]:
        return None

    async def get_muted(self) -> Optional[bool]:
        return None

    async def get_input(self) -> Optional[str]:
        return None

    async def get_current_app(self) -> Optional[str]:
        return None

    async def get_current_channel(self) -> dict[str, Any]:
        return {}

    async def get_power_state(self) -> dict[str, Any]:
        return await self._device_info()
