"""Settings' key files, the Devices page's choices and Test the switch (spec 2026-10-29, sections 3 and 4): a key uploaded (multipart), read
from a USB stick or (Zoom) typed, saved by the root helper and the config pointed at it; what the page is told about the saved keys (never
their secrets); the room's speakers, microphones and cameras as the device settings' choices; the Laptop switch pressed from Settings.

The helper is its real jobs on a temporary folder standing in for /etc/croom and /media (JobsAdmin), so a key is checked and written as a
room device would; nothing here needs root or touches the machine's own folders."""

import getpass
import grp
import json
import os

import aiohttp
import pytest
import yaml

from croom.admin.client import AdminError, AdminUnavailable
from croom.admin.jobs import Host, JobError, Jobs, Paths
from croom.meeting.providers.base import MeetingState
from croom.settings.pin import PinStore
from tests.unit.control.test_service import StubMeeting, client_factory, make_service  # noqa: F401  (client_factory is a fixture)
from tests.unit.settings.test_credentials import GOOGLE_KEY, ZOOM, ZOOM_S2S

PIN = "1234"
CONFIG = "room:\n  name: Lab\ncalendar:\n  google_calendar_id: c_1@resource.calendar.google.com\ntv:\n  host: 10.0.0.40\n"


class JobsAdmin:
    """The root helper's client, answered by the helper's own jobs on a temporary machine: what was asked is kept in `calls`."""

    def __init__(self, root):
        self.paths = Paths(config_dir=root / "etc", data_dir=root / "var", media_root=root / "media", install_dir=root / "opt")
        for folder in (self.paths.config_dir, self.paths.data_dir, self.paths.media_root, self.paths.install_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.jobs = Jobs(Host(paths=self.paths, owner=getpass.getuser(), group=grp.getgrgid(os.getgid()).gr_name,
                              run=lambda args: (0, ""), background=lambda work: None))
        self.calls = []
        self.gone = False

    @property
    def config(self):
        return self.paths.config

    def available(self):
        return not self.gone

    async def call(self, job, **args):
        if self.gone:
            raise AdminUnavailable("Not available on this device")
        self.calls.append((job, args))
        try:
            return {"ok": True, **self.jobs.table[job](args)}
        except JobError as e:
            raise AdminError(str(e)) from e

    def stick(self, name, content, label="KEYS"):
        """A file on a USB stick plugged into the TV Pi (/media/<user>/<label>/<name>)."""
        folder = self.paths.media_root / "room" / label
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        return str(path)

    def jobs_asked(self):
        return [job for job, _ in self.calls]


@pytest.fixture
def admin(tmp_path):
    helper = JobsAdmin(tmp_path)
    helper.config.write_text(CONFIG)
    return helper


@pytest.fixture
def pins(tmp_path):
    store = PinStore(tmp_path / "settings-pin.json")
    store.set_pin(PIN)
    return store


@pytest.fixture
def unlocked(pins):
    return {"X-Croom-Unlock": pins.unlock(PIN)}


@pytest.fixture
async def client(client_factory, admin, pins):  # noqa: F811
    return await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), meeting=StubMeeting()))


def upload(content, field="file", name="key.json"):
    form = aiohttp.FormData()
    form.add_field(field, content if isinstance(content, (bytes, str)) else json.dumps(content), filename=name,
                   content_type="application/json")
    return form


def config_of(admin):
    return yaml.safe_load(admin.config.read_text())


async def test_every_key_route_asks_for_the_pin_first(client, admin):
    for method, path, kwargs in (("get", "/api/settings/credentials", {}), ("get", "/api/settings/usb-files", {}),
                                 ("post", "/api/settings/credentials/google", {"data": upload(GOOGLE_KEY)}),
                                 ("post", "/api/settings/credentials/google/usb", {"json": {"path": "/media/x/y/k.json"}}),
                                 ("post", "/api/settings/credentials/zoom/typed", {"json": {"key": "a", "secret": "b"}}),
                                 ("post", "/api/settings/laptop/test", {"json": {}})):
        response = await getattr(client, method)(path, **kwargs)
        assert response.status == 401 and (await response.json())["error"] == "Enter the PIN", path
    assert admin.calls == []


async def test_an_uploaded_google_key_is_saved_by_the_helper_and_the_calendar_pointed_at_it(client, admin, unlocked):
    response = await client.post("/api/settings/credentials/google", data=upload(GOOGLE_KEY), headers=unlocked)
    assert response.status == 200
    body = await response.json()
    saved = admin.paths.config_dir / "google-service-account.json"
    assert body["summary"] == "Key for room@croom.iam.gserviceaccount.com" and body["path"] == str(saved)
    assert body["restart"] is True and body["pending_restart"] is True and body["saved_at"]
    assert json.loads(saved.read_text()) == GOOGLE_KEY and (saved.stat().st_mode & 0o777) == 0o600
    data = config_of(admin)
    assert data["calendar"]["google_credentials_path"] == str(saved)
    assert data["calendar"]["google_calendar_id"] == "c_1@resource.calendar.google.com" and data["tv"]["host"] == "10.0.0.40"
    assert admin.jobs_asked() == ["save_credential", "write_config"]
    page = await (await client.get("/api/settings/config/calendar", headers=unlocked)).json()
    assert page["pending_restart"] is True


async def test_what_the_page_is_told_of_the_keys_names_the_client_email_only(client, admin, unlocked):
    assert await (await client.get("/api/settings/credentials", headers=unlocked)).json() == {"google": None, "zoom": None}
    await client.post("/api/settings/credentials/google", data=upload(GOOGLE_KEY), headers=unlocked)
    response = await client.get("/api/settings/credentials", headers=unlocked)
    text = await response.text()
    body = json.loads(text)
    assert body["google"]["summary"] == "Key for room@croom.iam.gserviceaccount.com" and body["google"]["saved_at"] and body["zoom"] is None
    for secret in ("PRIVATE KEY", "MIIsecret", "1234567890", "private_key"):
        assert secret not in text


async def test_a_saved_key_applies_after_a_restart_even_when_the_path_is_the_same(client, admin, unlocked):
    await client.post("/api/settings/credentials/google", data=upload(GOOGLE_KEY), headers=unlocked)
    admin.calls.clear()
    second = dict(GOOGLE_KEY, client_email="other@croom.iam.gserviceaccount.com")
    body = await (await client.post("/api/settings/credentials/google", data=upload(second), headers=unlocked)).json()
    assert body["summary"] == "Key for other@croom.iam.gserviceaccount.com" and body["restart"] is True
    assert admin.jobs_asked() == ["save_credential"]                      # the config already points at it: nothing to write


async def test_a_key_of_the_wrong_kind_is_refused_naming_what_it_is_and_nothing_changes(client, admin, unlocked):
    response = await client.post("/api/settings/credentials/google", data=upload(ZOOM), headers=unlocked)
    assert response.status == 400
    assert await response.json() == {"error": "This is a Zoom credentials file, not a Google service-account key"}
    response = await client.post("/api/settings/credentials/zoom", data=upload(GOOGLE_KEY), headers=unlocked)
    assert (await response.json())["error"] == "This is a Google service-account key, not Zoom credentials"
    assert not (admin.paths.config_dir / "google-service-account.json").exists()
    assert admin.config.read_text() == CONFIG


async def test_an_unknown_kind_of_key_is_refused(client, admin, unlocked):
    response = await client.post("/api/settings/credentials/calendar", data=upload(GOOGLE_KEY), headers=unlocked)
    assert response.status == 404 and "Google service-account key or Zoom SDK keys" in (await response.json())["error"]
    response = await client.post("/api/settings/credentials/ssh/usb", json={"path": "/x"}, headers=unlocked)
    assert response.status == 404
    assert admin.calls == []


async def test_an_upload_must_be_a_form_with_a_file_of_at_most_64_kb_of_text(client, admin, unlocked):
    response = await client.post("/api/settings/credentials/google", json=GOOGLE_KEY, headers=unlocked)
    assert response.status == 415 and "multipart/form-data" in (await response.json())["error"]
    response = await client.post("/api/settings/credentials/google", data=upload(GOOGLE_KEY, field="other"), headers=unlocked)
    assert response.status == 400 and await response.json() == {"error": "Choose a key file to upload"}
    big = dict(GOOGLE_KEY, padding="x" * (64 * 1024))
    response = await client.post("/api/settings/credentials/google", data=upload(big), headers=unlocked)
    assert response.status == 413 and "too large" in (await response.json())["error"]
    response = await client.post("/api/settings/credentials/google", data=upload(b"\xff\xfe\x00binary"), headers=unlocked)
    assert response.status == 400 and "not text" in (await response.json())["error"]
    response = await client.post("/api/settings/credentials/google", data=upload("{not json"), headers=unlocked)
    assert response.status == 400 and "not a JSON file" in (await response.json())["error"]
    assert admin.jobs_asked() == ["save_credential"]                      # the one that was text went to the helper, and was refused
    assert admin.config.read_text() == CONFIG


async def test_a_key_on_a_usb_stick_is_listed_and_saved_by_its_path(client, admin, unlocked):
    path = admin.stick("conference-a-zoom.json", ZOOM_S2S)
    admin.stick("notes.txt", "not a key")
    listed = await (await client.get("/api/settings/usb-files", headers=unlocked)).json()
    assert listed == {"files": [{"path": path, "name": "conference-a-zoom.json", "stick": "KEYS", "size": len(json.dumps(ZOOM_S2S))}]}
    response = await client.post("/api/settings/credentials/zoom/usb", json={"path": path}, headers=unlocked)
    assert response.status == 200
    body = await response.json()
    assert body["summary"] == "Zoom Meeting SDK keys, joining as room@crystalpm.com"
    saved = admin.paths.config_dir / "zoom-credentials.json"
    assert json.loads(saved.read_text()) == ZOOM_S2S and config_of(admin)["meeting"]["zoom_credentials_path"] == str(saved)
    assert admin.jobs_asked() == ["usb_files", "read_usb_file", "save_credential", "write_config"]


async def test_a_path_that_is_not_a_listed_key_file_is_refused_by_the_helper(client, admin, unlocked):
    for path in ("/etc/shadow", str(admin.paths.media_root / "room" / "KEYS" / ".." / ".." / "x.json"), ""):
        response = await client.post("/api/settings/credentials/google/usb", json={"path": path}, headers=unlocked)
        assert response.status == 400, path
    assert admin.config.read_text() == CONFIG
    response = await client.post("/api/settings/credentials/google/usb", data="path=/x", headers=unlocked)
    assert response.status == 415


async def test_zoom_keys_typed_at_the_page_are_saved_and_keep_the_rooms_zoom_user(client, admin, unlocked):
    admin.stick("zoom.json", ZOOM_S2S)
    path = (await (await client.get("/api/settings/usb-files", headers=unlocked)).json())["files"][0]["path"]
    await client.post("/api/settings/credentials/zoom/usb", json={"path": path}, headers=unlocked)
    response = await client.post("/api/settings/credentials/zoom/typed", json={"key": "newid", "secret": "newsecret"}, headers=unlocked)
    assert response.status == 200 and (await response.json())["summary"] == "Zoom Meeting SDK keys, joining as room@crystalpm.com"
    saved = json.loads((admin.paths.config_dir / "zoom-credentials.json").read_text())
    assert saved == dict(ZOOM_S2S, sdk_client_id="newid", sdk_client_secret="newsecret")


async def test_typed_zoom_keys_are_refused_by_field(client, admin, unlocked):
    response = await client.post("/api/settings/credentials/zoom/typed", json={"key": "", "secret": "x y"}, headers=unlocked)
    assert response.status == 400
    body = await response.json()
    assert set(body["errors"]) == {"key", "secret"} and body["errors"]["key"] == "The SDK key can't be blank"
    assert admin.calls == []


async def test_a_bad_path_in_the_config_is_said_and_put_right_by_a_new_key(client, admin, unlocked):
    admin.config.write_text(CONFIG + "meeting:\n  zoom_credentials_path: /etc/croom/gone.json\n")
    body = await (await client.get("/api/settings/credentials", headers=unlocked)).json()
    assert body["zoom"] == {"path": "/etc/croom/gone.json", "problem": "There is no file at /etc/croom/gone.json: upload the key again"}
    await client.post("/api/settings/credentials/zoom", data=upload(ZOOM), headers=unlocked)
    body = await (await client.get("/api/settings/credentials", headers=unlocked)).json()
    assert body["zoom"]["summary"] == "Zoom Meeting SDK keys"


async def test_without_the_helper_a_key_cant_be_saved_but_the_keys_are_still_described(client, admin, unlocked):
    admin.gone = True
    for method, path, kwargs in (("get", "/api/settings/usb-files", {}),
                                 ("post", "/api/settings/credentials/google", {"data": upload(GOOGLE_KEY)}),
                                 ("post", "/api/settings/credentials/google/usb", {"json": {"path": "/x"}}),
                                 ("post", "/api/settings/credentials/zoom/typed", {"json": {"key": "a", "secret": "b"}})):
        response = await getattr(client, method)(path, headers=unlocked, **kwargs)
        assert response.status == 503 and (await response.json())["error"] == "Not available on this device", path
    assert (await client.get("/api/settings/credentials", headers=unlocked)).status == 200


async def test_a_key_saved_when_the_config_cannot_be_written_says_so(client, admin, unlocked):
    admin.config.write_text(CONFIG + "ai:\n  backend: quantum\n")          # hand-written, and refused by the schema: every save is held
    response = await client.post("/api/settings/credentials/google", data=upload(GOOGLE_KEY), headers=unlocked)
    assert response.status == 409
    words = (await response.json())["error"]
    assert words.startswith("The key is saved, but the room's settings could not be pointed at it") and "AI backend" in words


async def test_a_meetings_save_at_the_schemas_list_limits_is_read(client, admin, unlocked):
    dialogs = [f"{n:03d}" + "\u00e9" * 117 for n in range(40)]          # the longest list, escaped as JSON sends it: well over 4 KB
    response = await client.post("/api/settings/config/meetings", json={"values": {"meeting.dialogs.acknowledge": dialogs}},
                                 headers=unlocked)
    assert response.status == 200 and config_of(admin)["meeting"]["dialogs"]["acknowledge"] == dialogs


async def test_other_routes_still_read_at_most_4_kb(client, admin, unlocked):
    response = await client.post("/api/settings", json={"key_click": True, "padding": "x" * 5000})
    assert response.status == 400


# --- the Devices page


async def test_the_devices_page_offers_the_rooms_devices_and_keeps_the_configs(client_factory, admin, pins, unlocked):  # noqa: F811
    admin.config.write_text(CONFIG + "audio:\n  input_device: MeetUp\n")

    async def found():
        return {"audio.output_device": [("alsa_output.usb-meetup", "Logitech MeetUp Analog Stereo")],
                "audio.input_device": [("alsa_input.usb-meetup", "Logitech MeetUp Mic")],
                "video.device": [("/dev/video0", "Logitech MeetUp (/dev/video0)")]}

    client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), find_devices=found))
    body = await (await client.get("/api/settings/config/devices", headers=unlocked)).json()
    fields = {f["path"]: f for f in body["fields"]}
    assert fields["audio.output_device"]["choices"] == [{"value": "auto", "label": "Automatic"},
                                                        {"value": "alsa_output.usb-meetup", "label": "Logitech MeetUp Analog Stereo"}]
    assert [c["value"] for c in fields["audio.input_device"]["choices"]] == ["auto", "alsa_input.usb-meetup", "MeetUp"]
    assert fields["video.device"]["choices"][1] == {"value": "/dev/video0", "label": "Logitech MeetUp (/dev/video0)"}
    assert body["values"]["audio.input_device"] == "MeetUp" and body["values"]["video.device"] == "auto"
    response = await client.post("/api/settings/config/devices", json={"values": {"video.device": "/dev/video0"}}, headers=unlocked)
    assert response.status == 200 and config_of(admin)["video"]["device"] == "/dev/video0"


async def test_a_listing_that_fails_still_opens_the_devices_page(client_factory, admin, pins, unlocked):  # noqa: F811
    async def broken():
        raise RuntimeError("pw-dump hung")

    client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), find_devices=broken))
    response = await client.get("/api/settings/config/devices", headers=unlocked)
    assert response.status == 200
    fields = {f["path"]: f for f in (await response.json())["fields"]}
    assert fields["audio.output_device"]["choices"] == [{"value": "auto", "label": "Automatic"}]


# --- Test the switch


class LaptopToTest:
    def __init__(self):
        self.tests = 0

    async def test_switch(self):
        self.tests += 1
        return {"ok": True, "lines": ["Pin: GPIO17 (header pin 11)", "Pressed: the MeetUp is now on the laptop side (2.1 s)."]}


async def test_test_the_switch_answers_the_checks_lines(client_factory, pins, unlocked):  # noqa: F811
    laptop = LaptopToTest()
    meeting = StubMeeting()
    client = await client_factory(make_service(pin_store=pins, laptop=laptop, meeting=meeting))
    response = await client.post("/api/settings/laptop/test", json={}, headers=unlocked)
    assert response.status == 200 and (await response.json())["lines"][1].startswith("Pressed")
    meeting.set_state(MeetingState.CONNECTED)
    response = await client.post("/api/settings/laptop/test", json={}, headers=unlocked)
    assert response.status == 409 and await response.json() == {"error": "Leave the meeting first"}
    assert laptop.tests == 1
    assert (await client.post("/api/settings/laptop/test", data="x", headers=unlocked)).status == 415


async def test_test_the_switch_in_a_room_whose_switch_is_off_says_to_turn_it_on(client_factory, pins, unlocked):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins, meeting=StubMeeting()))
    response = await client.post("/api/settings/laptop/test", json={}, headers=unlocked)
    assert response.status == 409
    assert (await response.json())["error"] == "The Laptop switch is off: turn it on, save, and restart the room software to test it"


async def test_a_form_that_is_not_one_is_refused_in_words(client, admin, unlocked):
    headers = dict(unlocked, **{"Content-Type": "multipart/form-data; boundary=xyz"})
    response = await client.post("/api/settings/credentials/google", data=b"no boundary here", headers=headers)
    assert response.status == 400 and (await response.json())["error"] in ("The upload could not be read: choose the key file again",
                                                                           "Choose a key file to upload")
    assert admin.calls == []
