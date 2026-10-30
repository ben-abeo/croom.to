"""The room's key files from Settings (spec 2026-10-29, section 4): what a key is (in words with nothing secret in them), what the page is
told about the file the config names, and the Zoom keys typed at the page."""

import json
import os

import pytest

from croom.settings.credentials import CredentialError, describe, summary, typed_zoom
from croom.settings.schema import SettingError

GOOGLE_KEY = {"type": "service_account", "project_id": "croom", "private_key_id": "abc123",
              "client_email": "room@croom.iam.gserviceaccount.com", "client_id": "1234567890",
              "private_key": "-----BEGIN PRIVATE KEY-----\nMIIsecret\n-----END PRIVATE KEY-----\n"}
ZOOM = {"sdk_client_id": "sdkid", "sdk_client_secret": "sdksecret"}
ZOOM_S2S = dict(ZOOM, account_id="acc", s2s_client_id="s2sid", s2s_client_secret="s2ssecret", room_user="room@crystalpm.com")


def test_a_google_keys_summary_names_its_client_email_and_nothing_else():
    assert summary("google", GOOGLE_KEY) == "Key for room@croom.iam.gserviceaccount.com"


def test_zoom_summaries():
    assert summary("zoom", ZOOM) == "Zoom Meeting SDK keys"
    assert summary("zoom", ZOOM_S2S) == "Zoom Meeting SDK keys, joining as room@crystalpm.com"


@pytest.mark.parametrize("kind, data, words", [
    ("google", ZOOM, "This is a Zoom credentials file, not a Google service-account key"),
    ("zoom", GOOGLE_KEY, "This is a Google service-account key, not Zoom credentials"),
    ("google", {"installed": {}}, "This is a Google OAuth client file, not a Google service-account key"),
    ("google", dict(GOOGLE_KEY, private_key=""), "This Google key has no private_key: download a new key for the service account"),
    ("zoom", {"sdk_client_id": "REPLACE_ME", "sdk_client_secret": "x"}, "These Zoom credentials have no sdk_client_id (or it is still REPLACE)"),
    ("google", [1, 2], "This is not a JSON file: a key file is the .json file the console downloads"),
    ("calendar", GOOGLE_KEY, "The key must be a Google service-account key or Zoom credentials"),
])
def test_a_key_that_is_not_what_it_is_saved_as_is_refused_naming_what_it_is(kind, data, words):
    with pytest.raises(CredentialError, match="^" + words.replace("(", r"\(").replace(")", r"\)") + "$"):
        summary(kind, data)


def test_describe_tells_what_a_saved_key_is_and_when_never_its_secrets(tmp_path):
    path = tmp_path / "google-service-account.json"
    path.write_text(json.dumps(GOOGLE_KEY))
    os.utime(path, (1_800_000_000, 1_800_000_000))
    described = describe("google", str(path))
    assert described["summary"] == "Key for room@croom.iam.gserviceaccount.com" and described["path"] == str(path)
    assert described["saved_at"].startswith("2027-01-15")
    for secret in ("MIIsecret", "abc123", "1234567890"):
        assert secret not in json.dumps(described)


def test_describe_says_what_is_wrong_with_the_file_the_config_names(tmp_path):
    assert describe("google", "") is None and describe("zoom", None) is None
    assert describe("google", "keys/google.json")["problem"] == "keys/google.json is not a file's full path: upload the key again"
    missing = str(tmp_path / "gone.json")
    assert describe("google", missing)["problem"] == f"There is no file at {missing}: upload the key again"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert describe("google", str(broken))["problem"] == f"{broken} can't be read as a key file: upload the key again"
    zoom = tmp_path / "zoom.json"
    zoom.write_text(json.dumps(ZOOM))
    assert describe("google", str(zoom)) == {"path": str(zoom), "problem": "This is a Zoom credentials file, not a Google service-account key: "
                                                                            "upload the key again"}


def test_typed_zoom_keys_make_the_file_and_keep_the_rooms_own_zoom_user():
    assert json.loads(typed_zoom(" newid ", "newsecret")) == {"sdk_client_id": "newid", "sdk_client_secret": "newsecret"}
    kept = json.loads(typed_zoom("newid", "newsecret", ZOOM_S2S))
    assert kept == dict(ZOOM_S2S, sdk_client_id="newid", sdk_client_secret="newsecret")
    half = dict(ZOOM, account_id="acc")                                   # half a server-to-server set is not the room's user: left out
    assert json.loads(typed_zoom("a", "b", half)) == {"sdk_client_id": "a", "sdk_client_secret": "b"}


def test_typed_zoom_keys_are_checked_by_field():
    with pytest.raises(SettingError) as refused:
        typed_zoom("", "has space")
    assert refused.value.errors == {"key": "The SDK key can't be blank",
                                    "secret": "The SDK secret is one word of at most 200 characters, as the Zoom app's page shows it"}
    with pytest.raises(SettingError):
        typed_zoom(None, "x" * 201)
