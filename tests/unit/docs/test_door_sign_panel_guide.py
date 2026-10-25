"""
The door sign panel guide builds to a PDF, is self-contained, and says what the app in android/door-sign does: its package
and activity, how long the corner is held, how often a sign that cannot reach the device tries again, the PIN's length, the
colours of the light bar and the panel maker's broadcasts. Each number and name is read from the app's own code.
"""

import re
import subprocess
import sys
from html import unescape
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUIDES = REPO / "docs" / "guides"
GUIDE = GUIDES / "crystal-meet-door-sign-panel"
APP = REPO / "android" / "door-sign" / "app" / "src" / "main"
JAVA = APP / "java" / "to" / "croom" / "doorsign"


def text():
    """The guide as a reader sees it: each tag a space, entities decoded, white space collapsed."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def java(name):
    return (JAVA / f"{name}.java").read_text(encoding="utf-8")


def millis(source, constant):
    value = re.search(rf"{constant}\s*=\s*([\d_]+)", source).group(1)
    return int(value.replace("_", ""))


def test_the_guide_builds_to_a_pdf(tmp_path):
    pytest.importorskip("playwright.sync_api")
    out = tmp_path / "guide.pdf"
    result = subprocess.run([sys.executable, str(GUIDE / "build.py"), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes().startswith(b"%PDF")


def test_the_guide_is_self_contained():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "url(http" not in html and "@import" not in html
    for asset in ("crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2", "fonts/OFL.txt"):
        assert (GUIDE / asset).is_file(), asset
    assert (GUIDES / "crystal-meet-door-sign-panel.pdf").is_file()


def test_the_guide_names_the_app_the_manifest_declares():
    manifest = (APP / "AndroidManifest.xml").read_text(encoding="utf-8")
    gradle = (REPO / "android" / "door-sign" / "app" / "build.gradle").read_text(encoding="utf-8")
    package = re.search(r'applicationId "([^"]+)"', gradle).group(1)
    assert package == "to.croom.doorsign" and 'android:name=".MainActivity"' in manifest
    assert "android.intent.category.HOME" in manifest   # set-home-activity needs the app to be a home app
    t = text()
    for needle in (
        f"adb shell cmd package set-home-activity {package}/.MainActivity",
        f"adb shell setprop persist.sys.bootapp {package}/{package}.MainActivity",
        f"adb shell am start -n {package}/.MainActivity",
        f"adb uninstall {package}",
        f"adb shell pm clear {package}",
        "adb install -r door-sign.apk",
        "android/door-sign/app/build/outputs/apk/debug/app-debug.apk",
        "./gradlew testDebugUnitTest assembleDebug",
        "adb logcat -s DoorSign",
        "Light bar: free",
    ):
        assert needle in t, needle


def test_the_guide_gives_the_apps_own_timings_and_pin_rule():
    main = java("MainActivity")
    t = text()
    assert f"for {millis(main, 'CORNER_HOLD_MS') // 1000} seconds" in t
    assert f"tries again every {millis(main, 'RETRY_MS') // 1000} seconds" in t
    assert re.search(r"\[0-9\]\{4,8\}", java("Pin"))
    assert "4 to 8 digits" in t


def test_the_guide_gives_the_colours_the_app_shows():
    light_bar = java("LightBar")
    for state, rgb in (("free", "0, FULL, 0"), ("soon", "FULL, FULL, 0"), ("occupied", "FULL, 0, 0")):
        assert f'"{state}".equals(state)) return new int[] {{{rgb}}}' in light_bar, state
    t = text()
    for needle in ("Green Free.", "Yellow A booking starts within 10 minutes.", "Red In a meeting, or booked.",
                   "Off The sign cannot reach the room device"):
        assert needle in t, needle
    sign = (REPO / "src" / "croom" / "control" / "static" / "sign.js").read_text(encoding="utf-8")
    assert "const SOON_MS = 10 * 60 * 1000;" in sign and "window.CrystalPanel.setLight(state)" in sign


def test_the_guide_uses_the_broadcasts_the_app_sends():
    main = java("MainActivity")
    hide = re.search(r'SYSTEM_BARS_HIDE = "([^"]+)"', main).group(1)
    show = re.search(r'SYSTEM_BARS_SHOW = "([^"]+)"', main).group(1)
    t = text()
    assert f"adb shell am broadcast -a {hide}" in t and f"adb shell am broadcast -a {show}" in t


def test_the_readme_and_the_room_guide_point_at_the_panel_guide():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "docs/guides/crystal-meet-door-sign-panel.pdf" in readme and "android/door-sign" in readme
    room = (GUIDES / "crystal-meet-room-setup" / "index.html").read_text(encoding="utf-8")
    assert 'the guide "Set up a Crystal Meet door sign panel"' in room
