"""Settings › Software's routes (spec 2026-10-29, sections 3, 6 and 8): GET /api/settings/software (the TV Pi's version, its last update
and what GitHub says), POST /api/settings/software/update {device, commit}, POST /api/settings/software/rollback {device} and POST
/api/settings/reboot {device}. All behind the PIN; the POSTs are refused in a meeting ("Leave the meeting first"); without the root helper
the TV Pi's card says "Not available on this device". The helper is its real jobs on a temporary folder with fake commands and a fake
GitHub; the agent's GitHub is the real client on a fake transport."""

import getpass
import grp
import os

import pytest

from croom.admin.jobs import Host, Jobs
from croom.meeting.providers.base import MeetingState
from croom.settings.github import CANT_CHECK, GitHub
from croom.settings.pin import PinStore
from tests.unit.admin.test_update import A, B, P, Clock, FakeGitHub, Probe, Runner, link, release, write_state
from tests.unit.control.test_service import StubMeeting, client_factory, make_service  # noqa: F401  (client_factory is a fixture)
from tests.unit.control.test_settings_credentials_api import CONFIG, JobsAdmin
from tests.unit.settings.test_github import NEWEST, REPO, Transport, commit, compare

PIN = "1234"
ROUTES = (("get", "/api/settings/software"), ("post", "/api/settings/software/update"), ("post", "/api/settings/software/rollback"),
          ("post", "/api/settings/reboot"))


@pytest.fixture
def pins(tmp_path):
    store = PinStore(tmp_path / "settings-pin.json")
    store.set_pin(PIN)
    return store


@pytest.fixture
def unlocked(pins):
    return {"X-Croom-Unlock": pins.unlock(PIN)}


class Helper(JobsAdmin):
    """The root helper on a TV Pi that runs release A with P kept before it; its background work is kept in `queued` to run when a
    test says (an update answers before it builds)."""

    def __init__(self, root, on_main=True):
        super().__init__(root)
        self.config.write_text(CONFIG)
        release(self.paths, A)
        release(self.paths, P)
        link(self.paths, A)
        write_state(self.paths, {"previous_release": P})
        self.runner, self.clock, self.queued = Runner(), Clock(), []
        self.jobs = Jobs(Host(paths=self.paths, owner=getpass.getuser(), group=grp.getgrgid(os.getgid()).gr_name, run=self.runner,
                              run_long=self.runner, probe=Probe(), clock=self.clock, sleep=self.clock.sleep, monotonic=self.clock,
                              background=self.queued.append, github=FakeGitHub(on_main)))

    def work(self):
        while self.queued:
            self.queued.pop(0)()


@pytest.fixture
def helper(tmp_path):
    return Helper(tmp_path)


def github_with_changes():
    answers = dict(NEWEST)
    answers[f"{REPO}/compare/{A}...{B}"] = (200, compare(A, B, [commit("1" * 40, "fix: one"), commit(B, "feat(settings): the Software page")]))
    return GitHub(fetch=Transport(answers))


def service(pins, admin=None, github=None, meeting=None):
    return make_service(pin_store=pins, admin=admin, github=github, meeting=meeting or StubMeeting())


async def test_every_software_route_asks_for_the_pin_first(client_factory, pins, helper):  # noqa: F811
    client = await client_factory(service(pins, helper, github_with_changes()))
    for method, path in ROUTES:
        response = await getattr(client, method)(path, json={"device": "tv", "commit": B}) if method == "post" else await client.get(path)
        assert response.status == 401 and (await response.json())["error"] == "Enter the PIN", path
    assert helper.calls == []


async def test_the_tv_pis_card_says_its_version_the_newest_and_the_changes(client_factory, pins, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, helper, github_with_changes()))
    response = await client.get("/api/settings/software", headers=unlocked)
    assert response.status == 200
    answer = await response.json()
    tv = answer["tv"]
    assert answer["in_meeting"] is False
    assert tv["available"] is True and tv["version"] == A and tv["previous"] == P and tv["installed"]
    assert tv["newest"]["commit"] == B and tv["up_to_date"] is False and tv["error"] is None
    assert [c["title"] for c in tv["changes"]] == ["feat(settings): the Software page", "fix: one"]
    assert tv["running"]["title"] == "The running one" and tv["update"] == {"state": "none"}


async def test_without_github_the_card_says_it_cant_check(client_factory, pins, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, helper))
    tv = (await (await client.get("/api/settings/software", headers=unlocked)).json())["tv"]
    assert tv["version"] == A and tv["newest"] is None and tv["error"] == "Can't check for updates right now"
    transport = Transport(NEWEST)
    transport.down = True
    client = await client_factory(service(pins, helper, GitHub(fetch=transport)))
    tv = (await (await client.get("/api/settings/software", headers=unlocked)).json())["tv"]
    assert tv["error"] == CANT_CHECK and tv["version"] == A


async def test_without_the_helper_the_tv_pis_card_is_not_available(client_factory, pins, unlocked):  # noqa: F811
    transport = Transport(NEWEST)
    client = await client_factory(service(pins, None, GitHub(fetch=transport)))
    answer = await (await client.get("/api/settings/software", headers=unlocked)).json()
    assert answer["tv"] == {"available": False, "error": "Not available on this device"}
    assert transport.asked == []
    for method, path in ROUTES[1:]:
        response = await client.post(path, json={"device": "tv", "commit": B}, headers=unlocked)
        assert response.status == 503 and (await response.json())["error"] == "Not available on this device", path


async def test_update_asks_the_helper_and_answers_at_once(client_factory, pins, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, helper, github_with_changes()))
    response = await client.post("/api/settings/software/update", json={"device": "tv", "commit": B}, headers=unlocked)
    assert response.status == 200 and await response.json() == {"updating": True}
    assert helper.calls == [("update", {"commit": B})]
    tv = (await (await client.get("/api/settings/software", headers=unlocked)).json())["tv"]
    assert tv["update"]["state"] == "building" and tv["update"]["commit"] == B
    helper.work()
    tv = (await (await client.get("/api/settings/software", headers=unlocked)).json())["tv"]
    assert tv["update"]["state"] == "done" and tv["version"] == B and tv["previous"] == A
    assert tv["update"]["lines"][-1] == "The room is running bbbbbbb"


@pytest.mark.parametrize("body, status, words", [
    ({"device": "tv"}, 400, "Send the version to install (a commit)"),
    ({"device": "tv", "commit": "main"}, 400, "Send the version to install (a commit)"),
    ({"device": "fridge", "commit": B}, 404, "There is no device fridge"),
    ([1, 2], 400, "Send a JSON object"),
])
async def test_update_refusals(client_factory, pins, helper, unlocked, body, status, words):  # noqa: F811
    client = await client_factory(service(pins, helper))
    response = await client.post("/api/settings/software/update", json=body, headers=unlocked)
    assert response.status == status and (await response.json())["error"] == words
    assert helper.calls == []


async def test_the_helpers_refusal_is_said_in_its_words(client_factory, pins, tmp_path, unlocked):  # noqa: F811
    helper = Helper(tmp_path, on_main=False)
    client = await client_factory(service(pins, helper))
    response = await client.post("/api/settings/software/update", json={"device": "tv", "commit": B}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "That version is not on main on GitHub"
    response = await client.post("/api/settings/software/update", json={"device": "tv", "commit": A}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "This TV Pi runs that version already"


async def test_posts_must_be_json(client_factory, pins, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, helper))
    for _, path in ROUTES[1:]:
        response = await client.post(path, data="device=tv", headers=unlocked)
        assert response.status == 415, path
    assert helper.calls == []


@pytest.mark.parametrize("state", [MeetingState.JOINING, MeetingState.CONNECTED, MeetingState.IN_LOBBY, MeetingState.LEAVING])
async def test_update_switch_back_restart_and_reboot_are_refused_in_a_meeting(client_factory, pins, helper, unlocked, state):  # noqa: F811
    meeting = StubMeeting()
    meeting.state = state
    client = await client_factory(service(pins, helper, meeting=meeting))
    for path in ("/api/settings/software/update", "/api/settings/software/rollback", "/api/settings/reboot", "/api/settings/restart"):
        response = await client.post(path, json={"device": "tv", "commit": B}, headers=unlocked)
        assert response.status == 409 and (await response.json())["error"] == "Leave the meeting first", path
    assert helper.calls == []
    answer = await (await client.get("/api/settings/software", headers=unlocked)).json()
    assert answer["in_meeting"] is True


async def test_reboot_asks_the_helper(client_factory, pins, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, helper))
    response = await client.post("/api/settings/reboot", json={"device": "tv"}, headers=unlocked)
    assert response.status == 200 and await response.json() == {"rebooting": True}
    assert helper.calls == [("reboot", {})]
    helper.work()
    assert ["systemctl", "reboot"] in helper.runner.calls
    response = await client.post("/api/settings/reboot", json={"device": "door"}, headers=unlocked)
    assert response.status == 404


async def test_switch_back_asks_the_helper(client_factory, pins, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, helper))
    response = await client.post("/api/settings/software/rollback", json={"device": "tv"}, headers=unlocked)
    assert response.status == 200 and await response.json() == {"rolling_back": True}
    helper.work()
    tv = (await (await client.get("/api/settings/software", headers=unlocked)).json())["tv"]
    assert tv["version"] == P and tv["previous"] == A


async def test_reboot_and_restart_are_refused_while_an_update_runs(client_factory, pins, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, helper))
    await client.post("/api/settings/software/update", json={"device": "tv", "commit": B}, headers=unlocked)
    for path in ("/api/settings/reboot", "/api/settings/restart", "/api/settings/software/update"):
        response = await client.post(path, json={"device": "tv", "commit": B}, headers=unlocked)
        assert response.status == 409 and (await response.json())["error"] == "An update is under way: wait for it to finish", path


def test_from_config_asks_the_real_github(monkeypatch):
    from croom.control.service import ControlService
    from croom.core.config import Config

    class Fake:
        pass
    fake = Fake()
    fake.available = lambda: False
    assert isinstance(ControlService.from_config(Config(), admin=fake)._github, GitHub)


async def test_an_update_waits_for_a_saved_change_to_be_restarted_with(client_factory, pins, helper, unlocked):  # noqa: F811
    helper.jobs._update_state(last_write=helper.clock() - 60)
    client = await client_factory(service(pins, helper))
    response = await client.post("/api/settings/software/update", json={"device": "tv", "commit": B}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "Restart now first, then update"
