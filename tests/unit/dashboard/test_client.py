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
