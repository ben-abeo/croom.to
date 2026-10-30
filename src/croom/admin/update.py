"""
The TV Pi's software updates (spec 2026-10-29, section 6): the helper's jobs `update {commit}`, `update_status` and `rollback`, and
what it does at its start about an update that was cut short.

The layout, under the install folder (/opt/croom):

    releases/<commit>/venv      a release: a venv with that commit of the room software installed, root's, readable by all
    releases/<commit>/VERSION   the commit, written last: a release folder without it is unfinished
    venv -> releases/<commit>/venv    the running release, which croom.service and croom-admin.service run from
    VERSION                     a copy of the running release's, for a person reading it over SSH

The room's user can't change any of it (the helper runs as root from it). The installer makes the layout, moving an older venv into a
release on its first run.

An update builds the new release beside the running one (python -m venv; pip install git+...@<commit>; the meeting browser that
version's Playwright wants; a check that it imports), writing what it is doing as lines in admin-state.json; then switches the link
at once (a new link renamed over the old), restarts the room software and watches it: not answering GET /api/status within 3 minutes,
the link goes back and the room software restarts on the release before. Two releases are kept: the running one and the one before
it. A build that fails or is cut off never touched the running release, and its folder is removed (at once, or at the helper's next
start). The record: admin-state.json's "update", {"state", "commit", "from", "lines", "at", "why"?}, where state is one of
building, switching, restarting, watching, rolling_back (under way), done, failed, rolled_back.

`rollback` switches to the release kept before the running one, and watches it the same way.
"""

import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from croom.admin.jobs import HEALTH_TIMEOUT_S, UPDATE_UNDER_WAY, JobError, Jobs, _only
from croom.settings.github import CANT_CHECK, GitHubError, is_commit, short

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/ben-abeo/croom.to.git"
UPDATE_TIMEOUT_S = 3 * 60         # how long a room on a new release has to answer GET /api/status
MAX_LINES = 40                    # the progress lines kept in the record
MAX_WHY = 300                     # characters of a command's own words in a failure
# What the installer and an update call a release: its commit; "unknown" for a venv moved in whose commit pip did not record; local-<time>
# for one the installer built from another source (CROOM_REPO)
RELEASE_NAME = re.compile(r"[0-9a-f]{40}|unknown|local-[0-9]+")
UNDER_WAY = ("building", "switching", "restarting", "watching", "rolling_back")
NEEDS_INSTALLER = "This TV Pi can't update itself until its installer has been run once more (it sets up the release folders)"
RESTARTING_NOW = "The room is restarting: wait for it to come back"
# The command that checks a new release before it is used: the agent and the helper import with its libraries
IMPORT_CHECK = "import croom.core.agent, croom.admin.jobs"


def _words(output: str) -> str:
    """The end of a command's output, on one line: what a failed pip or venv says last is what went wrong."""
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    return " ".join(lines[-3:])[-MAX_WHY:]


class Updater:
    """The update jobs of one helper: they share its Jobs' locks, state file, restart and health watch."""

    def __init__(self, jobs: Jobs):
        self._jobs = jobs
        self._host = jobs._host

    @property
    def _paths(self):
        return self._host.paths

    # --- the releases on disk

    def running(self) -> Optional[str]:
        """The running release's name (a commit, or "unknown" for one the installer moved in without knowing its commit); None when
        the venv is not a link into releases/ (a TV Pi whose installer has not set the layout up)."""
        link = self._paths.venv_link
        if not link.is_symlink():
            return None
        target = Path(os.path.normpath(link.parent / os.readlink(link)))
        if target.name != "venv" or target.parent.parent != Path(os.path.normpath(self._paths.releases)):
            return None
        name = target.parent.name
        return name if RELEASE_NAME.fullmatch(name) else None

    def _release(self, name: str) -> Path:
        return self._paths.releases / name

    def _complete(self, name: Optional[str]) -> bool:
        return bool(name) and (self._release(name) / "VERSION").is_file() and (self._release(name) / "venv").is_dir()

    def previous(self) -> Optional[str]:
        """The release kept before the running one, when it is still there and whole."""
        name = self._jobs._read_state().get("previous_release")
        if isinstance(name, str) and RELEASE_NAME.fullmatch(name) and name != self.running() and self._complete(name):
            return name
        return None

    def version(self) -> Dict[str, Any]:
        """{"version": the running commit or "unknown", "installed": when its release was finished (ISO 8601) or None, "previous"}."""
        name = self.running()
        version_file = self._release(name) / "VERSION" if name else self._paths.version_file
        try:
            version = version_file.read_text().strip()
            installed = datetime.fromtimestamp(version_file.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError:
            version, installed = "", None
        return {"version": version or "unknown", "installed": installed, "previous": self.previous()}

    def _switch(self, name: str) -> None:
        """Point the venv link at release `name` in one step: a new link beside it, renamed over it (a reader sees the old link or the
        new, never none). The VERSION beside it follows."""
        link = self._paths.venv_link
        temp = link.with_name(f".venv-{name}.{os.getpid()}")
        temp.unlink(missing_ok=True)
        os.symlink(os.path.join("releases", name, "venv"), temp)
        try:
            os.replace(temp, link)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        try:
            self._jobs._place(self._paths.version_file, (self._release(name) / "VERSION").read_bytes(), 0o644, owner=False)
        except OSError as e:
            logger.warning(f"Could not copy the running release's VERSION beside it: {e}")

    def _remove(self, name: str) -> None:
        """Remove a release folder (never the running one or the one kept before it: the callers make sure, and this checks)."""
        if name in (self.running(), self._jobs._read_state().get("previous_release")):
            return
        path = self._release(name)
        try:
            if path.is_symlink() or not path.is_dir():
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path)
        except OSError as e:
            logger.warning(f"Could not remove the release folder {path}: {e}")

    def sweep(self) -> List[str]:
        """Remove what an update left unfinished: release folders without their VERSION, and links it was making. The running release
        and the one before it are never touched. The names removed."""
        removed = []
        for temp in self._paths.install_dir.glob(".venv-*"):
            if temp.is_symlink():
                temp.unlink(missing_ok=True)
        if not self._paths.releases.is_dir():
            return removed
        keep = {self.running(), self._jobs._read_state().get("previous_release")}
        for path in sorted(self._paths.releases.iterdir()):
            if path.name in keep or self._complete(path.name):
                continue
            self._remove(path.name)
            removed.append(path.name)
        if removed:
            logger.warning(f"Removed unfinished releases: {', '.join(removed)}")
        return removed

    def _prune(self) -> None:
        """Keep two releases, the running one and the one before it: every other folder goes."""
        if not self._paths.releases.is_dir():
            return
        keep: Set[Optional[str]] = {self.running(), self._jobs._read_state().get("previous_release")}
        for path in sorted(self._paths.releases.iterdir()):
            if path.name not in keep:
                self._remove(path.name)

    # --- the record

    def _now(self) -> str:
        return datetime.fromtimestamp(self._host.clock()).astimezone().isoformat(timespec="seconds")

    def _record(self, **changes: Any) -> None:
        record = self._jobs._read_state().get("update")
        record = dict(record) if isinstance(record, dict) else {}
        record.update(changes, at=self._now())
        self._jobs._update_state(update=record)

    def _line(self, words: str, **changes: Any) -> None:
        """A progress line for the page (and the log), with any change to the record."""
        logger.info(f"Update: {words}")
        record = self._jobs._read_state().get("update")
        lines = list(record.get("lines") or []) if isinstance(record, dict) else []
        self._record(lines=(lines + [words])[-MAX_LINES:], **changes)

    # --- the jobs

    def update_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """The last update's record (state none before any)."""
        _only(args)
        record = self._jobs._read_state().get("update")
        return record if isinstance(record, dict) else {"state": "none"}

    def _begin(self) -> None:
        """Claim the helper for an update: refused while one runs or a restart is watched."""
        with self._jobs._watch_lock:
            if self._jobs._updating:
                raise JobError(UPDATE_UNDER_WAY)
            if self._jobs._watching:
                raise JobError(RESTARTING_NOW)
            self._jobs._updating = True

    def _end(self) -> None:
        with self._jobs._watch_lock:
            self._jobs._updating = False
            self._jobs._watching = False

    def update(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Install `commit` (a commit of main on GitHub) as a new release and move the room to it, in the background: the answer goes
        at once, and update_status follows it."""
        _only(args, "commit")
        commit = args.get("commit")
        if not is_commit(commit):
            raise JobError("The version must be a commit: 40 characters, 0 to 9 and a to f")
        self._begin()
        try:
            running = self.running()
            if running is None:
                raise JobError(NEEDS_INSTALLER)
            if commit == running:
                raise JobError("This TV Pi runs that version already")
            try:
                on_main = self._host.github.on_branch(commit)
            except GitHubError as e:
                raise JobError(CANT_CHECK) from e
            if not on_main:
                raise JobError("That version is not on main on GitHub")
            self._jobs._update_state(update={"state": "building", "commit": commit, "from": running, "lines": [], "at": self._now(),
                                             "started": self._now()})
            self._host.background(lambda: self._run(commit, running, build=True))
        except BaseException:
            self._end()
            raise
        return {"updating": True}

    def rollback(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Switch to the release kept before the running one, and watch it as an update is watched (back again if it does not
        answer)."""
        _only(args)
        self._begin()
        try:
            running = self.running()
            if running is None:
                raise JobError(NEEDS_INSTALLER)
            previous = self.previous()
            if previous is None:
                raise JobError("There is no earlier version kept on this TV Pi")
            self._jobs._update_state(update={"state": "switching", "commit": previous, "from": running, "lines": [], "at": self._now(),
                                             "started": self._now(), "rollback": True})
            self._host.background(lambda: self._run(previous, running, build=False))
        except BaseException:
            self._end()
            raise
        return {"rolling_back": True}

    # --- the work, in the background

    def _run(self, commit: str, running: str, build: bool) -> None:
        """Build (unless the release is there whole), switch, restart, watch; never raises (the record says what happened)."""
        try:
            if build and not self._build(commit, running):
                return
            self._record(state="switching")
            self._switch(commit)
            self._line(f"Switched to {short(commit)}", state="restarting")
            self._after_switch(commit, running, restart=True)
        except Exception as e:  # noqa: BLE001 - the record must end, whatever went wrong
            logger.exception("The update failed")
            self._recover(commit, running, f"The helper failed: {type(e).__name__}")
        finally:
            self._end()

    def _build(self, commit: str, running: str) -> bool:
        """Make release `commit` beside the running one; False (the record says why, the folder is gone) when it could not be made."""
        name = short(commit)
        if self._complete(commit):
            self._line(f"{name} is on this TV Pi already")
            return True
        release = self._release(commit)
        self._remove(commit)   # a folder an earlier try left unfinished
        self._paths.releases.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chmod(self._paths.releases, 0o755)
        release.mkdir(mode=0o755)
        os.chmod(release, 0o755)
        venv = release / "venv"
        steps = [
            (f"Making a new environment for {name}", [self._host.python, "-m", "venv", str(venv)], True),
            (f"Installing {name} from GitHub: this takes several minutes",
             [str(venv / "bin" / "pip"), "install", "--no-input", "--disable-pip-version-check", f"git+{REPO_URL}@{commit}"], True),
            ("Installing the meeting browser it uses",
             ["runuser", "-u", self._host.owner, "--", "env", f"PLAYWRIGHT_BROWSERS_PATH={self._paths.browsers}",
              str(venv / "bin" / "playwright"), "install", "chromium"], True),
            ("Checking that it starts", [str(venv / "bin" / "python"), "-c", IMPORT_CHECK], False),
        ]
        for words, command, long in steps:
            self._line(words)
            code, output = (self._host.run_long if long else self._host.run)(command)
            if code != 0:
                why = _words(output) or f"{command[0]} said {code}"
                self._remove(commit)
                self._line(f"{words.split(':')[0]} failed: the room is still on {short(running)}", state="failed", why=why)
                logger.error(f"The update to {commit} failed at '{words}': {why}")
                return False
        self._jobs._place(release / "VERSION", (commit + "\n").encode("ascii"), 0o644, owner=False)
        return True

    def _after_switch(self, commit: str, running: str, restart: bool) -> None:
        """The room software restarted on release `commit` (restart: done here first) and watched for 3 minutes: done, or back to
        `running`."""
        with self._jobs._watch_lock:
            self._jobs._watching = True
        if restart:
            self._line("Restarting the room software", state="restarting")
            self._jobs._restart_room()
        self._line("Waiting for the room to answer (up to 3 minutes)", state="watching")
        if self._jobs._comes_back(UPDATE_TIMEOUT_S):
            self._jobs._update_state(previous_release=running)
            self._prune()
            self._line(f"The room is running {short(commit)}", state="done")
            logger.info(f"Updated to {commit}")
            # The helper itself runs from the link: started again, it is the new release's, with the jobs that release's room
            # software asks for. --no-block: systemctl would otherwise wait on the stop of this very process.
            code, output = self._host.run(["systemctl", "--no-block", "restart", "croom-admin"])
            if code != 0:
                logger.warning(f"Could not restart the helper on the new release: {output.strip()}")
            return
        self._recover(commit, running, f"The room did not answer within 3 minutes of starting {short(commit)}")

    def _recover(self, commit: str, running: str, why: str) -> None:
        """After a failure: when the link never moved off release `running`, the update failed and the room was not touched (what was
        built goes); otherwise back to `running` (_go_back). Never raises."""
        try:
            if self.running() == running:
                self._remove(commit)
                self._line(f"{why}: the room is still on {short(running)}", state="failed", why=why)
                return
        except Exception:  # noqa: BLE001
            logger.exception("Could not close the update's record")
            return
        self._go_back(commit, running, why)

    def _go_back(self, commit: str, running: str, why: str) -> None:
        """Back to release `running`: switched back, the room restarted and watched. The release that failed is removed (unless it is
        one of the two kept). Never raises."""
        try:
            self._line(f"{why}: going back to {short(running)}", state="rolling_back", why=why)
            if self.running() != running:
                self._switch(running)
            self._jobs._restart_room()
            self._prune()
            self._line(f"Back on {short(running)}", state="rolled_back")
            logger.error(f"The update to {commit} was undone: {why}")
            if not self._jobs._comes_back(HEALTH_TIMEOUT_S):
                self._record(why=why + f", and the room has not answered on {short(running)} either")
        except Exception:  # noqa: BLE001
            logger.exception("Could not go back to the release before the update")
            self._record(state="failed", why=why + f"; going back to {short(running)} failed too")

    def resume(self) -> None:
        """At the helper's start. An update cut off while it was building (a reboot, a power cut) never touched the running release:
        its record is closed and its folder removed. One cut off after the switch is taken up where it was: the room is watched (and
        restarted first if it may not have been), and taken back if it does not answer. Then the unfinished folders go."""
        record = self._jobs._read_state().get("update")
        record = record if isinstance(record, dict) else {}
        state, commit, running = record.get("state"), record.get("commit"), record.get("from")
        if state in UNDER_WAY and isinstance(commit, str) and isinstance(running, str):
            now = self.running()
            if state == "building" or (state == "switching" and now != commit):
                self._line(f"The update was cut off before it was finished: the room is still on {short(running)}", state="failed",
                           why="The update was cut off before it was finished")
            else:
                with self._jobs._watch_lock:
                    self._jobs._updating = True
                logger.warning(f"An update to {commit} was under way when the helper stopped: taking it up again")
                self._host.background(lambda: self._take_up(state, commit, running))
        self.sweep()

    def _take_up(self, state: str, commit: str, running: str) -> None:
        try:
            if state == "rolling_back":
                self._go_back(commit, running, (self._jobs._read_state().get("update") or {}).get("why") or "The update was undone")
                return
            self._after_switch(commit, running, restart=state in ("switching", "restarting"))
        except Exception as e:  # noqa: BLE001
            logger.exception("Taking up the update failed")
            self._recover(commit, running, f"The helper failed: {type(e).__name__}")
        finally:
            self._end()
