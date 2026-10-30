"""The door sign app's releases (croom.settings.apk, room settings spec 2026-10-29 section 6): GitHub releases tagged
door-sign-<versionCode> with the APK as app-release.apk; the newest and the releases between for Settings › Software; the APK downloaded,
checked (size, SHA-256 when GitHub states it, a zip) and kept whole in the data directory, the two newest kept. GitHub is the real client
on a fake transport; the download is a fake that writes what a test gives it."""

import hashlib
import json

import pytest

from croom.settings.apk import (
    APK_ASSET,
    MAX_APK_BYTES,
    ApkError,
    ApkStore,
    parse_release,
    version_code,
)
from croom.settings.github import CANT_CHECK, GitHub
from tests.unit.settings.test_github import REPO, Transport

APK = b"PK\x03\x04" + b"door sign app " * 100


def asset(content=APK, name=APK_ASSET, digest=True):
    item = {"name": name, "size": len(content), "browser_download_url": "https://evil.example.com/x.apk"}
    if digest:
        item["digest"] = "sha256:" + hashlib.sha256(content).hexdigest()
    return item


def a_release(code, content=APK, name=None, assets=None, body="", **more):
    return dict({"tag_name": f"door-sign-{code}", "name": name if name is not None else f"Door sign {code}", "draft": False,
                 "prerelease": False, "published_at": f"2026-10-{20 + code:02d}T10:00:00Z", "body": body,
                 "assets": assets if assets is not None else [asset(content)]}, **more)


class Download:
    """The APK download: `files` maps a URL to the bytes it gives; `down` makes it fail as a network would; `cut` writes half and
    fails. `asked` lists the URLs."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.asked = []
        self.down = False
        self.cut = False

    def __call__(self, url, path, max_bytes):
        self.asked.append(url)
        if self.down:
            raise OSError("Network is unreachable")
        content = self.files.get(url)
        if content is None:
            raise OSError("HTTP Error 404: Not Found")
        if len(content) > max_bytes:
            raise OSError("too large")
        if self.cut:
            path.write_bytes(content[: len(content) // 2])
            raise OSError("Connection reset by peer")
        path.write_bytes(content)


def url(code):
    return f"https://github.com/ben-abeo/croom.to/releases/download/door-sign-{code}/{APK_ASSET}"


def store_on(tmp_path, releases=(), files=None):
    answers = {f"{REPO}/releases?per_page=50": (200, list(releases))}
    for release in releases:
        answers[f"{REPO}/releases/tags/{release['tag_name']}"] = (200, release)
    transport = Transport(answers)
    download = Download(files if files is not None else {url(version_code(r["tag_name"])): APK for r in releases})
    return ApkStore(GitHub(fetch=transport), tmp_path / "door-sign", download=download), transport, download


def test_a_release_is_one_tagged_door_sign_with_the_apk():
    assert version_code("door-sign-12") == 12
    for tag in ("door-sign-0", "door-sign-012", "door-sign-x", "v1.0", "door-sign-12-beta", None):
        assert version_code(tag) is None, tag
    release = parse_release(a_release(3))
    assert release == {"version_code": 3, "tag": "door-sign-3", "title": "Door sign 3", "date": "2026-10-23T10:00:00Z", "size": len(APK),
                       "sha256": hashlib.sha256(APK).hexdigest()}
    assert parse_release(a_release(3, draft=True)) is None
    assert parse_release(a_release(3, prerelease=True)) is None
    assert parse_release(a_release(3, assets=[asset(name="app-debug.apk")])) is None
    assert parse_release(a_release(3, assets=[dict(asset(), size=0)])) is None
    assert parse_release(a_release(3, assets=[dict(asset(), size=MAX_APK_BYTES + 1)])) is None
    assert parse_release(dict(a_release(3), tag_name="v3")) is None


def test_the_sha256_is_the_assets_digest_or_a_line_in_the_notes():
    digest = hashlib.sha256(b"other").hexdigest()
    assert parse_release(a_release(3, assets=[asset(digest=False)]))["sha256"] is None
    assert parse_release(a_release(3, assets=[asset(digest=False)], body=f"Fixes.\n\nSHA-256: {digest.upper()}\n"))["sha256"] == digest
    assert parse_release(a_release(3, name=" "))["title"] == "door-sign-3"


def test_the_summary_lists_the_newest_and_the_releases_between(tmp_path):
    store, _, _ = store_on(tmp_path, [a_release(2), a_release(5, name="Wi-Fi from Settings"), a_release(3), a_release(4, draft=True)])
    summary = store.summary("2")
    assert summary["newest"]["version_code"] == 5 and summary["up_to_date"] is False and summary["error"] is None
    assert [r["version_code"] for r in summary["changes"]] == [5, 3]
    assert summary["changes"][0]["title"] == "Wi-Fi from Settings"
    assert summary["running"]["version_code"] == 2
    assert store.summary("5")["up_to_date"] is True and store.summary("5")["changes"] == []
    assert store.summary("6")["up_to_date"] is True
    unknown = store.summary("")
    assert unknown["up_to_date"] is None and unknown["newest"]["version_code"] == 5


def test_no_releases_and_github_down(tmp_path):
    store, transport, _ = store_on(tmp_path, [])
    assert store.summary("2") == {"newest": None, "running": None, "changes": None, "more": 0, "up_to_date": None, "error": None}
    store, transport, _ = store_on(tmp_path, [a_release(2)])
    transport.down = True
    assert store.summary("2")["error"] == CANT_CHECK


def test_the_apk_is_downloaded_checked_and_kept(tmp_path):
    store, transport, download = store_on(tmp_path, [a_release(3)])
    apk = store.ensure(3)
    assert apk["sha256"] == hashlib.sha256(APK).hexdigest() and apk["size"] == len(APK) and apk["version_code"] == 3
    assert apk["path"].read_bytes() == APK
    # From the repository's own release address, never the URL the API gives
    assert download.asked == [url(3)]
    meta = json.loads((tmp_path / "door-sign" / "door-sign-3.json").read_text())
    assert meta == {"version_code": 3, "sha256": apk["sha256"], "size": len(APK), "tag": "door-sign-3"}
    # Kept: the second time neither GitHub nor the download is asked
    asked = len(transport.asked)
    assert store.ensure(3)["path"] == apk["path"]
    assert download.asked == [url(3)] and len(transport.asked) == asked
    assert sorted(p.name for p in (tmp_path / "door-sign").iterdir()) == ["door-sign-3.apk", "door-sign-3.json"]


def test_an_apk_that_is_not_what_github_states_is_refused(tmp_path):
    wrong = b"PK\x03\x04" + b"x" * (len(APK) - 4)
    store, _, _ = store_on(tmp_path, [a_release(3)], files={url(3): wrong})
    with pytest.raises(ApkError) as caught:
        store.ensure(3)
    assert "is not the one its release states" in str(caught.value) and caught.value.status == 409
    store, _, _ = store_on(tmp_path, [a_release(3)], files={url(3): APK + b"more"})
    with pytest.raises(ApkError) as caught:
        store.ensure(3)
    assert f"not the {len(APK)} GitHub states" in str(caught.value)
    html = b"<html>" + b" " * (len(APK) - 6)
    store, _, _ = store_on(tmp_path, [a_release(3, assets=[asset(html, digest=False)])], files={url(3): html})
    with pytest.raises(ApkError) as caught:
        store.ensure(3)
    assert str(caught.value) == "The door sign app 3 from GitHub is not an APK"
    assert sorted(p.name for p in (tmp_path / "door-sign").iterdir()) == []      # nothing half-made is kept


def test_a_release_without_a_digest_is_kept_with_the_sha256_it_has(tmp_path):
    store, _, _ = store_on(tmp_path, [a_release(3, assets=[asset(digest=False)])])
    assert store.ensure(3)["sha256"] == hashlib.sha256(APK).hexdigest()


def test_refusals_in_words(tmp_path):
    store, transport, download = store_on(tmp_path, [a_release(3, assets=[asset(name="other.apk")])])
    with pytest.raises(ApkError) as caught:
        store.ensure(4)
    assert str(caught.value) == "There is no door sign release 4 on GitHub" and caught.value.status == 404
    with pytest.raises(ApkError) as caught:
        store.ensure(3)
    assert str(caught.value) == "The door sign release 3 on GitHub has no app-release.apk"
    for bad in (0, -1, True, "3", None):
        with pytest.raises(ApkError) as caught:
            store.ensure(bad)
        assert caught.value.status == 400
    store, transport, download = store_on(tmp_path, [a_release(5)])
    download.down = True
    with pytest.raises(ApkError) as caught:
        store.ensure(5)
    assert str(caught.value) == CANT_CHECK and caught.value.status == 503
    store, transport, download = store_on(tmp_path, [a_release(6)])
    transport.down = True
    with pytest.raises(ApkError) as caught:
        store.ensure(6)
    assert str(caught.value) == CANT_CHECK and caught.value.status == 503


def test_a_download_cut_off_is_never_kept_and_is_swept(tmp_path):
    store, _, download = store_on(tmp_path, [a_release(3)])
    download.cut = True
    with pytest.raises(ApkError):
        store.ensure(3)
    assert list((tmp_path / "door-sign").iterdir()) == []
    # A power cut leaves a partial file, or an APK without its record: the next download sweeps them
    folder = tmp_path / "door-sign"
    (folder / ".partial-abc.apk").write_bytes(b"PK")
    (folder / "door-sign-2.apk").write_bytes(APK)
    download.cut = False
    store.ensure(3)
    assert sorted(p.name for p in folder.iterdir()) == ["door-sign-3.apk", "door-sign-3.json"]


def test_a_kept_apk_that_changed_is_downloaded_again(tmp_path):
    store, _, download = store_on(tmp_path, [a_release(3)])
    path = store.ensure(3)["path"]
    path.write_bytes(b"PK\x03\x04" + b"y" * (len(APK) - 4))
    assert store.ensure(3)["path"].read_bytes() == APK
    assert download.asked == [url(3), url(3)]


def test_the_two_newest_are_kept(tmp_path):
    store, _, _ = store_on(tmp_path, [a_release(n) for n in (2, 3, 4, 5)])
    for code in (2, 3, 4):
        store.ensure(code)
    names = sorted(p.name for p in (tmp_path / "door-sign").iterdir())
    assert names == ["door-sign-3.apk", "door-sign-3.json", "door-sign-4.apk", "door-sign-4.json"]
    store.ensure(2)        # an older one asked for again is kept until the next download
    assert (tmp_path / "door-sign" / "door-sign-2.apk").exists()
