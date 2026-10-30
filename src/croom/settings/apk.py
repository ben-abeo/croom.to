"""
The door sign app's releases (room settings spec 2026-10-29, section 6): each is an APK attached to a GitHub release of the repository
tagged door-sign-<versionCode>, as the asset APK_ASSET. The TV Pi lists them for Settings › Software (the newest, and the releases
between the one a panel runs and it), downloads the one a panel is to install into its data directory, and serves it to that panel
(GET /api/devices/apk/<versionCode>, only to a door sign that has joined). The panel installs it itself, as the device owner, after
checking that it is the door sign app, signed with the same key, and newer than the one it runs.

A download is checked before it is kept: its size is the one GitHub states, its SHA-256 the one GitHub states (the asset's "digest",
or a line "SHA-256: <hex>" in the release's notes) when it states one, and it is a zip (an APK is one). It is written beside the cache
and renamed into place whole, with its size and SHA-256 next to it, so a download cut off half way is never served; at each start of a
download what is not whole is removed. The two newest are kept.

The HTTP is injected: `download(url, path, max_bytes)` writes the file or raises OSError; the tests give a fake one.
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from croom.settings.github import CANT_CHECK, GitHub, GitHubError

logger = logging.getLogger(__name__)

APK_ASSET = "app-release.apk"
TAG = re.compile(r"door-sign-([1-9][0-9]{0,9})")
SHA256_LINE = re.compile(r"sha-?256\s*[:=]\s*([0-9a-fA-F]{64})\b", re.IGNORECASE)
MAX_APK_BYTES = 200 * 1024 * 1024
KEEP = 2
DOWNLOAD_TIMEOUT_S = 60
MAX_CHANGES = 50

Download = Callable[[str, Path, int], None]


class ApkError(Exception):
    """A release that can't be had: the words for the person, and the HTTP status for the route (404 no such release, 409 a release
    that is not right, 503 GitHub not answering)."""

    def __init__(self, words: str, status: int = 409):
        super().__init__(words)
        self.status = status


def download_url(url: str, path: Path, max_bytes: int) -> None:
    """GET `url` (following GitHub's redirect to its file store) into `path`; OSError when there is no whole answer."""
    request = urllib.request.Request(url, headers={"User-Agent": "crystal-meet-room", "Accept": "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response, open(path, "wb") as out:
        if response.status != 200:
            raise OSError(f"GitHub answered {response.status}")
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OSError("the file is larger than a door sign app can be")
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())


def version_code(tag: Any) -> Optional[int]:
    """door-sign-12 -> 12; None for any other tag."""
    match = TAG.fullmatch(tag) if isinstance(tag, str) else None
    return int(match.group(1)) if match else None


def _stated_sha256(release: Dict[str, Any], asset: Dict[str, Any]) -> Optional[str]:
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:") and re.fullmatch(r"[0-9a-fA-F]{64}", digest[7:]):
        return digest[7:].lower()
    body = release.get("body")
    match = SHA256_LINE.search(body) if isinstance(body, str) else None
    return match.group(1).lower() if match else None


def parse_release(release: Any) -> Optional[Dict[str, Any]]:
    """A GitHub release as a door sign release: {"version_code", "tag", "title", "date", "size", "sha256" (None when not stated)}; None
    for a release that is not one (another tag, a draft or a pre-release, no APK_ASSET)."""
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        return None
    code = version_code(release.get("tag_name"))
    if code is None:
        return None
    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    asset = next((a for a in assets if isinstance(a, dict) and a.get("name") == APK_ASSET), None)
    size = asset.get("size") if asset else None
    if asset is None or not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_APK_BYTES:
        return None
    title = release.get("name") if isinstance(release.get("name"), str) and release.get("name").strip() else release["tag_name"]
    date = release.get("published_at") if isinstance(release.get("published_at"), str) else ""
    return {"version_code": code, "tag": release["tag_name"], "title": title.strip()[:200], "date": date, "size": size,
            "sha256": _stated_sha256(release, asset)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ApkStore:
    """
    Args:
        github: the GitHub client (its releases, cached as its other answers are).
        directory: where the APKs are kept (the data directory's door-sign/).
        download: the HTTP download (download_url unless a test stands in for it).
    """

    def __init__(self, github: GitHub, directory: Path, download: Download = download_url):
        self._github = github
        self.directory = Path(directory)
        self._download = download
        self._lock = threading.Lock()

    # --- what GitHub has

    def releases(self) -> List[Dict[str, Any]]:
        """The door sign releases, newest first; GitHubError when GitHub can't say."""
        found = [parse_release(r) for r in self._github.releases()]
        return sorted((r for r in found if r is not None), key=lambda r: -r["version_code"])

    def summary(self, running: Any) -> Dict[str, Any]:
        """What Settings › Software shows of a door sign that runs `running` (its versionCode, as it says it in its check-ins):
        {"newest": the newest release or None, "changes": the releases newer than the running one, newest first, "more", "up_to_date"
        (None when unknown), "error": CANT_CHECK or None}. Never raises."""
        answer: Dict[str, Any] = {"newest": None, "running": None, "changes": None, "more": 0, "up_to_date": None, "error": None}
        try:
            releases = self.releases()
        except GitHubError:
            answer["error"] = CANT_CHECK
            return answer
        if not releases:
            return answer
        answer["newest"] = releases[0]
        code = int(running) if isinstance(running, str) and running.isdigit() else None
        if code is None:
            return answer
        answer["up_to_date"] = code >= releases[0]["version_code"]
        answer["running"] = next((r for r in releases if r["version_code"] == code), None)
        newer = [r for r in releases if r["version_code"] > code]
        answer["changes"] = newer[:MAX_CHANGES]
        answer["more"] = max(0, len(newer) - MAX_CHANGES)
        return answer

    # --- the APK a panel installs

    def _paths(self, code: int):
        return self.directory / f"door-sign-{code}.apk", self.directory / f"door-sign-{code}.json"

    def cached(self, code: int) -> Optional[Dict[str, Any]]:
        """The APK of release `code` when it is here whole: {"path", "sha256", "size", "version_code"}; None otherwise."""
        apk, meta = self._paths(code)
        try:
            info = json.loads(meta.read_text())
            size = apk.stat().st_size
        except (OSError, ValueError):
            return None
        if not isinstance(info, dict) or info.get("size") != size or not isinstance(info.get("sha256"), str):
            return None
        return {"path": apk, "sha256": info["sha256"], "size": size, "version_code": code}

    def ensure(self, code: Any) -> Dict[str, Any]:
        """The APK of release door-sign-<code>, downloaded from GitHub when it is not here already (and checked: size, SHA-256, a zip):
        {"path", "sha256", "size", "version_code"}; ApkError in words otherwise."""
        if not isinstance(code, int) or isinstance(code, bool) or code <= 0:
            raise ApkError("Send the version of the door sign app (its version code)", 400)
        with self._lock:
            kept = self.cached(code)
            if kept is not None:
                if _sha256(kept["path"]) == kept["sha256"]:
                    return kept
                logger.warning(f"The door sign app {code} kept on the TV Pi is not whole any more: downloading it again")
            try:
                release = parse_release(self._github.release(f"door-sign-{code}"))
            except GitHubError as e:
                if e.status == 404:
                    raise ApkError(f"There is no door sign release {code} on GitHub", 404) from e
                raise ApkError(CANT_CHECK, 503) from e
            if release is None:
                raise ApkError(f"The door sign release {code} on GitHub has no {APK_ASSET}", 409)
            return self._fetch(code, release)

    def _fetch(self, code: int, release: Dict[str, Any]) -> Dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._sweep()
        url = f"https://github.com/{self._github.repo}/releases/download/{release['tag']}/{APK_ASSET}"
        fd, temp = tempfile.mkstemp(prefix=".partial-", suffix=".apk", dir=str(self.directory))
        os.close(fd)
        temp_path = Path(temp)
        try:
            try:
                self._download(url, temp_path, MAX_APK_BYTES)
            except OSError as e:
                logger.warning(f"Could not download the door sign app {code} from {url}: {e}")
                raise ApkError(CANT_CHECK, 503) from e
            size = temp_path.stat().st_size
            if size != release["size"]:
                raise ApkError(f"The door sign app {code} from GitHub is {size} bytes, not the {release['size']} GitHub states", 409)
            with open(temp_path, "rb") as f:
                if f.read(4) != b"PK\x03\x04":
                    raise ApkError(f"The door sign app {code} from GitHub is not an APK", 409)
            sha256 = _sha256(temp_path)
            if release["sha256"] and sha256 != release["sha256"]:
                raise ApkError(f"The door sign app {code} from GitHub is not the one its release states (SHA-256)", 409)
            apk, meta = self._paths(code)
            os.chmod(temp_path, 0o644)
            os.replace(temp_path, apk)
            self._write_meta(meta, {"version_code": code, "sha256": sha256, "size": size, "tag": release["tag"]})
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()
        logger.info(f"Downloaded the door sign app {code} ({size} bytes, SHA-256 {sha256})")
        self._prune(keep=code)
        return {"path": apk, "sha256": sha256, "size": size, "version_code": code}

    @staticmethod
    def _write_meta(meta: Path, data: Dict[str, Any]) -> None:
        fd, temp = tempfile.mkstemp(prefix=".meta-", dir=str(meta.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, meta)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise

    def _codes(self) -> List[int]:
        codes = set()
        for path in self.directory.glob("door-sign-*.apk"):
            code = version_code(path.stem)
            if code is not None:
                codes.add(code)
        return sorted(codes, reverse=True)

    def _sweep(self) -> None:
        """Remove downloads cut off half way, and APKs without their size and SHA-256 beside them."""
        for path in list(self.directory.glob(".partial-*")) + list(self.directory.glob(".meta-*")):
            with contextlib.suppress(OSError):
                path.unlink()
        for code in self._codes():
            if self.cached(code) is None:
                for path in self._paths(code):
                    with contextlib.suppress(OSError):
                        path.unlink()

    def _prune(self, keep: int) -> None:
        """Keep the KEEP newest APKs (and `keep`, the one just made)."""
        for code in self._codes()[KEEP:]:
            if code == keep:
                continue
            for path in self._paths(code):
                with contextlib.suppress(OSError):
                    path.unlink()
