"""
The door sign's camera in UniFi Protect (spec 2026-11-06, section 11): the manual says what the app says. The card's three "Camera off"
reasons, the ports and the user are read from the app's source, so the manual can't drift from a change there.
"""

import re
from html import unescape
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "android" / "door-sign" / "app" / "src" / "main" / "java" / "to" / "croom" / "doorsign"
MANUAL_MD = REPO / "docs" / "guides" / "confluence" / "93-door-sign-manual.md"
MANUAL_HTML = REPO / "docs" / "guides" / "crystal-meet-door-sign-manual" / "index.html"


def java_string(file, name):
    match = re.search(rf'static final String {name} = "([^"]*)";', (APP / file).read_text(encoding="utf-8"))
    assert match, name
    return match.group(1)


def java_int(file, name):
    match = re.search(rf"static final int {name} = (\d+);", (APP / file).read_text(encoding="utf-8"))
    assert match, name
    return match.group(1)


def manuals():
    html = " ".join(unescape(re.sub(r"<[^>]+>", "", MANUAL_HTML.read_text(encoding="utf-8"))).split())
    return {"md": MANUAL_MD.read_text(encoding="utf-8").replace("`", ""), "html": html}


def test_the_manual_gives_the_cards_words_for_a_camera_that_is_off():
    words = [java_string("Panel.java", "CAMERA_NOT_OWNER"), java_string("Panel.java", "CAMERA_NO_PASSWORD"),
             java_string("Panel.java", "CAMERA_FAILED").rstrip(": ")]
    for name, text in manuals().items():
        for needle in words:
            assert needle in text, (name, needle)


def test_the_manual_gives_the_ports_and_the_user():
    onvif, rtsp = java_int("Onvif.java", "PORT"), java_int("Onvif.java", "RTSP_PORT")
    discovery = java_int("Discovery.java", "PORT")
    user = re.search(r'SIGN_CAMERA_USER = "([^"]+)"', (REPO / "src" / "croom" / "settings" / "checkin.py").read_text()).group(1)
    for name, text in manuals().items():
        for needle in (f"ONVIF TCP {onvif}", f"RTSP TCP {rtsp}", f"WS-Discovery UDP {discovery}", f"user {user}",
                       f"ONVIF port {onvif}, user {user}"):
            assert needle in text, (name, needle)
