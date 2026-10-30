"""Settings › Software on the room page (spec 2026-10-29, sections 3, 6 and 8): the TV Pi's card with its version, the newest on GitHub and
the changes between, Update with the helper's progress lines, what came of the last update (undone by itself when the room did not start
with it), Switch back, Restart room software and Reboot; refused in a meeting; "Not available on this device" without the root helper;
"Can't check for updates right now" without GitHub; a place for the other devices' cards; it fits the touch Pi's 1280 by 800. The helper is
a scripted stand-in whose record a test moves along; GitHub is the real client on a fake transport. Driven with the Playwright Chromium."""

import pytest

from croom.admin.client import AdminError, AdminUnavailable
from croom.meeting.providers.base import MeetingState
from croom.settings.github import GitHub
from tests.unit.control.test_page import PageServer, browser, fits  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_settings_page import unlocked_page
from tests.unit.settings.test_github import NEWEST, REPO, Transport, commit, compare

playwright = pytest.importorskip("playwright.sync_api")

A = "a" * 40
B = "b" * 40
P = "c" * 40


class ScriptedHelper:
    """The root helper's client, answering the Software jobs from what a test sets: `version`, `previous`, `record` (update_status);
    `refuse` maps a job to the words it is refused with; `calls` lists what was asked."""

    def __init__(self):
        self.version, self.previous, self.record = A, P, {"state": "none"}
        self.refuse = {}
        self.calls = []
        self.gone = False

    def available(self):
        return not self.gone

    async def call(self, job, **args):
        if self.gone:
            raise AdminUnavailable("Not available on this device")
        self.calls.append((job, args))
        if job in self.refuse:
            raise AdminError(self.refuse[job])
        if job == "version":
            return {"ok": True, "version": self.version, "installed": "2026-10-20T09:00:00-04:00", "previous": self.previous}
        if job == "update_status":
            return {"ok": True, **self.record}
        if job == "update":
            self.record = {"state": "building", "commit": args["commit"], "from": self.version, "lines": [], "at": "2026-10-30T10:00:00-04:00"}
            return {"ok": True, "updating": True}
        if job == "rollback":
            self.record = {"state": "switching", "commit": self.previous, "from": self.version, "lines": [], "rollback": True}
            return {"ok": True, "rolling_back": True}
        if job == "reboot":
            return {"ok": True, "rebooting": True}
        if job in ("restart", "restart_status"):
            return {"ok": True, "restarting": True, "state": "none", "undone": False}
        raise AssertionError(f"unexpected job {job}")

    def jobs(self):
        return [job for job, _ in self.calls if job not in ("version", "update_status", "restart_status")]


def github(changes=2, more=0):
    answers = dict(NEWEST)
    commits = [commit(f"{n + 1:040x}", f"change number {n + 1}") for n in range(changes - 1)] + [commit(B, "feat(settings): the Software page")]
    answers[f"{REPO}/compare/{A}...{B}"] = (200, compare(A, B, commits, total=changes + more))
    answers[f"{REPO}/compare/{B}...{B}"] = (200, compare(B, B, [], status="identical"))
    return GitHub(fetch=Transport(answers))


def open_software(page):
    page.click("#settings-menu [data-page='software']")
    page.wait_for_selector("#settings-page .software-card[data-device='tv']:not([hidden])", timeout=5000)


def text(page, selector):
    return page.locator(selector).inner_text().strip()


def wait_text(page, selector, words, timeout=8000):
    page.wait_for_function("([s, w]) => { const n = document.querySelector(s); return n && n.textContent.trim() === w; }",
                           arg=[selector, words], timeout=timeout)


def test_the_tv_pis_card_shows_its_version_the_newest_and_the_changes(browser):  # noqa: F811
    helper = ScriptedHelper()
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_software(page)
        assert text(page, ".software-title") == "TV Pi"
        assert text(page, ".software-version") == "Version aaaaaaa, 28 Oct 2026"        # the commit's date, from GitHub
        assert text(page, ".software-newest") == "Newest: bbbbbbb, 30 Oct 2026"
        assert [t.strip() for t in page.locator(".software-change-title").all_inner_texts()] == \
            ["feat(settings): the Software page", "change number 1"]
        assert page.locator(".software-update").is_visible()
        assert text(page, ".software-back") == "Switch back to ccccccc"
        assert page.locator(".software-restart").is_visible() and text(page, ".software-restart") == "Restart room software"
        assert text(page, ".software-reboot") == "Reboot"
        assert not page.locator(".software-notice").is_visible() and not page.locator(".software-progress").is_visible()
        assert page.locator(".software-devices").count() == 1                            # where the touch Pi's and door sign's cards go
        assert fits(page)
        assert errors == []
        page.close()


def test_update_shows_the_helpers_progress_and_then_the_version_it_runs(browser):  # noqa: F811
    helper = ScriptedHelper()
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.click(".software-update")
        page.wait_for_function("document.querySelector('.software-message').textContent === 'Updating'", timeout=5000)
        assert ("update", {"commit": B}) in helper.calls
        helper.record = dict(helper.record, lines=["Making a new environment for bbbbbbb", "Installing bbbbbbb from GitHub: this takes several minutes"])
        page.wait_for_function("document.querySelectorAll('.software-line').length === 2", timeout=8000)
        assert page.locator(".software-progress").is_visible()
        assert not page.locator(".software-update").is_visible() and not page.locator(".software-back").is_visible()
        assert fits(page)
        helper.version, helper.previous = B, A
        helper.record = dict(helper.record, state="done", lines=helper.record["lines"] + ["The room is running bbbbbbb"])
        wait_text(page, ".software-newest", "Up to date")
        assert text(page, ".software-version").startswith("Version bbbbbbb")
        assert text(page, ".software-notice") == "Updated to bbbbbbb on 30 Oct 2026"
        assert not page.locator(".software-update").is_visible() and not page.locator(".software-changes").is_visible()
        assert text(page, ".software-back") == "Switch back to aaaaaaa"
        assert page.locator(".software-message").inner_text() == ""
        page.close()


def test_an_update_the_room_did_not_start_with_says_it_was_undone(browser):  # noqa: F811
    helper = ScriptedHelper()
    helper.record = {"state": "rolled_back", "commit": B, "from": A, "at": "2026-10-30T10:05:00-04:00",
                     "why": "The room did not answer within 3 minutes of starting bbbbbbb",
                     "lines": ["Switched to bbbbbbb", "The room did not answer within 3 minutes of starting bbbbbbb: going back to aaaaaaa",
                               "Back on aaaaaaa"]}
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        assert text(page, ".software-notice") == \
            "The update to bbbbbbb was undone: the room did not start with it. It is back on aaaaaaa."
        assert page.locator(".software-notice").get_attribute("title") == "The room did not answer within 3 minutes of starting bbbbbbb"
        assert page.locator(".software-line").count() == 3
        assert page.locator(".software-update").is_visible()            # the newest may be tried again
        page.close()


def test_a_build_that_failed_says_why_and_that_the_room_is_as_it_was(browser):  # noqa: F811
    helper = ScriptedHelper()
    helper.record = {"state": "failed", "commit": B, "from": A, "why": "ERROR: No matching distribution found for evdev",
                     "lines": ["Installing bbbbbbb from GitHub failed: the room is still on aaaaaaa"]}
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        assert text(page, ".software-notice") == ("The update to bbbbbbb did not happen: ERROR: No matching distribution found for evdev. "
                                                  "The room is still on aaaaaaa.")
        page.close()


def test_update_restart_and_reboot_are_refused_in_a_meeting(browser):  # noqa: F811
    helper = ScriptedHelper()
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server, stopped=True)   # no status poll moves the page to the meeting screen meanwhile
        open_software(page)
        server._loop.call_soon_threadsafe(server.meeting.set_state, MeetingState.CONNECTED)
        for selector in (".software-update", ".software-restart"):
            page.evaluate("document.querySelector('.software-message').textContent = ''")
            page.click(selector)
            wait_text(page, ".software-message", "Leave the meeting first", timeout=5000)
        page.evaluate("document.querySelector('.software-message').textContent = ''")
        page.click(".software-reboot")
        page.click(".software-reboot")
        wait_text(page, ".software-message", "Leave the meeting first", timeout=5000)
        assert helper.jobs() == []
        page.close()


def test_reboot_needs_a_second_tap(browser):  # noqa: F811
    helper = ScriptedHelper()
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.click(".software-reboot")
        assert text(page, ".software-reboot") == "Tap again to reboot"
        assert helper.jobs() == []
        page.click(".software-reboot")
        wait_text(page, ".software-message", "Rebooting the TV Pi…", timeout=5000)
        assert helper.jobs() == ["reboot"]
        assert text(page, ".software-reboot") == "Reboot"
        page.close()


def test_restart_room_software_and_switch_back(browser):  # noqa: F811
    helper = ScriptedHelper()
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.click(".software-restart")
        wait_text(page, ".software-message", "Restarting the room software…", timeout=5000)
        assert helper.jobs() == ["restart"]
        page.click("#back-from-settings")                                 # leaving the page stops its waiting
        open_software(page)
        page.click(".software-back")
        wait_text(page, ".software-message", "Switching back to ccccccc", timeout=5000)
        assert helper.jobs() == ["restart", "rollback"]
        page.close()


def test_a_helper_refusal_is_said_in_its_words(browser):  # noqa: F811
    helper = ScriptedHelper()
    helper.refuse["update"] = "That version is not on main on GitHub"
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.click(".software-update")
        wait_text(page, ".software-message", "That version is not on main on GitHub", timeout=5000)
        page.close()


def test_without_the_helper_the_tv_pis_card_is_not_available(browser):  # noqa: F811
    with PageServer(github=github()) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        assert text(page, ".software-version") == "Not available on this device"
        for selector in (".software-update", ".software-restart", ".software-reboot", ".software-newest", ".software-changes"):
            assert not page.locator(selector).is_visible(), selector
        page.click("#back-from-settings")
        page.click("#settings-menu [data-page='security']")                # the other pages work all the same
        page.wait_for_selector("#settings-page .pin", timeout=5000)
        page.close()


def test_without_github_the_card_says_it_cant_check(browser):  # noqa: F811
    helper = ScriptedHelper()
    with PageServer(admin=helper) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        assert text(page, ".software-newest") == "Can't check for updates right now"
        assert text(page, ".software-version") == "Version aaaaaaa, installed 20 Oct 2026"
        assert not page.locator(".software-update").is_visible()
        assert page.locator(".software-reboot").is_visible()
        page.close()


def test_a_long_list_of_changes_and_lines_fits_the_touch_pi_and_a_phone(browser):  # noqa: F811
    helper = ScriptedHelper()
    helper.record = {"state": "rolled_back", "commit": B, "from": A, "why": "x" * 300,
                     "lines": [f"Line {n} of what the helper did, which can be quite long when it quotes pip" for n in range(40)]}
    with PageServer(admin=helper, github=github(changes=60, more=12)) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        assert page.locator(".software-change").count() == 51               # 50 titles, and "and 22 more"
        assert text(page, ".software-more") == "and 22 more"
        assert fits(page)
        for selector in (".software-changes", ".software-progress"):
            assert page.evaluate(f"(() => {{ const n = document.querySelector('{selector}'); return n.scrollHeight > n.clientHeight; }})()")
        card = page.locator(".software-card").bounding_box()
        box = page.locator("#settings-page").bounding_box()
        assert card["y"] + card["height"] <= box["y"] + box["height"] + 1   # the card is whole on the screen: its lists scroll inside it
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()


def test_another_devices_card_is_drawn_under_the_tv_pis(browser):  # noqa: F811
    helper = ScriptedHelper()
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        page.evaluate("croomSettings.softwareCard((box, answer) => { box.textContent = 'Touch Pi beside ' + answer.tv.version.slice(0, 7); })")
        open_software(page)
        assert text(page, ".software-devices") == "Touch Pi beside aaaaaaa"
        page.close()


def test_while_the_update_restarts_the_room_the_page_waits_for_it_to_come_back(browser):  # noqa: F811
    helper = ScriptedHelper()
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.click(".software-update")
        page.wait_for_function("document.querySelector('.software-message').textContent === 'Updating'", timeout=5000)
        # The room software goes away for its restart: the page's readings fail, and so does GET /api/status, for a while
        page.route("**/api/settings/software", lambda route: route.abort())
        page.route("**/api/status", lambda route: route.abort())
        wait_text(page, ".software-message", "Restarting the room software…")
        helper.version, helper.previous = B, A
        helper.record = dict(helper.record, state="done", lines=["The room is running bbbbbbb"])
        page.wait_for_timeout(2500)
        assert text(page, ".software-message") == "Restarting the room software…"   # a failure meanwhile is the restart, not an error
        page.unroute("**/api/settings/software")
        page.unroute("**/api/status")
        wait_text(page, ".software-newest", "Up to date")
        assert text(page, ".software-notice") == "Updated to bbbbbbb on 30 Oct 2026"
        page.close()


def test_with_no_release_to_switch_back_to_the_card_offers_none(browser):  # noqa: F811
    """The helper names no release to go back to (none kept, or one whose own helper does not start: a release from before room
    settings): no Switch back."""
    helper = ScriptedHelper()
    helper.previous = None
    with PageServer(admin=helper, github=github()) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        assert page.locator(".software-update").is_visible()
        assert not page.locator(".software-back").is_visible()
        page.close()
