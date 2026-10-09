"""
One small JSON file next to the agent's state holds the screensaver choice and the
camera's home and presets (spec 2026-10-08 sound and camera, section 4.5).
"""

import json

import pytest

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


def test_an_unserialisable_value_raises_and_changes_nothing(tmp_path):
    path = tmp_path / "control-settings.json"
    store = SettingsStore(path)
    store.save("screensaver", "quiet")
    with pytest.raises(TypeError):
        store.save("camera", {"home": object()})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["control-settings.json"]
    assert store.get("screensaver") == "quiet" and store.get("camera") is None
    assert SettingsStore(path).load() == {"screensaver": "quiet"}
    store.save("screensaver", "bounce")
    assert SettingsStore(path).get("screensaver") == "bounce"


def test_a_write_that_fails_changes_nothing(tmp_path, monkeypatch):
    """The value is remembered only once it is on disk: a later save of another key must not carry a value
    the caller was told did not save (a camera home after a full disk)."""
    store = SettingsStore(tmp_path / "s.json")
    store.save("screensaver", "info")

    def refuse(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("croom.control.settings.os.replace", refuse)
    with pytest.raises(OSError):
        store.save("camera", {"home": {"pan_s": 1.0, "tilt_s": 2.0}})
    assert store.get("camera") is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.json"]          # the temp file is gone
    monkeypatch.undo()
    store.save("screensaver", "quiet")
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8")) == {"screensaver": "quiet"}
