"""Settings › Check and Logs on the room page (spec 2026-10-29, section 3): a button for each check and what it says in a block that scrolls
inside itself; the log newest first, All or Warnings and errors, and Load more; both fit the touch Pi's 1280 by 800, and Logs says "Not
available on this device" without the root helper. The checks run on stand-ins; the helper is its real jobs with a fake journalctl.
Driven with the Playwright Chromium, as test_page.py is."""

import asyncio
import getpass
import grp
import json
import os
from datetime import datetime

import pytest

from croom.admin.jobs import Host, Jobs
from croom.core.config import Config
from croom.settings.checks import Checks
from tests.unit.calendar.test_check import FakeProvider
from tests.unit.calendar.test_check import config_for as calendar_config
from tests.unit.control.test_page import PageServer, browser, fits  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_settings_credentials_api import CONFIG, JobsAdmin
from tests.unit.control.test_settings_page import unlocked_page

playwright = pytest.importorskip("playwright.sync_api")

AT_US = 1_800_000_000_000_000     # a moment the entries are written at, in microseconds


class Journal:
    """journalctl as the helper runs it: `pages` are what it prints, one list of lines for each time it is asked; `calls` what it was."""

    def __init__(self, *pages):
        self.pages, self.calls = list(pages), []

    def __call__(self, args):
        self.calls.append(list(args))
        lines = self.pages.pop(0) if self.pages else []
        return 0, "\n".join(lines)


def entry(text, priority=6, n=0):
    return json.dumps({"MESSAGE": text, "PRIORITY": str(priority), "__REALTIME_TIMESTAMP": str(AT_US - n * 1_000_000),
                       "__CURSOR": f"s=ab;i={n}"})


def helper(tmp_path, journal):
    admin = JobsAdmin(tmp_path)
    admin.config.write_text(CONFIG)
    admin.jobs = Jobs(Host(paths=admin.paths, owner=getpass.getuser(), group=grp.getgrgid(os.getgid()).gr_name, run=journal,
                           background=lambda work: None))
    return admin


def shown_time(n=0):
    """How the page shows the time of entry n: the TV Pi's own time (here, this machine's)."""
    at = datetime.fromtimestamp(AT_US / 1_000_000 - n)
    return f"{at:%b} {at.day} {at:%H:%M:%S}"


def open_tool(page, key):
    page.click(f"#settings-menu [data-page='{key}']")
    page.wait_for_selector(f"#settings-page .{key}-page", timeout=5000)


def checks_on_stand_ins(tmp_path):
    config = calendar_config(tmp_path)
    config.data_dir = str(tmp_path)
    checks = Checks(lambda: config, calendar_provider=FakeProvider)

    async def long_tv(out):
        await asyncio.sleep(0.4)                               # long enough for the page to show it running
        for n in range(80):
            print(f"Line {n} of what the TV check found", file=out)
        return 1
    checks._runs["tv"] = long_tv
    return checks


def test_the_check_page_runs_each_check_and_shows_what_it_says(browser, tmp_path):  # noqa: F811
    with PageServer(checks=checks_on_stand_ins(tmp_path)) as server:      # no root helper: Check works all the same
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_tool(page, "check")
        assert [b.inner_text().strip() for b in page.locator(".check-run").all()] == \
            ["Calendar", "TV", "Laptop switch", "Google sign-in", "Zoom keys"]
        assert fits(page)
        page.click(".check-run[data-check='calendar']")
        page.wait_for_function("document.querySelector('.check-verdict').textContent === 'Calendar: passed'", timeout=8000)
        assert page.locator(".check-output").inner_text().startswith("Google Calendar: connected as ")
        assert page.locator(".check-run[data-check='calendar']").get_attribute("aria-pressed") == "true"
        page.click(".check-run[data-check='tv']")
        page.wait_for_function("document.querySelector('.check-verdict').textContent === 'TV: checking…'", timeout=5000)
        assert not page.locator(".check-output").is_visible()
        page.wait_for_function("document.querySelector('.check-verdict').textContent === 'TV: did not pass'", timeout=8000)
        output = page.locator(".check-output")
        assert output.inner_text().splitlines()[79] == "Line 79 of what the TV check found"
        assert page.evaluate("() => { const o = document.querySelector('.check-output'); return o.scrollHeight > o.clientHeight; }")
        assert fits(page)                                                  # the lines scroll inside the block, not the page
        family = page.evaluate("getComputedStyle(document.querySelector('.check-output')).fontFamily")
        assert "monospace" in family
        page.click(".check-run[data-check='calendar']")                    # the last answer is kept, and the check runs again
        page.wait_for_function("document.querySelector('.check-verdict').textContent.startsWith('Calendar: ')", timeout=5000)
        page.click(".check-run[data-check='laptop']")
        page.wait_for_function("document.querySelector('.check-verdict').textContent === 'Laptop switch: did not pass'", timeout=8000)
        assert page.locator(".check-output").inner_text() == "Laptop mode is off (the Laptop switch page): the room page has no Laptop button."
        assert errors == []
        page.close()


def test_a_check_another_screen_is_running_says_already_running(browser, tmp_path):  # noqa: F811
    checks = Checks(lambda: Config())

    async def slow(out):
        await asyncio.sleep(2)
        return 0
    checks._runs["zoom"] = slow
    with PageServer(checks=checks) as server:
        first, second = unlocked_page(browser, server), unlocked_page(browser, server)
        open_tool(first, "check")
        open_tool(second, "check")
        first.click(".check-run[data-check='zoom']")
        first.wait_for_function("document.querySelector('.check-verdict').textContent === 'Zoom keys: checking…'", timeout=5000)
        second.click(".check-run[data-check='zoom']")
        second.wait_for_function("document.querySelector('.check-verdict').textContent === 'Zoom keys: Already running'", timeout=5000)
        first.wait_for_function("document.querySelector('.check-verdict').textContent === 'Zoom keys: passed'", timeout=8000)
        first.close()
        second.close()


def test_the_logs_page_shows_the_log_newest_first_and_loads_more(browser, tmp_path):  # noqa: F811
    first = [entry("2026-10-30 10:42:13,512 - croom.tv.service - ERROR - The TV went away", 3, 0),
             entry("2026-10-30 10:42:12,001 - croom.tv.service - WARNING - The TV is slow", 4, 1)] + \
            [entry(f"Line {n}", 6, n) for n in range(2, 200)]
    older = [entry("Started croom.service - Crystal Meet room agent.", 6, 200)]
    journal = Journal(first, older, [entry("2026-10-30 10:42:13,512 - croom.tv.service - ERROR - The TV went away", 3, 0)])
    admin = helper(tmp_path, journal)
    with PageServer(admin=admin, config_file=admin.config) as server:
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_tool(page, "logs")
        page.wait_for_selector(".log-entry", timeout=5000)
        rows = page.locator(".log-entry")
        assert rows.count() == 200
        assert rows.nth(0).locator(".log-time").inner_text() == shown_time(0)
        assert rows.nth(0).locator(".log-level").inner_text() == "Error"
        assert rows.nth(0).locator(".log-text").inner_text() == "croom.tv.service - The TV went away"
        assert rows.nth(1).get_attribute("data-level") == "warning" and rows.nth(2).locator(".log-text").inner_text() == "Line 2"
        assert page.locator(".logs-level[data-level='all']").get_attribute("aria-pressed") == "true"
        assert fits(page)                                                  # 200 lines scroll inside the block
        assert page.evaluate("() => { const l = document.querySelector('.logs-list'); return l.scrollHeight > l.clientHeight; }")
        page.locator(".logs-more").click()
        page.wait_for_function("document.querySelectorAll('.log-entry').length === 201", timeout=5000)
        assert rows.nth(200).locator(".log-text").inner_text() == "Started croom.service - Crystal Meet room agent."
        assert journal.calls[1][-1] == "--after-cursor=s=ab;i=199"
        assert page.locator(".logs-more").count() == 0 and page.locator(".logs-end").inner_text() == "The start of the log"
        page.click(".logs-level[data-level='warning']")
        page.wait_for_function("document.querySelectorAll('.log-entry').length === 1", timeout=5000)
        assert "--priority=warning" in journal.calls[2] and not any(a.startswith("--after-cursor") for a in journal.calls[2])
        assert page.locator(".logs-level[data-level='warning']").get_attribute("aria-pressed") == "true"
        page.click(".logs-level[data-level='all']")                        # the journal has nothing more to say: the page says so
        page.wait_for_function("document.querySelector('.logs-page .settings-loading').textContent === 'Nothing in the log'",
                               timeout=5000)
        assert fits(page) and errors == []
        page.close()


def test_the_logs_page_without_the_helper_is_not_available_on_this_device(browser, tmp_path):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server)
        open_tool(page, "logs")
        page.wait_for_function("document.querySelector('.logs-page .settings-loading').textContent === 'Not available on this device'",
                               timeout=5000)
        assert fits(page)
        page.close()


def test_check_and_logs_fit_a_phone_with_no_sideways_scroll(browser, tmp_path):  # noqa: F811
    journal = Journal([entry("2026-10-30 10:42:13,512 - croom.x - WARNING - " + "a long line " * 40, 4, 0)])
    admin = helper(tmp_path, journal)
    with PageServer(admin=admin, config_file=admin.config, checks=checks_on_stand_ins(tmp_path)) as server:
        page = unlocked_page(browser, server)
        page.set_viewport_size({"width": 390, "height": 844})
        open_tool(page, "logs")
        page.wait_for_selector(".log-entry", timeout=5000)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.click("#back-from-settings")
        open_tool(page, "check")
        page.click(".check-run[data-check='calendar']")
        page.wait_for_function("document.querySelector('.check-verdict').textContent === 'Calendar: passed'", timeout=8000)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()
