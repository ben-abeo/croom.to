"""
What GitHub says about the room software (spec 2026-10-29, section 6): the newest commit of `main` on `ben-abeo/croom.to`, and the
commit titles between the version a device runs and it. Settings › Software shows them; the root helper asks whether a version it is
told to install is on `main` at all.

GitHub's API is asked without a sign-in (60 requests an hour from one address, which a room with a 10-minute cache stays well under).
Every answer is kept for 10 minutes, and a failure for a minute, so that a page that looks every few seconds asks GitHub once. When
GitHub does not answer, or answers with something that is not what it should be, the words are "Can't check for updates right now".

The HTTP is injected: `fetch(url) -> (status, body)`, raising OSError when there is no answer at all. The real one is `fetch_url`
(urllib); the tests give a fake one. Nothing here is async: the control service asks from a thread, the helper from its own.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

REPO = "ben-abeo/croom.to"
BRANCH = "main"
API = "https://api.github.com"
CACHE_S = 10 * 60                 # an answer is kept this long
FAILURE_CACHE_S = 60              # and a failure this long: a page that polls does not spend the hour's 60 requests
FETCH_TIMEOUT_S = 10
MAX_CHANGES = 50                  # the titles a page lists; beyond them it says how many more
CANT_CHECK = "Can't check for updates right now"
COMMIT = re.compile(r"[0-9a-f]{40}")

Fetch = Callable[[str], Tuple[int, bytes]]


class GitHubError(Exception):
    """GitHub did not give an answer that can be used. `status` is its HTTP status, when it gave one (404: it does not know that
    commit)."""

    def __init__(self, words: str = CANT_CHECK, status: Optional[int] = None):
        super().__init__(words)
        self.status = status


def fetch_url(url: str) -> Tuple[int, bytes]:
    """GET `url` from GitHub's API: (status, body). An HTTP error status is an answer too; no answer at all raises OSError."""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "crystal-meet-room",
                                                   "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except OSError:
            body = b""
        return e.code, body


def short(commit: str) -> str:
    """A commit as people read it: its first 7 characters (a release that is not named by a commit keeps its name)."""
    return commit[:7] if is_commit(commit) else commit


def is_commit(value: Any) -> bool:
    return isinstance(value, str) and bool(COMMIT.fullmatch(value))


def _commit(raw: Any) -> Dict[str, str]:
    """One of the API's commit objects as the page shows it: {"commit", "short", "title" (the message's first line), "date"}."""
    if not isinstance(raw, dict) or not is_commit(raw.get("sha")):
        raise GitHubError()
    detail = raw.get("commit") if isinstance(raw.get("commit"), dict) else {}
    message = detail.get("message") if isinstance(detail.get("message"), str) else ""
    who = detail.get("committer") if isinstance(detail.get("committer"), dict) else {}
    date = who.get("date") if isinstance(who.get("date"), str) else ""
    title = message.strip().splitlines()[0].strip() if message.strip() else ""
    return {"commit": raw["sha"], "short": short(raw["sha"]), "title": title[:200], "date": date}


class GitHub:
    """
    Args:
        fetch: the HTTP GET (fetch_url unless a test stands in for it).
        clock: monotonic seconds, for the cache.
        repo, branch: where the room software's versions are.
    """

    def __init__(self, fetch: Fetch = fetch_url, clock: Callable[[], float] = time.monotonic, repo: str = REPO, branch: str = BRANCH):
        self._fetch = fetch
        self._clock = clock
        self._repo = repo
        self._branch = branch
        self._cache: Dict[str, Tuple[float, Any]] = {}     # url -> (until, the JSON or the GitHubError)
        self._lock = threading.Lock()

    def _get(self, path: str) -> Any:
        url = f"{API}/repos/{self._repo}/{path}"
        with self._lock:
            kept = self._cache.get(url)
            if kept and self._clock() < kept[0]:
                if isinstance(kept[1], GitHubError):
                    raise kept[1]
                return kept[1]
        try:
            status, body = self._fetch(url)
            if status != 200:
                raise GitHubError(status=status)
            answer = json.loads(body.decode("utf-8"))
        except GitHubError as e:
            self._keep(url, e, FAILURE_CACHE_S)
            raise
        except (OSError, ValueError) as e:   # no answer, or one that is not JSON (UnicodeDecodeError is a ValueError)
            failure = GitHubError()
            self._keep(url, failure, FAILURE_CACHE_S)
            raise failure from e
        self._keep(url, answer, CACHE_S)
        return answer

    def _keep(self, url: str, value: Any, seconds: float) -> None:
        with self._lock:
            self._cache[url] = (self._clock() + seconds, value)

    def newest(self) -> Dict[str, str]:
        """The newest commit of the branch: {"commit", "short", "title", "date"}; GitHubError when GitHub can't say."""
        return _commit(self._get(f"commits/{self._branch}"))

    def compare(self, base: str, head: str) -> Dict[str, Any]:
        """What is in `head` that is not in `base`: {"status" (ahead: base is in head's history; identical; behind; diverged), "base"
        (the base commit), "commits" (newest first, at most MAX_CHANGES), "more" (how many beyond those)}."""
        answer = self._get(f"compare/{base}...{head}")
        if not isinstance(answer, dict) or not isinstance(answer.get("commits"), list):
            raise GitHubError()
        commits = [_commit(raw) for raw in answer["commits"]][::-1]
        total = answer.get("total_commits")
        total = total if isinstance(total, int) and total >= len(commits) else len(commits)
        status = answer.get("status") if isinstance(answer.get("status"), str) else ""
        return {"status": status, "base": _commit(answer.get("base_commit")), "commits": commits[:MAX_CHANGES],
                "more": total - min(len(commits), MAX_CHANGES)}

    def on_branch(self, commit: str) -> bool:
        """Whether `commit` is in the branch's history (the newest, or one before it): only such a version is installed."""
        if not is_commit(commit):
            return False
        try:
            return self.compare(commit, self._branch)["status"] in ("ahead", "identical")
        except GitHubError as e:
            if e.status == 404:   # GitHub knows no such commit
                return False
            raise

    def releases(self) -> list:
        """The repository's releases as GitHub lists them (the newest 50; the door sign's APKs are attached to them, croom.settings.apk);
        GitHubError when GitHub can't say."""
        answer = self._get("releases?per_page=50")
        if not isinstance(answer, list):
            raise GitHubError()
        return answer

    def release(self, tag: str) -> Dict[str, Any]:
        """The release tagged `tag`; GitHubError (status 404 when there is none)."""
        answer = self._get(f"releases/tags/{tag}")
        if not isinstance(answer, dict):
            raise GitHubError()
        return answer

    @property
    def repo(self) -> str:
        return self._repo

    def summary(self, running: Optional[str]) -> Dict[str, Any]:
        """What Settings › Software shows of GitHub for a device that runs `running` (a commit, or "unknown"):
        {"newest": the newest commit or None, "running": that commit's title and date when GitHub knows it, "changes": the commits
        between, newest first (None when they can't be listed), "more", "up_to_date" (None when unknown), "error": CANT_CHECK or None}.
        Never raises."""
        answer: Dict[str, Any] = {"newest": None, "running": None, "changes": None, "more": 0, "up_to_date": None, "error": None}
        try:
            newest = self.newest()
        except GitHubError:
            answer["error"] = CANT_CHECK
            return answer
        answer["newest"] = newest
        if not is_commit(running):
            return answer
        answer["up_to_date"] = running == newest["commit"]
        try:
            between = self.compare(running, newest["commit"])
        except GitHubError as e:
            if e.status != 404:   # a commit GitHub does not know (a version built by hand) has no list; anything else is GitHub
                answer["error"] = CANT_CHECK
            return answer
        answer["running"] = between["base"]
        answer["changes"] = between["commits"]
        answer["more"] = between["more"]
        return answer

