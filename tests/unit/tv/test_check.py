"""croom --check-tv: what it says of the room's TV, and its exit code, on a fake TV and a stand-in for the search of the
network. The check looks and changes nothing: it never wakes the TV, never pairs, never switches an input, and leaves nothing
connected. Its words are what a person at a shell reads, so they are pinned."""
import asyncio
import io
import os
import subprocess
import sys

import pytest
import yaml
from croom.core.config import Config, TvConfig
from croom.tv import check
from croom.tv.check import check_tv
from croom.tv.errors import TvError, TvTimeout
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import DoingTv, LibraryLike, forgetful_tv, in_standby, service_for, silent_tv, within

KEY = "tv-client-key"
OFF_HINT = "Check that the TV is on and that tv.host is its address: a TV that is fully off does not answer."
PROMPT_HINT = ("If the TV is showing a prompt to allow this device, it no longer knows this device: Forget TV, then Pair, under "
               "Options > TV on the room's table.")


def room(tmp_path, factory=FakeTvClient, key="key-9", **tv):
    """A config with a TV in it, and a TV service on a fake TV (service.woken says what it was asked to wake), keeping its key
    where the config says the agent's data directory is."""
    settings = dict({"enabled": True, "host": "10.0.0.41", "mac": "AA:BB:CC:DD:EE:FF"}, **tv)
    config = Config(data_dir=str(tmp_path))
    config.tv = TvConfig(**settings)
    return config, service_for(tmp_path, factory, key=key, **settings)


async def run(config, service=None, discover=None):
    """The check, as a person reads it: its exit code, its words, and the configs it built a service from."""
    out, built = io.StringIO(), []

    def factory(given):
        built.append(given)
        if service is None:
            raise AssertionError("no TV service was wanted here")
        return service
    code = await check_tv(config, out=out, service_factory=factory, discover=discover)
    assert out.getvalue().isascii()                               # its own words print on any terminal (the other checks' do)
    return code, out.getvalue(), built


def showing(input_id):
    """A factory for a TV that is on and shows this input."""
    def build(host, key):
        client = DoingTv(host, key)
        client.tv_state.current_input, client.tv_state.current_app_id = input_id, DoingTv.APPS.get(input_id)
        return client
    return build


def unlisted(app):
    """A factory for a TV that is on with this app on its screen, and a list of inputs with none for it."""
    def build(host, key):
        client = DoingTv(host, key)
        client.tv_state.inputs, client.tv_state.current_input, client.tv_state.current_app_id = {}, None, app
        return client
    return build


def test_the_waits_are_the_ones_settled():
    assert check.CHECK_TIMEOUT_S == 5.0                           # the look at the TV is answered within this, or it is not
    assert check.LET_GO_TIMEOUT_S == 3.0                          # and the hang-up is over within this


# A TV that answers

async def test_a_paired_tv_that_answers_is_reported_with_its_name_its_state_and_its_inputs(tmp_path):
    config, service = room(tmp_path)
    code, text, built = await run(config, service)
    assert code == 0, text
    assert text.splitlines() == [
        "TV: 10.0.0.41, MAC AA:BB:CC:DD:EE:FF",
        f"The TV is paired with this device (its key is in {tmp_path / KEY}).",
        "The TV answers: Boardroom TV, on.",
        "Inputs: HDMI 1 (HDMI_1), HDMI 2 (HDMI_2).",
        "The TV shows HDMI 1 (HDMI_1), the room Pi's input (tv.pi_input).",
        "Ready: the TV answers.",
    ]
    assert built == [config]                                      # the service is the config's own


async def test_the_check_is_made_with_the_service_the_config_makes_when_none_is_given(tmp_path, monkeypatch):
    config, service = room(tmp_path)
    monkeypatch.setattr(check.TvService, "from_config", classmethod(lambda cls, given: service if given is config else None))
    out = io.StringIO()
    assert await check_tv(config, out=out) == 0 and "The TV answers: Boardroom TV" in out.getvalue()


async def test_a_tv_in_standby_answers_and_is_not_woken_or_turned_on_or_asked_what_it_shows(tmp_path):
    config, service = room(tmp_path, in_standby)
    code, text, _ = await run(config, service)
    assert code == 0, text
    assert "The TV answers: Boardroom TV, in standby (its screen is off)." in text
    assert "The TV shows" not in text and "Warning" not in text
    client = FakeTvClient.instances[-1]
    assert service.woken == [] and not [call for call in client.calls if call[0] in ("power_on", "power_off", "set_input", "button")]


async def test_a_tv_in_standby_is_not_asked_what_it_shows_and_what_it_last_showed_is_no_warning(tmp_path):
    def dark_on_another_input(host, key):
        client = in_standby(host, key)
        client.tv_state.current_input, client.tv_state.current_app_id = "HDMI_2", DoingTv.APPS["HDMI_2"]
        return client
    config, service = room(tmp_path, dark_on_another_input)
    code, text, _ = await run(config, service)
    assert code == 0 and "in standby" in text and "The TV shows" not in text and "Warning" not in text


async def test_an_input_with_no_label_of_its_own_is_listed_once(tmp_path):
    def plain(host, key):
        client = FakeTvClient(host, key)
        client.tv_state.inputs = {"HDMI_1": {"id": "HDMI_1", "label": "HDMI 1"}, "HDMI_3": {"id": "HDMI_3", "label": ""}}
        return client
    config, service = room(tmp_path, plain)
    code, text, _ = await run(config, service)
    assert code == 0 and "Inputs: HDMI 1 (HDMI_1), HDMI_3." in text


async def test_a_tv_that_gives_no_name_or_inputs_is_reported_as_such(tmp_path):
    def mute(host, key):
        client = FakeTvClient(host, key)
        client.tv_info.hello_info, client.tv_info.system_info = {}, {}
        client.tv_state.inputs, client.tv_state.current_input = {}, None
        return client
    config, service = room(tmp_path, mute)
    code, text, _ = await run(config, service)
    assert code == 0 and "The TV answers: it gives no name, on." in text and "The TV lists no inputs." in text


async def test_a_key_the_tv_issues_while_it_is_looked_at_is_kept_and_said(tmp_path):
    """Somebody accepts the TV's pairing prompt during the look (the stored key was not one it knew): the TV issues a new key, and the
    service keeps it, as it does at every connect, or the next start would show the prompt again. It is the one thing the check
    changes, so it says so."""
    class Renewing(FakeTvClient):
        async def connect(self):
            connected = await super().connect()
            self.client_key = "key-new"                           # the prompt was accepted: the TV hands out a key
            return connected
    config, service = room(tmp_path, Renewing)
    code, text, _ = await run(config, service)
    assert code == 0, text
    assert text.splitlines()[:4] == [
        "TV: 10.0.0.41, MAC AA:BB:CC:DD:EE:FF",
        f"The TV is paired with this device (its key is in {tmp_path / KEY}).",
        "The TV issued a new key (its pairing prompt was accepted during the check), which is kept.",
        "The TV answers: Boardroom TV, on.",
    ]
    assert (tmp_path / KEY).read_text() == "key-new"


async def test_a_key_that_the_tv_hands_back_as_it_was_is_not_news(tmp_path):
    config, service = room(tmp_path)
    code, text, _ = await run(config, service)
    assert code == 0 and "issued a new key" not in text and (tmp_path / KEY).read_text() == "key-9"


async def test_the_check_wakes_nothing_pairs_nothing_and_leaves_nothing_connected(tmp_path):
    config, service = room(tmp_path)
    code, _, _ = await run(config, service)
    client = FakeTvClient.instances[-1]
    assert code == 0 and service.woken == []
    assert [call[0] for call in client.calls if call[0] in ("connect", "paired", "disconnect")] == ["connect", "disconnect"]
    assert ("connect", "key-9") in client.calls                   # with the stored key, which is not changed
    assert (tmp_path / KEY).read_text() == "key-9"
    assert client.connected is False and service._client is None and service._task is None


# What the TV shows against the input the room Pi is on

async def test_a_tv_that_shows_another_input_than_the_room_pis_is_warned_about(tmp_path):
    config, service = room(tmp_path, showing("HDMI_2"))
    code, text, _ = await run(config, service)
    assert code == 0, text
    assert "The TV shows HDMI 2 (HDMI_2)." in text.splitlines()                  # (and not "the room Pi's input", which it is not)
    assert "Warning: The TV is not showing the room Pi's input HDMI_1 (tv.pi_input): a meeting join switches it." in text
    assert text.rstrip().endswith("The TV answers, with the warnings above.")
    assert not [call for call in FakeTvClient.instances[-1].calls if call[0] == "set_input"]       # looking is not switching


async def test_the_warning_says_the_join_leaves_the_tv_alone_when_it_is_not_to_be_kept_on(tmp_path):
    config, service = room(tmp_path, showing("HDMI_2"), keep_on=False)
    code, text, _ = await run(config, service)
    assert code == 0 and "tv.keep_on is false, so a meeting join leaves it" in text and "switches it" not in text


async def test_an_input_the_tv_does_not_list_is_warned_about_with_the_ones_it_does(tmp_path):
    config, service = room(tmp_path, pi_input="HDMI_9")
    code, text, _ = await run(config, service)
    assert code == 0, text
    assert ("Warning: tv.pi_input is HDMI_9, which is not one of the TV's inputs (HDMI_1, HDMI_2): a meeting join cannot switch the "
            "TV to the room Pi.") in text
    assert "switches it" not in text                              # (the other warning would be a promise it cannot keep)


async def test_an_unnamed_room_pi_input_is_warned_about(tmp_path):
    config, service = room(tmp_path, pi_input="")
    code, text, _ = await run(config, service)
    assert code == 0 and "Warning: tv.pi_input is not set, so a meeting join cannot switch the TV to the room Pi." in text


async def test_an_app_that_is_named_for_the_room_pis_input_is_the_room_pi_when_the_tv_lists_no_input_for_it(tmp_path):
    config, service = room(tmp_path, unlisted("com.webos.app.hdmi1"))
    code, text, _ = await run(config, service)
    assert code == 0, text
    assert "The TV shows the app com.webos.app.hdmi1, the room Pi's input (tv.pi_input)." in text and "Warning" not in text


@pytest.mark.parametrize("app", ["com.webos.app.livetv", "netflix", "com.webos.app.hdmi2"])
async def test_live_tv_and_the_apps_are_not_the_room_pi(tmp_path, app):
    config, service = room(tmp_path, unlisted(app))
    code, text, _ = await run(config, service)
    assert code == 0, text
    assert f"The TV shows the app {app}." in text and "The TV is not showing the room Pi's input HDMI_1" in text


async def test_a_tv_that_says_neither_an_input_nor_an_app_is_not_warned_about(tmp_path):
    def blank(host, key):
        client = FakeTvClient(host, key)
        client.tv_state.current_input = client.tv_state.current_app_id = None
        return client
    config, service = room(tmp_path, blank)
    code, text, _ = await run(config, service)
    assert code == 0 and "The TV shows" not in text and "Warning" not in text


# The MAC address

@pytest.mark.parametrize("mac, line, warning", [
    ("", "MAC not set", "Warning: The TV's MAC address is not set (tv.mac in the room's config)"),
    (None, "MAC not set", "Warning: The TV's MAC address is not set (tv.mac in the room's config)"),
    ("AA:BB:CC", "MAC AA:BB:CC", "Warning: 'AA:BB:CC' is not a MAC address (tv.mac in the room's config wants one like"),
    (8041827059, "MAC 8041827059", "Warning: tv.mac is not text (8041827059): YAML reads an address of digits only, such as "
                                   '10:20:30:40:50:59, as a number, so write it in quotes: tv.mac: "10:20:30:40:50:59".'),
])
async def test_a_mac_address_that_is_no_use_is_said_so_and_the_tv_that_answers_still_passes(tmp_path, mac, line, warning):
    config, service = room(tmp_path, mac=mac)
    code, text, _ = await run(config, service)
    assert code == 0, text
    assert f"TV: 10.0.0.41, {line}" in text and warning in text
    assert text.rstrip().endswith("The TV answers, with the warnings above.")


async def test_an_address_of_digits_only_that_is_not_in_quotes_in_the_config_file_is_a_number_to_yaml(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("tv:\n  enabled: true\n  host: 10.0.0.41\n  mac: 10:20:30:40:50:59\n")
    config = Config.from_dict(yaml.safe_load(config_file.read_text()))
    assert config.tv.mac == 8041827059                            # what the file says, as the agent reads it
    config.data_dir = str(tmp_path)
    _, service = room(tmp_path, mac=config.tv.mac)
    code, text, _ = await run(config, service)
    assert "TV: 10.0.0.41, MAC 8041827059" in text and 'tv.mac: "10:20:30:40:50:59"' in text


async def test_a_mac_problem_is_said_whatever_comes_after_it(tmp_path):
    config, service = room(tmp_path, key=None, mac="")
    code, text, _ = await run(config, service)
    assert code == 1 and "Warning: The TV's MAC address is not set" in text and "is not paired" in text


# A TV that does not answer

async def test_a_tv_that_is_not_paired_is_not_asked_and_the_key_is_looked_for_where_the_agent_keeps_it(tmp_path):
    config, service = room(tmp_path, key=None)
    code, text, _ = await run(config, service)
    assert code == 1
    assert ("The TV is not paired with this device: pair it under Options > TV on the room's table. This check found no TV key "
            f"it can read at {tmp_path / KEY}; if the TV is paired, run the check as the user the agent runs as.") in text
    assert FakeTvClient.instances == [] and not (tmp_path / KEY).exists()       # nothing was connected, and nothing was paired


async def test_a_tv_that_does_not_answer_is_said_so_with_the_reason_and_something_to_check(tmp_path):
    config, service = room(tmp_path, silent_tv)
    code, text, _ = await run(config, service)
    assert code == 1, text
    assert "The TV at 10.0.0.41 is not answering: refused" in text
    assert "Check that the TV is on and that tv.host is its address: a TV that is fully off does not answer." in text
    assert "prompt" not in text                                            # (it refused the connection: there is no prompt to speak of)
    assert "Ready" not in text and service.woken == []                     # (a TV that is off is not woken to be looked at)
    assert FakeTvClient.instances[-1].connected is False and service._client is None


async def test_a_tv_that_is_slow_to_answer_is_given_the_time_the_check_has_and_no_more(tmp_path, gate, monkeypatch):
    monkeypatch.setattr(check, "CHECK_TIMEOUT_S", 0.3)
    config, service = room(tmp_path, LibraryLike)                        # its connect waits for a gate that stays shut
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    code, text, _ = await within(run(config, service), 5.0)
    assert code == 1 and "The TV at 10.0.0.41 did not answer within 0.3 seconds" in text
    assert PROMPT_HINT in text and OFF_HINT in text                        # no answer is a TV that is off, or one that shows a prompt
    assert 0.25 <= loop.time() - t0 < 1.5
    assert service._client is None and not service._attempt.locked()      # the try that was cut off let go of its client


async def test_a_prompt_that_nobody_answers_is_a_tv_that_did_not_answer_and_the_words_say_what_it_may_be(tmp_path):
    """The library waits ten seconds for the person at a TV that shows its pairing prompt, and then times out, as it does for a
    TV that does not answer: the two cannot be told apart, so the words are for both."""
    def prompting(host, key):
        client = FakeTvClient(host, key)
        client.prompt_timeout = True
        return client
    config, service = room(tmp_path, prompting)
    code, text, _ = await run(config, service)
    assert code == 1
    assert "The TV at 10.0.0.41 is not answering: no answer in time" in text
    assert OFF_HINT in text and PROMPT_HINT in text
    assert (tmp_path / KEY).read_text() == "key-9"                          # nothing was paired


async def test_a_tv_that_has_forgotten_this_device_is_told_so_and_is_not_paired_again(tmp_path):
    config, service = room(tmp_path, forgetful_tv)
    code, text, _ = await run(config, service)
    assert code == 1, text
    assert "The TV no longer accepts this table: Forget TV, then Pair" in text
    assert "Check that the TV is on" not in text and "prompt" not in text   # it is on, and has said no
    assert (tmp_path / KEY).read_text() == "key-9"                          # the stored key is as it was: nothing was paired
    assert service.woken == []


async def test_a_hang_up_that_goes_wrong_does_not_change_what_the_check_found(tmp_path):
    class Stubborn(FakeTvClient):
        async def disconnect(self):
            raise RuntimeError("the library choked")
    config, service = room(tmp_path, Stubborn)
    code, text, _ = await run(config, service)
    assert code == 0 and "The TV answers: Boardroom TV, on." in text


async def test_a_hang_up_that_never_ends_is_cut_off(tmp_path, monkeypatch):
    class Stuck(FakeTvClient):
        async def disconnect(self):
            await asyncio.sleep(30)
    monkeypatch.setattr(check, "LET_GO_TIMEOUT_S", 0.2)
    config, service = room(tmp_path, Stuck)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    code, _, _ = await within(run(config, service), 5.0)
    assert code == 0 and loop.time() - t0 < 1.5


# The TV is not configured, or not switched on

async def test_with_no_tv_configured_the_tvs_on_the_network_are_listed(tmp_path):
    found = [{"host": "10.0.0.41", "name": "Boardroom TV", "location": "http://10.0.0.41:1234/d.xml"},
             {"host": "10.0.0.50", "name": "", "location": ""}]
    asked = []

    async def discover():
        asked.append(True)
        return found
    code, text, built = await run(Config(), discover=discover)
    assert code == 1, text
    assert text.splitlines() == [
        "No TV is configured. On the table use Options > TV > Find TVs and select yours.",
        "Looking for LG and Samsung TVs on the network (this takes a few seconds)...",
        "Found 2 TVs:",
        "  Boardroom TV  10.0.0.41",
        "  (no name)  10.0.0.50",
        "On the room's table open Options > TV > Find TVs, select yours, then accept the request on the TV. "
        "The room saves its connection settings automatically.",
    ]
    assert asked == [True] and built == []                                # no service: there is no TV to ask


async def test_a_tv_that_is_switched_on_with_no_address_is_said_to_want_an_address_and_is_searched_for_too(tmp_path):
    config = Config()
    config.tv = TvConfig(enabled=True, host="")                           # the first run: Find TVs is to fill the address in

    async def discover():
        return [{"host": "10.0.0.41", "name": "Boardroom TV", "location": ""}]
    code, text, built = await run(config, discover=discover)
    assert code == 1 and built == []
    assert text.splitlines()[0] == "No TV is configured. On the table use Options > TV > Find TVs and select yours."
    assert "  Boardroom TV  10.0.0.41" in text


async def test_one_tv_found_is_one_tv(tmp_path):
    async def discover():
        return [{"host": "10.0.0.41", "name": "Boardroom TV", "location": ""}]
    code, text, _ = await run(Config(), discover=discover)
    assert "Found 1 TV:" in text


async def test_with_no_tv_configured_and_none_on_the_network_the_check_says_what_to_look_at(tmp_path):
    async def discover():
        return []
    code, text, _ = await run(Config(), discover=discover)
    assert code == 1
    assert "No TV answered. Check that the TV is on, on the same network as this device, and allows network control." in text


async def test_a_search_that_cannot_be_made_is_said_in_the_words_it_came_with(tmp_path):
    async def discover():
        raise TvError("The search for TVs could not be sent: Network is unreachable")
    code, text, _ = await run(Config(), discover=discover)
    assert code == 1
    assert "The search for TVs could not be sent: Network is unreachable" in text and "No LG TV answered" not in text


async def test_a_tv_that_is_configured_but_not_enabled_is_left_alone_and_the_config_is_said_to_be_why(tmp_path):
    config, service = room(tmp_path, enabled=False)

    async def discover():
        raise AssertionError("a configured TV is not searched for")
    code, text, built = await run(config, service, discover)
    assert code == 1
    assert text.splitlines() == ["TV: 10.0.0.41, MAC AA:BB:CC:DD:EE:FF",
                                 "The TV is switched off in the config (tv.enabled is false), so the agent leaves it alone: set "
                                 "tv.enabled to true."]
    assert FakeTvClient.instances == [] and built == []


async def test_a_service_that_cannot_be_made_is_said_in_words(tmp_path):
    config, _ = room(tmp_path)
    out = io.StringIO()

    def factory(given):
        raise PermissionError(13, "Permission denied", "/var/lib/croom")
    assert await check_tv(config, out=out, service_factory=factory) == 1
    assert "The TV service could not be set up: [Errno 13] Permission denied: '/var/lib/croom'" in out.getvalue()


# The look at the TV, which the service takes for the check

async def test_a_look_at_the_tv_reads_what_it_shows_and_starts_nothing(tmp_path):
    _, service = room(tmp_path)
    await service.probe(5.0)
    shown = service.status()
    assert shown["reachable"] is True and shown["name"] == "Boardroom TV" and shown["on"] is True
    assert service._task is None and service._rewake is None and service.woken == []      # no loop, no wake: a look
    await service.stop()
    assert FakeTvClient.instances[-1].connected is False


@pytest.mark.parametrize("case, words", [
    ("not set up", "not set up"), ("not paired", "Pair the TV under Options"),
])
async def test_a_look_at_a_tv_that_cannot_be_asked_is_refused_in_words_and_connects_to_nothing(tmp_path, case, words):
    if case == "not set up":
        _, service = room(tmp_path, enabled=False)
    else:
        _, service = room(tmp_path, key=None)
    with pytest.raises(TvError) as failure:
        await service.probe(5.0)
    assert words in str(failure.value) and FakeTvClient.instances == []


async def test_a_look_that_ends_in_no_answer_is_a_timeout_and_a_refusal_or_a_closed_door_is_not(tmp_path):
    def prompting(host, key):
        client = FakeTvClient(host, key)
        client.prompt_timeout = True
        return client
    for factory, kind in ((prompting, TvTimeout), (silent_tv, TvError), (forgetful_tv, TvError)):
        _, service = room(tmp_path, factory)
        with pytest.raises(TvError) as failure:
            await service.probe(5.0)
        assert type(failure.value) is kind


async def test_a_look_that_is_cut_off_is_a_timeout(tmp_path, gate):
    _, service = room(tmp_path, LibraryLike)
    with pytest.raises(TvTimeout):
        await service.probe(0.2)


async def test_a_look_at_a_tv_that_has_refused_this_device_says_so_and_a_second_look_does_not_ask_again(tmp_path):
    _, service = room(tmp_path, forgetful_tv)
    for _ in range(2):
        with pytest.raises(TvError) as failure:
            await service.probe(5.0)
        assert "no longer accepts this table" in str(failure.value)
    assert len(FakeTvClient.instances) == 1                                 # (the TV would show its prompt again for every try)


def test_the_key_is_kept_where_the_service_says(tmp_path):
    _, service = room(tmp_path)
    assert service.key_path == tmp_path / KEY


# The command line

def croom(*args, **kwargs):
    """`python -m croom.core.agent ...` for as long as a check may take. The timeout matters: a flag that is not acted on falls
    through to the whole agent, which listens on the network until it is killed, and a test that waits for it waits for ever."""
    return subprocess.run([sys.executable, "-m", "croom.core.agent", *args], capture_output=True, text=True, timeout=30, **kwargs)


class TestCommandLine:
    def test_help_names_the_flag(self):
        result = croom("--help")
        assert result.returncode == 0 and "--check-tv" in result.stdout

    def test_flag_runs_the_check(self, tmp_path):
        """A TV that is set up and not paired: the check says so and goes no further (no key, so no connection, no search)."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({"data_dir": str(tmp_path / "data"),
                                       "tv": {"enabled": True, "host": "10.0.0.41", "mac": "AA:BB:CC:DD:EE:FF"}}))
        result = croom("--check-tv", "-c", str(cfg))
        assert result.returncode == 1, result.stderr
        assert "TV: 10.0.0.41, MAC AA:BB:CC:DD:EE:FF" in result.stdout and "The TV is not paired with this device" in result.stdout
        assert str(tmp_path / "data" / KEY) in result.stdout

    def test_what_a_terminal_cannot_show_is_replaced_and_is_not_a_traceback(self, tmp_path):
        """The check prints names that a TV, or a person's directory, chose: on a terminal that cannot encode one (an ASCII one) a ?
        stands for it, and the check goes on to say what it found."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({"data_dir": str(tmp_path / "données"),
                                       "tv": {"enabled": True, "host": "10.0.0.41", "mac": "AA:BB:CC:DD:EE:FF"}}))
        result = croom("--check-tv", "-c", str(cfg), env=dict(os.environ, PYTHONIOENCODING="ascii"))
        assert "UnicodeEncodeError" not in result.stderr, result.stderr
        assert result.returncode == 1 and "donn?es" in result.stdout and "The TV is not paired with this device" in result.stdout
