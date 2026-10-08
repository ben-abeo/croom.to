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
