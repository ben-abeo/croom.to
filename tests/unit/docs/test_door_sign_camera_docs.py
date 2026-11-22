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


def java_ms(file, name):
    match = re.search(rf"static final long {name} = ([\d_]+);", (APP / file).read_text(encoding="utf-8"))
    assert match, name
    return int(match.group(1).replace("_", ""))


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


def test_the_manual_gives_the_sound_switch_its_format_and_the_cards_words():
    """The sound (spec 2026-11-10): the switch's name from the schema, the format and "Sound off" from the app's source."""
    from croom.settings.schema import BY_PATH

    label = BY_PATH["control.door_sign_camera_sound"].label
    rate = int(java_int("Onvif.java", "SAMPLE_RATE")) // 1000
    kbps = java_int("Onvif.java", "SOUND_BITRATE_KBPS")
    off = java_string("Panel.java", "SOUND_OFF").rstrip(": ")
    for name, text in manuals().items():
        for needle in (label, f"AAC, {rate} kHz mono, {kbps} kbit/s", off, "Android has not granted the app the microphone",
                       "the microphone did not start", ", with sound", "consent"):
            assert needle in text, (name, needle)


def test_the_manual_gives_people_detection_its_switch_its_numbers_and_the_cards_words():
    """People detection (spec 2026-11-22): the switch's name from the schema; how often it looks, how long someone stays and the
    card's words from the app's source."""
    from croom.settings.schema import BY_PATH

    label = BY_PATH["control.door_sign_camera_people"].label
    off = java_string("Panel.java", "PEOPLE_OFF").rstrip(": ")
    any_movement = java_string("PeopleWatch.java", "ANY_MOVEMENT").lstrip(". ")
    always = java_string("PeopleWatch.java", "ALWAYS").lstrip(". ")
    look, hold, still = (java_ms("Presence.java", name) for name in ("LOOK_MS", "HOLD_MS", "STILL_MS"))
    line = ", with people detection"
    assert f'"{line}"' in (APP / "CameraService.java").read_text(encoding="utf-8")
    for name, text in manuals().items():
        for needle in (label, off, any_movement, always, f"up to {1000 // look} times a second", f"until {hold // 1000} seconds after the last",
                       f"nothing has moved for {still // 1000} seconds", line, "version 5 or later", "adopt it again", "AI Port",
                       "set its recording back to"):
            assert needle in text, (name, needle)


def test_the_people_model_is_the_file_the_app_readme_says_it_is():
    """The model is a binary in the repository: the README says where it is from, and its SHA-256, so it can be checked and replaced."""
    import hashlib

    model = APP.parents[3] / "assets" / "people.tflite"
    readme = (REPO / "android" / "door-sign" / "README.md").read_text(encoding="utf-8")
    asset = java_string("PeopleModel.java", "ASSET")
    assert model.name == asset and f"app/src/main/assets/{asset}" in readme
    assert hashlib.sha256(model.read_bytes()).hexdigest() in readme
