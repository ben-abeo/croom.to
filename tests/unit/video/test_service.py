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
            "capture": False,
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


class TestVideoServiceDeviceFailure:
    """A camera that is present but fails to start must not take the service down (spec 4.3)."""

    async def test_start_survives_camera_failure(self):
        from unittest.mock import AsyncMock, MagicMock
        from croom.video.camera import CameraInfo

        info = CameraInfo(id="fake-cam", name="Fake camera", backend=None)
        camera = MagicMock()
        camera.open = AsyncMock(return_value=True)
        camera.set_resolution = AsyncMock(return_value=True)
        camera.set_fps = AsyncMock(return_value=True)
        camera.start = AsyncMock(side_effect=RuntimeError("device busy"))
        camera.stop = AsyncMock()
        with patch("croom.video.service.get_cameras", return_value=[info]), \
             patch("croom.video.service.create_camera", return_value=camera):
            service = VideoService()
            await service.start()
            assert service._running is True
            assert service._camera is None
            assert service._capture_task is None
            await service.stop()
            assert service._running is False


class TestVideoServiceLeavesCameraFree:
    """In a room the meeting apps own the camera; the agent must not hold it open."""

    def _camera(self):
        from unittest.mock import AsyncMock, MagicMock
        camera = MagicMock()
        for name in ("open", "set_resolution", "set_fps", "start", "stop", "close"):
            setattr(camera, name, AsyncMock(return_value=True))
        return camera

    async def test_room_service_never_opens_the_camera(self):
        from croom.video.camera import CameraInfo

        info = CameraInfo(id="/dev/video0", name="Logitech MeetUp", backend=None)
        camera = self._camera()
        with patch("croom.video.service.get_cameras", return_value=[info]), \
             patch("croom.video.service.create_camera", return_value=camera) as make:
            service = VideoService.from_config(Config())
            await service.start()
            assert service.cameras == [info]
            make.assert_not_called()
            assert service._capture_task is None
            await service.stop()

    async def test_room_service_leaves_the_camera_free_with_ai_on(self):
        config = Config()
        config.ai.enabled = True
        assert VideoService.from_config(config).config["capture"] is False

    async def test_capture_on_still_opens_and_streams(self):
        from croom.video.camera import CameraInfo

        info = CameraInfo(id="/dev/video0", name="Logitech MeetUp", backend=None)
        camera = self._camera()
        with patch("croom.video.service.get_cameras", return_value=[info]), \
             patch("croom.video.service.create_camera", return_value=camera):
            service = VideoService(config={"capture": True})
            await service.start()
            camera.open.assert_awaited_once()
            camera.start.assert_awaited_once()
            assert service._capture_task is not None
            await service.stop()
