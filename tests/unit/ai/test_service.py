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
